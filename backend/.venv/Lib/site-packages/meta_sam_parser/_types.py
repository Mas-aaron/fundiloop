# Copyright (c) Meta Platforms, Inc. and affiliates. All Rights Reserved.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Generic, Literal, Protocol, TypeAlias, TypeVar

SegmentationMedia: TypeAlias = Literal["image", "video"]
DiagnosticSeverity: TypeAlias = Literal["warning", "error"]
IncompleteReason: TypeAlias = Literal["response", "eof"]
ResponseSourceOperation: TypeAlias = Literal["iterator", "next", "return"]


@dataclass(frozen=True, slots=True)
class OutputTextLane:
    """Identity of one official Responses API output-text lane."""

    item_id: str
    output_index: int
    content_index: int


class ResponsesEvent(Protocol):
    """Minimum attribute-object event contract accepted by the stream adapter."""

    @property
    def type(self) -> str: ...


ResponsesEventLike: TypeAlias = ResponsesEvent | Mapping[str, object]


@dataclass(frozen=True, slots=True)
class FrameReference:
    """A zero-based source-video frame index."""

    frame_index: int


@dataclass(frozen=True, slots=True)
class SegmentationMaskBounds:
    """Half-open source-media bounds for a box-local mask."""

    left: float
    top: float
    right: float
    bottom: float


SegmentationMaskEncoding: TypeAlias = Literal["lossless", "one_bit"]
"""The wire encodings of a mask payload: ``~`` selects lossless, ``!`` one_bit."""


@dataclass(frozen=True, slots=True)
class SegmentationMask:
    """One complete mask exactly as the SAM API emitted it.

    ``payload`` is the base85 text after the encoding marker and is never
    partial; ``width`` and ``height`` are the raster's own dimensions, not the
    frame's.
    """

    encoding: SegmentationMaskEncoding
    payload: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class SegmentationMaskIdentity:
    """Stable identity used to revision masks for one object and frame."""

    media: SegmentationMedia
    frame_index: int | None
    object_id: str


@dataclass(frozen=True, slots=True)
class SegmentationTextRecord:
    """A non-structured output line retained in source order."""

    kind: Literal["text"] = field(default="text", init=False)
    order: int
    text: str


@dataclass(frozen=True, slots=True)
class SegmentationBoxRecord:
    """A half-open or explicitly supplied box in source-media coordinates."""

    kind: Literal["box"] = field(default="box", init=False)
    order: int
    object_id: str
    left: float
    top: float
    right: float
    bottom: float
    frame: FrameReference | None = None
    confidence: float | None = None
    """Detection confidence from the box token's optional ``c`` field.

    A finite number from 0 through 1, or ``None`` when the API omitted ``c``.
    ``None`` does not mean zero.
    """


@dataclass(frozen=True, slots=True)
class SegmentationMaskRecord:
    """A decoded-and-validated complete mask record."""

    kind: Literal["mask"] = field(default="mask", init=False)
    order: int
    object_id: str
    identity: SegmentationMaskIdentity
    revision: int
    mask: SegmentationMask
    bounds: SegmentationMaskBounds
    frame: FrameReference | None = None
    confidence: float | None = None
    """Detection confidence from the mask token's optional ``c`` field.

    A finite number from 0 through 1, or ``None`` when the API omitted ``c``.
    ``None`` does not mean zero.
    """


SegmentationRecord: TypeAlias = (
    SegmentationTextRecord | SegmentationBoxRecord | SegmentationMaskRecord
)


@dataclass(frozen=True, slots=True)
class SegmentationDiagnostic:
    """A recoverable problem with one source line."""

    severity: DiagnosticSeverity
    code: str
    message: str
    line: int
    raw: str


@dataclass(frozen=True, slots=True)
class CompletedOutcome:
    """A parser outcome for a completed response."""

    status: Literal["completed"] = field(default="completed", init=False)


@dataclass(frozen=True, slots=True)
class IncompleteOutcome:
    """A parser outcome for an incomplete response or end of input."""

    status: Literal["incomplete"] = field(default="incomplete", init=False)
    reason: IncompleteReason
    detail: str | None = None


ResponseStreamOutcome: TypeAlias = CompletedOutcome | IncompleteOutcome


@dataclass(frozen=True, slots=True)
class ImageSegmentationSnapshot:
    """An immutable cumulative image-segmentation view."""

    media: Literal["image"] = field(default="image", init=False)
    revision: int
    records: tuple[SegmentationRecord, ...]
    diagnostics: tuple[SegmentationDiagnostic, ...]
    raw_output: str


@dataclass(frozen=True, slots=True)
class VideoSegmentationSnapshot:
    """An immutable cumulative video-segmentation view."""

    media: Literal["video"] = field(default="video", init=False)
    revision: int
    records: tuple[SegmentationRecord, ...]
    diagnostics: tuple[SegmentationDiagnostic, ...]
    raw_output: str


SegmentationSnapshot: TypeAlias = ImageSegmentationSnapshot | VideoSegmentationSnapshot


@dataclass(frozen=True, slots=True)
class ImageSegmentationResult(ImageSegmentationSnapshot):
    """The final immutable image-segmentation view and outcome."""

    outcome: ResponseStreamOutcome


@dataclass(frozen=True, slots=True)
class VideoSegmentationResult(VideoSegmentationSnapshot):
    """The final immutable video-segmentation view and outcome."""

    outcome: ResponseStreamOutcome


SegmentationResult: TypeAlias = ImageSegmentationResult | VideoSegmentationResult


EventT_co = TypeVar("EventT_co", covariant=True)
ResultT_co = TypeVar("ResultT_co", covariant=True)


@dataclass(frozen=True, slots=True)
class ParserFinish(Generic[EventT_co, ResultT_co]):
    """Events emitted while finalizing and the final parser result."""

    events: tuple[EventT_co, ...]
    result: ResultT_co


class ResponseFormatParser(Protocol[EventT_co, ResultT_co]):
    """Incremental parser created by a response format."""

    def push(self, chunk: str, *, emit: bool = True) -> tuple[EventT_co, ...]: ...

    def finish(
        self, outcome: ResponseStreamOutcome
    ) -> ParserFinish[EventT_co, ResultT_co]: ...


class ResponseFormat(Protocol[EventT_co, ResultT_co]):
    """Factory for isolated incremental parser state."""

    def create_parser(self) -> ResponseFormatParser[EventT_co, ResultT_co]: ...
