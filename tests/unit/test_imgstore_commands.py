"""Image-store and action opcodes 0x0047-0x004D (protocol 2.4).

Frames are [status][cmd_lo][data], status 0x00 ACK / 0xFF NACK, with bare NACKs.
Firmware_Unified BG22 at eb512a2 sends 0x01/0x00 instead; that must never be
read as success (under that scheme 0x00 is a NACK).
"""

from __future__ import annotations

import struct
import zlib

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.exceptions import (
    AuthenticationRequiredError,
    CommandRejectedError,
    IntegrityCheckError,
    InvalidResponseError,
)
from opendisplay.models.imgstore import FlashSlotState, image_digest
from opendisplay.protocol.commands import (
    FLASH_ERASE_ALL,
    CommandCode,
    build_action_fire_command,
    build_action_info_command,
    build_flash_display_command,
    build_flash_erase_command,
    build_flash_info_command,
    build_flash_list_command,
    build_flash_preload_command,
)
from opendisplay.protocol.responses import (
    parse_action_fire,
    parse_action_info,
    parse_flash_info,
    parse_flash_list,
    parse_flash_slot_ack,
)

# ---------------------------------------------------------------------------
# request builders
# ---------------------------------------------------------------------------


def test_request_bytes() -> None:
    assert build_flash_info_command() == b"\x00\x47"
    assert build_flash_list_command() == b"\x00\x48"
    assert build_flash_erase_command(4) == b"\x00\x49\x04"
    assert build_flash_erase_command() == b"\x00\x49\xff"
    assert build_flash_preload_command(24) == b"\x00\x4a\x18"
    assert build_flash_display_command(0) == b"\x00\x4b\x00"
    assert build_action_info_command() == b"\x00\x4c"
    assert build_action_fire_command() == b"\x00\x4d\x00"
    assert build_action_fire_command(7) == b"\x00\x4d\x07"


@pytest.mark.parametrize(
    "build",
    [
        lambda: build_flash_erase_command(32),
        lambda: build_flash_preload_command(-1),
        lambda: build_flash_display_command(0xFF),
        lambda: build_action_fire_command(0xFE),  # ACK would mimic the auth-required frame
    ],
)
def test_out_of_range_arguments_are_refused(build) -> None:
    with pytest.raises(ValueError):
        build()


def test_opcode_values() -> None:
    assert [c.value for c in (CommandCode.FLASH_INFO, CommandCode.ACTION_FIRE)] == [0x0047, 0x004D]


# ---------------------------------------------------------------------------
# response parsers
# ---------------------------------------------------------------------------

INFO = b"\x00\x47\x01\x19" + struct.pack("<II", 20480, 512 * 1024)


def test_flash_info() -> None:
    info = parse_flash_info(INFO)
    assert info.mounted and info.slot_count == 25
    assert info.slot_stride == 20480 and info.capacity == 524288
    assert info.max_frame_bytes == 20480 - 32


def test_flash_info_unmounted_reports_no_capacity() -> None:
    info = parse_flash_info(b"\x00\x47\x00\x00" + struct.pack("<II", 20480, 0))
    assert not info.mounted and info.max_frame_bytes == 0


def test_flash_list() -> None:
    frame = b"\x00\x48\x03" + bytes([0, 0xFC]) + struct.pack("<I", 0xDEADBEEF)
    frame += bytes([1, 0xFF]) + struct.pack("<I", 0) + bytes([2, 0xFE]) + struct.pack("<I", 0xFFFFFFFF)
    slots = parse_flash_list(frame)
    assert [(s.index, s.state, s.valid) for s in slots] == [(0, 0xFC, True), (1, 0xFF, False), (2, 0xFE, False)]
    assert slots[0].digest == 0xDEADBEEF
    assert FlashSlotState(slots[2].state) is FlashSlotState.WRITING


def test_flash_list_rejects_length_mismatch() -> None:
    with pytest.raises(InvalidResponseError):
        parse_flash_list(b"\x00\x48\x02" + bytes(6))


def test_flash_list_nack_when_unmounted() -> None:
    with pytest.raises(CommandRejectedError) as exc:
        parse_flash_list(b"\xff\x48")
    assert exc.value.command == CommandCode.FLASH_LIST


@pytest.mark.parametrize(
    ("cmd", "slot"),
    [(CommandCode.FLASH_ERASE, 3), (CommandCode.FLASH_ERASE, FLASH_ERASE_ALL), (CommandCode.FLASH_DISPLAY, 0)],
)
def test_slot_ack(cmd: CommandCode, slot: int) -> None:
    parse_flash_slot_ack(bytes([0x00, cmd & 0xFF, slot]), cmd, slot)


def test_slot_nack_carries_the_echoed_index() -> None:
    with pytest.raises(CommandRejectedError) as exc:
        parse_flash_slot_ack(b"\xff\x4b\x05", CommandCode.FLASH_DISPLAY, 5)
    assert exc.value.data == b"\x05"


def test_slot_ack_for_another_slot_is_invalid() -> None:
    with pytest.raises(InvalidResponseError):
        parse_flash_slot_ack(b"\x00\x4a\x02", CommandCode.FLASH_PRELOAD, 3)


def test_inverted_status_bytes_are_never_read_as_success() -> None:
    """BG22 eb512a2: 0x01 = ACK, 0x00 = NACK. Its NACK [00][49][03] is
    byte-identical to a real ACK for slot 3, so a 0x01 must not be tolerated --
    a parser that accepts both schemes would turn refusals into successes."""
    with pytest.raises(InvalidResponseError, match="inverted"):
        parse_flash_info(b"\x01\x47" + INFO[2:])
    with pytest.raises(InvalidResponseError, match="inverted"):
        parse_flash_slot_ack(b"\x01\x49\x03", CommandCode.FLASH_ERASE, 3)


def test_wrong_echo_is_invalid() -> None:
    with pytest.raises(InvalidResponseError, match="echo"):
        parse_flash_info(b"\x00\x48" + INFO[2:])


def test_auth_required_status() -> None:
    with pytest.raises(AuthenticationRequiredError):
        parse_action_info(b"\xfe\x4c")


def test_action_info_signed_temperature() -> None:
    info = parse_action_info(b"\x00\x4c" + bytes([5, 4, 1, 0, 5, 3]) + struct.pack("<b", -7))
    assert (info.entries, info.active, info.inactive, info.rejected) == (5, 4, 1, 0)
    assert (info.last_trigger, info.fire_count, info.last_temp_c) == (5, 3, -7)


def test_action_fire_ack_must_echo_host_id() -> None:
    parse_action_fire(b"\x00\x4d\x07", 7)
    with pytest.raises(InvalidResponseError):
        parse_action_fire(b"\x00\x4d\x08", 7)


def test_image_digest_is_zlib_crc32_of_uncompressed_frame() -> None:
    frame = bytes(range(256)) * 10
    assert image_digest(frame) == zlib.crc32(frame)
    assert image_digest(b"123456789") == 0xCBF43926  # CRC-32/ISO-HDLC check value


# ---------------------------------------------------------------------------
# device methods
# ---------------------------------------------------------------------------


class _FakeConnection:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.written: list[bytes] = []
        self.timeouts: list[float] = []

    async def write_command(self, cmd: bytes, response: bool = True) -> None:
        self.written.append(cmd)

    async def read_response(self, timeout: float) -> bytes:
        self.timeouts.append(timeout)
        return self.response


def _device(response: bytes) -> tuple[OpenDisplayDevice, _FakeConnection]:
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response)
    device._connection = fake  # type: ignore[assignment]
    return device, fake


async def test_device_read_flash_info() -> None:
    device, fake = _device(INFO)
    info = await device.read_flash_info()
    assert fake.written == [b"\x00\x47"] and info.slot_count == 25


async def test_device_erase_all_ack_is_not_mistaken_for_a_gate_refusal() -> None:
    """[00][49][FF] is the ERASE_ALL ACK. Without a session it must not be
    taken for the gate's [00][cmd][FF] decrypt-failure frame."""
    device, fake = _device(b"\x00\x49\xff")
    await device.erase_flash_slot()
    assert fake.written == [b"\x00\x49\xff"]
    assert fake.timeouts == [device.TIMEOUT_FLASH_ERASE_ALL]


async def test_device_plaintext_ff_frame_is_still_a_refusal_for_other_commands() -> None:
    device, _ = _device(b"\x00\x4b\xff")
    with pytest.raises(IntegrityCheckError):
        await device.display_flash_slot(3)


async def test_device_erase_one_slot_nack() -> None:
    device, fake = _device(b"\xff\x49\x1f")
    with pytest.raises(CommandRejectedError):
        await device.erase_flash_slot(31)
    assert fake.timeouts == [device.TIMEOUT_FLASH_ERASE]


async def test_device_preload_and_display() -> None:
    device, fake = _device(b"\x00\x4a\x02")
    await device.preload_flash_slot(2)
    fake.response = b"\x00\x4b\x02"
    await device.display_flash_slot(2)
    assert fake.written == [b"\x00\x4a\x02", b"\x00\x4b\x02"]


async def test_device_list_slots_and_actions() -> None:
    device, fake = _device(b"\x00\x48\x01\x00\xfc" + struct.pack("<I", 1))
    assert (await device.list_flash_slots())[0].valid
    fake.response = b"\x00\x4c" + bytes(7)
    assert (await device.read_action_info()).entries == 0
    fake.response = b"\x00\x4d\x05"
    await device.fire_action(5)
    assert fake.written[-1] == b"\x00\x4d\x05"
