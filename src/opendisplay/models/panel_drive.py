"""Panel drive data: the length-prefixed config packets 0xAE-0xB1.

Firmware that drives its panel from config (rather than a compiled-in panel
table) stores the recipe in four packet types from the length-prefixed range
(``0x80``-``0xFF``, see :class:`LengthPrefixedPacket`):

- ``0xAE`` display_mode — one drive mode of a display: its name, what it
  renders, how many passes it takes, its temperature window, and the drive
  sequence the firmware executes.
- ``0xAF`` display_lut — one waveform slot.
- ``0xB0`` display_calibration — the measured palette of a mode. Host-only: the
  device skips it; it exists so a host can quantise against measured colour.
- ``0xB1`` display_panel — BUSY-line behaviour, the 4-colour code map, and the
  sleep sequence.

Each packet body is a fixed head followed by one variable tail. The heads are
modelled here; drive sequences and waveform data are kept as opaque bytes,
because the firmware executes them and the host has no use for their contents.

These are read-only views. The bytes that are written back to a device are the
packets themselves (``GlobalConfig.length_prefixed_packets``), untouched.

Layout source: ``include/opendisplay_structs.h`` in OpenDisplay/Firmware_Silabs
(branch ``feat/od-epd-driver``, structs 2.2).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import ClassVar, Self

from .enums import ColorScheme

PACKET_TYPE_DISPLAY_MODE = 0xAE
PACKET_TYPE_DISPLAY_LUT = 0xAF
PACKET_TYPE_DISPLAY_CALIBRATION = 0xB0
PACKET_TYPE_DISPLAY_PANEL = 0xB1

#: First packet type of the length-prefixed range.
LENGTH_PREFIXED_MIN_TYPE = 0x80

#: ``display_mode.color_scheme`` value meaning "use the display's own scheme".
COLOR_SCHEME_INHERIT = 0xFF

#: ``display_mode.lut_slot`` value meaning "no custom waveform for this pass".
LUT_SLOT_NONE = 0xFF

MAX_PASSES = 8


@dataclass(frozen=True)
class LengthPrefixedPacket:
    """One config packet from the length-prefixed range, kept verbatim.

    Packet types ``0x80``-``0xFF`` carry an explicit body length, so a reader
    can step over a type it does not know. On the wire a packet is
    ``[number:1][packet_type:1][length:2 LE][body]``.
    """

    number: int  # packet index within the config, as the device stored it
    packet_type: int
    body: bytes

    HEADER_SIZE: ClassVar[int] = 4

    def to_bytes(self) -> bytes:
        """Serialize the packet, header included."""
        return bytes([self.number, self.packet_type]) + len(self.body).to_bytes(2, "little") + self.body


def _crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (init 0xFFFF, poly 0x1021), the config CRC."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _require(body: bytes, size: int, name: str) -> None:
    if len(body) < size:
        raise ValueError(f"{name} body too short: {len(body)} bytes (need at least {size})")


@dataclass(frozen=True)
class DisplayMode:
    """One drive mode of a display (packet 0xAE)."""

    instance_number: int  # display this mode belongs to
    mode_index: int  # id a host names when starting a transfer in this mode
    name: str  # e.g. "full", "fast", "16colour"
    color_scheme: int  # what the mode renders; COLOR_SCHEME_INHERIT = the display's scheme
    passes: int  # frames one transfer streams (1..8)
    temp_min_c: int
    temp_max_c: int
    temp_policy: int  # what the device does outside the window: 0 advisory, 1 substitute, 2 reject
    lut_slots: tuple[int, ...]  # waveform slot per pass; LUT_SLOT_NONE = none
    vsn1: tuple[int, ...]  # per-pass waveform byte-3 patch value; 0 = none
    sequence: bytes  # encoded drive sequence, executed by the firmware

    _HEAD: ClassVar[struct.Struct] = struct.Struct("<BB12sBBbbB8s8sH")

    @property
    def color_scheme_enum(self) -> ColorScheme | int | None:
        """The scheme this mode renders: an enum, a raw int the library does not know, or None to inherit."""
        if self.color_scheme == COLOR_SCHEME_INHERIT:
            return None
        try:
            return ColorScheme.from_value(self.color_scheme)
        except ValueError:
            return self.color_scheme

    @classmethod
    def from_body(cls, body: bytes) -> Self:
        """Parse a display_mode packet body.

        Raises:
            ValueError: If the body is shorter than its head or its declared sequence
        """
        _require(body, cls._HEAD.size, "display_mode")
        instance, index, name, scheme, passes, t_min, t_max, policy, slots, vsn1, seq_len = cls._HEAD.unpack_from(body)
        _require(body, cls._HEAD.size + seq_len, "display_mode")
        used = min(passes, MAX_PASSES)
        return cls(
            instance_number=instance,
            mode_index=index,
            name=name.split(b"\x00", 1)[0].decode("ascii", errors="replace"),
            color_scheme=scheme,
            passes=passes,
            temp_min_c=t_min,
            temp_max_c=t_max,
            temp_policy=policy,
            lut_slots=tuple(slots[:used]),
            vsn1=tuple(vsn1[:used]),
            sequence=body[cls._HEAD.size : cls._HEAD.size + seq_len],
        )


@dataclass(frozen=True)
class DisplayLut:
    """One waveform slot (packet 0xAF)."""

    instance_number: int
    lut_slot: int  # id referenced by DisplayMode.lut_slots
    crc16: int  # CRC-16/CCITT-FALSE over ``data``
    data: bytes  # the waveform, at the size the panel's register holds

    _HEAD: ClassVar[struct.Struct] = struct.Struct("<BBH")

    @property
    def crc_valid(self) -> bool:
        """Whether ``data`` matches its CRC. Firmware treats a failing slot as absent."""
        return _crc16_ccitt_false(self.data) == self.crc16

    @classmethod
    def from_body(cls, body: bytes) -> Self:
        """Parse a display_lut packet body.

        Raises:
            ValueError: If the body is shorter than its head
        """
        _require(body, cls._HEAD.size, "display_lut")
        instance, slot, crc16 = cls._HEAD.unpack_from(body)
        return cls(instance_number=instance, lut_slot=slot, crc16=crc16, data=body[cls._HEAD.size :])


@dataclass(frozen=True)
class CalibrationEntry:
    """One renderable colour of a mode: the panel codes that produce it and its measured CIELAB."""

    codes: int  # packed 2-bit panel codes, pass p in bits [2p+1:2p]
    lab_l: float
    lab_a: float
    lab_b: float

    def code_for_pass(self, pass_index: int) -> int:
        """The 2-bit panel code this colour uses in pass ``pass_index`` (0-based).

        These are panel codes already; never apply ``DisplayPanel.code_map`` to them.
        """
        return (self.codes >> (2 * pass_index)) & 0x03


@dataclass(frozen=True)
class DisplayCalibration:
    """The measured palette of one drive mode (packet 0xB0, host-only)."""

    instance_number: int
    mode_index: int  # the DisplayMode this palette belongs to
    flags: int
    entries: tuple[CalibrationEntry, ...]

    _HEAD: ClassVar[struct.Struct] = struct.Struct("<BBBB")
    _ENTRY: ClassVar[struct.Struct] = struct.Struct("<Hhhh")
    _FLAG_MEASURED: ClassVar[int] = 0x01

    @property
    def measured(self) -> bool:
        """True if the colours were measured on this unit; False if they are nominal placeholders."""
        return bool(self.flags & self._FLAG_MEASURED)

    @classmethod
    def from_body(cls, body: bytes) -> Self:
        """Parse a display_calibration packet body.

        Raises:
            ValueError: If the body is shorter than its head or its declared entries
        """
        _require(body, cls._HEAD.size, "display_calibration")
        instance, mode_index, flags, count = cls._HEAD.unpack_from(body)
        _require(body, cls._HEAD.size + count * cls._ENTRY.size, "display_calibration")
        entries = tuple(
            CalibrationEntry(codes, lab_l / 100, lab_a / 100, lab_b / 100)
            for codes, lab_l, lab_a, lab_b in (
                cls._ENTRY.unpack_from(body, cls._HEAD.size + i * cls._ENTRY.size) for i in range(count)
            )
        )
        return cls(instance_number=instance, mode_index=mode_index, flags=flags, entries=entries)


@dataclass(frozen=True)
class DisplayPanel:
    """Per-panel drive properties (packet 0xB1)."""

    instance_number: int
    flags: int
    busy_idle_level: int  # pin level meaning not-busy
    busy_timeout_ms: int  # poll ceiling when BUSY is usable
    busy_fixed_delay_ms: int  # blind wait when it is not
    code_map: tuple[int, int, int, int]  # logical colour index -> panel 2-bit code (4-colour path)
    mode_count: int  # how many display_mode packets belong to this display
    sleep_sequence: bytes  # encoded sleep sequence, executed by the firmware

    _HEAD: ClassVar[struct.Struct] = struct.Struct("<BBBHH4sBB")
    _FLAG_BUSY_USABLE: ClassVar[int] = 0x01
    _FLAG_LUT_SUPPORTED: ClassVar[int] = 0x02

    @property
    def busy_usable(self) -> bool:
        """Whether the BUSY line can be polled (otherwise the firmware waits ``busy_fixed_delay_ms``)."""
        return bool(self.flags & self._FLAG_BUSY_USABLE)

    @property
    def lut_supported(self) -> bool:
        """Whether the panel honours a custom waveform."""
        return bool(self.flags & self._FLAG_LUT_SUPPORTED)

    @classmethod
    def from_body(cls, body: bytes) -> Self:
        """Parse a display_panel packet body.

        Raises:
            ValueError: If the body is shorter than its head or its declared sleep sequence
        """
        _require(body, cls._HEAD.size, "display_panel")
        instance, flags, idle, timeout, delay, code_map, mode_count, sleep_len = cls._HEAD.unpack_from(body)
        _require(body, cls._HEAD.size + sleep_len, "display_panel")
        return cls(
            instance_number=instance,
            flags=flags,
            busy_idle_level=idle,
            busy_timeout_ms=timeout,
            busy_fixed_delay_ms=delay,
            code_map=(code_map[0], code_map[1], code_map[2], code_map[3]),
            mode_count=mode_count,
            sleep_sequence=body[cls._HEAD.size : cls._HEAD.size + sleep_len],
        )
