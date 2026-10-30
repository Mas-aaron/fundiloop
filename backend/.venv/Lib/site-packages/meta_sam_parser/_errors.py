# Copyright (c) Meta Platforms, Inc. and affiliates. All Rights Reserved.

from __future__ import annotations

from ._types import OutputTextLane, ResponseSourceOperation


class ResponsesStreamError(Exception):
    """Base error for stable parser failures exposed by this package."""

    def __init__(
        self,
        message: str,
        code: str,
        *,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause


class ResponsesStreamConsumedError(ResponsesStreamError):
    """Raised when a parsed stream is assigned more than one consumer mode."""

    def __init__(self) -> None:
        super().__init__(
            "A parsed response stream can be iterated only once.",
            "stream_consumed",
        )


class ResponsesStreamAbortedError(ResponsesStreamError):
    """Raised when explicit cleanup abandons a stream before completion."""

    def __init__(self) -> None:
        super().__init__(
            "The parsed response stream was abandoned before completion.",
            "stream_aborted",
        )


class ResponsesStreamFailedError(ResponsesStreamError):
    """Raised for an official ``response.failed`` event."""

    def __init__(self, message: str, event: object) -> None:
        super().__init__(message, "response_failed")
        self.event = event


class ResponsesStreamEventError(ResponsesStreamError):
    """Raised for an official stream ``error`` event."""

    def __init__(self, message: str, event: object) -> None:
        super().__init__(message, "response_error")
        self.event = event


class ResponsesStreamLaneError(ResponsesStreamError):
    """Raised when output-text lane identity or ordering is inconsistent."""

    def __init__(
        self,
        message: str,
        expected: OutputTextLane | None,
        received: OutputTextLane | None,
    ) -> None:
        super().__init__(message, "response_lane")
        self.expected = expected
        self.received = received


class ResponsesStreamRefusalError(ResponsesStreamError):
    """Raised for an official refusal event on any content lane."""

    def __init__(self, message: str, event: object) -> None:
        super().__init__(message, "response_refusal")
        self.event = event


class ResponsesStreamParserError(ResponsesStreamError):
    """Raised when a non-package exception escapes a response-format parser."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(
            "The response format parser failed.",
            "parser_error",
            cause=cause,
        )


class ResponsesStreamSourceError(ResponsesStreamError):
    """Raised when creating, reading, or closing the event source fails."""

    def __init__(
        self,
        operation: ResponseSourceOperation,
        cause: BaseException,
        prior_error: BaseException | None = None,
    ) -> None:
        super().__init__(
            f"The Responses event source failed during {operation}.",
            "source_error",
            cause=cause,
        )
        self.operation = operation
        self.prior_error = prior_error


class InvalidSegmentationMaskError(ResponsesStreamError):
    """Raised when a complete segmentation-mask payload is invalid."""

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        super().__init__(message, "invalid_mask_payload", cause=cause)
