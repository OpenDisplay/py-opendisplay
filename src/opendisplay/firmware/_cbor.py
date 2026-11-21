"""The CBOR subset SMP uses (RFC 8949): unsigned and negative ints, bytes, text, arrays, maps, bools.

Encoding writes the shortest heads and definite lengths, as the ESP32 server's own writer does
(``od_smp.cpp`` ``put_head``). Decoding also takes the indefinite-length arrays and maps Zephyr may
send, and ``null``. Tags, floats and ``undefined`` are refused: no SMP reply this client reads
carries them.
"""

from __future__ import annotations

from typing import Any

_FALSE, _TRUE, _NULL = 0xF4, 0xF5, 0xF6
_BREAK = 0xFF


def _head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([major << 5 | value])
    for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if value < 1 << (8 * size):
            return bytes([major << 5 | info]) + value.to_bytes(size, "big")
    raise ValueError(f"{value} does not fit in 64 bits")


def encode(value: Any) -> bytes:
    """Encode ``value``: bool, int, bytes, str, list or dict with str keys."""
    if value is True or value is False:
        return bytes([_TRUE if value else _FALSE])
    if isinstance(value, int):
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, (bytes, bytearray)):
        return _head(2, len(value)) + bytes(value)
    if isinstance(value, str):
        raw = value.encode()
        return _head(3, len(raw)) + raw
    if isinstance(value, list):
        return _head(4, len(value)) + b"".join(encode(v) for v in value)
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(encode(k) + encode(v) for k, v in value.items())
    raise TypeError(f"Cannot encode {type(value).__name__} as CBOR")


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        """The next ``n`` bytes."""
        if self.pos + n > len(self.data):
            raise ValueError("CBOR data ends early")
        chunk = self.data[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def peek_break(self) -> bool:
        """Consume an indefinite container's "break" byte if it is next."""
        if self.pos >= len(self.data):
            raise ValueError("CBOR data ends early")
        if self.data[self.pos] == _BREAK:
            self.pos += 1
            return True
        return False

    def item(self) -> Any:  # pylint: disable=too-many-return-statements
        """The next complete item."""
        initial = self.take(1)[0]
        major, info = initial >> 5, initial & 0x1F
        if major == 7:
            if initial in (_FALSE, _TRUE):
                return initial == _TRUE
            if initial == _NULL:
                return None
            raise ValueError(f"Unsupported CBOR simple value or float 0x{initial:02x}")
        if major == 6:
            raise ValueError("CBOR tags are not supported")
        if info == 31 and major in (4, 5):
            return self._indefinite(major)
        if info < 24:
            value = info
        elif info <= 27:
            value = int.from_bytes(self.take(1 << (info - 24)), "big")
        else:
            raise ValueError(f"Unsupported CBOR length 0x{info:02x}")
        if major == 0:
            return value
        if major == 1:
            return -1 - value
        if major == 2:
            return self.take(value)
        if major == 3:
            return self.take(value).decode()
        if major == 4:
            return [self.item() for _ in range(value)]
        return {self.item(): self.item() for _ in range(value)}

    def _indefinite(self, major: int) -> Any:
        if major == 4:
            items = []
            while not self.peek_break():
                items.append(self.item())
            return items
        mapping = {}
        while not self.peek_break():
            key = self.item()
            mapping[key] = self.item()
        return mapping


def decode(data: bytes) -> Any:
    """Decode one CBOR item that spans all of ``data``.

    Raises:
        ValueError: Malformed, unsupported, or followed by trailing bytes.
    """
    reader = _Reader(data)
    value = reader.item()
    if reader.pos != len(data):
        raise ValueError(f"{len(data) - reader.pos} trailing bytes after the CBOR item")
    return value
