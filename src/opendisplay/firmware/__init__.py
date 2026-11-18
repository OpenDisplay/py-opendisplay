"""Firmware 3.x updates: release manifests, transfers and ``update()`` (piece 5).

Today's ``opendisplay.ota`` and ``opendisplay.models.firmware`` keep their names (Home Assistant
imports them); their names are also re-exported here.
"""

from ..models.firmware import FirmwareVersion, firmware_ota_asset, firmware_release_repo
from ..ota import find_nrf_dfu_device, perform_nrf_dfu, perform_silabs_ota
from .releases import Artefact, BoardRelease, ReleaseManifest, can_roll_back
from .updater import UpdateOutcome, UpdateRefusedError, UpdateResult, enable_ota, update

__all__ = [
    "Artefact",
    "BoardRelease",
    "FirmwareVersion",
    "ReleaseManifest",
    "UpdateOutcome",
    "UpdateRefusedError",
    "UpdateResult",
    "can_roll_back",
    "enable_ota",
    "find_nrf_dfu_device",
    "firmware_ota_asset",
    "firmware_release_repo",
    "perform_nrf_dfu",
    "perform_silabs_ota",
    "update",
]
