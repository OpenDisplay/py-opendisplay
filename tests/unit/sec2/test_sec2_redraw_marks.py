"""SEC2 actions after which the firmware redraws note it (Firmware_Unified f6d3a83, design doc table)."""

from __future__ import annotations

import secrets
import types

import pytest
from refdevice import RefDevice

import opendisplay
from opendisplay import redraw
from opendisplay.sec2 import wire
from opendisplay.sec2.errors import ClaimAttemptsExhaustedError
from opendisplay.sec2.keystore import MemoryKeyStore

NAME = "OD4064C9"
ADDRESS = "AA:BB:CC:DD:EE:FF"


def _device(**kwargs: object) -> RefDevice:
    device = RefDevice(**kwargs)  # type: ignore[arg-type]
    device.mac_address = ADDRESS  # type: ignore[attr-defined]
    return device


async def test_a_confirmed_claim_notes_the_reboot_into_normal_mode() -> None:
    device = _device(claim_mode=True)

    async with opendisplay.connect(transport=device, key_store=MemoryKeyStore()) as dev:
        flow = await dev.security.claim()
        await flow.begin()
        code = await flow.exchange_nonces()
        assert redraw.remaining(ADDRESS) == 0  # nothing ends claim mode before CONFIRM
        await flow.confirm(code.display)

    assert redraw.remaining(ADDRESS) > 0


async def test_a_wrong_code_notes_nothing() -> None:
    device = _device(claim_mode=True)

    async with opendisplay.connect(transport=device, key_store=MemoryKeyStore()) as dev:
        flow = await dev.security.claim()
        await flow.begin()
        code = await flow.exchange_nonces()
        with pytest.raises(Exception, match="does not match"):
            await flow.confirm(f"{(int(code.code) + 1) % 1_000_000:06d}")

    assert redraw.remaining(ADDRESS) == 0  # ABORT keeps claim mode: no reboot


async def test_using_the_display_without_a_key_notes_the_reboot() -> None:
    async with opendisplay.connect(transport=_device(claim_mode=True)) as dev:
        await dev.security.use_without_key()

    assert redraw.remaining(ADDRESS) > 0


async def test_attempts_exhausted_notes_the_reboot() -> None:
    device = _device(claim_mode=True)

    def exhausted(sub: int, body: bytes) -> None:
        device._error(0x55, sub, wire.SecErr.ATTEMPTS_EXHAUSTED)  # noqa: SLF001

    device._claim = exhausted  # type: ignore[method-assign]  # noqa: SLF001

    async with opendisplay.connect(transport=device, key_store=MemoryKeyStore()) as dev:
        flow = await dev.security.claim()
        with pytest.raises(ClaimAttemptsExhaustedError):
            await flow.begin()

    assert redraw.remaining(ADDRESS) > 0


def _show_key(dev: opendisplay.Device, shown: bool) -> None:
    security = types.SimpleNamespace(show_key_on_screen=shown)
    dev.config._cached = types.SimpleNamespace(security_config=security)  # type: ignore[assignment]  # noqa: SLF001


@pytest.mark.parametrize(("shown", "noted"), [(True, True), (False, False)])
async def test_a_rotation_notes_a_redraw_only_while_the_key_is_shown(shown: bool, noted: bool) -> None:
    key = secrets.token_bytes(16)
    device = _device(k_dev=key)
    store = MemoryKeyStore()
    store.promote(NAME, key, uid=device.uid)

    async with opendisplay.connect(transport=device, key_store=store) as dev:
        _show_key(dev, shown)
        await dev.security.rotate_key()

    assert (redraw.remaining(ADDRESS) > 0) is noted


async def test_unclaim_notes_nothing() -> None:  # od_sec2.c: state change only, no redraw, no reboot
    key = secrets.token_bytes(16)
    device = _device(k_dev=key)
    store = MemoryKeyStore()
    store.promote(NAME, key, uid=device.uid)

    async with opendisplay.connect(transport=device, key_store=store) as dev:
        await dev.security.unclaim()

    assert redraw.remaining(ADDRESS) == 0
