"""The public API as callers see it, pinned.

The snapshot covers what ``opendisplay`` exports, what ``opendisplay.device`` exports, and the
public members of ``OpenDisplayDevice`` with their signatures. The import list is what the two
Home Assistant integrations import today, including from submodules.

Regenerate after an intended API change with::

    UPDATE_API_SNAPSHOT=1 uv run pytest tests/unit/test_public_api_snapshot.py
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
from pathlib import Path

import pytest

import opendisplay
import opendisplay.device
from opendisplay import OpenDisplayDevice

SNAPSHOT = Path(__file__).parent / "data" / "public_api.json"

DEVICE_MODULE_EXPORTS = ("OpenDisplayDevice", "prepare_image", "preview_image")

HA_IMPORTS = {
    "opendisplay": [
        "MANUFACTURER_ID",
        "AdvertisementTracker",
        "AuthenticationFailedError",
        "AuthenticationRequiredError",
        "BLEConnectionError",
        "BLETimeoutError",
        "BuzzerActivateConfig",
        "ColorScheme",
        "DitherMode",
        "FitMode",
        "GlobalConfig",
        "LedFlashConfig",
        "LedFlashStep",
        "NfcNotSupportedError",
        "NfcRecordType",
        "NfcWriteError",
        "OpenDisplayConnectionError",
        "OpenDisplayDevice",
        "OpenDisplayError",
        "OpenDisplayTimeoutError",
        "PartialState",
        "RefreshMode",
        "Rotation",
        "parse_advertisement",
        "prepare_image",
        "voltage_to_percent",
    ],
    "opendisplay.device": ["OpenDisplayDevice"],
    "opendisplay.exceptions": [
        "AuthenticationFailedError",
        "AuthenticationRequiredError",
        "BLEConnectionError",
        "OTAError",
    ],
    "opendisplay.models": ["FirmwareVersion"],
    "opendisplay.models.advertisement": [
        "AdvertisementData",
        "ButtonChangeEvent",
        "Sht40Reading",
        "TouchChangeEvent",
        "TouchTracker",
    ],
    "opendisplay.models.config": ["BinaryInputs", "SensorData"],
    "opendisplay.models.config_json": ["config_from_json", "config_to_json"],
    "opendisplay.models.enums": ["CapacityEstimator", "ICType", "PowerMode", "SensorType"],
    "opendisplay.models.firmware": ["firmware_ota_asset", "firmware_release_repo"],
    "opendisplay.ota": ["perform_silabs_ota"],
}


def _describe(member: object) -> str:
    """One line that changes when a caller-visible aspect of ``member`` changes."""
    if isinstance(member, property):
        return "property"
    if isinstance(member, (staticmethod, classmethod)):
        kind = type(member).__name__
        return f"{kind} {inspect.signature(inspect.unwrap(member.__func__))}"
    if inspect.isfunction(member):
        prefix = "async " if inspect.iscoroutinefunction(inspect.unwrap(member)) else ""
        return f"{prefix}{inspect.signature(inspect.unwrap(member))}"
    return repr(member)


def _current_api() -> dict[str, object]:
    members = {
        name: _describe(member)
        for name, member in sorted(vars(OpenDisplayDevice).items())
        if not name.startswith("_") or name == "__init__"
    }
    return {
        "opendisplay.__all__": sorted(opendisplay.__all__),
        "opendisplay.device": sorted(n for n in DEVICE_MODULE_EXPORTS if hasattr(opendisplay.device, n)),
        "OpenDisplayDevice": members,
    }


def test_public_api_matches_snapshot() -> None:
    current = _current_api()
    if os.environ.get("UPDATE_API_SNAPSHOT") == "1":
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    assert current == json.loads(SNAPSHOT.read_text())


@pytest.mark.parametrize(
    ("module", "name"),
    [(module, name) for module, names in HA_IMPORTS.items() for name in names],
)
def test_home_assistant_imports(module: str, name: str) -> None:
    assert hasattr(importlib.import_module(module), name)


def test_device_module_is_importable_by_old_path() -> None:
    from opendisplay.device import OpenDisplayDevice as from_device
    from opendisplay.device import prepare_image, preview_image

    assert from_device is opendisplay.OpenDisplayDevice
    assert prepare_image is opendisplay.prepare_image
    assert callable(preview_image)
