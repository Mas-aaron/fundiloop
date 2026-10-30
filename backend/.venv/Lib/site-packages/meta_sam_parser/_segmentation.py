# Copyright (c) Meta Platforms, Inc. and affiliates. All Rights Reserved.

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import cast

from ._errors import InvalidSegmentationMaskError
from ._mask_codec import decode_mask_to_raster
from ._types import (
    FrameReference,
    ImageSegmentationResult,
    ImageSegmentationSnapshot,
    ParserFinish,
    ResponseFormat,
    ResponseStreamOutcome,
    SegmentationBoxRecord,
    SegmentationDiagnostic,
    SegmentationMask,
    SegmentationMaskBounds,
    SegmentationMaskIdentity,
    SegmentationMaskRecord,
    SegmentationMedia,
    SegmentationRecord,
    SegmentationResult,
    SegmentationSnapshot,
    SegmentationTextRecord,
    VideoSegmentationResult,
    VideoSegmentationSnapshot,
)

_MAXIMUM_SAFE_INTEGER = (1 << 53) - 1
_JS_WHITESPACE = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_JS_WHITESPACE_CLASS = (
    r"\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_JS_DOT_PATTERN = r"[^\n\r\u2028\u2029]"
# The SAM API record grammar. A line is a frame header ``<Nf>``, optionally with
# ``;key=value`` fields before its ``>``, followed by records. A record is an
# ASCII-decimal object id followed by tokens ``<|name|>`` or ``<|name;fields|>``;
# it needs exactly one ``box`` and one ``mask`` token, in any order, and other
# tokens are ignored with a warning. The payload alphabet never contains ``|``
# or ``;``, so ``[^|]*`` ends exactly at a token's closing ``|>`` and every
# ``;`` separates fields; ``,`` before a record is optional.
_API_HEADER_PATTERN = re.compile(rf"^<([0-9]+)f(?:;([^>]*))?>({_JS_DOT_PATTERN}*)$")
_RECORD_START_PATTERN = re.compile(r",?([0-9]+)(?=<\|)")
_TOKEN_PATTERN = re.compile(r"<\|([A-Za-z][A-Za-z0-9_.-]*)(?:;([^|]*))?\|>")
_RECORD_BOUNDARY = frozenset(",0123456789")
_SIGNED_INTEGER_PATTERN = re.compile(r"-?[0-9]+")
_UNSIGNED_INTEGER_PATTERN = re.compile(r"[0-9]+")
_MASK_DATA_PATTERN = re.compile(r"([0-9]+),([0-9]+),([!~][^|]+)")
_CONFIDENCE_PATTERN = re.compile(
    r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
)
_BOX_FIELD_KEYS = frozenset({"x1", "y1", "x2", "y2", "w", "h", "c"})
_MASK_FIELD_KEYS = frozenset({"x", "y", "data", "c"})
_FRAME_HEADER_FIELD_KEYS: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _PendingWarning:
    """Something ignored while keeping the record it belongs to.

    It is reported as a ``warning`` diagnostic only when that record is
    accepted, and each code and subject is reported once per stream.
    """

    code: str
    subject: str
    message: str


@dataclass(frozen=True, slots=True)
class _FieldRead:
    # Field values by key; ``None`` is a field written without ``=``.
    values: dict[str, str | None]
    # ``c`` appeared more than once, so no single value can be trusted.
    repeated_confidence: bool


@dataclass(frozen=True, slots=True)
class _ApiToken:
    name: str
    body: str | None


@dataclass(frozen=True, slots=True)
class _ApiRecord:
    object_id: str
    box: tuple[str, str, str, str, str, str]
    mask_height: int
    mask_width: int
    payload: str
    box_confidence: float | None
    mask_confidence: float | None


def _read_fields(
    body: str | None,
    owner: str,
    known: frozenset[str],
    warnings: list[_PendingWarning],
) -> _FieldRead | None:
    """Read one ``;``-separated field list.

    Whitespace around keys and values is trimmed and empty fields are skipped.
    A field without ``=`` has no value. A key the owner does not define is
    ignored with an ``ignored_field`` warning. A repeated key the owner defines
    makes the record malformed (``None``), except ``c``, which is reported
    through ``repeated_confidence``.
    """
    values: dict[str, str | None] = {}
    repeated_confidence = False
    for part in [] if body is None else body.split(";"):
        field = _trim(part)
        if not field:
            continue
        key_text, separator, value_text = field.partition("=")
        key = _trim(key_text)
        value = _trim(value_text) if separator else None
        if key not in known:
            name = key if key else field
            warnings.append(
                _PendingWarning(
                    code="ignored_field",
                    subject=f"{owner}:{name}",
                    message=f'Ignored unknown {owner} field "{name}".',
                )
            )
            continue
        if key in values:
            if key != "c":
                return None
            repeated_confidence = True
            continue
        values[key] = value
    return _FieldRead(values=values, repeated_confidence=repeated_confidence)


def _read_confidence(
    fields: _FieldRead, warnings: list[_PendingWarning]
) -> float | None:
    """Read the optional ``c`` field.

    An absent field gives ``None``. A field that is not one finite decimal
    number from 0 through 1 also gives ``None``, with an ``ignored_confidence``
    warning: the record is kept.
    """
    if "c" not in fields.values:
        return None
    text = fields.values["c"]
    value = (
        float(text)
        if not fields.repeated_confidence
        and text is not None
        and _CONFIDENCE_PATTERN.fullmatch(text) is not None
        else math.nan
    )
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        warnings.append(
            _PendingWarning(
                code="ignored_confidence",
                subject="c",
                message=(
                    "Ignored a confidence value that is not a number from 0 through 1."
                ),
            )
        )
        return None
    return value + 0.0


def _read_api_record(
    object_id: str,
    tokens: list[_ApiToken],
    warnings: list[_PendingWarning],
) -> _ApiRecord | None:
    """Return the record's fields, or ``None`` when the record is malformed.

    Warnings go to ``warnings`` in a fixed order: ignored tokens, box fields,
    mask fields, then box and mask confidence.
    """
    box_tokens = [token for token in tokens if token.name == "box"]
    mask_tokens = [token for token in tokens if token.name == "mask"]
    if len(box_tokens) != 1 or len(mask_tokens) != 1:
        return None
    for token in tokens:
        if token.name in ("box", "mask"):
            continue
        warnings.append(
            _PendingWarning(
                code="ignored_token",
                subject=token.name,
                message=f'Ignored unknown token "{token.name}".',
            )
        )
    box = _read_fields(box_tokens[0].body, "box", _BOX_FIELD_KEYS, warnings)
    mask = _read_fields(mask_tokens[0].body, "mask", _MASK_FIELD_KEYS, warnings)
    if box is None or mask is None:
        return None
    coordinates = tuple(box.values.get(key) for key in ("x1", "y1", "x2", "y2"))
    dimensions = tuple(box.values.get(key) for key in ("w", "h"))
    if (
        any(
            value is None or _SIGNED_INTEGER_PATTERN.fullmatch(value) is None
            for value in coordinates
        )
        or any(
            value is None or _UNSIGNED_INTEGER_PATTERN.fullmatch(value) is None
            for value in dimensions
        )
        or mask.values.get("x") != "0"
        or mask.values.get("y") != "0"
    ):
        return None
    data = _MASK_DATA_PATTERN.fullmatch(mask.values.get("data") or "")
    if data is None:
        return None
    return _ApiRecord(
        object_id=object_id,
        box=cast(tuple[str, str, str, str, str, str], coordinates + dimensions),
        mask_height=int(data.group(1)),
        mask_width=int(data.group(2)),
        payload=data.group(3),
        box_confidence=_read_confidence(box, warnings),
        mask_confidence=_read_confidence(mask, warnings),
    )


def _trim(value: str) -> str:
    return value.strip(_JS_WHITESPACE)


def _integer_text(value: str) -> int | None:
    """Parse a decimal integer token the way JavaScript's ``Number`` observes it.

    Callers only pass regex captures of ``-?[0-9]+``; values beyond the
    JavaScript safe-integer range are rejected so both parsers agree.
    """
    parsed = int(value)
    return parsed if abs(parsed) <= _MAXIMUM_SAFE_INTEGER else None


class _SegmentationParser:
    __slots__ = (
        "_buffer_parts",
        "_diagnostics",
        "_line",
        "_media",
        "_raw_output_parts",
        "_records",
        "_revision",
        "_revisions",
        "_warned",
    )

    def __init__(self, media: SegmentationMedia) -> None:
        self._media = media
        self._records: list[SegmentationRecord] = []
        self._diagnostics: list[SegmentationDiagnostic] = []
        self._revisions: dict[SegmentationMaskIdentity, int] = {}
        self._warned: set[tuple[str, str]] = set()
        self._raw_output_parts: list[str] = []
        self._buffer_parts: list[str] = []
        self._line = 0
        self._revision = 0

    def push(
        self, chunk: str, *, emit: bool = True
    ) -> tuple[SegmentationSnapshot, ...]:
        if not isinstance(chunk, str):
            raise TypeError("Parser chunks must be strings.")
        self._raw_output_parts.append(chunk)

        changed = False
        segments = chunk.split("\n")
        for segment in segments[:-1]:
            self._append_buffer(segment)
            raw = "".join(self._buffer_parts)
            if raw.endswith("\r"):
                raw = raw[:-1]
            self._clear_buffer()
            prior = (len(self._records), len(self._diagnostics))
            self._accept_line(raw)
            changed = changed or prior != (
                len(self._records),
                len(self._diagnostics),
            )
        self._append_buffer(segments[-1])

        if changed:
            self._revision += 1
        return (self._snapshot(),) if changed and emit is not False else ()

    def finish(
        self, outcome: ResponseStreamOutcome
    ) -> ParserFinish[SegmentationSnapshot, SegmentationResult]:
        events: tuple[SegmentationSnapshot, ...] = ()
        if self._buffer_parts:
            raw = "".join(self._buffer_parts)
            if raw.endswith("\r"):
                raw = raw[:-1]
            self._clear_buffer()
            prior = (len(self._records), len(self._diagnostics))
            self._accept_line(raw)
            if prior != (len(self._records), len(self._diagnostics)):
                self._revision += 1
                events = (self._snapshot(),)
        return ParserFinish(events=events, result=self._result(outcome))

    def _append_buffer(self, value: str) -> None:
        if value:
            self._buffer_parts.append(value)

    def _clear_buffer(self) -> None:
        self._buffer_parts.clear()

    def _accept_line(self, raw: str) -> bool:
        self._line += 1
        line = _trim(raw)
        if not line:
            return False

        if line.startswith("<"):
            accepted = self._accept_api_line(line, raw)
            if accepted is not None:
                return accepted

        self._add_record(SegmentationTextRecord(order=len(self._records), text=raw))
        return True

    def _accept_api_line(self, line: str, raw: str) -> bool | None:
        header = _API_HEADER_PATTERN.fullmatch(line)
        if header is None:
            return None
        frame_index = _integer_text(header.group(1))
        if frame_index is None or frame_index < 0:
            self._diagnose(
                "invalid_frame",
                "Frame references must be non-negative safe integers.",
                raw,
            )
            return True
        if self._media == "image" and frame_index != 0:
            self._diagnose(
                "unexpected_frame",
                "Image segmentation records require frame zero.",
                raw,
            )
            return True

        frame = FrameReference(frame_index) if self._media == "video" else None
        # Header warnings are reported with the line's first accepted record.
        header_warnings: list[_PendingWarning] = []
        _read_fields(
            header.group(2), "frame header", _FRAME_HEADER_FIELD_KEYS, header_warnings
        )
        body = header.group(3)
        if not body:
            self._diagnose(
                "malformed_record",
                "Malformed SAM API object record.",
                raw,
            )
            return True
        accepted = False
        position = 0
        while position < len(body):
            start = _RECORD_START_PATTERN.match(body, position)
            tokens: list[_ApiToken] = []
            end = position if start is None else start.end()
            token = None if start is None else _TOKEN_PATTERN.match(body, end)
            while token is not None:
                tokens.append(_ApiToken(name=token.group(1), body=token.group(2)))
                end = token.end()
                token = _TOKEN_PATTERN.match(body, end)
            record_warnings: list[_PendingWarning] = []
            record = (
                None
                if start is None
                or (end < len(body) and body[end] not in _RECORD_BOUNDARY)
                else _read_api_record(start.group(1), tokens, record_warnings)
            )
            if record is None:
                self._diagnose(
                    "malformed_record",
                    "Malformed SAM API object record.",
                    raw,
                )
                return True
            object_id = record.object_id
            coordinate_values = tuple(_integer_text(value) for value in record.box)
            if any(value is None for value in coordinate_values):
                self._diagnose(
                    "invalid_box", "SAM API box coordinates are invalid.", raw
                )
                return True
            (
                left,
                top,
                inclusive_right,
                inclusive_bottom,
                source_width,
                source_height,
            ) = cast(tuple[int, int, int, int, int, int], coordinate_values)
            if (
                source_width <= 0
                or source_height <= 0
                or left < 0
                or top < 0
                or inclusive_right < left
                or inclusive_bottom < top
                or inclusive_right >= source_width
                or inclusive_bottom >= source_height
            ):
                self._diagnose(
                    "invalid_box", "SAM API box coordinates are invalid.", raw
                )
                return True
            self._warn(header_warnings, raw)
            header_warnings = []
            self._warn(record_warnings, raw)
            bounds = SegmentationMaskBounds(
                left=left,
                top=top,
                right=inclusive_right + 1,
                bottom=inclusive_bottom + 1,
            )
            self._add_record(
                SegmentationBoxRecord(
                    order=len(self._records),
                    object_id=object_id,
                    frame=frame,
                    left=left,
                    top=top,
                    right=inclusive_right + 1,
                    bottom=inclusive_bottom + 1,
                    confidence=record.box_confidence,
                )
            )
            payload = record.payload
            self._accept_mask(
                object_id,
                frame,
                SegmentationMask(
                    encoding="lossless" if payload.startswith("~") else "one_bit",
                    payload=payload,
                    width=record.mask_width,
                    height=record.mask_height,
                ),
                raw,
                bounds,
                record.mask_confidence,
            )
            accepted = True
            position = end
        return accepted

    def _accept_mask(
        self,
        object_id: str,
        frame: FrameReference | None,
        mask: SegmentationMask,
        raw: str,
        bounds: SegmentationMaskBounds,
        confidence: float | None,
    ) -> None:
        area = mask.width * mask.height
        if mask.width <= 0 or mask.height <= 0 or area > _MAXIMUM_SAFE_INTEGER:
            self._diagnose(
                "invalid_mask_size", "Mask dimensions must be positive.", raw
            )
            return
        try:
            decode_mask_to_raster(mask)
        except InvalidSegmentationMaskError as error:
            self._diagnose("invalid_mask_payload", str(error), raw)
            return

        identity = SegmentationMaskIdentity(
            media=self._media,
            frame_index=None if frame is None else frame.frame_index,
            object_id=object_id,
        )
        revision = self._revisions.get(identity, 0) + 1
        self._revisions[identity] = revision
        self._add_record(
            SegmentationMaskRecord(
                order=len(self._records),
                object_id=object_id,
                frame=frame,
                identity=identity,
                revision=revision,
                mask=mask,
                bounds=bounds,
                confidence=confidence,
            )
        )

    def _add_record(self, record: SegmentationRecord) -> None:
        self._records.append(record)

    def _warn(self, warnings: list[_PendingWarning], raw: str) -> None:
        """Report each ignored item the first time its code and subject appear.

        The data the item came with was kept, so the diagnostic is a warning.
        """
        for warning in warnings:
            key = (warning.code, warning.subject)
            if key in self._warned:
                continue
            self._warned.add(key)
            self._diagnostics.append(
                SegmentationDiagnostic(
                    severity="warning",
                    code=warning.code,
                    message=warning.message,
                    line=self._line,
                    raw=raw,
                )
            )

    def _diagnose(self, code: str, message: str, raw: str) -> None:
        self._diagnostics.append(
            SegmentationDiagnostic(
                severity="error",
                code=code,
                message=message,
                line=self._line,
                raw=raw,
            )
        )

    def _snapshot(self) -> SegmentationSnapshot:
        records = tuple(self._records)
        diagnostics = tuple(self._diagnostics)
        if self._media == "image":
            return ImageSegmentationSnapshot(
                revision=self._revision,
                records=records,
                diagnostics=diagnostics,
                raw_output="".join(self._raw_output_parts),
            )
        return VideoSegmentationSnapshot(
            revision=self._revision,
            records=records,
            diagnostics=diagnostics,
            raw_output="".join(self._raw_output_parts),
        )

    def _result(self, outcome: ResponseStreamOutcome) -> SegmentationResult:
        records = tuple(self._records)
        diagnostics = tuple(self._diagnostics)
        if self._media == "image":
            return ImageSegmentationResult(
                revision=self._revision,
                records=records,
                diagnostics=diagnostics,
                raw_output="".join(self._raw_output_parts),
                outcome=outcome,
            )
        return VideoSegmentationResult(
            revision=self._revision,
            records=records,
            diagnostics=diagnostics,
            raw_output="".join(self._raw_output_parts),
            outcome=outcome,
        )


@dataclass(frozen=True, slots=True)
class _SegmentationFormat:
    _media: SegmentationMedia

    def create_parser(self) -> _SegmentationParser:
        return _SegmentationParser(self._media)


def image_segmentation_format() -> ResponseFormat[
    ImageSegmentationSnapshot, ImageSegmentationResult
]:
    """Create an image format whose parsers have isolated incremental state."""

    return cast(
        ResponseFormat[ImageSegmentationSnapshot, ImageSegmentationResult],
        _SegmentationFormat("image"),
    )


def video_segmentation_format() -> ResponseFormat[
    VideoSegmentationSnapshot, VideoSegmentationResult
]:
    """Create a video format whose parsers have isolated incremental state."""

    return cast(
        ResponseFormat[VideoSegmentationSnapshot, VideoSegmentationResult],
        _SegmentationFormat("video"),
    )
