"""opendisplay.connect() on firmware that does not follow the 3.0 connect order.

- Firmware_Silabs 2.x with encryption on gates every command behind the session, the firmware
  version read included: it answers ``[00][43][FE]``.
- A 3.0 build without the SEC2 module (the nRF52811 port) reports READ_IDENT SEC_V2 = 0 and does
  not know SEC_STATUS (SECURITY_V2.md § 13.6), so it never answers 0x0054.
"""

from __future__ import annotations

import secrets

import pytest
from refdevice import FW_3_0_2, RefDevice

import opendisplay
from opendisplay import LegacyKey, OpenDisplayDevice
from opendisplay.exceptions import AuthenticationRequiredError
from opendisplay.models.profile import Generation

FW_2_3_0 = b"\x00\x43\x02\x03\x28" + b"0" * 40 + b"\x00"
IDENT_NO_SEC2 = bytes.fromhex("004604010001a00f800003")  # flags 0x01: config driven, SEC_V2 = 0


class GatedLegacyDevice(RefDevice):
    """A 2.x BG22 with encryption on: nothing is answered before the 2.x handshake."""

    def __init__(self) -> None:
        super().__init__()
        self.authenticated = False

    def _plaintext(self, cmd: int) -> None:
        if cmd == 0x0043:
            self._queue.append(FW_2_3_0 if self.authenticated else b"\x00\x43\xfe")


class NoSec2Device(RefDevice):
    """A 3.0 build without the SEC2 module."""

    def _plaintext(self, cmd: int) -> None:
        if cmd == 0x0043:
            self._queue.append(FW_3_0_2)
        elif cmd == 0x0046:
            self._queue.append(IDENT_NO_SEC2)
        # 0x0054 is an unknown opcode here: no answer


@pytest.fixture
def legacy_handshake(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Stand in for the 2.x handshake; marks the device's session open."""
    used: list[bytes] = []

    async def authenticate(self: OpenDisplayDevice, key: bytes) -> None:
        used.append(key)
        self._conn.authenticated = True  # type: ignore[attr-defined]

    monkeypatch.setattr(OpenDisplayDevice, "authenticate", authenticate)
    return used


@pytest.mark.parametrize("wrap", [LegacyKey, bytes])
async def test_a_gated_version_read_runs_the_2x_handshake_first(
    wrap: type[LegacyKey] | type[bytes], legacy_handshake: list[bytes]
) -> None:
    device = GatedLegacyDevice()
    key = secrets.token_bytes(16)

    async with opendisplay.connect(transport=device, encryption_key=wrap(key)) as dev:
        assert dev.profile.generation is Generation.V2
        assert dev.profile.firmware["minor"] == 3

    assert legacy_handshake == [key]  # once: not again after the version read


async def test_a_gated_version_read_without_a_key_asks_for_one() -> None:
    with pytest.raises(AuthenticationRequiredError):
        async with opendisplay.connect(transport=GatedLegacyDevice()):
            pytest.fail("entered without a session")


async def test_the_old_api_reports_a_gated_version_read_as_needing_a_key() -> None:
    async with OpenDisplayDevice(transport=GatedLegacyDevice(), auto_interrogate=False) as device:
        with pytest.raises(AuthenticationRequiredError):
            await device.read_firmware_version()


async def test_a_3_0_device_without_sec2_connects_without_sec_status() -> None:
    device = NoSec2Device()

    async with opendisplay.connect(transport=device) as dev:
        assert dev.profile.generation is Generation.V3
        assert dev.security.status is None
        assert not dev.security.sec2_session_open

    assert b"\x00\x54" not in [f[:2] for f in device.written]


async def test_a_key_for_a_3_0_device_without_sec2_is_reported_unused(caplog: pytest.LogCaptureFixture) -> None:
    async with opendisplay.connect(transport=NoSec2Device(), encryption_key=bytes(16)) as dev:
        assert not dev.security.sec2_session_open

    assert "not used" in caplog.text


async def test_on_3_0_the_identity_is_read_before_sec_status() -> None:
    device = RefDevice()

    async with opendisplay.connect(transport=device):
        pass

    opcodes = [f[:2] for f in device.written]
    assert opcodes[:3] == [b"\x00\x43", b"\x00\x46", b"\x00\x54"]
