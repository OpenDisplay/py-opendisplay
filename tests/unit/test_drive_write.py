"""Uploads in a named drive mode through CMD_DRIVE_WRITE_START (0x0085) (piece 3b, Task 5b).

Wire (Firmware_Unified f6d3a83 opendisplay_protocol.h): START [00][85][mode][refresh][flags][rsv:2]
(+ uncompressed_size:4 LE + first compressed bytes when flags bit0); answer [00][85][mode][passes]
[temp_c]; refusal [FF][85][err][mode]; data over 0x71; END 0x72; refresh-complete 0x73.
"""

from __future__ import annotations

import dataclasses
import zlib
from pathlib import Path

import pytest
from PIL import Image

import opendisplay
from opendisplay import OpenDisplayDevice
from opendisplay.device.imaging import prepare_multipass
from opendisplay.exceptions import (
    DriveModeSubstitutedError,
    DriveModeTemperatureError,
    DriveSequenceUnsupportedError,
    InvalidResponseError,
    NeedsMigrationError,
    PanelBusyError,
)
from opendisplay.models.config import GlobalConfig
from opendisplay.protocol.commands import build_drive_write_start
from opendisplay.protocol.config_parser import parse_config_response
from opendisplay.protocol.config_serializer import serialize_config
from opendisplay.protocol.responses import parse_drive_write_start

ALPHA = parse_config_response(
    (Path(__file__).parent.parent / "fixtures" / "configs" / "bg22_alpha_drive_packets.bin").read_bytes()
)
# A 3.0 BG22 identity: target 0x03, no display records, flags 0x01, max_config 3120, 128 steps.
IDENT_BG22 = bytes.fromhex("004604030001300c800000")
ACK_DATA, ACK_END, REFRESHED = b"\x00\x71", b"\x00\x72", b"\x00\x73"
# SEC_STATUS of an unclaimed 3.0 device (connect() reads it before any handshake, piece 4).
SEC_UNCLAIMED = bytes.fromhex("005402000000000000441bf6c96440100e0100")


def _version(major: int = 3) -> bytes:
    return b"\x00\x43" + bytes([major, 0, 40]) + b"0" * 40 + b"\x02"


def _config_reply(config: GlobalConfig) -> bytes:
    blob = serialize_config(config)
    return b"\x00\x40\x00\x00" + len(blob).to_bytes(2, "little") + blob


def _streaming(config: GlobalConfig) -> GlobalConfig:
    """The config with streaming decompression advertised (transmission_modes bit 0x01)."""
    display = dataclasses.replace(config.displays[0], transmission_modes=config.displays[0].transmission_modes | 0x01)
    return dataclasses.replace(config, displays=[display])


def _image() -> Image.Image:
    image = Image.new("RGB", (184, 360))
    for x in range(184):
        for y in range(360):
            image.putpixel((x, y), ((x * 7) % 256, (y * 3) % 256, ((x + y) * 5) % 256))
    return image


async def _connected(fake_transport, config: GlobalConfig = ALPHA):  # noqa: ANN001, ANN202
    fake = fake_transport([_version(), IDENT_BG22, SEC_UNCLAIMED, _config_reply(config)])
    dev = opendisplay.connect(transport=fake)
    await dev.__aenter__()
    fake.written.clear()
    fake.timeouts.clear()
    return dev, fake


def _script_transfer(dev, fake, start_answer: bytes, payload_len: int, *, inline: int = 0) -> None:  # noqa: ANN001
    chunk = dev.legacy._channel.direct_chunk_size()  # noqa: SLF001
    chunks = -(-(payload_len - inline) // chunk) if payload_len > inline else 0
    fake._responses.extend([start_answer] + [ACK_DATA] * chunks + [ACK_END, REFRESHED])  # noqa: SLF001


# ── the wire format ──────────────────────────────────────────────────────────


def test_start_bytes_uncompressed() -> None:
    assert build_drive_write_start(1, 0) == bytes.fromhex("00850100000000")


def test_start_bytes_compressed_carry_the_size_and_the_first_bytes() -> None:
    cmd = build_drive_write_start(1, 1, uncompressed_size=49680, first_chunk=b"\x78\x01\xaa")

    assert cmd == bytes.fromhex("00850101010000") + (49680).to_bytes(4, "little") + b"\x78\x01\xaa"


def test_start_answer_is_parsed() -> None:
    assert parse_drive_write_start(bytes.fromhex("0085010319")) == (1, 3, 25)
    assert parse_drive_write_start(bytes.fromhex("008501037f")) == (1, 3, None)  # 0x7F: unknown
    assert parse_drive_write_start(bytes.fromhex("00850103f6")) == (1, 3, -10)  # int8


@pytest.mark.parametrize(
    ("err", "error"),
    [
        (0x06, DriveModeTemperatureError),
        (0x07, DriveSequenceUnsupportedError),
        (0x08, PanelBusyError),
        (0x02, NeedsMigrationError),
        (0x03, InvalidResponseError),
    ],
)
def test_each_refusal_maps_to_its_error(err: int, error: type[Exception]) -> None:
    with pytest.raises(error):
        parse_drive_write_start(bytes([0xFF, 0x85, err, 0x01]))


def test_an_unnamed_refusal_names_its_code() -> None:
    with pytest.raises(InvalidResponseError, match="0x03"):
        parse_drive_write_start(bytes([0xFF, 0x85, 0x03, 0x01]))


# ── the transfer ─────────────────────────────────────────────────────────────


async def test_colour16_upload_sends_start_all_passes_and_end(fake_transport) -> None:
    dev, fake = await _connected(fake_transport)
    frames, _ = prepare_multipass(_image(), ALPHA, next(m for m in ALPHA.display_modes if m.name == "colour16"))
    _script_transfer(dev, fake, bytes.fromhex("0085010319"), len(frames))

    await dev.display.show(_image(), mode="colour16")

    assert fake.written[0] == bytes.fromhex("00850100000000")  # BG22 here: no compression
    data = b"".join(frame[2:] for frame in fake.written[1:-1])
    assert all(frame[:2] == b"\x00\x71" for frame in fake.written[1:-1])
    assert data == frames
    assert fake.written[-1][:2] == b"\x00\x72"
    await dev.__aexit__(None, None, None)


async def test_mode_names_match_case_insensitively(fake_transport) -> None:
    dev, fake = await _connected(fake_transport)
    frames, _ = prepare_multipass(_image(), ALPHA, next(m for m in ALPHA.display_modes if m.name == "colour16"))
    _script_transfer(dev, fake, bytes.fromhex("0085010319"), len(frames))

    await dev.display.show(_image(), mode="Colour16")

    assert fake.written[0][2] == 1


async def test_compressed_start_carries_the_first_bytes_and_timeouts_follow_the_answer(fake_transport) -> None:
    config = _streaming(ALPHA)
    dev, fake = await _connected(fake_transport, config)
    frames, _ = prepare_multipass(_image(), config, next(m for m in config.display_modes if m.name == "colour16"))
    from opendisplay.encoding import compress_image_data

    stream = compress_image_data(frames, level=6, window_bits=9)  # the transfer's own, deterministic
    _script_transfer(dev, fake, bytes.fromhex("0085010319"), len(stream), inline=200 - 11)

    await dev.display.show(_image(), mode="colour16")

    start = fake.written[0]
    assert start[:7] == bytes.fromhex("00850100010000")  # flags bit0 COMPRESSED
    assert int.from_bytes(start[7:11], "little") == len(frames)
    sent = start[11:] + b"".join(f[2:] for f in fake.written[1:] if f[:2] == b"\x00\x71")
    assert zlib.decompressobj().decompress(sent) == frames
    assert fake.timeouts[0] == 10 + max(90, (3 - 1) * 45)  # START with inline data
    end_index = next(i for i, f in enumerate(fake.written) if f[:2] == b"\x00\x72")
    assert fake.timeouts[end_index] == max(90, 3 * 45)  # END: one refresh per pass


async def test_a_substituted_mode_sends_nothing_more_and_raises(fake_transport) -> None:
    dev, fake = await _connected(fake_transport)
    fake._responses.append(bytes.fromhex("0085000119"))  # the device runs mode 0 instead  # noqa: SLF001

    with pytest.raises(DriveModeSubstitutedError) as err:
        await dev.display.show(_image(), mode="colour16")

    assert (err.value.requested, err.value.actual, err.value.temp_c) == ("colour16", 0, 25)
    assert len(fake.written) == 1


async def test_a_pass_count_other_than_the_config_raises(fake_transport) -> None:
    dev, fake = await _connected(fake_transport)
    fake._responses.append(bytes.fromhex("0085010219"))  # device expects 2 frames, we encoded 3  # noqa: SLF001

    with pytest.raises(InvalidResponseError, match="pass"):
        await dev.display.show(_image(), mode="colour16")

    assert len(fake.written) == 1


async def test_a_temperature_refusal_names_the_window(fake_transport) -> None:
    dev, fake = await _connected(fake_transport)
    fake._responses.append(bytes([0xFF, 0x85, 0x06, 0x01]))  # noqa: SLF001

    with pytest.raises(DriveModeTemperatureError, match="colour16"):
        await dev.display.show(_image(), mode="colour16")


@pytest.mark.parametrize("name", ["fast", "partial", "FAST"])
async def test_reserved_names_are_rejected_before_any_io(fake_transport, name: str) -> None:
    dev, fake = await _connected(fake_transport)

    with pytest.raises(ValueError, match="refresh|partial"):
        await dev.display.show(_image(), mode=name)

    assert fake.written == []


async def test_an_unknown_name_lists_the_devices_modes(fake_transport) -> None:
    dev, fake = await _connected(fake_transport)

    with pytest.raises(ValueError, match="full, colour16"):
        await dev.display.show(_image(), mode="sepia")

    assert fake.written == []


async def test_a_single_pass_named_mode_sends_the_normal_frame(fake_transport) -> None:
    dev, fake = await _connected(fake_transport)
    frames, _ = prepare_multipass(_image(), ALPHA, next(m for m in ALPHA.display_modes if m.name == "full"))
    _script_transfer(dev, fake, bytes.fromhex("0085000119"), len(frames))

    await dev.display.show(_image(), mode="full")

    assert fake.written[0] == bytes.fromhex("00850000000000")
    assert b"".join(frame[2:] for frame in fake.written[1:-1]) == frames


async def test_show_without_a_mode_is_todays_upload(fake_transport, monkeypatch) -> None:
    dev, fake = await _connected(fake_transport)
    calls: list[tuple] = []

    async def upload_image(self: OpenDisplayDevice, image: Image.Image, **kwargs: object) -> Image.Image:
        calls.append((image, kwargs))
        return image

    monkeypatch.setattr(OpenDisplayDevice, "upload_image", upload_image)
    image = _image()

    result = await dev.display.show(image, dither_mode=opendisplay.DitherMode.NONE)

    assert result is None
    assert len(calls) == 1 and calls[0][0] is image
    assert calls[0][1]["dither_mode"] is opendisplay.DitherMode.NONE
    assert calls[0][1]["refresh_mode"] is opendisplay.RefreshMode.FULL
    assert fake.written == []


async def test_a_refused_end_raises(fake_transport) -> None:
    dev, fake = await _connected(fake_transport)
    frames, _ = prepare_multipass(_image(), ALPHA, next(m for m in ALPHA.display_modes if m.name == "full"))
    _script_transfer(dev, fake, bytes.fromhex("0085000119"), len(frames))
    fake._responses[-2] = b"\xff\x72"  # END refused instead of acknowledged  # noqa: SLF001

    with pytest.raises(InvalidResponseError):
        await dev.display.show(_image(), mode="full")


# ── review fixes (piece 3b review I1 I2 M1) ──────────────────────────────────


async def test_encoding_and_compression_run_off_the_event_loop(fake_transport, monkeypatch) -> None:  # I1
    import threading

    import opendisplay.device.api as api

    config = _streaming(ALPHA)
    dev, fake = await _connected(fake_transport, config)
    threads: dict[str, threading.Thread] = {}
    real_prepare, real_compress = api.prepare_multipass, api.compress_image_data

    def prepare(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        threads["prepare"] = threading.current_thread()
        return real_prepare(*args, **kwargs)

    def compress(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        threads["compress"] = threading.current_thread()
        return real_compress(*args, **kwargs)

    monkeypatch.setattr(api, "prepare_multipass", prepare)
    monkeypatch.setattr(api, "compress_image_data", compress)
    frames, _ = real_prepare(_image(), config, next(m for m in config.display_modes if m.name == "colour16"))
    from opendisplay.encoding import compress_image_data

    _script_transfer(
        dev,
        fake,
        bytes.fromhex("0085010319"),
        len(compress_image_data(frames, level=6, window_bits=9)),
        inline=200 - 11,
    )

    await dev.display.show(_image(), mode="colour16")

    assert threads["prepare"] is not threading.main_thread()
    assert threads["compress"] is not threading.main_thread()


async def test_an_option_the_mode_path_cannot_take_is_a_type_error_before_any_io(fake_transport) -> None:  # I2
    dev, fake = await _connected(fake_transport)

    with pytest.raises(TypeError):
        await dev.display.show(_image(), mode="colour16", tone=0.5)  # type: ignore[call-arg]

    assert fake.written == []


async def test_a_mode_upload_returns_the_panel_temperature(fake_transport) -> None:  # I2
    dev, fake = await _connected(fake_transport)
    frames, _ = prepare_multipass(_image(), ALPHA, next(m for m in ALPHA.display_modes if m.name == "full"))
    _script_transfer(dev, fake, bytes.fromhex("0085000119"), len(frames))

    assert await dev.display.show(_image(), mode="full") == 25


async def test_the_mode_of_display_0_is_used_not_another_displays(fake_transport) -> None:  # M1
    from opendisplay.models.panel_drive import LengthPrefixedPacket

    other = next(p for p in ALPHA.length_prefixed_packets if p.packet_type == 0xAE and p.body[1] == 1)
    body = bytearray(other.body)
    body[0] = 1  # the same mode_index, for display instance 1
    config = dataclasses.replace(
        ALPHA,
        length_prefixed_packets=[
            LengthPrefixedPacket(number=9, packet_type=0xAE, body=bytes(body)),
            *ALPHA.length_prefixed_packets,
        ],
    )
    dev, _ = await _connected(fake_transport, config)

    assert dev.display.drive_mode("colour16").instance_number == 0


async def test_nominal_colours_are_reported_for_an_unmeasured_multi_pass_mode(fake_transport) -> None:  # M3
    dev, _ = await _connected(fake_transport)

    assert dev.display.nominal_colours(dev.display.drive_mode("colour16")) is True
    assert dev.display.nominal_colours(dev.display.drive_mode("full")) is False
