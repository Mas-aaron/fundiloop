# Copyright (c) Meta Platforms, Inc. and affiliates. All Rights Reserved.
#
# Portions of this file are a dependency-free port of the COCO API mask module
# (`common/maskApi.c` and `PythonAPI/pycocotools/_mask.pyx`).
# Copyright (c) 2014, Piotr Dollar and Tsung-Yi Lin. All rights reserved.
# Licensed under the Simplified BSD License; see THIRD_PARTY_NOTICES.md in the
# repository root for the full license text.

from __future__ import annotations

from dataclasses import dataclass

from ._mask_codec import decode_mask_to_raster
from ._types import SegmentationMask

_MASK_32 = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class RLEObject:
    size: tuple[int, int]
    counts: str


@dataclass(slots=True)
class _DataArray:
    data: bytes
    shape: tuple[int, ...]

    def reshape(self, shape: tuple[int, ...]) -> _DataArray:
        return _DataArray(self.data, shape)


@dataclass(slots=True)
class _RLE:
    h: int
    w: int
    m: int
    cnts: list[int]


class _RLEs:
    __slots__ = ("R", "n")

    def __init__(self, n: int) -> None:
        self.R = [_RLE(0, 0, 0, [0]) for _ in range(n)]
        self.n = n


class _Masks:
    __slots__ = ("h", "mask", "n", "w")

    def __init__(self, h: int, w: int, n: int) -> None:
        self.mask = bytearray(h * w * n)
        self.h = h
        self.w = w
        self.n = n

    def to_data_array(self) -> _DataArray:
        return _DataArray(bytes(self.mask), (self.h, self.w, self.n))


def _rle_init(R: _RLE, h: int, w: int, m: int, cnts: list[int]) -> None:
    R.h = h
    R.w = w
    R.m = m
    R.cnts = [0] if m == 0 else cnts


def _encode(bitmask: _DataArray) -> RLEObject | list[RLEObject]:
    if len(bitmask.shape) == 3:
        return _encode_many(bitmask)
    if len(bitmask.shape) == 2:
        h = bitmask.shape[0]
        w = bitmask.shape[1]
        result = _encode_many(bitmask.reshape((h, w, 1)))
        return result[0]
    raise ValueError("wrong shape of bitmask")


def _encode_many(bitmask: _DataArray) -> list[RLEObject]:
    h = bitmask.shape[0]
    w = bitmask.shape[1]
    n = bitmask.shape[2]
    rles = _RLEs(n)
    _rle_encode(rles.R, bitmask.data, h, w, n)
    return _to_string(rles)


def _decode(rle_objects: RLEObject | list[RLEObject]) -> _DataArray:
    rles = _from_string(rle_objects)
    h = rles.R[0].h
    w = rles.R[0].w
    n = rles.n
    masks = _Masks(h, w, n)
    _rle_decode(rles.R, masks.mask, n)
    data_array = masks.to_data_array()
    return data_array if isinstance(rle_objects, list) else data_array.reshape((h, w))


def _rle_encode(R: list[_RLE], M: bytes, h: int, w: int, n: int) -> None:
    area = w * h
    cnts: list[int] = []
    for i in range(n):
        from_ = area * i
        to = area * (i + 1)
        values = M[from_:to]
        k = 0
        previous = 0
        count = 0
        for value in values:
            if value != previous:
                if k == len(cnts):
                    cnts.append(count)
                else:
                    cnts[k] = count
                k += 1
                count = 0
                previous = value
            count += 1
        if k == len(cnts):
            cnts.append(count)
        else:
            cnts[k] = count
        k += 1
        _rle_init(R[i], h, w, k, list(cnts))


def _rle_decode(R: list[_RLE], M: bytearray, n: int) -> None:
    position = 0
    for i in range(n):
        value = False
        for j in range(R[i].m):
            for _ in range(R[i].cnts[j]):
                M[position] = 0 if value is False else 1
                position += 1
            value = not value


def _i32(value: int) -> int:
    value &= _MASK_32
    return value if value < 1 << 31 else value - (1 << 32)


def _rle_to_string(R: _RLE) -> str:
    output: list[str] = []
    for i in range(R.m):
        value = R.cnts[i]
        if i > 2:
            value -= R.cnts[i - 2]
        value = _i32(value)
        more = True
        while more:
            character = value & 0x1F
            value >>= 5
            more = value != -1 if character & 0x10 else value != 0
            if more:
                character |= 0x20
            character += 48
            output.append(chr(character))
    return "".join(output)


def _to_string(rles: _RLEs) -> list[RLEObject]:
    return [
        RLEObject(size=(rle.h, rle.w), counts=_rle_to_string(rle)) for rle in rles.R
    ]


def _from_string(
    input_rle_objects: RLEObject | list[RLEObject],
) -> _RLEs:
    rle_objects = (
        input_rle_objects
        if isinstance(input_rle_objects, list)
        else [input_rle_objects]
    )
    rles = _RLEs(len(rle_objects))
    for index, rle_object in enumerate(rle_objects):
        _rle_from_string(
            rles.R[index], rle_object.counts, rle_object.size[0], rle_object.size[1]
        )
    return rles


def _rle_from_string(R: _RLE, value: str, h: int, w: int) -> None:
    counts: list[int] = []
    position = 0
    while position < len(value):
        count = 0
        shift = 0
        more = 1
        while more:
            character = ord(value[position]) - 48
            count = _i32(count | _i32((character & 0x1F) << ((5 * shift) & 0x1F)))
            more = character & 0x20
            position += 1
            shift += 1
            if not more and character & 0x10:
                count = _i32(count | _i32(-1 << ((5 * shift) & 0x1F)))
        if len(counts) > 2:
            count += counts[-2]
        counts.append(count)
    _rle_init(R, h, w, len(counts), counts)


_Point = tuple[float, float]
_Segment = tuple[float, float, float, float]


def _decode_to_svg_path(rle_objects: RLEObject | list[RLEObject]) -> list[str]:
    rle_array = rle_objects if isinstance(rle_objects, list) else [rle_objects]
    paths: list[str] = []
    for rle_object in rle_array:
        mask_data = _decode(rle_object)
        height = rle_object.size[0]
        width = rle_object.size[1]
        paths.append(_trace_contours(mask_data.data, height, width))
    return paths


def _trace_contours(mask: bytes, height: int, width: int) -> str:
    def get_pixel(row: int, col: int) -> int:
        if row < 0 or row >= height or col < 0 or col >= width:
            return 0
        return mask[col * height + row]

    segments: list[_Segment] = []
    for row in range(height + 1):
        for col in range(width + 1):
            tl = get_pixel(row - 1, col - 1)
            tr = get_pixel(row - 1, col)
            bl = get_pixel(row, col - 1)
            br = get_pixel(row, col)
            case_index = (tl << 3) | (tr << 2) | (br << 1) | bl
            top = (col - 0.5, row - 1.0)
            bottom = (col - 0.5, float(row))
            left = (col - 1.0, row - 0.5)
            right = (float(col), row - 0.5)

            if case_index in (0, 15):
                continue
            if case_index in (1, 14):
                segments.append((*left, *bottom))
            elif case_index in (2, 13):
                segments.append((*bottom, *right))
            elif case_index in (3, 12):
                segments.append((*left, *right))
            elif case_index in (4, 11):
                segments.append((*top, *right))
            elif case_index == 5:
                segments.append((*left, *top))
                segments.append((*bottom, *right))
            elif case_index in (6, 9):
                segments.append((*top, *bottom))
            elif case_index in (7, 8):
                segments.append((*left, *top))
            elif case_index == 10:
                segments.append((*top, *right))
                segments.append((*left, *bottom))

    if not segments:
        return ""

    paths = _connect_segments(segments)
    svg_paths: list[str] = []
    for path in paths:
        if len(path) < 2:
            continue
        commands = [f"M{_number(path[0][0])} {_number(path[0][1])}"]
        commands.extend(
            f"L{_number(point[0])} {_number(point[1])}" for point in path[1:]
        )
        commands.append("Z")
        svg_paths.append("".join(commands))
    return "".join(svg_paths)


def _number(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def _connect_segments(segments: list[_Segment]) -> list[list[_Point]]:
    adjacency: dict[_Point, list[_Point]] = {}
    insertion_order: list[_Point] = []

    def add(point: _Point, neighbor: _Point) -> None:
        if point not in adjacency:
            adjacency[point] = []
            insertion_order.append(point)
        adjacency[point].append(neighbor)

    for x1, y1, x2, y2 in segments:
        add((x1, y1), (x2, y2))
        add((x2, y2), (x1, y1))

    paths: list[list[_Point]] = []
    visited: set[_Point] = set()
    for start in insertion_order:
        neighbors = adjacency.get(start)
        if start in visited or not neighbors:
            continue

        path: list[_Point] = []
        current = start
        previous: _Point | None = None
        while True:
            path.append(current)
            current_neighbors = adjacency.get(current)
            if not current_neighbors:
                break

            next_ = next(
                (neighbor for neighbor in current_neighbors if neighbor != previous),
                None,
            )
            if next_ is None:
                break

            filtered = [neighbor for neighbor in current_neighbors if neighbor != next_]
            if filtered:
                adjacency[current] = filtered
            else:
                del adjacency[current]

            next_neighbors = adjacency.get(next_)
            if next_neighbors is not None:
                next_filtered = [
                    neighbor for neighbor in next_neighbors if neighbor != current
                ]
                if next_filtered:
                    adjacency[next_] = next_filtered
                else:
                    del adjacency[next_]

            if next_ == start:
                break

            previous = current
            current = next_
            visited.add(previous)

        if len(path) >= 3:
            paths.append(path)

    return paths


def decode_mask_to_rle(mask: SegmentationMask) -> RLEObject:
    raster = decode_mask_to_raster(mask)
    height = int(mask.height)
    width = int(mask.width)
    coco = bytearray(len(raster))
    for row in range(height):
        for col in range(width):
            coco[col * height + row] = raster[row * width + col]
    encoded = _encode(_DataArray(bytes(coco), (height, width)))
    if isinstance(encoded, list):
        raise RuntimeError("COCO encoding returned multiple masks for one raster.")
    return encoded


def decode_mask_to_svg_path(mask: SegmentationMask) -> str:
    paths = _decode_to_svg_path(decode_mask_to_rle(mask))
    if len(paths) != 1:
        raise RuntimeError("SVG conversion returned multiple paths for one mask.")
    return paths[0]
