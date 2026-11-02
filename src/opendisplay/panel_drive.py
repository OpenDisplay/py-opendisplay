"""Build and upload the config packets that describe how a panel is driven.

This is the host half of the config-driven driver. Panel behaviour used to live
in three places that had to agree and had nothing forcing them to: the vendored
bb_epaper library in each firmware repo, a per-target `opendisplay_map_epd()`
switch, and `display_palettes.py` here -- whose own comments say it mirrors
"bb_epaper's pColorLookup" and "firmware mapEpd" BY HAND. A library bump has
already silently changed init traffic on four shipped panels.

With drive data in the config there is one copy, and a host can read it back off
the device instead of mirroring it.

Three packets, all appended at the end of the id space (minor 1.6):
    0x2E display_mode         per display per mode: init sequence, LUT slots,
                              pass count, advisory temperature window
    0x2F display_lut          a 535-byte waveform
    0x30 display_calibration  measured CIELAB per palette slot, per mode

ORDERING MATTERS. Shipped firmware abandons the remainder of the config blob
when it meets an unknown packet id (skip-to-CRC). These must therefore be
emitted LAST, so an older device still ingests everything it does understand.
`build_blob()` enforces that rather than trusting the caller.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PACKET_TYPE_DISPLAY_MODE = 0x2E
PACKET_TYPE_DISPLAY_LUT = 0x2F
PACKET_TYPE_DISPLAY_CALIBRATION = 0x30

SEQ_FIELD_BYTES = 128
LUT_FIELD_BYTES = 535
CALIB_FIELD_BYTES = 96

# Sequence escapes -- must match src/od_epd.c exactly.
_ESC = 0x00
_ESC_BUSY = 0x01
_ESC_WAVE = 0x02
_END = 0x00
_WAVE_TOKENS: dict[str, int] = {f"@w{i}": i for i in range(7)}
_WAVE_TOKENS["@vcom"] = 0xFF

_POLICY = {"advise": 0, "discourage": 1, "refuse": 2}


def encode_sequence(steps: list[str]) -> bytes:
    """panels.yaml step list -> the length-prefixed blob the driver walks.

    Encoding (identical in od_epd.c):
        [len][cmd][data...]            len counts cmd+data, always >= 1
        0x00 0x01                      wait for BUSY
        0x00 0x02 n (is_wave,val)*n    step with wave-header substitution
        0x00 0x00                      end
    A zero length is otherwise impossible, which is why it is free as an escape.
    """
    out = bytearray()
    for raw in steps:
        s = raw.strip()
        if s == "@busy":
            out += bytes([_ESC, _ESC_BUSY])
            continue
        toks = s.split()
        parsed = [
            (1, _WAVE_TOKENS[t]) if t in _WAVE_TOKENS else (0, int(t, 16))
            for t in toks
        ]
        if any(is_wave for is_wave, _ in parsed):
            out += bytes([_ESC, _ESC_WAVE, len(parsed)])
            for is_wave, v in parsed:
                out += bytes([0xFF if is_wave else 0x00, v])
        else:
            out.append(len(parsed))
            out += bytes(v for _, v in parsed)
    out += bytes([_ESC, _END])
    return bytes(out)


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


@dataclass
class DisplayMode:
    instance_number: int
    mode_index: int
    name: str
    passes: int
    lut_slot: list[int] = field(default_factory=lambda: [0xFF] * 4)
    vsn1: list[int] = field(default_factory=lambda: [0] * 4)
    analog_from_wave: bool = False
    init_seq: bytes = b""
    temp_min_c: int = -128
    temp_max_c: int = 127
    temp_policy: int = 0

    def serialize(self) -> bytes:
        if len(self.init_seq) > SEQ_FIELD_BYTES:
            raise ValueError(
                f"init sequence {len(self.init_seq)} B exceeds the "
                f"{SEQ_FIELD_BYTES} B field (real sequences measure 62-94 B)"
            )
        return (
            struct.pack("<BB", self.instance_number, self.mode_index)
            + self.name.encode()[:12].ljust(12, b"\x00")
            + bytes([self.passes])
            + bytes((self.lut_slot + [0xFF] * 4)[:4])
            + bytes((self.vsn1 + [0] * 4)[:4])
            + bytes([1 if self.analog_from_wave else 0])
            + struct.pack("<bb", self.temp_min_c, self.temp_max_c)
            + bytes([self.temp_policy])
            + struct.pack("<H", len(self.init_seq))
            + self.init_seq.ljust(SEQ_FIELD_BYTES, b"\x00")
        )


@dataclass
class DisplayLut:
    instance_number: int
    lut_slot: int
    data: bytes

    def serialize(self) -> bytes:
        if len(self.data) > LUT_FIELD_BYTES:
            raise ValueError(f"LUT {len(self.data)} B exceeds {LUT_FIELD_BYTES} B")
        return (
            struct.pack("<BBHH", self.instance_number, self.lut_slot,
                        len(self.data), crc16_ccitt(self.data))
            + self.data.ljust(LUT_FIELD_BYTES, b"\x00")
        )


@dataclass
class DisplayCalibration:
    instance_number: int
    mode_index: int
    lab: list[tuple[float, float, float]]
    measured: bool = True

    def serialize(self) -> bytes:
        body = bytearray()
        for L, a, b in self.lab[:16]:
            body += struct.pack("<hhh", round(L * 100), round(a * 100), round(b * 100))
        return (
            struct.pack("<BBBB", self.instance_number, self.mode_index,
                        min(len(self.lab), 16), 1 if self.measured else 0)
            + bytes(body).ljust(CALIB_FIELD_BYTES, b"\x00")
        )


def _packets_for_panel(panel: dict[str, Any], waves: dict[str, bytes],
                       instance: int = 0) -> tuple[list[DisplayMode], list[DisplayLut]]:
    """Turn one panels.yaml entry into mode + LUT packets.

    Identical waveforms share a slot: the 16-colour mode uses colour+edit+edit,
    so three passes need only TWO slots. Deduplicating here rather than storing
    three copies is what makes the config fit -- a waveform is not compressible
    (the colour wave is 59% idle but the idle frames are interleaved, so RLE
    inflates it to 245%), so sharing is the only real saving available.
    """
    seqs = {k: encode_sequence(v) for k, v in (panel.get("init_sequences") or {}).items()}
    slot_of: dict[str, int] = {}
    luts: list[DisplayLut] = []
    modes: list[DisplayMode] = []

    for mi, m in enumerate(panel.get("modes") or []):
        names = m.get("lut_per_pass") or [None] * m.get("passes", 1)
        vsn1 = (m.get("vsn1_per_pass") or []) + [None] * 4
        slots: list[int] = []
        for wname in names[:4]:
            if not wname:
                slots.append(0xFF)
                continue
            if wname not in slot_of:
                slot_of[wname] = len(slot_of)
                luts.append(DisplayLut(instance, slot_of[wname], waves[wname]))
            slots.append(slot_of[wname])
        tg = panel.get("temp_gate") or {}
        modes.append(DisplayMode(
            instance_number=instance,
            mode_index=mi,
            name=m["name"],
            passes=m.get("passes", 1),
            lut_slot=slots,
            vsn1=[(v or 0) for v in vsn1[:4]],
            analog_from_wave=bool(m.get("analog_from_wave")),
            init_seq=seqs.get(m.get("init"), b""),
            temp_min_c=int(tg.get("min_c", -128)),
            temp_max_c=int(tg.get("max_c", 127)),
            temp_policy=_POLICY.get(str(tg.get("policy", "advise")), 0),
        ))
    return modes, luts


def packets_from_yaml(panels_yaml: str | Path, panel_id: int,
                      waves: dict[str, bytes], instance: int = 0) -> list[bytes]:
    """Serialized drive packets for one panel, ready to append to a config blob."""
    import yaml  # noqa: PLC0415  (optional dependency, only needed for authoring)

    doc = yaml.safe_load(Path(panels_yaml).read_text())
    panel = next(p for p in doc["panels"] if int(str(p["id"]), 0) == panel_id)
    modes, luts = _packets_for_panel(panel, waves, instance)
    out: list[bytes] = []
    for m in modes:
        out.append(bytes([m.mode_index, PACKET_TYPE_DISPLAY_MODE]) + m.serialize())
    for lo in luts:
        out.append(bytes([lo.lut_slot, PACKET_TYPE_DISPLAY_LUT]) + lo.serialize())
    return out


def append_drive_packets(base_blob: bytes, drive_packets: list[bytes],
                         max_size: int = 2048) -> bytes:
    """Append drive packets to an existing config blob, LAST and size-checked.

    Appending last is not a style choice: shipped firmware abandons the rest of
    the blob at the first unknown packet id, so anything new has to sit behind
    everything old or it silently truncates a device's configuration.

    Refuses rather than truncates when it will not fit -- the device cannot be
    asked how large a config it accepts (there is no capability field for it),
    so a host that guesses wrong must fail loudly.
    """
    total = len(base_blob) + sum(len(p) for p in drive_packets)
    if total > max_size:
        raise ValueError(
            f"config would be {total} B, over the {max_size} B limit. "
            f"Drop a mode or share more LUT slots; do not truncate."
        )
    return base_blob + b"".join(drive_packets)
