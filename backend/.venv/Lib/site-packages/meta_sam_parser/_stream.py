# Copyright (c) Meta Platforms, Inc. and affiliates. All Rights Reserved.

from __future__ import annotations

import asyncio
import math
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Mapping
from contextlib import suppress
from inspect import isawaitable
from types import TracebackType
from typing import Generic, Literal, TypeVar, cast

from ._errors import (
    ResponsesStreamAbortedError,
    ResponsesStreamConsumedError,
    ResponsesStreamError,
    ResponsesStreamEventError,
    ResponsesStreamFailedError,
    ResponsesStreamLaneError,
    ResponsesStreamParserError,
    ResponsesStreamRefusalError,
    ResponsesStreamSourceError,
)
from ._types import (
    CompletedOutcome,
    IncompleteOutcome,
    OutputTextLane,
    ParserFinish,
    ResponseFormat,
    ResponseFormatParser,
    ResponsesEventLike,
    ResponseStreamOutcome,
)

EventT = TypeVar("EventT")
ResultT = TypeVar("ResultT")
ObservedT = TypeVar("ObservedT")
_Consumption = Literal["idle", "iteration", "final"]
_MAXIMUM_SAFE_INTEGER = (1 << 53) - 1
_MISSING = object()


def _consume_future_exception(future: asyncio.Future[ObservedT]) -> None:
    if future.cancelled():
        return
    with suppress(BaseException):
        future.exception()


def _field(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return value.get(key, _MISSING)
    try:
        return getattr(value, key)
    except AttributeError:
        return _MISSING


def _nested_string(value: object, *path: str) -> str | None:
    current = value
    for key in path:
        current = _field(current, key)
        if current is _MISSING:
            return None
    return current if isinstance(current, str) else None


def _safe_nonnegative_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        integer = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        integer = int(value)
    else:
        return None
    return integer if 0 <= integer <= _MAXIMUM_SAFE_INTEGER else None


class ParsedResponsesStream(AsyncIterator[EventT], Generic[EventT, ResultT]):
    """One-owner async adapter from Responses API events to parsed snapshots."""

    def __init__(
        self,
        source: AsyncIterable[ResponsesEventLike],
        format_: ResponseFormat[EventT, ResultT],
    ) -> None:
        self._source = source
        self._format = format_
        self._loop: asyncio.AbstractEventLoop | None = None
        self._parser: ResponseFormatParser[EventT, ResultT] | None = None
        self._source_iterator: AsyncIterator[ResponsesEventLike] | None = None
        self._consumption: _Consumption = "idle"
        self._pull_lock = asyncio.Lock()
        self._pending: deque[EventT] = deque()
        self._drain_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._failure_task: asyncio.Task[None] | None = None
        self._result_future: asyncio.Future[ResultT] | None = None
        self._read_interruption: asyncio.Future[BaseException] | None = None
        self._active_read: asyncio.Future[ResponsesEventLike] | None = None
        self._terminal = False
        self._consumer_done = False
        self._result_value: ResultT | None = None
        self._has_result = False
        self._error: BaseException | None = None
        self._lane: OutputTextLane | None = None
        self._lane_done = False
        self._lane_finalizers: set[str] = set()
        self._text = ""
        self._saw_delta = False

    def __aiter__(self) -> ParsedResponsesStream[EventT, ResultT]:
        if self._consumption != "idle":
            raise ResponsesStreamConsumedError()
        self._consumption = "iteration"
        return self

    async def __anext__(self) -> EventT:
        if self._consumption != "iteration":
            raise ResponsesStreamConsumedError()
        try:
            async with self._pull_lock:
                return await self._next_one()
        except asyncio.CancelledError:
            await self._abort_after_cancellation()
            raise

    async def __aenter__(self) -> ParsedResponsesStream[EventT, ResultT]:
        self._ensure_loop()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        await self.aclose()
        return False

    def final_result(self) -> Awaitable[ResultT]:
        """Claim final-only mode immediately and await the shared terminal result."""

        self._ensure_loop()
        result = self._ensure_result_future()
        if self._consumption == "idle":
            self._consumption = "final"
            self._drain_task = self._ensure_loop().create_task(self._drain_final())
            self._observe(self._drain_task)
        return self._await_result(result)

    async def _await_result(self, result: asyncio.Future[ResultT]) -> ResultT:
        try:
            return await asyncio.shield(result)
        except asyncio.CancelledError:
            await self._abort_after_cancellation()
            raise

    async def aclose(self) -> None:
        """Best-effort close; abandoning a live stream rejects its final result."""

        self._ensure_loop()
        current = asyncio.current_task()
        if current is self._close_task or current is self._failure_task:
            return
        if self._consumer_done:
            return
        if self._consumption == "idle":
            self._consumption = "final"
        failure = self._start_failure(ResponsesStreamAbortedError())
        if current is self._active_read:
            # A source finalizer cannot join the read that is running it.
            return
        try:
            await asyncio.shield(failure)
        except asyncio.CancelledError:
            raise
        self._consumer_done = True
        if (
            isinstance(self._error, ResponsesStreamSourceError)
            and self._error.operation == "return"
        ):
            raise self._error

    async def _next_one(self) -> EventT:
        if self._pending:
            return self._pending.popleft()
        if self._error is not None:
            raise self._error
        if self._terminal:
            self._complete_iteration()

        try:
            self._ensure_source_iterator()
            self._ensure_parser()
            while True:
                done, event = await self._read_source()
                if self._terminal:
                    if self._error is not None:
                        raise self._error
                    self._complete_iteration()
                if done:
                    self._finish(IncompleteOutcome(reason="eof"), emit=True)
                    await self._close_source()
                    if self._pending:
                        return self._pending.popleft()
                    self._complete_iteration()
                assert event is not None
                outcome = self._accept(event, emit=True)
                if outcome is not None:
                    self._finish(outcome, emit=True)
                    await self._close_source()
                    if self._pending:
                        return self._pending.popleft()
                    self._complete_iteration()
                if self._pending:
                    return self._pending.popleft()
        except StopAsyncIteration:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await asyncio.shield(self._start_failure(error))
            assert self._error is not None
            raise self._error from self._error.__cause__
        raise AssertionError("unreachable")

    async def _drain_final(self) -> None:
        try:
            self._ensure_source_iterator()
            self._ensure_parser()
            while True:
                done, event = await self._read_source()
                if done:
                    self._finish(IncompleteOutcome(reason="eof"), emit=False)
                    await self._close_source()
                    self._consumer_done = True
                    self._resolve_result()
                    return
                assert event is not None
                outcome = self._accept(event, emit=False)
                if outcome is not None:
                    self._finish(outcome, emit=False)
                    await self._close_source()
                    self._consumer_done = True
                    self._resolve_result()
                    return
        except asyncio.CancelledError:
            await self._abort_after_cancellation()
            raise
        except Exception as error:
            await asyncio.shield(self._start_failure(error))

    def _accept(
        self, event: ResponsesEventLike, *, emit: bool
    ) -> ResponseStreamOutcome | None:
        event_type = _field(event, "type")
        assert isinstance(event_type, str)
        if event_type == "response.output_text.delta":
            lane = self._accept_lane(event)
            if self._lane_done:
                raise ResponsesStreamLaneError(
                    "The response emitted output text after finalizing its lane.",
                    self._lane,
                    lane,
                )
            delta = _field(event, "delta")
            if not isinstance(delta, str):
                raise ResponsesStreamLaneError(
                    "The output text delta is missing its text.",
                    self._lane,
                    lane,
                )
            self._saw_delta = True
            self._text += delta
            self._push_parser(delta, emit=emit)
            return None
        if event_type == "response.output_text.done":
            self._finalize_output_text(event, _field(event, "text"), emit=emit)
            return None
        if event_type == "response.content_part.done":
            part = _field(event, "part")
            part_type = _field(part, "type")
            if part_type == "output_text":
                self._finalize_output_text(event, _field(part, "text"), emit=emit)
            elif part_type == "refusal":
                refusal = _field(part, "refusal")
                message = (
                    refusal if isinstance(refusal, str) else "The response was refused."
                )
                raise ResponsesStreamRefusalError(message, event)
            return None
        if event_type in ("response.refusal.delta", "response.refusal.done"):
            refusal = _field(event, "refusal")
            delta = _field(event, "delta")
            message = (
                refusal
                if isinstance(refusal, str)
                else delta
                if isinstance(delta, str)
                else "The response was refused."
            )
            raise ResponsesStreamRefusalError(message, event)
        if event_type == "response.completed":
            if self._lane is None or not self._lane_done:
                raise ResponsesStreamLaneError(
                    "The response completed before finalizing one output text lane.",
                    self._lane,
                    None,
                )
            return CompletedOutcome()
        if event_type == "response.incomplete":
            if self._lane is None or not self._lane_done:
                raise ResponsesStreamLaneError(
                    "The response became incomplete before finalizing one "
                    "output text lane.",
                    self._lane,
                    None,
                )
            detail = _nested_string(event, "response", "incomplete_details", "reason")
            return IncompleteOutcome(reason="response", detail=detail)
        if event_type == "response.failed":
            failed_message = _nested_string(event, "response", "error", "message")
            if failed_message is None:
                failed_message = "The response failed."
            raise ResponsesStreamFailedError(failed_message, event)
        if event_type == "error":
            event_message = _nested_string(event, "error", "message")
            if event_message is None:
                event_message = _nested_string(event, "message")
            if event_message is None:
                event_message = "The Responses stream reported an error."
            raise ResponsesStreamEventError(event_message, event)
        return None

    def _finalize_output_text(
        self, event: ResponsesEventLike, text: object, *, emit: bool
    ) -> None:
        lane = self._accept_lane(event)
        event_type = _field(event, "type")
        assert isinstance(event_type, str)
        if self._lane_done and event_type in self._lane_finalizers:
            raise ResponsesStreamLaneError(
                "The response finalized its output text lane more than once.",
                self._lane,
                lane,
            )
        if not isinstance(text, str):
            raise ResponsesStreamLaneError(
                "The completed output text is missing its text.",
                self._lane,
                lane,
            )
        if self._lane_done:
            if text != self._text:
                raise ResponsesStreamLaneError(
                    "The response finalized its output text lane twice with "
                    "conflicting text.",
                    self._lane,
                    lane,
                )
            self._lane_finalizers.add(event_type)
            return
        if self._saw_delta and text != self._text:
            raise ResponsesStreamLaneError(
                "The finalized output text conflicts with its accumulated deltas.",
                self._lane,
                lane,
            )
        if not self._saw_delta:
            self._text = text
            self._push_parser(text, emit=emit)
        self._lane_done = True
        self._lane_finalizers.add(event_type)

    def _accept_lane(self, event: ResponsesEventLike) -> OutputTextLane:
        item_id = _field(event, "item_id")
        output_index = _safe_nonnegative_integer(_field(event, "output_index"))
        content_index = _safe_nonnegative_integer(_field(event, "content_index"))
        lane = (
            OutputTextLane(item_id, output_index, content_index)
            if isinstance(item_id, str)
            and item_id
            and output_index is not None
            and content_index is not None
            else None
        )
        if lane is None:
            raise ResponsesStreamLaneError(
                "The output text event has an invalid lane identity.",
                self._lane,
                None,
            )
        if self._lane is None:
            self._lane = lane
            return lane
        if self._lane != lane:
            raise ResponsesStreamLaneError(
                "The response interleaved multiple output text lanes.",
                self._lane,
                lane,
            )
        return lane

    def _push_parser(self, chunk: str, *, emit: bool) -> None:
        try:
            events = self._ensure_parser().push(chunk, emit=emit)
            if emit:
                self._pending.extend(events)
        except asyncio.CancelledError:
            raise
        except ResponsesStreamError:
            raise
        except Exception as error:
            raise ResponsesStreamParserError(error) from error

    def _finish(self, outcome: ResponseStreamOutcome, *, emit: bool) -> None:
        if self._terminal:
            return
        try:
            finished = self._ensure_parser().finish(outcome)
            if not isinstance(finished, ParserFinish):
                raise TypeError("Parser finish() must return ParserFinish.")
            if emit:
                self._pending.extend(finished.events)
            self._result_value = finished.result
        except asyncio.CancelledError:
            raise
        except ResponsesStreamError:
            raise
        except Exception as error:
            raise ResponsesStreamParserError(error) from error
        self._has_result = True
        self._terminal = True

    def _ensure_parser(self) -> ResponseFormatParser[EventT, ResultT]:
        if self._parser is not None:
            return self._parser
        try:
            self._parser = self._format.create_parser()
        except asyncio.CancelledError:
            raise
        except ResponsesStreamError:
            raise
        except Exception as error:
            raise ResponsesStreamParserError(error) from error
        return self._parser

    def _complete_iteration(self) -> None:
        if self._error is not None:
            raise self._error
        self._consumer_done = True
        self._resolve_result()
        raise StopAsyncIteration

    def _resolve_result(self) -> None:
        if not self._has_result or self._error is not None:
            return
        future = self._result_future
        if future is not None and not future.done():
            future.set_result(cast(ResultT, self._result_value))

    def _start_failure(self, error: BaseException) -> asyncio.Task[None]:
        if self._failure_task is not None:
            return self._failure_task
        failure = (
            error
            if isinstance(error, ResponsesStreamError)
            else ResponsesStreamParserError(error)
        )
        # Stop admitting reads before yielding, and hand the current read to
        # the one cleanup task. Consumer cancellation must not cancel it again.
        self._interrupt_reads(failure)
        active = self._active_read
        self._failure_task = self._ensure_loop().create_task(
            self._fail_and_close(
                failure, active, cancel_read=active is not asyncio.current_task()
            )
        )
        self._observe(self._failure_task)
        return self._failure_task

    async def _fail_and_close(
        self,
        failure: ResponsesStreamError,
        active: asyncio.Future[ResponsesEventLike] | None,
        *,
        cancel_read: bool,
    ) -> None:
        if active is not None:
            # A read initiating its own close must unwind itself: cancellation
            # could interrupt the finalizer that requested cleanup.
            if cancel_read and not active.done():
                active.cancel()
            try:
                await active
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
            except BaseException as error:
                failure = ResponsesStreamSourceError(
                    "return", error, prior_error=failure
                )
            finally:
                if self._active_read is active:
                    self._active_read = None
        if (
            isinstance(failure, ResponsesStreamSourceError)
            and failure.operation == "return"
            and self._close_task is not None
        ):
            self._fail(failure)
            return
        try:
            await self._close_source()
        except ResponsesStreamSourceError as close_error:
            cause = close_error.cause
            assert cause is not None
            failure = ResponsesStreamSourceError("return", cause, prior_error=failure)
        self._fail(failure)

    def _interrupt_reads(self, error: BaseException) -> None:
        future = self._ensure_read_interruption()
        if not future.done():
            future.set_result(error)

    def _fail(self, error: BaseException) -> None:
        if self._error is not None:
            return
        self._error = error
        self._terminal = True
        self._pending.clear()
        future = self._result_future
        if future is not None and not future.done():
            future.set_exception(error)

    async def _abort_after_cancellation(self) -> None:
        if self._consumer_done:
            return
        failure = self._start_failure(ResponsesStreamAbortedError())
        with suppress(asyncio.CancelledError):
            await asyncio.shield(failure)
        if failure.done():
            self._consumer_done = True

    def _ensure_source_iterator(self) -> AsyncIterator[ResponsesEventLike]:
        if self._source_iterator is not None:
            return self._source_iterator
        try:
            self._source_iterator = aiter(self._source)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise ResponsesStreamSourceError("iterator", error) from error
        return self._source_iterator

    async def _read_source(self) -> tuple[bool, ResponsesEventLike | None]:
        interrupted = self._ensure_read_interruption()
        if interrupted.done():
            raise interrupted.result()
        iterator = self._ensure_source_iterator()
        try:
            awaitable = iterator.__anext__()
            reading = asyncio.ensure_future(awaitable)
        except StopAsyncIteration:
            return True, None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise ResponsesStreamSourceError("next", error) from error
        self._active_read = reading
        self._observe(reading)
        try:
            completed, _pending = await asyncio.wait(
                (reading, interrupted), return_when=asyncio.FIRST_COMPLETED
            )
            if interrupted in completed:
                raise interrupted.result()
            try:
                event = reading.result()
            except StopAsyncIteration:
                return True, None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise ResponsesStreamSourceError("next", error) from error
            try:
                event_type = _field(event, "type")
            except Exception as error:
                raise ResponsesStreamSourceError("next", error) from error
            if not isinstance(event_type, str):
                shape_error = TypeError(
                    "The event source yielded an invalid Responses event."
                )
                raise ResponsesStreamSourceError("next", shape_error) from shape_error
            return False, event
        except asyncio.CancelledError:
            self._start_failure(ResponsesStreamAbortedError())
            raise
        finally:
            if self._active_read is reading and self._failure_task is None:
                self._active_read = None

    async def _close_source(self) -> None:
        current = asyncio.current_task()
        if current is self._active_read:
            self._start_failure(ResponsesStreamAbortedError())
            return
        if current is self._close_task:
            return
        if self._close_task is None:
            self._close_task = self._ensure_loop().create_task(self._run_close_source())
            self._observe(self._close_task)
        await asyncio.shield(self._close_task)

    async def _run_close_source(self) -> None:
        iterator = self._source_iterator
        target: object = self._source
        try:
            close = getattr(target, "aclose", None)
            if close is None:
                close = getattr(target, "close", None)
            if close is None and iterator is not None and iterator is not target:
                target = iterator
                close = getattr(target, "aclose", None)
                if close is None:
                    close = getattr(target, "close", None)
        except BaseException as error:
            raise ResponsesStreamSourceError("return", error) from error
        if close is None:
            return
        try:
            outcome = close()
            if isawaitable(outcome):
                await outcome
        except BaseException as error:
            raise ResponsesStreamSourceError("return", error) from error

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("A parsed response stream cannot cross event loops.")
        return loop

    def _ensure_result_future(self) -> asyncio.Future[ResultT]:
        if self._result_future is None:
            future = self._ensure_loop().create_future()
            self._result_future = future
            self._observe(future)
            if self._error is not None:
                future.set_exception(self._error)
            elif self._has_result and self._consumer_done:
                future.set_result(cast(ResultT, self._result_value))
        return self._result_future

    def _ensure_read_interruption(self) -> asyncio.Future[BaseException]:
        if self._read_interruption is None:
            self._read_interruption = self._ensure_loop().create_future()
        return self._read_interruption

    @staticmethod
    def _observe(future: asyncio.Future[ObservedT]) -> None:
        future.add_done_callback(_consume_future_exception)


def parse_responses_stream(
    source: AsyncIterable[ResponsesEventLike],
    format: ResponseFormat[EventT, ResultT],
) -> ParsedResponsesStream[EventT, ResultT]:
    """Create a lazy, single-consumer parser for a Responses event stream."""

    return ParsedResponsesStream(source, format)
