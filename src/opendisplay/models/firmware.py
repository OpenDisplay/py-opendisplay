"""Firmware version data structure."""

from __future__ import annotations

from typing import Final, TypedDict

from .enums import ICType

_FIRMWARE_REPOS: Final[dict[int, str]] = {
    ICType.NRF52840: "OpenDisplay/Firmware",
    ICType.ESP32_S3: "OpenDisplay/Firmware",
    ICType.ESP32_C3: "OpenDisplay/Firmware",
    ICType.ESP32_C6: "OpenDisplay/Firmware",
    ICType.NRF52811: "OpenDisplay/Firmware_NRF",
    ICType.EFR32BG22: "OpenDisplay/Firmware_Silabs",
}


def firmware_release_repo(ic_type: int) -> str | None:
    """Return the GitHub repo slug for a device's firmware, or None if unknown."""
    return _FIRMWARE_REPOS.get(ic_type)


def firmware_ota_asset(ic_type: int, tag: str) -> str | None:
    """Return the expected OTA asset filename for a given IC type and release tag.

    Returns None for IC types without a BLE OTA path (ESP32 variants).
    """
    if ic_type == ICType.NRF52840:
        return "NRF52840.zip"
    if ic_type == ICType.NRF52811:
        return f"EPD-nRF52-{tag}-ota.zip"
    if ic_type == ICType.EFR32BG22:
        return f"opendisplay-bg22-v{tag}.gbl"
    return None


class FirmwareVersion(TypedDict):
    """Firmware version information.

    Attributes:
        major: Major version number (0-255)
        minor: Minor version number (0-255)
        sha: Git commit SHA hash
    """

    major: int
    minor: int
    sha: str
