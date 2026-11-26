"""Handler refusals inside a SEC2 session (Firmware_Unified 6ad0ee0, SECURITY_V2.md § 9.5).

From 6ad0ee0 the device seals a refusal to a gated command while a session is live, so it
reaches the caller as its typed error. Older 3.0 firmware sent it unsealed: the session must
still discard it, and the timeout that follows names the discarded refusal.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from refdevice import RefDevice

import opendisplay
from opendisplay.exceptions import (
    ConfigKeyMismatchError,
    ConfigSecretNeedsClaimError,
    ConfigWriteBusyError,
    OpenDisplayTimeoutError,
)
from opendisplay.protocol.config_parser import parse_config_response
from opendisplay.sec2.keystore import MemoryKeyStore

NAME = "OD4064C9"
WRITE_CONFIG = 0x0041
CONFIG = parse_config_response(
    (Path(__file__).parents[2] / "fixtures" / "configs" / "bg22_alpha_drive_packets.bin").read_bytes()
)


def _claimed() -> tuple[RefDevice, MemoryKeyStore]:
    key = secrets.token_bytes(16)
    device = RefDevice(k_dev=key)
    store = MemoryKeyStore()
    store.promote(NAME, key, uid=device.uid)
    return device, store


@pytest.mark.parametrize(
    ("err", "error"),
    [(0x11, ConfigKeyMismatchError), (0x10, ConfigSecretNeedsClaimError), (0x01, ConfigWriteBusyError)],
)
async def test_a_sealed_config_write_refusal_raises_its_typed_error(err: int, error: type[Exception]) -> None:
    device, store = _claimed()
    device.refusals[WRITE_CONFIG] = bytes([0xFF, 0x41, err, 0x00])

    async with opendisplay.connect(transport=device, key_store=store) as dev:
        assert dev.security.sec2_session_open
        with pytest.raises(error):
            await dev.legacy.write_config(CONFIG)


async def test_an_unsealed_refusal_is_discarded_and_named_in_the_timeout() -> None:
    device, store = _claimed()
    device.refusals[WRITE_CONFIG] = bytes([0xFF, 0x41, 0x11, 0x00])
    device.seal_refusals = False

    async with opendisplay.connect(transport=device, key_store=store) as dev:
        with pytest.raises(OpenDisplayTimeoutError, match=r"unsealed refusal ff411100.*6ad0ee0") as caught:
            await dev.legacy.write_config(CONFIG)

    assert not isinstance(caught.value, ConfigKeyMismatchError)
