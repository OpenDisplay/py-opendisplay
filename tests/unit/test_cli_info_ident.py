"""How ``opendisplay info`` decides to ask for the identity, and what it does when things go wrong."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from opendisplay import cli
from opendisplay.exceptions import InvalidResponseError, NoStoredConfigError, OpenDisplayTimeoutError
from opendisplay.models.enums import ICType
from opendisplay.protocol.responses import parse_read_ident

IDENT = parse_read_ident(bytes.fromhex("004604010029a00f800003"))
FW3 = {"major": 3, "minor": 0, "patch": 0, "sha": "0" * 40}
FW2 = {"major": 2, "minor": 27, "patch": 1, "sha": "a" * 40}


class _Opened:
    """What ``cli._open_device`` returns: a connected ``Device`` whose ``legacy`` is the fake."""

    def __init__(self, legacy: object) -> None:
        self.legacy = legacy

    async def __aenter__(self) -> _Opened:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Device:
    """Counts identity reads; ``ic_type`` None stands for a device without a config."""

    def __init__(self, ic_type: int | None, error: Exception | None = None) -> None:
        self.config = SimpleNamespace(system=SimpleNamespace(ic_type=ic_type)) if ic_type is not None else None
        self.ident_reads = 0
        self._error = error

    async def read_ident(self):
        self.ident_reads += 1
        if self._error:
            raise self._error
        return IDENT


def test_2x_firmware_is_not_asked_for_its_identity() -> None:
    """2.x ESP32 and nRF firmware stays silent on READ_IDENT; asking would cost every run a timeout."""
    device = _Device(ICType.ESP32_S3)

    assert asyncio.run(cli._read_ident(device, FW2)) is None
    assert device.ident_reads == 0


@pytest.mark.parametrize(("fw", "ic_type"), [(FW3, ICType.ESP32_S3), (FW3, None), (FW2, ICType.EFR32BG22)])
def test_firmware_that_can_answer_is_asked(fw: dict, ic_type: int | None) -> None:
    device = _Device(ic_type)

    assert asyncio.run(cli._read_ident(device, fw)) is IDENT
    assert device.ident_reads == 1


def test_a_broken_identity_reply_does_not_cost_the_rest_of_the_report() -> None:
    device = _Device(ICType.EFR32BG22, error=InvalidResponseError("READ_IDENT tail cut short"))

    assert asyncio.run(cli._read_ident(device, FW2)) is None


def test_info_on_a_device_without_config_reports_firmware_and_identity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeDevice:
        mac_address = "AA:BB:CC:DD:EE:FF"
        device_name = "OD000000"
        config = None

        def __init__(self, **kwargs: object) -> None:
            self._skip_interrogation = "capabilities" in kwargs

        async def __aenter__(self) -> FakeDevice:
            if not self._skip_interrogation:
                raise NoStoredConfigError()
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def read_firmware_version(self):
            return FW3

        async def read_ident(self):
            return IDENT

        async def read_sec_status(self):  # firmware that does not answer SEC_STATUS
            raise OpenDisplayTimeoutError("silent")

    monkeypatch.setattr(cli, "_open_device", lambda kwargs: _Opened(FakeDevice(capabilities=None)))

    asyncio.run(cli._info({}, output_json=True))

    report = json.loads(capsys.readouterr().out)
    assert report["display"] is None
    assert report["ident"]["fw_target"] == "ESP32"
    assert report["firmware"]["major"] == 3
