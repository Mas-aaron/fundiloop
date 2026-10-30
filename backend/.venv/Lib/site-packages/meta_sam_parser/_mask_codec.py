# Copyright (c) Meta Platforms, Inc. and affiliates. All Rights Reserved.

from __future__ import annotations

import math
from array import array
from collections.abc import Sequence

from ._errors import InvalidSegmentationMaskError
from ._types import SegmentationMask

_EXCLUDED = frozenset({'"', "\\", ",", ";", "<", ">", "|"})
_ALPHABET = "".join(
    character for code in range(0x21, 0x7F) if (character := chr(code)) not in _EXCLUDED
)
_RADIX = 85
_PREFIX_LENGTH = 5
_CHARACTER_VALUES = [-1] * 128
for _index, _character in enumerate(_ALPHABET):
    _CHARACTER_VALUES[ord(_character)] = _index

_TOP = 1 << 24
_BOTTOM = 1 << 16
_MASK_8 = 0xFF
_MASK_16 = 0xFFFF
_MASK_32 = 0xFFFFFFFF
_INCREMENT = 14
_COUNT_LIMIT = 4096
_CONTEXT_COUNT = 1 << 12
_PADDING = 2
_SYMBOLS = 256
_TOTAL_INDEX = _SYMBOLS
_SPATIAL_MODES = 5
_ZERO_INCREMENT = 32
_ZERO_LIMIT = 16384
_ORDER_ONE_INCREMENT = 56
_ORDER_ONE_LIMIT = 8192
_ORDER_TWO_STEP = 14 * 44
_ORDER_TWO_LIMIT = 14 * 3584
_ORDER_ZERO_STEP = 2 * 16
_ORDER_ZERO_LIMIT = 2 * 2048
_ORDER_ZERO_INITIAL = 2
_ORDER_ONE_INITIAL = 1
_MAXIMUM_SAFE_INTEGER = (1 << 53) - 1


def _invalid(
    message: str, cause: BaseException | None = None
) -> InvalidSegmentationMaskError:
    return InvalidSegmentationMaskError(message, cause=cause)


def _u8(value: int) -> int:
    return value & _MASK_8


def _u16(value: int) -> int:
    return value & _MASK_16


def _u32(value: int) -> int:
    return value & _MASK_32


def _i32(value: int) -> int:
    value &= _MASK_32
    return value if value < 1 << 31 else value - (1 << 32)


def _as_safe_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if abs(value) <= _MAXIMUM_SAFE_INTEGER else None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        integer = int(value)
        return integer if abs(integer) <= _MAXIMUM_SAFE_INTEGER else None
    return None


def _javascript_string_length(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _digit(character: str) -> int:
    code = ord(character)
    value = _CHARACTER_VALUES[code] if code < len(_CHARACTER_VALUES) else -1
    if value < 0 or value >= _RADIX:
        raise _invalid("Mask payload contains an invalid character.")
    return value


def _pack(input_: Sequence[int]) -> str:
    length = len(input_)
    remaining_length = length
    prefix = [""] * _PREFIX_LENGTH
    for index in range(_PREFIX_LENGTH - 1, -1, -1):
        prefix[index] = _ALPHABET[remaining_length % _RADIX]
        remaining_length //= _RADIX
    if remaining_length != 0:
        raise _invalid("Mask payload is too large.")

    output = prefix
    offset = 0
    full = length - (length % 4)
    while offset < full:
        value = _u32(
            (input_[offset] << 24)
            | (input_[offset + 1] << 16)
            | (input_[offset + 2] << 8)
            | input_[offset + 3]
        )
        digits = [0] * 5
        for index in range(4, -1, -1):
            digits[index] = value % _RADIX
            value = (value - digits[index]) // _RADIX
        output.extend(_ALPHABET[encoded] for encoded in digits)
        offset += 4

    remainder = length - offset
    if remainder > 0:
        value = 0
        for index in range(4):
            value = _u32(
                (value << 8) | (input_[offset + index] if index < remainder else 0)
            )
        digits = [0] * 5
        for index in range(4, -1, -1):
            digits[index] = value % _RADIX
            value = (value - digits[index]) // _RADIX
        output.extend(_ALPHABET[digits[index]] for index in range(remainder + 1))
    return "".join(output)


def _unpack(payload: str) -> bytes:
    payload_length = _javascript_string_length(payload)
    if payload_length < _PREFIX_LENGTH:
        raise _invalid("Mask payload is missing its length prefix.")
    length = 0
    for character in payload[:_PREFIX_LENGTH]:
        length = length * _RADIX + _digit(character)
    if length > _MAXIMUM_SAFE_INTEGER:
        raise _invalid("Mask payload length is unsupported.")

    remainder = length % 4
    expected = (
        _PREFIX_LENGTH + (length // 4) * 5 + (0 if remainder == 0 else remainder + 1)
    )
    if payload_length != expected:
        raise _invalid("Mask payload length does not match its prefix.")

    output = bytearray(length)
    source = _PREFIX_LENGTH
    destination = 0
    full = length - remainder
    while destination < full:
        value = 0
        for index in range(5):
            value = value * _RADIX + _digit(payload[source + index])
        if value > _MASK_32:
            raise _invalid("Mask payload contains an out-of-range group.")
        source += 5
        output[destination] = (value >> 24) & _MASK_8
        output[destination + 1] = (value >> 16) & _MASK_8
        output[destination + 2] = (value >> 8) & _MASK_8
        output[destination + 3] = value & _MASK_8
        destination += 4

    if remainder > 0:
        value = 0
        for index in range(5):
            value = value * _RADIX + (
                _digit(payload[source + index]) if index < remainder + 1 else _RADIX - 1
            )
        if value > _MASK_32:
            raise _invalid("Mask payload contains an out-of-range tail.")
        for index in range(remainder):
            output[destination + index] = (value >> (24 - index * 8)) & _MASK_8

    if _pack(output) != payload:
        raise _invalid("Mask payload is not in canonical form.")
    return bytes(output)


class _RangeEncoder:
    __slots__ = ("_low", "_output", "_range")

    def __init__(self) -> None:
        self._low = 0
        self._range = _MASK_32
        self._output = bytearray()

    def encode(self, cumulative: int, frequency: int, total: int) -> None:
        scaled = self._range // total
        self._low = _u32(self._low + scaled * cumulative)
        self._range = scaled * frequency
        while True:
            if _u32(self._low ^ _u32(self._low + self._range)) < _TOP:
                pass
            elif self._range < _BOTTOM:
                self._range = _u32(-self._low) & (_BOTTOM - 1)
            else:
                break
            self._output.append((self._low >> 24) & _MASK_8)
            self._low = _u32(self._low << 8)
            self._range = _u32(self._range << 8)

    def finish(self) -> bytes:
        for _ in range(4):
            self._output.append((self._low >> 24) & _MASK_8)
            self._low = _u32(self._low << 8)
        return bytes(self._output)


class _RangeDecoder:
    __slots__ = ("_code", "_input", "_low", "_position", "_range", "_scaled")

    def __init__(self, input_: bytes) -> None:
        if len(input_) < 4:
            raise _invalid("Mask payload is missing its finalization.")
        self._input = input_
        self._position = 0
        self._low = 0
        self._range = _MASK_32
        self._code = 0
        self._scaled = 0
        for _ in range(4):
            self._code = _u32((self._code << 8) | self._read())

    def frequency(self, total: int) -> int:
        self._scaled = self._range // total
        if self._scaled == 0:
            raise _invalid("Mask payload has invalid coding state.")
        value = _u32(self._code - self._low) // self._scaled
        return total - 1 if value >= total else value

    def get_freq(self, total: int) -> int:
        return self.frequency(total)

    def decode(self, cumulative: int, frequency: int) -> None:
        self._low = _u32(self._low + self._scaled * cumulative)
        self._range = self._scaled * frequency
        while True:
            if _u32(self._low ^ _u32(self._low + self._range)) < _TOP:
                pass
            elif self._range < _BOTTOM:
                self._range = _u32(-self._low) & (_BOTTOM - 1)
            else:
                break
            self._code = _u32((self._code << 8) | self._read())
            self._low = _u32(self._low << 8)
            self._range = _u32(self._range << 8)

    def _read(self) -> int:
        if self._position >= len(self._input):
            raise _invalid("Mask payload ended before decoding completed.")
        value = self._input[self._position]
        self._position += 1
        return value


def _get_order_two(counts_by_context: dict[int, array[int]], key: int) -> array[int]:
    counts = counts_by_context.get(key)
    if counts is None:
        counts = array("H", [0]) * (_SYMBOLS + 1)
        counts_by_context[key] = counts
    return counts


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    return up if up_distance <= upper_left_distance else upper_left


def _predict_byte(
    output: bytearray,
    offset: int,
    x: int,
    y: int,
    mode: int,
    width: int,
) -> int:
    if mode < 0 or mode >= _SPATIAL_MODES:
        raise _invalid("Lossless mask payload uses an invalid predictor.")
    left = output[offset - 1] if x > 0 else 0
    up = output[offset - width] if y > 0 else 0
    if mode == 1:
        return up
    if mode == 2:
        return left
    if mode == 3:
        return (left + up) >> 1
    upper_left = output[offset - width - 1] if x > 0 and y > 0 else 0
    if mode == 0:
        return _paeth(left, up, upper_left)
    if upper_left >= max(left, up):
        return min(left, up)
    if upper_left <= min(left, up):
        return max(left, up)
    return left + up - upper_left


def _unzig(value: int, prediction: int) -> int:
    delta = -((value + 1) >> 1) if value & 1 else value >> 1
    return _u8(prediction + delta)


def _zero_context(residuals: bytearray, offset: int, x: int, y: int, width: int) -> int:
    left = int(residuals[offset - 1] == 0) if x > 0 else 1
    up = int(residuals[offset - width] == 0) if y > 0 else 1
    upper_left = int(residuals[offset - width - 1] == 0) if x > 0 and y > 0 else 1
    upper_right = (
        int(residuals[offset - width + 1] == 0) if y > 0 and x < width - 1 else 1
    )
    return left | (up << 1) | (upper_left << 2) | (upper_right << 3)


def _decode_lossless_raster(payload: str, width: int, height: int) -> bytes:
    if not payload.startswith("~"):
        raise _invalid("lossless mask payloads must start with ~.")
    packed = _unpack(payload[1:])
    if len(packed) < 5:
        raise _invalid("Lossless mask payload is truncated.")
    selector = packed[0]
    decoder = _RangeDecoder(packed[1:])
    length = width * height
    output = bytearray(length)
    residuals = bytearray(length)
    zero_counts = array("I", [1]) * 32
    order_one = array("H", [_ORDER_ONE_INITIAL]) * (_SYMBOLS * _SYMBOLS)
    order_one_totals = array("i", [_SYMBOLS * _ORDER_ONE_INITIAL]) * _SYMBOLS
    order_zero = array("H", [_ORDER_ZERO_INITIAL]) * _SYMBOLS
    order_zero_total = _SYMBOLS * _ORDER_ZERO_INITIAL
    order_two: dict[int, array[int]] = {}
    previous = 0

    for offset in range(length):
        x = offset % width
        y = offset // width
        zero_offset = _zero_context(residuals, offset, x, y, width) << 1
        zero = zero_counts[zero_offset]
        nonzero = zero_counts[zero_offset + 1]
        zero_frequency = decoder.get_freq(zero + nonzero)
        bit = 0 if zero_frequency < zero else 1
        decoder.decode(0 if bit == 0 else zero, zero if bit == 0 else nonzero)
        zero_counts[zero_offset + bit] = _u32(
            zero_counts[zero_offset + bit] + _ZERO_INCREMENT
        )
        if zero_counts[zero_offset] + zero_counts[zero_offset + 1] >= _ZERO_LIMIT:
            zero_counts[zero_offset] = (zero_counts[zero_offset] >> 1) or 1
            zero_counts[zero_offset + 1] = (zero_counts[zero_offset + 1] >> 1) or 1

        symbol = 0
        if bit == 1:
            above = residuals[offset - width] if offset >= width else 0
            second = _get_order_two(order_two, (previous << 8) | above)
            first_offset = previous << 8
            excluded_zero = second[0] + order_one[first_offset] + order_zero[0]
            total = (
                second[_TOTAL_INDEX]
                + order_one_totals[previous]
                + order_zero_total
                - excluded_zero
            )
            target = decoder.get_freq(total)
            cumulative = 0
            symbol = 1
            frequency = second[1] + order_one[first_offset + 1] + order_zero[1]
            while cumulative + frequency <= target:
                cumulative += frequency
                symbol += 1
                if symbol >= _SYMBOLS:
                    raise _invalid("Lossless mask payload is malformed.")
                frequency = (
                    second[symbol]
                    + order_one[first_offset + symbol]
                    + order_zero[symbol]
                )
            decoder.decode(cumulative, frequency)
            second[symbol] = _u16(second[symbol] + _ORDER_TWO_STEP)
            second[_TOTAL_INDEX] = _u16(second[_TOTAL_INDEX] + _ORDER_TWO_STEP)
            if second[_TOTAL_INDEX] >= _ORDER_TWO_LIMIT:
                total_after_scaling = 0
                for value in range(_SYMBOLS):
                    second[value] = second[value] >> 1
                    total_after_scaling += second[value]
                second[_TOTAL_INDEX] = _u16(total_after_scaling)
            order_one[first_offset + symbol] = _u16(
                order_one[first_offset + symbol] + _ORDER_ONE_INCREMENT
            )
            order_one_totals[previous] = _i32(
                order_one_totals[previous] + _ORDER_ONE_INCREMENT
            )
            if order_one_totals[previous] >= _ORDER_ONE_LIMIT:
                total_after_scaling = 0
                for value in range(_SYMBOLS):
                    scaled = (order_one[first_offset + value] >> 1) or 1
                    order_one[first_offset + value] = scaled
                    total_after_scaling += scaled
                order_one_totals[previous] = _i32(total_after_scaling)
            order_zero[symbol] = _u16(order_zero[symbol] + _ORDER_ZERO_STEP)
            order_zero_total += _ORDER_ZERO_STEP
            if order_zero_total >= _ORDER_ZERO_LIMIT:
                total_after_scaling = 0
                for value in range(_SYMBOLS):
                    order_zero[value] = order_zero[value] >> 1
                    total_after_scaling += order_zero[value]
                order_zero_total = total_after_scaling
        residuals[offset] = symbol
        output[offset] = _unzig(
            symbol, _predict_byte(output, offset, x, y, selector, width)
        )
        previous = symbol
    return bytes(output)


def _context_at(raster: bytearray, position: int, prior: int, earlier: int) -> int:
    return (
        raster[position - 1]
        | (raster[position - 2] << 1)
        | (raster[prior - 2] << 2)
        | (raster[prior - 1] << 3)
        | (raster[prior] << 4)
        | (raster[prior + 1] << 5)
        | (raster[prior + 2] << 6)
        | (raster[earlier - 2] << 7)
        | (raster[earlier - 1] << 8)
        | (raster[earlier] << 9)
        | (raster[earlier + 1] << 10)
        | (raster[earlier + 2] << 11)
    )


def _update_counts(counts: array[int], offset: int, bit: int) -> None:
    counts[offset + bit] = _u16(counts[offset + bit] + _INCREMENT)
    if counts[offset] + counts[offset + 1] >= _COUNT_LIMIT:
        counts[offset] = (counts[offset] >> 1) or 1
        counts[offset + 1] = (counts[offset + 1] >> 1) or 1


def _encode_raster(input_: Sequence[int], width: int, height: int) -> str:
    encoder = _RangeEncoder()
    counts = array("H", [1]) * (_CONTEXT_COUNT * 2)
    padded_width = width + 2 * _PADDING
    padded = bytearray(padded_width * (height + _PADDING))
    for y in range(height):
        row = (y + _PADDING) * padded_width + _PADDING
        for x in range(width):
            position = row + x
            context = _context_at(
                padded,
                position,
                position - padded_width,
                position - 2 * padded_width,
            )
            offset = context << 1
            zero = counts[offset]
            one = counts[offset + 1]
            bit = input_[y * width + x]
            if bit not in (0, 1):
                raise _invalid("Decoded mask is not binary.")
            if bit == 0:
                encoder.encode(0, zero, zero + one)
            else:
                encoder.encode(zero, one, zero + one)
            padded[position] = bit
            _update_counts(counts, offset, bit)
    return f"!{_pack(encoder.finish())}"


def _decode_raster(payload: str, width: int, height: int) -> bytes:
    decoder = _RangeDecoder(_unpack(payload[1:]))
    counts = array("H", [1]) * (_CONTEXT_COUNT * 2)
    padded_width = width + 2 * _PADDING
    padded = bytearray(padded_width * (height + _PADDING))
    output = bytearray(width * height)
    for y in range(height):
        row = (y + _PADDING) * padded_width + _PADDING
        for x in range(width):
            position = row + x
            context = _context_at(
                padded,
                position,
                position - padded_width,
                position - 2 * padded_width,
            )
            offset = context << 1
            zero = counts[offset]
            one = counts[offset + 1]
            value = decoder.frequency(zero + one)
            bit = 0 if value < zero else 1
            if bit == 0:
                decoder.decode(0, zero)
            else:
                decoder.decode(zero, one)
            padded[position] = bit
            output[y * width + x] = bit
            _update_counts(counts, offset, bit)
    return bytes(output)


def decode_mask_to_raster(mask: SegmentationMask) -> bytes:
    """Strictly decode a complete mask with structural checks and no quota ceiling."""

    if mask.encoding not in ("one_bit", "lossless"):
        raise _invalid(f"Unsupported complete mask encoding: {mask.encoding}.")
    width = _as_safe_integer(mask.width)
    height = _as_safe_integer(mask.height)
    if width is None or height is None or width <= 0 or height <= 0:
        raise _invalid("Mask dimensions must be positive safe integers.")
    area = width * height
    if area > _MAXIMUM_SAFE_INTEGER:
        raise _invalid("Mask dimensions produce an unsafe decoded area.")
    if not isinstance(mask.payload, str):
        raise _invalid("Mask payload must be a string.")
    if mask.encoding == "one_bit" and not mask.payload.startswith("!"):
        raise _invalid("one_bit mask payloads must start with !.")
    if mask.encoding == "lossless" and not mask.payload.startswith("~"):
        raise _invalid("lossless mask payloads must start with ~.")

    try:
        if mask.encoding == "lossless":
            coverage = _decode_lossless_raster(mask.payload, width, height)
            return bytes(value >= 129 for value in coverage)
        raster = _decode_raster(mask.payload, width, height)
        if _encode_raster(raster, width, height) != mask.payload:
            raise _invalid("Mask payload is not canonical or has invalid finalization.")
        return raster
    except InvalidSegmentationMaskError:
        raise
    except MemoryError:
        raise
    except Exception as error:
        raise _invalid("Mask payload could not be decoded.", error) from error
