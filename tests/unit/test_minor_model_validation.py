"""Minor model/encoding validation fixes (§4)."""

from __future__ import annotations

import pytest
from epaper_dithering import ColorScheme
from PIL import Image

from opendisplay.encoding.images import encode_image
from opendisplay.models.advertisement import parse_advertisement
from opendisplay.models.enums import SensorType


def test_sensor_type_has_bq27220() -> None:
    assert SensorType.BQ27220 == 5


def test_encode_image_grayscale4_raises() -> None:
    img = Image.new("P", (4, 2))
    with pytest.raises(ValueError, match="two 1-bit planes"):
        encode_image(img, ColorScheme.GRAYSCALE_4)


def test_touch_event_rejects_negative_start_byte() -> None:
    # v1 advertisement (14 bytes: 11 dynamic + temp + battery + status)
    data = bytes([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 0x7C, 0x8B, 0x53])
    adv = parse_advertisement(data)
    assert adv.format_version == "v1"

    assert adv.touch_event(-1) is None  # would index from the end -> garbage
    assert adv.touch_event(7) is None  # past the valid 0-6 range
    assert adv.touch_event(0) is not None  # valid offset still works
