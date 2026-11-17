"""SEC2 in opendisplay.connect() and Device.security (piece 4, Task 7), against a reference device."""

from __future__ import annotations

import secrets

import pytest
from refdevice import RefDevice

import opendisplay
from opendisplay import DeviceKey, LegacyKey, OpenDisplayDevice
from opendisplay.exceptions import NoStoredConfigError, Sec2RequiredError
from opendisplay.models.profile import Generation
from opendisplay.sec2 import errors
from opendisplay.sec2.keystore import MemoryKeyStore
from opendisplay.sec2.wire import SecState

NAME = "OD4064C9"


def _claimed(**kwargs: object) -> tuple[RefDevice, bytes]:
    key = secrets.token_bytes(16)
    return RefDevice(k_dev=key, **kwargs), key  # type: ignore[arg-type]


async def test_a_claimed_device_opens_a_sec2_session_with_the_stored_key() -> None:
    device, key = _claimed()
    store = MemoryKeyStore()
    store.promote(NAME, key, uid=device.uid)

    async with opendisplay.connect(transport=device, key_store=store) as dev:
        assert dev.profile.generation is Generation.V3
        assert dev.security.status is not None and dev.security.status.state is SecState.CLAIMED
        assert dev.security.sec2_session_open
        with pytest.raises(NoStoredConfigError):  # answered, sealed, under the session
            await dev.config.read()
        assert device.written[-1][:2] == b"\x00\x40" and len(device.written[-1]) == 18  # a sealed request

    assert device.written[0][:2] == b"\x00\x43"  # the firmware version first
    assert device.written[1][:2] == b"\x00\x54"  # then SEC_STATUS, before any handshake
    assert b"\x00\x50" not in [f[:2] for f in device.written]  # never the 2.x handshake


async def test_a_device_key_given_directly_works_without_a_store() -> None:
    device, key = _claimed()

    async with opendisplay.connect(transport=device, encryption_key=DeviceKey(key)) as dev:
        assert dev.security.sec2_session_open


async def test_a_plain_key_is_tried_with_the_scheme_the_device_speaks() -> None:
    device, key = _claimed()

    async with opendisplay.connect(transport=device, encryption_key=key) as dev:
        assert dev.security.sec2_session_open


async def test_wrong_keys_open_no_session_and_keep_every_key() -> None:  # Review Focus 1
    device, _ = _claimed()
    store = MemoryKeyStore()
    wrong = [secrets.token_bytes(16) for _ in range(2)]
    for key in wrong:
        store.add_candidate(NAME, key, "user")

    with pytest.raises(errors.Sec2KeyRequiredError):
        async with opendisplay.connect(transport=device, key_store=store):
            pytest.fail("entered without a session")

    assert sorted(store.keys_to_try(NAME)) == sorted(wrong)  # nothing deleted on a refusal
    assert not device.is_connected


async def test_up_to_three_keys_are_tried_and_the_working_one_is_promoted() -> None:
    device, key = _claimed()
    store = MemoryKeyStore()
    store.add_candidate(NAME, key, "user")
    for _ in range(2):
        store.add_candidate(NAME, secrets.token_bytes(16), "user")  # newer, tried first

    async with opendisplay.connect(transport=device, key_store=store) as dev:
        assert dev.security.sec2_session_open

    assert store.keys_to_try(NAME) == [key]
    assert store.uid(NAME) == device.uid
    hellos = [f for f in device.written if f[:3] == b"\x00\x56\x01" and f[3] & 0x80]
    assert len(hellos) == 3


async def test_an_unclaimed_device_needs_no_handshake() -> None:
    device = RefDevice()

    async with opendisplay.connect(transport=device) as dev:
        assert dev.security.status is not None and dev.security.status.state is SecState.UNCLAIMED
        assert not dev.security.sec2_session_open

    assert all(f[:2] != b"\x00\x56" for f in device.written)


async def test_a_migrated_2x_key_is_rotated_by_default() -> None:
    device, key = _claimed(key_legacy=True)
    store = MemoryKeyStore()
    store.promote(NAME, key, uid=device.uid)

    async with opendisplay.connect(transport=device, key_store=store) as dev:
        assert dev.security.sec2_session_open

    assert device.k_dev != key and not device.key_legacy
    assert store.keys_to_try(NAME) == [device.k_dev]


async def test_rotation_can_be_turned_off() -> None:
    device, key = _claimed(key_legacy=True)
    store = MemoryKeyStore()
    store.promote(NAME, key, uid=device.uid)

    async with opendisplay.connect(transport=device, key_store=store, rotate_legacy_key=False):
        pass

    assert device.k_dev == key and store.keys_to_try(NAME) == [key]


async def test_without_a_store_a_migrated_key_is_not_rotated(caplog: pytest.LogCaptureFixture) -> None:
    device, key = _claimed(key_legacy=True)

    async with opendisplay.connect(transport=device, encryption_key=key):
        pass

    assert device.k_dev == key  # rotating without a place to keep the new key would lose it
    assert "not rotating" in caplog.text.lower()


async def test_claim_rotate_and_unclaim_through_device_security() -> None:
    device = RefDevice(claim_mode=True)
    store = MemoryKeyStore()

    async with opendisplay.connect(transport=device, key_store=store) as dev:
        flow = await dev.security.claim()
        await flow.begin()
        code = await flow.exchange_nonces()
        assert code.code == device.shown_code
        await flow.confirm(code.display)
        assert dev.security.sec2_session_open and device.claimed
        assert store.keys_to_try(NAME) == [device.k_dev]
        with pytest.raises(NoStoredConfigError):  # answered under the claim's session
            await dev.config.read()
        assert len(device.written[-1]) == 18  # sealed: [00][40][ctr:4][tag:12]

        old = device.k_dev
        new = await dev.security.rotate_key()
        assert new == device.k_dev != old and store.keys_to_try(NAME) == [new]
        assert dev.security.sec2_session_open

        await dev.security.unclaim()
        assert not device.claimed and store.keys_to_try(NAME) == []
        assert not dev.security.sec2_session_open


async def test_use_without_a_key_ends_claim_mode() -> None:
    device = RefDevice(claim_mode=True)

    async with opendisplay.connect(transport=device) as dev:
        await dev.security.use_without_key()

    assert not device.claim_mode


async def test_claiming_needs_a_key_store() -> None:
    device = RefDevice(claim_mode=True)

    async with opendisplay.connect(transport=device) as dev:
        with pytest.raises(ValueError, match="key store"):
            await dev.security.claim()


async def test_a_2x_key_on_a_claimed_3_0_device_through_the_old_api_is_typed() -> None:
    device, _ = _claimed()

    with pytest.raises(Sec2RequiredError, match="SEC2"):
        async with OpenDisplayDevice(transport=device, encryption_key=bytes(16), auto_interrogate=False):
            pytest.fail("entered")


async def test_a_legacy_key_on_2x_firmware_uses_the_2x_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    device = RefDevice()
    device._plaintext = lambda cmd: device._queue.append(  # type: ignore[method-assign]  # noqa: SLF001
        b"\x00\x43\x02\x1b\x28" + b"0" * 40 + b"\x00" if cmd == 0x0043 else b""
    )
    used: list[bytes] = []

    async def legacy_authenticate(self: OpenDisplayDevice, key: bytes) -> None:
        used.append(key)

    monkeypatch.setattr(OpenDisplayDevice, "authenticate", legacy_authenticate)
    key = secrets.token_bytes(16)

    async with opendisplay.connect(transport=device, encryption_key=LegacyKey(key)) as dev:
        assert dev.profile.generation is Generation.V2

    assert used == [key]
    assert device.written[0][:2] == b"\x00\x43"  # the version is read before the 2.x handshake


def test_typed_credentials_check_their_length() -> None:
    with pytest.raises(ValueError, match="16"):
        DeviceKey(bytes(15))
    with pytest.raises(ValueError, match="16"):
        LegacyKey(bytes(17))
