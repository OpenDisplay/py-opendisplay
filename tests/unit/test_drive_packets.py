"""Drive-data packets (0xAE/0xAF/0xB0/0xB1, length-prefixed) survive a
read-modify-write.

The Rev 1 fixed packets (0x2E-0x31) were previously skipped by size and never
modelled, so `interrogate()` followed by `write_config()` wrote the config back
WITHOUT them -- silently deleting every drive mode and waveform from a device
whose panel is driven entirely from config. The packets now live in the
length-prefixed 0x80+ range ([number][id][length:2 LE][body]), so an unknown
one is skippable instead of fatal, and the burned 0x2E-0x31 ids are stepped
over and deliberately dropped.

The byte layouts here mirror panel_drive.py, which is what actually generates
the packets uploaded to a device.
"""

from __future__ import annotations

import struct

import pytest

from opendisplay.exceptions import ConfigParseError
from opendisplay.models.config import (
    DisplayCalibrationInfo,
    DisplayLutInfo,
    DisplayModeInfo,
    DisplayPanelInfo,
)
from opendisplay.protocol.config_parser import parse_tlv_config
from opendisplay.protocol.config_serializer import (
    serialize_config,
    serialize_display_calibration,
    serialize_display_lut,
    serialize_display_mode,
    serialize_display_panel,
)

_SEQ = bytes(range(1, 72))


def _mode(mode_index: int = 1, name: str = "colour16", passes: int = 3) -> DisplayModeInfo:
    return DisplayModeInfo(
        instance_number=0,
        mode_index=mode_index,
        name=name,
        passes=passes,
        lut_slot=[0, 1, 1] + [0xFF] * 5,
        vsn1=[0, 0x14, 0x41, 0, 0, 0, 0, 0],
        temp_min_c=18,
        temp_max_c=32,
        temp_policy=0,
        init_seq=_SEQ,
        color_scheme=10,  # BWRY16 lives on the MODE, never in 0x20
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


def _lut(slot: int = 0, n: int = 535) -> DisplayLutInfo:
    data = bytes((i * 7) & 0xFF for i in range(n))
    return DisplayLutInfo(instance_number=0, lut_slot=slot, lut_len=len(data), crc16=0x77AF, data=data)


def _calib() -> DisplayCalibrationInfo:
    return DisplayCalibrationInfo(
        instance_number=0,
        mode_index=0,
        entry_count=4,
        measured=False,
        lab=[(26.0, 0.0, 0.0), (90.0, 0.0, 0.0), (52.0, 62.0, 48.0), (80.0, 2.0, 78.0)],
        codes=[(0, 0, 0), (1, 1, 1), (2, 0, 2), (3, 1, 1)],
    )


# parse_tlv_config rejects a config without the four mandatory packets, so every
# fixture carries zeroed ones. Their contents are irrelevant here -- the point is
# what happens to the DRIVE packets that follow them.
_REQUIRED: tuple[tuple[int, int, bytes], ...] = (
    (0, 0x01, bytes(22)),  # system
    (0, 0x02, bytes(22)),  # manufacturer
    (0, 0x04, bytes(30)),  # power
    (0, 0x20, bytes(46)),  # display
)


def _blob(*packets: tuple[int, int, bytes]) -> bytes:
    """The TLV body parse_tlv_config takes (wrapper already stripped).

    Legacy ids are emitted bare; 0x80+ ids get the LENGTH-PREFIXED header the
    wire carries.
    """
    out = b""
    for num, ptype, body in _REQUIRED + packets:
        out += bytes([num, ptype])
        if ptype >= 0x80:
            out += struct.pack("<H", len(body))
        out += body
    return out


def test_wire_sizes_are_real_not_padded() -> None:
    """Bodies carry their real size; the wire header carries the length. An
    SSD16xx-class waveform must cost its ~85 bytes, not Rev 1's fixed 541."""
    assert len(serialize_display_mode(_mode())) == 37 + len(_SEQ)
    assert len(serialize_display_lut(_lut())) == 4 + 535
    assert len(serialize_display_lut(_lut(n=80))) == 4 + 80
    assert len(serialize_display_calibration(_calib())) == 4 + 4 * 8
    assert len(serialize_display_panel(_panel())) == 13 + 8


def test_drive_packets_are_parsed_not_skipped() -> None:
    data = _blob(
        (0, 0xB1, serialize_display_panel(_panel())),
        (0, 0xAE, serialize_display_mode(_mode(0, "full", 1))),
        (1, 0xAE, serialize_display_mode(_mode(1, "colour16", 3))),
        (0, 0xAF, serialize_display_lut(_lut(0))),
        (1, 0xAF, serialize_display_lut(_lut(1))),
        (0, 0xB0, serialize_display_calibration(_calib())),
    )
    cfg = parse_tlv_config(data)

    assert len(cfg.display_panels) == 1
    assert len(cfg.display_modes) == 2
    assert len(cfg.display_luts) == 2
    assert len(cfg.display_calibrations) == 1
    assert cfg.unparsed_tail == b""
    assert cfg.unknown_ex_packets == []

    names = {m.mode_index: m.name for m in cfg.display_modes}
    assert names == {0: "full", 1: "colour16"}
    colour16 = next(m for m in cfg.display_modes if m.name == "colour16")
    assert colour16.passes == 3
    assert colour16.lut_slot[:3] == [0, 1, 1]
    assert colour16.vsn1[:3] == [0, 0x14, 0x41]
    assert colour16.color_scheme == 10
    assert colour16.colour_count_hint == 16
    assert next(m for m in cfg.display_modes if m.name == "full").colour_count_hint == 4

    panel = cfg.display_panels[0]
    assert panel.code_map == [0, 1, 3, 2]  # BWRY: yellow=2 maps to panel code 3
    assert panel.busy_usable and panel.busy_idle_level == 1


def test_each_packet_round_trips_byte_exact() -> None:
    """A read-modify-write must not alter one byte of a drive packet."""
    for ptype, body in (
        (0xB1, serialize_display_panel(_panel())),
        (0xAE, serialize_display_mode(_mode())),
        (0xAF, serialize_display_lut(_lut(1))),
        (0xB0, serialize_display_calibration(_calib())),
    ):
        cfg = parse_tlv_config(_blob((0, ptype, body)))
        again = {
            0xB1: lambda: serialize_display_panel(cfg.display_panels[0]),
            0xAE: lambda: serialize_display_mode(cfg.display_modes[0]),
            0xAF: lambda: serialize_display_lut(cfg.display_luts[0]),
            0xB0: lambda: serialize_display_calibration(cfg.display_calibrations[0]),
        }[ptype]()
        assert again == body, f"0x{ptype:02X} did not round-trip"


def test_calibration_codes_pack_lsb_first() -> None:
    """codes:u16 LE packs pass p into bits [2p+1:2p] -- structs 2.2, NOT the
    Rev 1 one-byte MSB-first packing that silently swapped pass order."""
    body = serialize_display_calibration(_calib())
    # Entry 3 is codes (3, 1, 1): pass0=3 -> bits1:0, pass1=1 -> bits3:2,
    # pass2=1 -> bits5:4  =>  0b01_01_11 = 0x17.
    cw = struct.unpack_from("<H", body, 4 + 3 * 8)[0]
    assert cw == 0x17
    cfg = parse_tlv_config(_blob((0, 0xB0, body)))
    assert cfg.display_calibrations[0].codes[3][:3] == (3, 1, 1)


def test_waveform_slots_are_global_not_per_display() -> None:
    """Two displays referencing one slot is the saving that makes several
    displays fit; the device resolves a waveform by slot alone."""
    data = _blob(
        (0, 0xB1, serialize_display_panel(_panel())),
        (0, 0xAE, serialize_display_mode(_mode(0, "colour16", 3))),
        (1, 0xAF, serialize_display_lut(_lut(1))),
    )
    cfg = parse_tlv_config(data)
    assert [lo.lut_slot for lo in cfg.display_luts] == [1]
    assert cfg.display_modes[0].lut_slot[:3] == [0, 1, 1]


def test_calibration_lab_scaling() -> None:
    cfg = parse_tlv_config(_blob((0, 0xB0, serialize_display_calibration(_calib()))))
    lab = cfg.display_calibrations[0].lab
    assert len(lab) == 4
    # Stored as int16 hundredths; 52.0/62.0/48.0 must come back unrounded.
    assert lab[2] == (52.0, 62.0, 48.0)
    assert cfg.display_calibrations[0].measured is False


def test_oversized_seq_len_is_rejected() -> None:
    """seq_len is attacker-controlled on the pre-auth config path; a value past
    the packet body must raise, not read into the next packet."""
    body = bytearray(serialize_display_mode(_mode()))
    struct.pack_into("<H", body, 35, 0xFFFF)
    with pytest.raises(ConfigParseError):
        parse_tlv_config(_blob((0, 0xAE, bytes(body))))


def test_burned_rev1_packets_are_dropped() -> None:
    """A stale config carrying the fixed-size 0x2E-0x31 drafts parses PAST them
    (nothing behind them is lost) and drops them on re-serialize -- current
    firmware ignores them, so re-emitting would only waste config space."""
    data = _blob(
        (0, 0x2E, bytes(157)),
        (0, 0x2F, bytes(541)),
        (0, 0x30, bytes(116)),
        (0, 0x31, bytes(36)),
        (0, 0xB1, serialize_display_panel(_panel())),
    )
    cfg = parse_tlv_config(data)
    assert cfg.display_modes == []  # burned draft NOT modelled
    assert len(cfg.display_panels) == 1  # the real packet behind survives
    out = serialize_config(cfg)
    # No burned ids in the rewritten blob (checked on the header positions by
    # re-parsing: a stray 0x2E byte inside a body is fine).
    assert parse_tlv_config(out[3:-2]).display_panels[0].code_map == [0, 1, 3, 2]
    assert len(out) < len(data) + 3 + 2  # the 850 burned bytes are gone


def test_unknown_length_prefixed_packet_is_skipped_and_preserved() -> None:
    """An unknown 0x80+ id must not stop the walk (that is the point of the
    length prefix) and must ride along verbatim on re-serialize."""
    mystery = bytes(range(40))
    data = _blob(
        (0, 0xC7, mystery),
        (0, 0xB1, serialize_display_panel(_panel())),
    )
    cfg = parse_tlv_config(data)
    assert cfg.unknown_ex_packets == [(0, 0xC7, mystery)]
    assert len(cfg.display_panels) == 1  # parsing continued past 0xC7
    out = serialize_config(cfg)
    cfg2 = parse_tlv_config(out[3:-2])
    assert cfg2.unknown_ex_packets == [(0, 0xC7, mystery)]


def test_findmy_packet_rides_along_verbatim() -> None:
    """0x2D de-provisions the device if dropped; it is carried opaquely."""
    blob128 = bytes(i & 0xFF for i in range(128))
    data = _blob(
        (0, 0x2D, blob128),
        (0, 0xB1, serialize_display_panel(_panel())),
    )
    cfg = parse_tlv_config(data)
    assert cfg.findmy_raw == blob128
    assert len(cfg.display_panels) == 1  # nothing behind 0x2D is lost
    out = serialize_config(cfg)
    assert parse_tlv_config(out[3:-2]).findmy_raw == blob128
