# Copyright (c) Meta Platforms, Inc. and affiliates. All Rights Reserved.

"""Object-prompt text for SAM API requests.

Builds the ``input_text`` a request sends to name objects with boxes and points:
one block per source frame, each an object id followed by a box and points. See
"SAM API input" in the protocol document. The builder only formats text; it does
not send requests, and the API remains the authority on which prompts it accepts.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

__all__ = [
    "FramePrompt",
    "ObjectPromptError",
    "ObjectPromptErrorCode",
    "PromptBox",
    "PromptBoxLike",
    "PromptObject",
    "PromptPoint",
    "PromptPointLabel",
    "build_object_prompt",
]

PromptPointLabel = Literal["positive", "negative"]
"""A positive point marks part of the object; a negative point marks a region
that is not."""

ObjectPromptErrorCode = Literal[
    "no_objects",
    "invalid_size",
    "invalid_object_id",
    "duplicate_object_id",
    "empty_object",
    "invalid_frame",
    "duplicate_frame",
    "empty_prompt",
    "invalid_box",
    "invalid_point",
    "invalid_point_label",
]

_CANONICAL_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)")
_MAX_SAFE_INTEGER = 2**53 - 1


class ObjectPromptError(ValueError):
    """Raised for input the builder cannot encode as object-prompt text."""

    def __init__(self, message: str, code: ObjectPromptErrorCode) -> None:
        super().__init__(message)
        self.code: ObjectPromptErrorCode = code


class PromptBoxLike(Protocol):
    """Anything with half-open box edges, such as a parsed ``SegmentationBoxRecord``.

    The edges must be whole pixels; the builder rejects fractional values.
    """

    @property
    def left(self) -> float: ...
    @property
    def top(self) -> float: ...
    @property
    def right(self) -> float: ...
    @property
    def bottom(self) -> float: ...


@dataclass(frozen=True, slots=True)
class PromptBox:
    """A box with half-open ``right`` and ``bottom``, like a parsed box record."""

    left: int
    top: int
    right: int
    bottom: int


@dataclass(frozen=True, slots=True)
class PromptPoint:
    x: int
    y: int
    label: PromptPointLabel = "positive"


@dataclass(frozen=True, slots=True)
class FramePrompt:
    """One object's prompts on one source frame.

    Frame 0 is the only frame of an image.
    """

    frame: int = 0
    box: PromptBoxLike | None = None
    points: Sequence[PromptPoint] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PromptObject:
    """An object and its prompts; ``id`` is an int or its decimal string."""

    id: int | str
    prompts: Sequence[FramePrompt]


def _is_non_negative_integer(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _MAX_SAFE_INTEGER
    )


def _object_id_text(object_id: object) -> str:
    if _is_non_negative_integer(object_id):
        return str(object_id)
    if (
        isinstance(object_id, str)
        and _CANONICAL_DECIMAL.fullmatch(object_id)
        and int(object_id) <= _MAX_SAFE_INTEGER
    ):
        return object_id
    raise ObjectPromptError(
        f"Object id {object_id!r} is not a non-negative integer or its decimal string.",
        "invalid_object_id",
    )


def _box_token(box: object, width: int, height: int, view: str) -> str:
    edges = [getattr(box, name, None) for name in ("left", "top", "right", "bottom")]
    if not all(_is_non_negative_integer(edge) for edge in edges):
        raise ObjectPromptError(
            f"Box {box!r} must have non-negative integer edges.", "invalid_box"
        )
    left, top, right, bottom = (int(edge) for edge in edges)  # type: ignore[arg-type]
    if not (left < right <= width and top < bottom <= height):
        raise ObjectPromptError(
            f"Box {box!r} must have left < right <= {width} "
            f"and top < bottom <= {height}.",
            "invalid_box",
        )
    return f"<|box;x1={left};y1={top};x2={right - 1};y2={bottom - 1};{view}|>"


def _point_token(point: object, width: int, height: int, view: str) -> str:
    x = getattr(point, "x", None)
    y = getattr(point, "y", None)
    if not (
        _is_non_negative_integer(x)
        and _is_non_negative_integer(y)
        and int(x) < width  # type: ignore[arg-type]
        and int(y) < height  # type: ignore[arg-type]
    ):
        raise ObjectPromptError(
            f"Point {point!r} must have integer coordinates "
            f"inside the {width}x{height} media.",
            "invalid_point",
        )
    label = getattr(point, "label", "positive")
    if label not in ("positive", "negative"):
        raise ObjectPromptError(
            f"Point label {label!r} must be 'positive' or 'negative'.",
            "invalid_point_label",
        )
    prefix = "-" if label == "negative" else ""
    return f"{prefix}<|point;x={x};y={y};{view}|>"


def build_object_prompt(
    *, size: tuple[int, int], objects: Sequence[PromptObject]
) -> str:
    """Build object-prompt text for a SAM API request.

    ``size`` is the media's ``(width, height)`` in pixels, as displayed after any
    rotation the file specifies. Every coordinate is in this space, so scale
    coordinates from a scaled or cropped view to it before building the prompt.
    Frames are emitted in ascending order, objects in input order within a frame,
    and each object's box before its points.

    Raises:
        ObjectPromptError: for input the builder cannot encode, such as a size or
            coordinate that is not a non-negative integer, a box or point outside
            ``size``, an empty box, an object or frame prompt with no box and no
            points, or the same object or frame given twice.
    """
    if (
        not isinstance(size, tuple)
        or len(size) != 2
        or not all(_is_non_negative_integer(value) and value > 0 for value in size)
    ):
        raise ObjectPromptError(
            f"Size {size!r} must be a (width, height) tuple of positive integers.",
            "invalid_size",
        )
    width, height = size
    if len(objects) == 0:
        raise ObjectPromptError("At least one object is required.", "no_objects")
    view = f"w={width};h={height}"
    seen_ids: set[str] = set()
    segments_by_frame: dict[int, list[str]] = {}
    for prompt_object in objects:
        object_id = _object_id_text(getattr(prompt_object, "id", None))
        if object_id in seen_ids:
            raise ObjectPromptError(
                f"Object {object_id} is given more than once; "
                "list all of its frames in one object.",
                "duplicate_object_id",
            )
        seen_ids.add(object_id)
        prompts = getattr(prompt_object, "prompts", ())
        if isinstance(prompts, (str, bytes)) or len(prompts) == 0:
            raise ObjectPromptError(
                f"Object {object_id} has no prompts.", "empty_object"
            )
        seen_frames: set[int] = set()
        for prompt in prompts:
            frame = getattr(prompt, "frame", 0)
            if not _is_non_negative_integer(frame):
                raise ObjectPromptError(
                    f"Frame {frame!r} of object {object_id} "
                    "is not a non-negative integer.",
                    "invalid_frame",
                )
            if frame in seen_frames:
                raise ObjectPromptError(
                    f"Object {object_id} has more than one prompt on frame {frame}.",
                    "duplicate_frame",
                )
            seen_frames.add(frame)
            box = getattr(prompt, "box", None)
            points = getattr(prompt, "points", ()) or ()
            if box is None and len(points) == 0:
                raise ObjectPromptError(
                    f"Object {object_id} has neither a box nor points "
                    f"on frame {frame}.",
                    "empty_prompt",
                )
            tokens = ([] if box is None else [_box_token(box, width, height, view)]) + [
                _point_token(point, width, height, view) for point in points
            ]
            segments_by_frame.setdefault(frame, []).append(object_id + "".join(tokens))
    return "".join(
        f"<{frame}f>" + ",".join(segments_by_frame[frame])
        for frame in sorted(segments_by_frame)
    )
