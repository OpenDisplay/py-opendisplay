"""Image-store and action-runner responses (opcodes 0x0047-0x004D, protocol 2.4)."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from enum import IntEnum

# Bytes of every slot_stride the device spends on its own slot header.
FLASH_SLOT_HEADER_SIZE = 32
# slot_count never exceeds this, which keeps a FLASH_LIST reply in one frame.
FLASH_MAX_SLOTS = 32


class FlashSlotState(IntEnum):
    """FlashSlotRecord.state: a bit lattice NOR can walk without an erase."""

    BAD = 0x00
    VALID = 0xFC
    WRITING = 0xFE  # torn or aborted write; reusable as free
    FREE = 0xFF


def image_digest(frame: bytes) -> int:
    """The digest FLASH_LIST reports for a slot holding ``frame``.

    CRC-32/ISO-HDLC over the UNCOMPRESSED panel-ready frame -- the device
    stores after inflating, so compressing the upload does not change it.
    """
    return zlib.crc32(frame) & 0xFFFFFFFF


@dataclass(frozen=True)
class FlashInfo:
    """CMD_FLASH_INFO (0x0047). Fields other than ``mounted`` mean nothing while unmounted."""

    mounted: bool
    slot_count: int
    slot_stride: int
    capacity: int

    @property
    def max_frame_bytes(self) -> int:
        """Largest frame one slot can hold (0 when the store is not mounted)."""
        if not self.mounted or self.slot_stride <= FLASH_SLOT_HEADER_SIZE:
            return 0
        return self.slot_stride - FLASH_SLOT_HEADER_SIZE


@dataclass(frozen=True)
class FlashSlot:
    """One CMD_FLASH_LIST (0x0048) record."""

    index: int
    state: int
    digest: int

    @property
    def valid(self) -> bool:
        """True if the slot holds a complete, displayable frame."""
        return self.state == FlashSlotState.VALID


@dataclass(frozen=True)
class ActionInfo:
    """CMD_ACTION_INFO (0x004C): statistics of the FIRST 0xB2 table on the device."""

    entries: int
    active: int
    inactive: int
    rejected: int
    last_trigger: int
    fire_count: int
    last_temp_c: int
