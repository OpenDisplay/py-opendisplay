"""The SEC2 key store: key retention rules of SECURITY_V2.md § 8.8, mirrored from od-keystore.js."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from opendisplay.sec2.errors import Sec2StorageError
from opendisplay.sec2.keystore import JsonFileKeyStore, KeyStore, MemoryKeyStore

K1, K2, K3, K4 = (bytes([i]) * 16 for i in (1, 2, 3, 4))
UID = bytes.fromhex("0000441bf6c96440")


@pytest.fixture(params=["memory", "json"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> KeyStore:
    if request.param == "memory":
        return MemoryKeyStore()
    return JsonFileKeyStore(tmp_path / "keys.json")


def test_an_unknown_device_has_no_keys(store: KeyStore) -> None:
    assert store.keys_to_try("OD4064C9") == []
    assert store.uid("OD4064C9") is None


def test_a_staged_key_is_tried_and_promoting_it_drops_the_others(store: KeyStore) -> None:
    store.add_candidate("OD4064C9", K1, "user")
    store.stage("OD4064C9", K2, "claim")

    assert store.keys_to_try("OD4064C9") == [K2, K1]  # the pending key first

    store.promote("OD4064C9", K2, uid=UID)

    assert store.keys_to_try("OD4064C9") == [K2]
    assert store.uid("OD4064C9") == UID


def test_keys_to_try_is_at_most_three_confirmed_first(store: KeyStore) -> None:
    store.promote("OD4064C9", K1, uid=UID)
    for key in (K2, K3, K4):
        store.add_candidate("OD4064C9", key, "user")

    assert store.keys_to_try("OD4064C9") == [K1, K4, K3]


def test_staging_keeps_the_confirmed_key_until_the_new_one_is_proven(store: KeyStore) -> None:  # Review Focus 5
    store.promote("OD4064C9", K1, uid=UID)
    store.stage("OD4064C9", K2, "rotation")

    assert store.keys_to_try("OD4064C9") == [K1, K2]


def test_forget_removes_every_key(store: KeyStore) -> None:
    store.promote("OD4064C9", K1, uid=UID)
    store.forget("OD4064C9")

    assert store.keys_to_try("OD4064C9") == []


def test_a_key_of_the_wrong_length_is_refused(store: KeyStore) -> None:
    with pytest.raises(ValueError, match="16"):
        store.stage("OD4064C9", bytes(15), "claim")


def test_the_json_store_survives_a_new_instance_and_is_private(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "keys.json"
    JsonFileKeyStore(path).stage("OD4064C9", K2, "claim")
    JsonFileKeyStore(path).promote("OD4064C9", K2, uid=UID)

    assert JsonFileKeyStore(path).keys_to_try("OD4064C9") == [K2]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())["format"] == 1


def test_an_unreadable_json_store_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    path.write_text("{not json")

    store = JsonFileKeyStore(path)
    with pytest.raises(Sec2StorageError, match="unreadable"):
        store.stage("OD4064C9", K1, "claim")
    assert path.read_text() == "{not json"


def test_staging_fails_loudly_when_the_store_cannot_be_written(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(Sec2StorageError):
            JsonFileKeyStore(locked / "keys.json").stage("OD4064C9", K1, "claim")
    finally:
        locked.chmod(0o700)


def test_keys_are_not_in_the_repr() -> None:
    store = MemoryKeyStore()
    store.promote("OD4064C9", K1, uid=UID)

    assert K1.hex() not in repr(store)
