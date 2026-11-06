"""0xB2 action table: byte-exact model, and REPLACE (never append) on write.

The device resolves 0xB2 by first match. This library used to carry the packet
as an opaque unknown 0x80+ packet, so interrogate() -> append a new table ->
write_config() kept the old table AND added the new one: the device kept
running the old one while the config grew by one table per write.
"""

from __future__ import annotations

import logging
import struct

import pytest
from test_config_json_roundtrip import _base_config, _display

from opendisplay.models.action_table import (
    ACTION_STEPS_MAX,
    PACKET_TYPE_ACTION_TABLE,
    ActionEffect,
    ActionEntry,
    ActionStep,
    ActionTable,
    ActionTrigger,
    pack_temp_window,
    unpack_temp_window,
)
from opendisplay.protocol.config_parser import _split_packets, parse_config_response
from opendisplay.protocol.config_serializer import calculate_config_crc, serialize_config


def _entry_bytes(trigger: int, arg: int, flags: int, steps: list[tuple[int, int, int, int]]) -> bytes:
    head = struct.pack("<BBHBB", 6 + 4 * len(steps), trigger, arg, flags, len(steps))
    return head + b"".join(bytes(s) for s in steps)


BUTTON_LED = _entry_bytes(ActionTrigger.BUTTON_PRESS, 1 << 2, 0x01, [(ActionEffect.LED, 0x31, 8, 25), (2, 0, 0, 0)])
HOST_SLOT = _entry_bytes(ActionTrigger.HOST, 7, 0x01, [(ActionEffect.DISPLAY_SLOT, 3, 0, 0)])
# Unknown trigger, unknown effect, a reserved flag bit and the enable bit CLEAR:
# all legal, all must survive untouched.
FUTURE = _entry_bytes(0x42, 0xBEEF, 0x80, [(0x09, 0xAA, 0x55, 0xFF)])
TEMP = _entry_bytes(ActionTrigger.TEMP_EXIT, pack_temp_window(-5, 40), 0x01, [(ActionEffect.BUZZER, 1, 0, 0)])


def _ex(number: int, ptype: int, body: bytes) -> bytes:
    return bytes([number, ptype]) + struct.pack("<H", len(body)) + body


def _device_blob(*ex_packets: bytes) -> bytes:
    """A config blob as a device would return it, with raw 0x80+ packets at the end."""
    base = serialize_config(_base_config(displays=[_display()]))
    body = base[:-2] + b"".join(ex_packets)
    return body + calculate_config_crc(body).to_bytes(2, "little")


def _wire_tables(blob: bytes) -> list[bytes]:
    """Every 0xB2 body on the wire, in order -- what the device would see."""
    _, ex, _ = _split_packets(blob[3:-2])
    return [body for _, ptype, body in ex if ptype == PACKET_TYPE_ACTION_TABLE]


# ---------------------------------------------------------------------------
# packet model
# ---------------------------------------------------------------------------


def test_body_round_trips_byte_exact() -> None:
    body = BUTTON_LED + HOST_SLOT + FUTURE + TEMP
    table = ActionTable.from_bytes(body)

    assert table.to_bytes() == body
    assert len(table.entries) == 4
    assert table.trailing == b""
    assert not table.rejected

    btn, host, future, temp = table.entries
    assert btn.trigger == ActionTrigger.BUTTON_PRESS and btn.trigger_arg == 4 and btn.enabled
    assert btn.steps[0] == ActionStep(ActionEffect.LED, 0x31, 8, 25)
    assert host.steps == [ActionStep(ActionEffect.DISPLAY_SLOT, 3, 0, 0)]
    assert not future.enabled and future.flags == 0x80 and not future.known
    assert future.steps[0] == ActionStep(0x09, 0xAA, 0x55, 0xFF)
    assert unpack_temp_window(temp.trigger_arg) == (-5, 40)


def test_built_table_matches_hand_encoded_bytes() -> None:
    table = ActionTable(
        entries=[
            ActionEntry(
                trigger=ActionTrigger.BUTTON_PRESS,
                trigger_arg=1 << 2,
                steps=[ActionStep(ActionEffect.LED, 0x31, 8, 25), ActionStep(ActionEffect.LED_STOP)],
            ),
            ActionEntry(trigger=ActionTrigger.HOST, trigger_arg=7, steps=[ActionStep(ActionEffect.DISPLAY_SLOT, 3)]),
        ]
    )
    assert table.to_bytes() == BUTTON_LED + HOST_SLOT


def test_structural_fault_stops_the_walk_and_is_kept_verbatim() -> None:
    # entry_len says 2 steps, step_count says 1: the device stops here and never
    # reads the HOST entry behind it.
    bad = bytearray(_entry_bytes(ActionTrigger.BOOT, 0, 1, [(1, 1, 1, 1)]))
    bad[0] = 6 + 8
    bad += bytes(4)
    body = BUTTON_LED + bytes(bad) + HOST_SLOT

    table = ActionTable.from_bytes(body)

    assert len(table.entries) == 1
    assert table.rejected
    assert table.trailing == bytes(bad) + HOST_SLOT
    assert table.to_bytes() == body


def test_too_many_steps_is_a_structural_fault() -> None:
    nine = _entry_bytes(ActionTrigger.BOOT, 0, 1, [(0, 0, 0, 0)] * (ACTION_STEPS_MAX + 1))
    table = ActionTable.from_bytes(nine)
    assert table.entries == [] and table.rejected and table.to_bytes() == nine


def test_short_remainder_is_ignored_not_rejected() -> None:
    body = HOST_SLOT + b"\x06\x01\x00"
    table = ActionTable.from_bytes(body)
    assert len(table.entries) == 1
    assert not table.rejected
    assert table.to_bytes() == body


@pytest.mark.parametrize(
    "entry",
    [
        ActionEntry(trigger=1, steps=[ActionStep(0)] * (ACTION_STEPS_MAX + 1)),
        ActionEntry(trigger=1, trigger_arg=0x10000),
        ActionEntry(trigger=256),
        ActionEntry(trigger=1, steps=[ActionStep(0, delay_cs=256)]),
    ],
)
def test_invalid_entries_refuse_to_serialize(entry: ActionEntry) -> None:
    with pytest.raises(ValueError):
        entry.to_bytes()


def test_enabled_setter_preserves_reserved_flag_bits() -> None:
    e = ActionEntry(trigger=ActionTrigger.BOOT, flags=0x81)
    e.enabled = False
    assert e.flags == 0x80
    e.enabled = True
    assert e.flags == 0x81


def test_temp_window_range() -> None:
    assert pack_temp_window(-128, 127) == 0x7F80
    with pytest.raises(ValueError):
        pack_temp_window(0, 128)


# ---------------------------------------------------------------------------
# config integration: one table, and writing one REPLACES it
# ---------------------------------------------------------------------------


def test_config_parse_models_the_table_not_an_unknown_packet() -> None:
    cfg = parse_config_response(_device_blob(_ex(0, 0xB2, BUTTON_LED + HOST_SLOT)))

    assert cfg.action_table is not None
    assert cfg.action_table.to_bytes() == BUTTON_LED + HOST_SLOT
    assert all(ptype != 0xB2 for _, ptype, _ in cfg.unknown_ex_packets)


def test_unchanged_round_trip_keeps_exactly_one_identical_table() -> None:
    cfg = parse_config_response(_device_blob(_ex(3, 0xB2, FUTURE + TEMP)))
    out = serialize_config(cfg)

    assert _wire_tables(out) == [FUTURE + TEMP]
    assert _ex(3, 0xB2, FUTURE + TEMP) in out  # packet number preserved too


def test_interrogate_assign_write_replaces_the_table() -> None:
    """The reported bug: read, set a new table, write. The device must end up
    with ONE table -- the new one -- not old + new."""
    cfg = parse_config_response(_device_blob(_ex(0, 0xB2, BUTTON_LED)))

    cfg.action_table = ActionTable.from_bytes(HOST_SLOT)
    out = serialize_config(cfg)

    assert _wire_tables(out) == [HOST_SLOT]
    # And repeated writes do not grow the config.
    again = serialize_config(parse_config_response(out))
    assert len(again) == len(out)
    assert _wire_tables(again) == [HOST_SLOT]


def test_legacy_append_to_unknown_ex_packets_replaces_rather_than_stacks() -> None:
    """Code that predates action_table appends a raw 0xB2. That is the newest
    intent, so it wins -- and the parsed table is not written alongside it."""
    cfg = parse_config_response(_device_blob(_ex(0, 0xB2, BUTTON_LED)))

    cfg.unknown_ex_packets.append((0, 0xB2, HOST_SLOT))
    out = serialize_config(cfg)

    assert _wire_tables(out) == [HOST_SLOT]


def test_config_with_stacked_tables_heals_on_rewrite(caplog: pytest.LogCaptureFixture) -> None:
    """A device already holding several tables (the bug's aftermath) runs the
    FIRST; a read-modify-write keeps that one and drops the shadowed copies."""
    blob = _device_blob(_ex(0, 0xB2, BUTTON_LED), _ex(0, 0xB2, HOST_SLOT), _ex(0, 0xB2, TEMP))

    with caplog.at_level(logging.WARNING):
        cfg = parse_config_response(blob)
    assert "3 action tables" in caplog.text
    assert cfg.action_table is not None and cfg.action_table.to_bytes() == BUTTON_LED

    assert _wire_tables(serialize_config(cfg)) == [BUTTON_LED]


def test_action_table_sorts_after_legacy_packets_and_keeps_other_ex_packets() -> None:
    other = _ex(0, 0xC0, b"\x01\x02\x03")
    cfg = parse_config_response(_device_blob(_ex(0, 0xB2, HOST_SLOT), other))
    out = serialize_config(cfg)

    assert other in out
    assert out.index(_ex(0, 0xB2, HOST_SLOT)) > out.index(bytes([0, 0x20]))
    assert _wire_tables(out) == [HOST_SLOT]


def test_no_table_writes_no_packet() -> None:
    out = serialize_config(_base_config(displays=[_display()]))
    assert _wire_tables(out) == []
