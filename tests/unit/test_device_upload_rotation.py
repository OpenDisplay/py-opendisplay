"""Test upload image source-rotation behavior."""

from __future__ import annotations

import pytest
from epaper_dithering import ColorScheme, DitherMode
from PIL import Image

from opendisplay import prepare_image
from opendisplay.encoding.pipeline import _rotate_source_image
from opendisplay.models.config import (
    DisplayConfig,
    GlobalConfig,
    ManufacturerData,
    PowerOption,
    SystemConfig,
)
from opendisplay.models.enums import FitMode, Rotation


def _config(width: int = 2, height: int = 2) -> GlobalConfig:
    return GlobalConfig(
        system=SystemConfig(
            ic_type=0,
            communication_modes=0,
            device_flags=0,
            pwr_pin=0xFF,
            reserved=b"\x00" * 17,
        ),
        manufacturer=ManufacturerData(
            manufacturer_id=0,
            board_type=0,
            board_revision=0,
            reserved=b"\x00" * 18,
        ),
        power=PowerOption(
            power_mode=0,
            battery_capacity_mah=b"\x00\x00\x00",
            sleep_timeout_ms=0,
            tx_power=0,
            sleep_flags=0,
            battery_sense_pin=0xFF,
            battery_sense_enable_pin=0xFF,
            battery_sense_flags=0,
            capacity_estimator=0,
            voltage_scaling_factor=0,
            deep_sleep_current_ua=0,
            deep_sleep_time_seconds=0,
            reserved=b"\x00" * 12,
        ),
        displays=[
            DisplayConfig(
                instance_number=0,
                display_technology=0,
                panel_ic_type=0,
                pixel_width=width,
                pixel_height=height,
                active_width_mm=10,
                active_height_mm=10,
                tag_type=0,
                rotation=0,
                reset_pin=0xFF,
                busy_pin=0xFF,
                dc_pin=0xFF,
                cs_pin=0xFF,
                data_pin=0,
                partial_update_support=0,
                color_scheme=ColorScheme.MONO.value,
                transmission_modes=0,
                clk_pin=0,
                reserved_pins=b"\x00" * 7,
                full_update_mC=0,
                reserved=b"\x00" * 13,
            )
        ],
    )


def _stub_prepare_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "opendisplay.encoding.pipeline.get_palette_for_display",
        lambda panel_ic_type, color_scheme, use_measured_palettes: None,
    )
    monkeypatch.setattr(
        "opendisplay.encoding.pipeline.dither_image",
        lambda image, palette, mode, tone_compression: image.convert("P"),
    )
    monkeypatch.setattr(
        "opendisplay.encoding.pipeline.encode_image",
        lambda image, color_scheme: b"\x01\x02",
    )


def test_rotate_source_image_requires_rotation_enum() -> None:
    """rotate must be Rotation enum, not raw int."""
    image = Image.new("RGB", (2, 1), (255, 255, 255))

    with pytest.raises(TypeError, match="rotate must be Rotation"):
        _rotate_source_image(image, 90)  # type: ignore[arg-type]


def test_rotate_source_image_uses_clockwise_semantics() -> None:
    """ROTATE_90 and ROTATE_270 should rotate clockwise from caller perspective."""
    image = Image.new("RGB", (2, 2))
    image.putpixel((0, 0), (255, 0, 0))  # A
    image.putpixel((1, 0), (0, 255, 0))  # B
    image.putpixel((0, 1), (0, 0, 255))  # C
    image.putpixel((1, 1), (255, 255, 0))  # D

    rotated_90 = _rotate_source_image(image, Rotation.ROTATE_90)
    assert [rotated_90.getpixel((x, y)) for y in range(2) for x in range(2)] == [
        (0, 0, 255),  # C
        (255, 0, 0),  # A
        (255, 255, 0),  # D
        (0, 255, 0),  # B
    ]

    rotated_270 = _rotate_source_image(image, Rotation.ROTATE_270)
    assert [rotated_270.getpixel((x, y)) for y in range(2) for x in range(2)] == [
        (0, 255, 0),  # B
        (255, 255, 0),  # D
        (255, 0, 0),  # A
        (0, 0, 255),  # C
    ]


def test_prepare_image_rotates_before_fit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rotation should be applied before fit strategy sees source dimensions."""
    _stub_prepare_pipeline(monkeypatch)

    seen: dict[str, tuple[int, int]] = {}

    def fake_fit_image(image: Image.Image, target_size: tuple[int, int], fit: FitMode) -> Image.Image:
        seen["size_before_fit"] = image.size
        return image.resize(target_size)

    monkeypatch.setattr("opendisplay.encoding.pipeline.fit_image", fake_fit_image)

    image = Image.new("RGB", (4, 2), (255, 255, 255))
    encoded, compressed, processed = prepare_image(
        image,
        config=_config(width=2, height=2),
        dither_mode=DitherMode.BURKES,
        compress=False,
        fit=FitMode.CONTAIN,
        rotate=Rotation.ROTATE_90,
    )

    assert seen["size_before_fit"] == (2, 4)
    assert encoded == b"\x01\x02"
    assert compressed is None
    assert processed.size == (2, 2)


def test_prepare_image_without_rotation_preserves_orientation_before_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROTATE_0 should keep source orientation before fit."""
    _stub_prepare_pipeline(monkeypatch)

    seen: dict[str, tuple[int, int]] = {}

    def fake_fit_image(image: Image.Image, target_size: tuple[int, int], fit: FitMode) -> Image.Image:
        seen["size_before_fit"] = image.size
        return image.resize(target_size)

    monkeypatch.setattr("opendisplay.encoding.pipeline.fit_image", fake_fit_image)

    image = Image.new("RGB", (4, 2), (255, 255, 255))
    prepare_image(
        image,
        config=_config(width=2, height=2),
        dither_mode=DitherMode.BURKES,
        compress=False,
        fit=FitMode.CONTAIN,
        rotate=Rotation.ROTATE_0,
    )

    assert seen["size_before_fit"] == (4, 2)
