"""Reading SEC_STATUS (0x0054) from a device and showing it in `opendisplay info` (SECURITY_V2.md § 5.1)."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from rich.console import Console
from test_cli_info_report import _config, _ctx

from opendisplay import OpenDisplayDevice, cli
from opendisplay.cli_info import _build_info_tree, _info_to_json
from opendisplay.exceptions import OpenDisplayTimeoutError
from opendisplay.sec2.wire import SecState, parse_sec_status

E1002_FRESH = bytes.fromhex("005402002135020000441bf6c96440100e0803")


async def test_read_sec_status_sends_the_plaintext_request_and_parses_the_answer(fake_transport) -> None:
    fake = fake_transport([E1002_FRESH])
    async with OpenDisplayDevice(transport=fake, auto_interrogate=False) as device:
        status = await device.read_sec_status()

    assert fake.written == [bytes([0x00, 0x54])]
    assert status.state is SecState.UNCLAIMED and status.claim_mode


async def test_a_silent_device_times_out(fake_transport) -> None:
    fake = fake_transport([OpenDisplayTimeoutError("silent")])
    async with OpenDisplayDevice(transport=fake, auto_interrogate=False) as device:
        with pytest.raises(OpenDisplayTimeoutError):
            await device.read_sec_status()


def _text(ctx) -> str:  # noqa: ANN001
    console = Console(width=120, record=True, force_terminal=False)
    console.print(_build_info_tree(ctx))
    return console.export_text()


def test_info_shows_the_security_state() -> None:
    ctx = dataclasses.replace(_ctx(_config()), sec_status=parse_sec_status(E1002_FRESH))

    text = _text(ctx)
    assert "Device security" in text
    assert "UNCLAIMED" in text and "claim mode, 565 s left, 3 codes left" in text
    assert "0000441bf6c96440" in text
    assert _info_to_json(ctx)["sec2"] == {
        "sec_ver": 2,
        "state": "UNCLAIMED",
        "claim_mode": True,
        "claim_window_s": 565,
        "renders_left": 3,
        "uid": "0000441bf6c96440",
        "session_timeout_s": 3600,
        "rotation_suggested": False,
        "key_legacy": False,
    }


def test_info_without_a_security_state_has_no_section() -> None:
    ctx = _ctx(_config())

    assert "Device security" not in _text(ctx)
    assert _info_to_json(ctx)["sec2"] is None


def test_info_does_not_ask_2x_firmware() -> None:
    class _Device:
        async def read_sec_status(self):  # noqa: ANN202
            pytest.fail("asked a 2.x device")

    assert asyncio.run(cli._read_sec_status(_Device(), {"major": 2, "minor": 27, "patch": 0, "sha": ""})) is None  # type: ignore[arg-type]


def test_info_survives_a_broken_status_answer() -> None:
    from opendisplay.exceptions import InvalidResponseError

    class _Device:
        async def read_sec_status(self):  # noqa: ANN202
            raise InvalidResponseError("garbled")

    assert asyncio.run(cli._read_sec_status(_Device(), {"major": 3, "minor": 0, "patch": 2, "sha": ""})) is None  # type: ignore[arg-type]
