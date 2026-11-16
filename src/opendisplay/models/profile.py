"""One description of a connected device, built from data already read.

``build_profile`` is pure: it combines the firmware version, the stored config (if any) and the
identity (if the device answered READ_IDENT) into a ``DeviceProfile``. It performs no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

from .config import DisplayConfig, GlobalConfig
from .enums import ColorScheme, ICType
from .firmware import FirmwareVersion
from .ident import DeviceIdent, FirmwareTarget

DEFAULT_MAX_CONFIG: Final = 4096  # config size limit assumed when the identity does not state one

_TARGET_BY_IC_TYPE: Final[dict[int, FirmwareTarget]] = {
    ICType.NRF52840: FirmwareTarget.NRF52840,
    ICType.ESP32_S3: FirmwareTarget.ESP32,
    ICType.ESP32_C3: FirmwareTarget.ESP32,
    ICType.ESP32_C6: FirmwareTarget.ESP32,
    ICType.NRF52811: FirmwareTarget.NRF52811,
    ICType.EFR32BG22: FirmwareTarget.EFR32BG22,
}


class Generation(Enum):
    """Firmware generation, decided by the firmware major version alone."""

    V2 = "2.x"  # firmware major below 3
    V3 = "3.x"  # firmware major 3 and up: config-driven, SEC2


@dataclass(frozen=True)
class DeviceProfile:  # pylint: disable=too-many-instance-attributes
    """What the library knows about a connected device.

    Attributes:
        generation: Firmware generation, from the firmware major version.
        firmware: The firmware version as read.
        target: Firmware family, from the identity; else mapped from the config's ``ic_type``;
            else ``None``.
        ident: The identity; ``None`` when it was not asked (2.x) or not answered.
        ident_missing: A 3.x device did not answer READ_IDENT.
        configured: A config with at least one display is stored.
        needs_migration: 3.x only: the identity says so, or (without an identity tail) a
            configured display has no drive packets.
        width: Display 0 of the config, else display record 0 of the identity, else ``None``.
        height: As ``width``.
        color_scheme: As ``width``; the enum when known, the raw value when not.
        rotation: Display 0 of the config, else ``None``.
        max_config: Largest config the device stores: the identity's value, else 4096.
        supports_partial: Display 0 supports partial (0x76) updates.
        supports_pipe: Display 0 advertises PIPE_WRITE.
        supports_compression: Display 0 accepts compressed uploads (ZIP or streaming).
        drive_modes: Names of the config's drive modes (0xAE), in ``mode_index`` order.
        code_map: Logical colour index to panel code, from the first 0xB1 packet.
    """

    generation: Generation
    firmware: FirmwareVersion
    target: FirmwareTarget | int | None
    ident: DeviceIdent | None
    ident_missing: bool
    configured: bool
    needs_migration: bool
    width: int | None
    height: int | None
    color_scheme: ColorScheme | int | None
    rotation: int | None
    max_config: int
    supports_partial: bool
    supports_pipe: bool
    supports_compression: bool
    drive_modes: tuple[str, ...]
    code_map: tuple[int, int, int, int] | None


def _color_scheme(raw: int) -> ColorScheme | int:
    try:
        return ColorScheme.from_value(raw)
    except ValueError:
        return raw


def _target(config: GlobalConfig | None, ident: DeviceIdent | None) -> FirmwareTarget | int | None:
    if ident is not None:
        return ident.fw_target
    if config is None:
        return None
    return _TARGET_BY_IC_TYPE.get(config.system.ic_type)


def _needs_migration(generation: Generation, config: GlobalConfig | None, ident: DeviceIdent | None) -> bool:
    if generation is Generation.V2:
        return False
    if ident is not None and ident.needs_migration is not None:
        return ident.needs_migration
    if config is None or not config.displays:
        return False
    return not config.display_modes and not config.display_controllers


def build_profile(
    firmware: FirmwareVersion,
    config: GlobalConfig | None,
    ident: DeviceIdent | None,
    *,
    ident_missing: bool = False,
) -> DeviceProfile:
    """Describe a device from its firmware version, stored config and identity. No I/O.

    Args:
        firmware: Firmware version as read from the device.
        config: Stored config, or ``None`` when the device has none.
        ident: Identity, or ``None`` when not asked or not answered.
        ident_missing: The device was asked for its identity and did not answer.
    """
    generation = Generation.V3 if firmware["major"] >= 3 else Generation.V2
    display: DisplayConfig | None = config.displays[0] if config is not None and config.displays else None

    width: int | None = None
    height: int | None = None
    color_scheme: ColorScheme | int | None = None
    if display is not None:
        width, height = display.pixel_width, display.pixel_height
        color_scheme = _color_scheme(display.color_scheme)
    elif ident is not None and ident.displays:
        record = ident.displays[0]
        width, height = record.pixel_width, record.pixel_height
        color_scheme = _color_scheme(record.color_scheme)

    drive_modes: tuple[str, ...] = ()
    code_map: tuple[int, int, int, int] | None = None
    if config is not None:
        drive_modes = tuple(m.name for m in sorted(config.display_modes, key=lambda m: m.mode_index))
        panels = config.display_panels
        code_map = panels[0].code_map if panels else None

    return DeviceProfile(
        generation=generation,
        firmware=firmware,
        target=_target(config, ident),
        ident=ident,
        ident_missing=ident_missing,
        configured=display is not None,
        needs_migration=_needs_migration(generation, config, ident),
        width=width,
        height=height,
        color_scheme=color_scheme,
        rotation=display.rotation if display is not None else None,
        max_config=ident.max_config if ident is not None and ident.max_config is not None else DEFAULT_MAX_CONFIG,
        supports_partial=bool(display.partial_update_support) if display is not None else False,
        supports_pipe=display.supports_pipe_write if display is not None else False,
        supports_compression=(
            display.supports_zip or display.supports_streaming_decompression if display is not None else False
        ),
        drive_modes=drive_modes,
        code_map=code_map,
    )
