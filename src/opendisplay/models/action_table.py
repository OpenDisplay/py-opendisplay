"""0xB2 action table: trigger -> ordered effects, run by the device itself.

Layout per opendisplay_structs.h 2.3 (ActionEntryHead / ActionStep). The packet
body is a concatenation of entries::

    +0 entry_len    6 + 4 * step_count
    +1 trigger      ActionTrigger
    +2 trigger_arg  u16 LE, meaning depends on the trigger
    +4 flags        bit0 = enabled
    +5 step_count   <= ACTION_STEPS_MAX
    +6 steps        step_count x [effect][arg0][arg1][delay_cs]

The device walks the body the same way :meth:`ActionTable.from_bytes` does: the
first STRUCTURAL fault stops the walk, so that entry and everything after it
never load. Those bytes are kept in :attr:`ActionTable.trailing` so a parsed
table re-serializes byte for byte. Unknown trigger or effect values are NOT a
fault -- the device keeps such entries and counts them inactive -- so they parse
into ordinary entries carrying the raw number.

A config holds at most ONE 0xB2. The device resolves it by first match, so a
second copy is silently shadowed; GlobalConfig carries the table as a single
field for exactly that reason.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from enum import IntEnum

_LOGGER = logging.getLogger(__name__)

PACKET_TYPE_ACTION_TABLE = 0xB2
ACTION_ENTRY_HEAD_SIZE = 6
ACTION_STEP_SIZE = 4
ACTION_STEPS_MAX = 8
ACTION_FLAG_ENABLED = 0x01


class ActionTrigger(IntEnum):
    """ActionEntryHead.trigger (OD_ACTION_TRIG_*)."""

    NONE = 0  # never fires; counted inactive
    BOOT = 1
    BUTTON_PRESS = 2  # arg = 1 << button_id, 0 = any (matched by equality)
    BUTTON_LONG = 3  # arg = hold time in cs; no firmware emits it yet
    NFC_FIELD = 4
    HOST = 5  # fired by CMD_ACTION_FIRE; arg = host_id (low byte), 0 = any
    TEMP_ENTER = 6  # arg = [min_c:int8][max_c:int8]
    TEMP_EXIT = 7


class ActionEffect(IntEnum):
    """ActionStep.effect (OD_ACTION_EFF_*)."""

    NONE = 0  # delay only
    LED = 1  # arg0 = colour byte, arg1 = brightness 1..16
    LED_STOP = 2
    DISPLAY_SLOT = 3  # arg0 = image-store slot
    BUZZER = 4  # arg0 = melody id


def _u8(name: str, value: int) -> int:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must be 0..255, got {value}")
    return value


def pack_temp_window(min_c: int, max_c: int) -> int:
    """Pack a TEMP_ENTER/TEMP_EXIT window into a trigger_arg (both inclusive)."""
    for name, v in (("min_c", min_c), ("max_c", max_c)):
        if not -128 <= v <= 127:
            raise ValueError(f"{name} must fit int8, got {v}")
    return (min_c & 0xFF) | ((max_c & 0xFF) << 8)


def unpack_temp_window(trigger_arg: int) -> tuple[int, int]:
    """Inverse of :func:`pack_temp_window`: (min_c, max_c)."""
    lo, hi = struct.unpack("<bb", struct.pack("<H", trigger_arg & 0xFFFF))
    return lo, hi


@dataclass
class ActionStep:
    """One step: run ``effect`` then wait ``delay_cs`` x 10 ms."""

    effect: int
    arg0: int = 0
    arg1: int = 0
    delay_cs: int = 0

    def to_bytes(self) -> bytes:
        """Serialize to the 4-byte wire form."""
        return bytes(
            [
                _u8("effect", self.effect),
                _u8("arg0", self.arg0),
                _u8("arg1", self.arg1),
                _u8("delay_cs", self.delay_cs),
            ]
        )

    @property
    def known(self) -> bool:
        """True if this library knows the effect value."""
        return self.effect in ActionEffect


@dataclass
class ActionEntry:
    """One trigger and the steps it runs."""

    trigger: int
    trigger_arg: int = 0
    steps: list[ActionStep] = field(default_factory=list)
    flags: int = ACTION_FLAG_ENABLED

    @property
    def enabled(self) -> bool:
        """The device reads this live before every step."""
        return bool(self.flags & ACTION_FLAG_ENABLED)

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.flags = (self.flags | ACTION_FLAG_ENABLED) if value else (self.flags & ~ACTION_FLAG_ENABLED)

    @property
    def known(self) -> bool:
        """True if this library knows the trigger and every effect.

        The device makes the same judgement against ITS build (and against its
        hardware -- a buzzer step is inactive on a board without one), so this
        is a hint, not the device's verdict. CMD_ACTION_INFO reports that.
        """
        return self.trigger in ActionTrigger and all(s.known for s in self.steps)

    def to_bytes(self) -> bytes:
        """Serialize to the wire form; entry_len is derived, never stored."""
        if len(self.steps) > ACTION_STEPS_MAX:
            raise ValueError(f"an action entry holds at most {ACTION_STEPS_MAX} steps, got {len(self.steps)}")
        if not 0 <= self.trigger_arg <= 0xFFFF:
            raise ValueError(f"trigger_arg must be 0..65535, got {self.trigger_arg}")
        entry_len = ACTION_ENTRY_HEAD_SIZE + ACTION_STEP_SIZE * len(self.steps)
        head = struct.pack(
            "<BBHBB",
            entry_len,
            _u8("trigger", self.trigger),
            self.trigger_arg,
            _u8("flags", self.flags),
            len(self.steps),
        )
        return head + b"".join(s.to_bytes() for s in self.steps)


@dataclass
class ActionTable:
    """The whole 0xB2 body.

    ``trailing`` holds the bytes from the first entry the device would reject
    (or a remainder too short to be an entry) to the end of the body. The
    device never loads them; they are kept only so a parsed table round-trips
    exactly. Build new tables with ``trailing`` empty.
    """

    entries: list[ActionEntry] = field(default_factory=list)
    trailing: bytes = b""
    packet_number: int = 0

    @property
    def rejected(self) -> bool:
        """True if the walk stopped on a structural fault (what 0x004C counts)."""
        return len(self.trailing) >= ACTION_ENTRY_HEAD_SIZE

    def to_bytes(self) -> bytes:
        """Serialize the body (without the [number][id][length] header)."""
        return b"".join(e.to_bytes() for e in self.entries) + self.trailing

    @classmethod
    def from_bytes(cls, body: bytes, packet_number: int = 0) -> ActionTable:
        """Parse a 0xB2 body with the device's own walk rules."""
        entries: list[ActionEntry] = []
        off = 0
        n = len(body)
        while off + ACTION_ENTRY_HEAD_SIZE <= n:
            entry_len, trigger, trigger_arg, flags, steps = struct.unpack_from("<BBHBB", body, off)
            if (
                entry_len < ACTION_ENTRY_HEAD_SIZE
                or off + entry_len > n
                or entry_len != ACTION_ENTRY_HEAD_SIZE + ACTION_STEP_SIZE * steps
                or steps > ACTION_STEPS_MAX
            ):
                _LOGGER.warning(
                    "action table: malformed entry at +%d (len %d, steps %d); the device ignores it and the rest",
                    off,
                    entry_len,
                    steps,
                )
                break
            step_list = [
                ActionStep(*body[p : p + ACTION_STEP_SIZE])
                for p in range(off + ACTION_ENTRY_HEAD_SIZE, off + entry_len, ACTION_STEP_SIZE)
            ]
            entries.append(ActionEntry(trigger=trigger, trigger_arg=trigger_arg, steps=step_list, flags=flags))
            off += entry_len
        return cls(entries=entries, trailing=bytes(body[off:]), packet_number=packet_number)
