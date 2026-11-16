"""BWRY uploads take their colour-to-code table from the device's 0xB1 code_map (piece 3b, Task 4).

Firmware 3.0 stores code_map and does not apply it: hosts send panel codes
(Firmware_Unified docs/CONFIG_DRIVEN_DISPLAY.md "code_map").
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from epaper_dithering import DitherMode
from PIL import Image
from test_profile import _config, _display

from opendisplay.device.imaging import _capabilities_from_config, prepare_image
from opendisplay.exceptions import ImageEncodingError
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.config import GlobalConfig
from opendisplay.models.enums import ColorScheme
from opendisplay.models.panel_drive import LengthPrefixedPacket
from opendisplay.protocol.config_parser import parse_config_response

ALPHA = parse_config_response(
    (Path(__file__).parent.parent / "fixtures" / "configs" / "bg22_alpha_drive_packets.bin").read_bytes()
)
_PANEL_BODY = next(p.body for p in ALPHA.length_prefixed_packets if p.packet_type == 0xB1)


def _panel(code_map: tuple[int, int, int, int], *, instance: int = 0) -> LengthPrefixedPacket:
    body = bytearray(_PANEL_BODY)
    body[0] = instance
    body[7:11] = bytes(code_map)  # <BBBHH4s...: instance, flags, idle level, timeout, delay, code_map
    return LengthPrefixedPacket(number=0, packet_type=0xB1, body=bytes(body))


def _bwry(panel_ic_type: int = 0x001C, packets: list[LengthPrefixedPacket] | None = None) -> GlobalConfig:
    display = _display(
        panel_ic_type=panel_ic_type, pixel_width=4, pixel_height=1, color_scheme=ColorScheme.BWRY, rotation=0
    )
    return _config(displays=[display], length_prefixed_packets=packets or [])


def _red_yellow() -> Image.Image:
    image = Image.new("RGB", (4, 1))
    for x, colour in enumerate([(0, 0, 0), (255, 255, 255), (255, 255, 0), (255, 0, 0)]):
        image.putpixel((x, 0), colour)
    return image


def _encode(capabilities: DeviceCapabilities, panel_ic_type: int = 0x001C) -> bytes:
    data, _, _ = prepare_image(
        _red_yellow(),
        capabilities=capabilities,
        panel_ic_type=panel_ic_type,
        dither_mode=DitherMode.NONE,
        compress=False,
        use_measured_palettes=False,
    )
    return data


def _codes(byte: int) -> list[int]:
    return [(byte >> shift) & 0x03 for shift in (6, 4, 2, 0)]


def test_capabilities_carry_the_code_map_of_display_0() -> None:
    assert _capabilities_from_config(_bwry(packets=[_panel((0, 1, 3, 2))])).bwry_codes == (0, 1, 3, 2)


def test_without_a_panel_packet_there_is_no_code_map() -> None:
    assert _capabilities_from_config(_bwry()).bwry_codes is None


def test_another_displays_panel_packet_is_not_used() -> None:
    assert _capabilities_from_config(_bwry(packets=[_panel((0, 1, 3, 2), instance=1)])).bwry_codes is None


def test_a_swapped_code_map_exchanges_the_red_and_yellow_codes() -> None:
    plain = _capabilities_from_config(_bwry(packets=[_panel((0, 1, 2, 3))]))
    swapped = dataclasses.replace(plain, bwry_codes=(0, 1, 3, 2))

    plain_codes, swapped_codes = _codes(_encode(plain)[0]), _codes(_encode(swapped)[0])

    assert plain_codes[:2] == swapped_codes[:2]  # black and white unchanged
    assert (swapped_codes[2], swapped_codes[3]) == (plain_codes[3], plain_codes[2])  # yellow and red exchanged
    assert plain_codes[2] != plain_codes[3]


def test_the_code_map_wins_over_the_built_in_table() -> None:
    # 0x001D is swapped in the built-in table; a device whose 0xB1 says identity is sent identity.
    identity = _capabilities_from_config(_bwry(panel_ic_type=0x001D, packets=[_panel((0, 1, 2, 3))]))
    built_in = _capabilities_from_config(_bwry(panel_ic_type=0x001D))

    assert _encode(identity, 0x001D) == _encode(dataclasses.replace(identity, bwry_codes=None), 0x001C)
    assert _encode(built_in, 0x001D) != _encode(identity, 0x001D)


@pytest.mark.parametrize("code_map", [(0, 1, 1, 3), (0, 1, 2, 4)])
def test_a_code_map_that_is_not_a_permutation_raises(code_map: tuple[int, int, int, int]) -> None:
    capabilities = dataclasses.replace(_capabilities_from_config(_bwry()), bwry_codes=code_map)

    with pytest.raises(ImageEncodingError, match="code_map"):
        _encode(capabilities)
