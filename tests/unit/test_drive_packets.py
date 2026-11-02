"""Drive-data packets (0x2E/0x2F/0x30/0x31) survive a read-modify-write.

These were previously skipped by size and never modelled, so `interrogate()`
followed by `write_config()` wrote the config back WITHOUT them -- silently
deleting every drive mode and waveform from a device whose panel is driven
entirely from config. 0x31 was not even in the size table, so parsing stopped
there and everything after it landed in `unparsed_tail`.

The byte layouts here mirror panel_drive.py, which is what actually generates
the packets uploaded to a device.
"""

from __future__ import annotations

import struct

from opendisplay.models.config import (
    DisplayCalibrationInfo,
    DisplayLutInfo,
    DisplayModeInfo,
    DisplayPanelInfo,
)
from opendisplay.protocol.config_parser import parse_tlv_config
from opendisplay.protocol.config_serializer import (
    serialize_display_calibration,
    serialize_display_lut,
    serialize_display_mode,
    serialize_display_panel,
)


def _mode(mode_index: int = 1, name: str = "colour16", passes: int = 3) -> DisplayModeInfo:
    return DisplayModeInfo(
        instance_number=0,
        mode_index=mode_index,
        name=name,
        passes=passes,
        lut_slot=[0, 1, 1, 0xFF],
        vsn1=[0, 0x14, 0x41, 0],
        temp_min_c=18,
        temp_max_c=32,
        temp_policy=0,
        init_seq=bytes(range(1, 72)),
        reserved_flags=1,
    )


def _panel() -> DisplayPanelInfo:
    return DisplayPanelInfo(
        instance_number=0,
        busy_usable=True,
        lut_supported=True,
        busy_idle_level=1,
        busy_timeout_ms=30000,
        busy_fixed_delay_ms=0,
        code_map=[0, 1, 3, 2],
        mode_count=2,
        sleep_seq=bytes([2, 2, 0, 2, 7, 0xA5, 0, 0]),
    )


def _lut(slot: int = 0) -> DisplayLutInfo:
    data = bytes((i * 7) & 0xFF for i in range(535))
    return DisplayLutInfo(
        instance_number=0, lut_slot=slot, lut_len=len(data), crc16=0x77AF, data=data
    )


def _calib() -> DisplayCalibrationInfo:
    return DisplayCalibrationInfo(
        instance_number=0,
        mode_index=0,
        entry_count=4,
        measured=False,
        lab=[(26.0, 0.0, 0.0), (90.0, 0.0, 0.0), (52.0, 62.0, 48.0), (80.0, 2.0, 78.0)],
    )


# parse_tlv_config rejects a config without the four mandatory packets, so every
# fixture carries zeroed ones. Their contents are irrelevant here -- the point is
# what happens to the DRIVE packets that follow them.
_REQUIRED: tuple[tuple[int, int, bytes], ...] = (
    (0, 0x01, bytes(22)),   # system
    (0, 0x02, bytes(22)),   # manufacturer
    (0, 0x04, bytes(30)),   # power
    (0, 0x20, bytes(46)),   # display
)


def _blob(*packets: tuple[int, int, bytes]) -> bytes:
    """The TLV body parse_tlv_config takes (wrapper already stripped)."""
    out = b""
    for num, ptype, body in _REQUIRED + packets:
        out += bytes([num, ptype]) + body
    return out


def test_wire_sizes_match_the_firmware_constants() -> None:
    assert len(serialize_display_mode(_mode())) == 157
    assert len(serialize_display_lut(_lut())) == 541
    assert len(serialize_display_calibration(_calib())) == 100
    assert len(serialize_display_panel(_panel())) == 36


def test_drive_packets_are_parsed_not_skipped() -> None:
    data = _blob(
        (0, 0x31, serialize_display_panel(_panel())),
        (0, 0x2E, serialize_display_mode(_mode(0, "full", 1))),
        (1, 0x2E, serialize_display_mode(_mode(1, "colour16", 3))),
        (0, 0x2F, serialize_display_lut(_lut(0))),
        (1, 0x2F, serialize_display_lut(_lut(1))),
        (0, 0x30, serialize_display_calibration(_calib())),
    )
    cfg = parse_tlv_config(data)

    assert len(cfg.display_panels) == 1
    assert len(cfg.display_modes) == 2
    assert len(cfg.display_luts) == 2
    assert len(cfg.display_calibrations) == 1
    # 0x31 used to be absent from the size table, which stopped the walk dead.
    assert cfg.unparsed_tail == b""

    names = {m.mode_index: m.name for m in cfg.display_modes}
    assert names == {0: "full", 1: "colour16"}
    colour16 = next(m for m in cfg.display_modes if m.name == "colour16")
    assert colour16.passes == 3
    assert colour16.lut_slot[:3] == [0, 1, 1]
    assert colour16.vsn1[:3] == [0, 0x14, 0x41]
    assert colour16.colour_count_hint == 16
    assert next(m for m in cfg.display_modes if m.name == "full").colour_count_hint == 4

    panel = cfg.display_panels[0]
    assert panel.code_map == [0, 1, 3, 2]  # BWRY: yellow=2 maps to panel code 3
    assert panel.busy_usable and panel.busy_idle_level == 1


def test_each_packet_round_trips_byte_exact() -> None:
    """A read-modify-write must not alter one byte, including bytes this
    library does not interpret (0x2E byte 23 is reserved but older writers set
    it, and rewriting it as zero would silently edit the device's config)."""
    for ptype, body in (
        (0x31, serialize_display_panel(_panel())),
        (0x2E, serialize_display_mode(_mode())),
        (0x2F, serialize_display_lut(_lut(1))),
        (0x30, serialize_display_calibration(_calib())),
    ):
        cfg = parse_tlv_config(_blob((0, ptype, body)))
        again = {
            0x31: lambda: serialize_display_panel(cfg.display_panels[0]),
            0x2E: lambda: serialize_display_mode(cfg.display_modes[0]),
            0x2F: lambda: serialize_display_lut(cfg.display_luts[0]),
            0x30: lambda: serialize_display_calibration(cfg.display_calibrations[0]),
        }[ptype]()
        assert again == body, f"0x{ptype:02X} did not round-trip"


def test_reserved_mode_byte_is_preserved() -> None:
    body = bytearray(serialize_display_mode(_mode()))
    assert body[23] == 1
    cfg = parse_tlv_config(_blob((0, 0x2E, bytes(body))))
    assert cfg.display_modes[0].reserved_flags == 1
    assert serialize_display_mode(cfg.display_modes[0])[23] == 1


def test_waveform_slots_are_global_not_per_display() -> None:
    """Two displays referencing one slot is the saving that makes several
    displays fit; the device resolves a waveform by slot alone."""
    data = _blob(
        (0, 0x31, serialize_display_panel(_panel())),
        (0, 0x2E, serialize_display_mode(_mode(0, "colour16", 3))),
        (1, 0x2F, serialize_display_lut(_lut(1))),
    )
    cfg = parse_tlv_config(data)
    assert [lo.lut_slot for lo in cfg.display_luts] == [1]
    assert cfg.display_modes[0].lut_slot[:3] == [0, 1, 1]


def test_calibration_lab_scaling() -> None:
    cfg = parse_tlv_config(_blob((0, 0x30, serialize_display_calibration(_calib()))))
    lab = cfg.display_calibrations[0].lab
    assert len(lab) == 4
    # Stored as int16 hundredths; 52.0/62.0/48.0 must come back unrounded.
    assert lab[2] == (52.0, 62.0, 48.0)
    assert cfg.display_calibrations[0].measured is False


def test_truncated_mode_sequence_is_bounded() -> None:
    """seq_len is attacker-controlled on the pre-auth config path; it must not
    read past the 128-byte field."""
    body = bytearray(serialize_display_mode(_mode()))
    struct.pack_into("<H", body, 27, 0xFFFF)
    cfg = parse_tlv_config(_blob((0, 0x2E, bytes(body))))
    assert len(cfg.display_modes[0].init_seq) == 128
