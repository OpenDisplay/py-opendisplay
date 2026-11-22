"""Warnings for firmware upload bugs (C1 BWR/BWY plane loss, C2 width truncation), fixed in 2.10."""

from __future__ import annotations

import logging

import pytest
from epaper_dithering import DitherMode
from PIL import Image

from opendisplay.device.facade import OpenDisplayDevice
from opendisplay.device.imaging import _warn_firmware_upload_limitations, prepare_image
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.enums import ColorScheme
from opendisplay.models.firmware import FirmwareVersion


def _fw(major: int, minor: int) -> FirmwareVersion:
    return FirmwareVersion(major=major, minor=minor, patch=0, sha="")


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.parametrize("scheme", [ColorScheme.BWR, ColorScheme.BWY])
def test_bwr_and_bwy_warn_about_the_lost_colour_plane_before_2_10(
    scheme: ColorScheme, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        _warn_firmware_upload_limitations(scheme, 800, _fw(2, 9))
    assert any("red/yellow" in m for m in _messages(caplog))


@pytest.mark.parametrize("firmware", [_fw(2, 10), _fw(2, 27), _fw(3, 0), None])
def test_bwr_does_not_warn_from_2_10_or_when_the_firmware_is_unknown(
    firmware: FirmwareVersion | None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        _warn_firmware_upload_limitations(ColorScheme.BWR, 800, firmware)
    assert _messages(caplog) == []


def test_non_byte_aligned_mono_width_warns_before_2_10(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _warn_firmware_upload_limitations(ColorScheme.MONO, 122, _fw(2, 9))  # 122 % 8 != 0
    assert any("truncate" in m for m in _messages(caplog))


@pytest.mark.parametrize("firmware", [_fw(2, 10), _fw(3, 0), None])
def test_non_byte_aligned_width_does_not_warn_from_2_10_or_when_unknown(
    firmware: FirmwareVersion | None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        _warn_firmware_upload_limitations(ColorScheme.MONO, 122, firmware)
    assert _messages(caplog) == []


def test_aligned_mono_width_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _warn_firmware_upload_limitations(ColorScheme.MONO, 800, _fw(2, 9))
    assert _messages(caplog) == []


def test_grayscale4_non_aligned_width_is_exempt(caplog: pytest.LogCaptureFixture) -> None:
    # firmware row-pads the 4-gray path, so a non-aligned width is safe there.
    with caplog.at_level(logging.WARNING):
        _warn_firmware_upload_limitations(ColorScheme.GRAYSCALE_4, 122, _fw(2, 9))
    assert _messages(caplog) == []


def test_prepare_image_alone_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    # It never knows the firmware, so it cannot tell whether the bug applies.
    caps = DeviceCapabilities(width=8, height=8, color_scheme=ColorScheme.BWR)
    with caplog.at_level(logging.WARNING):
        prepare_image(Image.new("RGB", (8, 8)), capabilities=caps, compress=False)
    assert not any("red/yellow" in m for m in _messages(caplog))


def _bwr_device(firmware: FirmwareVersion | None) -> OpenDisplayDevice:
    caps = DeviceCapabilities(width=8, height=8, color_scheme=ColorScheme.BWR)
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", capabilities=caps)
    device._fw_version = firmware
    return device


def test_a_device_on_old_firmware_warns_when_preparing_an_upload(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _bwr_device(_fw(2, 9))._prepare_image(Image.new("RGB", (8, 8)), DitherMode.NONE, compress=False)
    assert any("red/yellow" in m for m in _messages(caplog))


@pytest.mark.parametrize("firmware", [_fw(3, 0), None])
def test_a_device_on_current_or_unknown_firmware_does_not_warn(
    firmware: FirmwareVersion | None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        _bwr_device(firmware)._prepare_image(Image.new("RGB", (8, 8)), DitherMode.NONE, compress=False)
    assert not any("red/yellow" in m for m in _messages(caplog))
