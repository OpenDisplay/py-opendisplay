"""Device identity as answered by READ_IDENT (0x0046).

Layout (Firmware_Unified ``shared/protocol/opendisplay_protocol.h``, CMD_READ_IDENT)::

    [0x00][0x46][proto_minor][fw_target][display_count]
    display_count x [panel_ic_type:2 LE][pixel_width:2 LE][pixel_height:2 LE][color_scheme:1]
    3.0 tail: [flags:1][max_config:2 LE][max_seq_steps:2 LE][escapes:1]
    release board (Firmware_Unified #107): [board_len:1][board:board_len]

A 2.x device ends after the display records, and firmware before #107 after ``escapes``; later
firmware may append more bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

IDENT_FLAG_CONFIG_DRIVEN = 0x01
IDENT_FLAG_NEEDS_MIGRATION = 0x02
IDENT_FLAG_BUILTIN_DRIVER = 0x04
IDENT_FLAG_SEC_V2 = 0x08
IDENT_FLAG_CLAIMED = 0x10
IDENT_FLAG_CLAIM_MODE = 0x20
IDENT_FLAG_LEGACY_SECRETS_OPEN = 0x40

IDENT_ESC_WINDOW = 0x01
IDENT_ESC_FILL = 0x02


class FirmwareTarget(IntEnum):
    """Which firmware family answers (``OD_IDENT_TARGET_*``), one value per MCU family.

    2.x nRF52840 builds answered 0x01; 0x05 exists since 3.0. 0x06 is provisional in the firmware.
    """

    ESP32 = 0x01
    NRF54 = 0x02
    EFR32BG22 = 0x03
    NRF52811 = 0x04
    NRF52840 = 0x05
    TELINK = 0x06


@dataclass(frozen=True)
class IdentDisplay:
    """One display record. The fields carry the same values as the display config packet's."""

    panel_ic_type: int
    pixel_width: int
    pixel_height: int
    color_scheme: int


@dataclass(frozen=True)
class DeviceIdent:
    """What a device reports about itself.

    ``flags``, ``max_config``, ``max_seq_steps`` and ``escapes`` are ``None`` when the reply has
    no 3.0 tail. ``board`` names the release board (``"s3-n16r8"``, ``"bg22"``) whose images the
    device takes; ``None`` on firmware before that field, or one that is not a release build.
    ``flags`` is the raw byte: bits beyond the three named ones are set by 3.0 firmware and are
    kept so nothing is lost.
    """

    proto_minor: int
    fw_target: FirmwareTarget | int
    displays: tuple[IdentDisplay, ...]
    flags: int | None = None
    max_config: int | None = None
    max_seq_steps: int | None = None
    escapes: int | None = None
    board: str | None = None  # the release manifest's boards[].board; None if not reported
    extra: bytes = b""

    @property
    def has_tail(self) -> bool:
        """Whether the device sent the 3.0 tail."""
        return self.flags is not None

    def _flag(self, mask: int) -> bool | None:
        return None if self.flags is None else bool(self.flags & mask)

    def _escape(self, mask: int) -> bool | None:
        return None if self.escapes is None else bool(self.escapes & mask)

    @property
    def config_driven(self) -> bool | None:
        """Panels are driven from the config's drive packets (0xAE-0xB1)."""
        return self._flag(IDENT_FLAG_CONFIG_DRIVEN)

    @property
    def needs_migration(self) -> bool | None:
        """A configured display cannot be drawn until a config with drive data is written."""
        return self._flag(IDENT_FLAG_NEEDS_MIGRATION)

    @property
    def builtin_driver(self) -> bool | None:
        """A display is driven by a built-in controller driver (packet 0xB3)."""
        return self._flag(IDENT_FLAG_BUILTIN_DRIVER)

    @property
    def sec2(self) -> bool | None:
        """The firmware speaks SECURITY_V2 (claim, sealed sessions)."""
        return self._flag(IDENT_FLAG_SEC_V2)

    @property
    def claimed(self) -> bool | None:
        """The device is claimed (advisory: READ_IDENT is unauthenticated)."""
        return self._flag(IDENT_FLAG_CLAIMED)

    @property
    def claim_mode(self) -> bool | None:
        """The device is in claim mode."""
        return self._flag(IDENT_FLAG_CLAIM_MODE)

    @property
    def legacy_secrets_open(self) -> bool | None:
        """Unclaimed, but the stored config still holds 2.x secrets."""
        return self._flag(IDENT_FLAG_LEGACY_SECRETS_OPEN)

    @property
    def supports_window(self) -> bool | None:
        """The drive-sequence WINDOW escape (0x0C) is compiled in."""
        return self._escape(IDENT_ESC_WINDOW)

    @property
    def supports_fill(self) -> bool | None:
        """The drive-sequence FILL escape (0x0D) is compiled in."""
        return self._escape(IDENT_ESC_FILL)
