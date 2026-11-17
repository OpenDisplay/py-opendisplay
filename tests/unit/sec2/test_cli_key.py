"""`opendisplay key export` / `key import`: moving a device key between clients (SECURITY_V2.md § 10.7)."""

from __future__ import annotations

from pathlib import Path

import pytest
from refdevice import RefDevice

import opendisplay
from opendisplay import cli, cli_sec2
from opendisplay.landing import build_landing_url
from opendisplay.sec2.errors import Sec2KeyRequiredError
from opendisplay.sec2.keystore import JsonFileKeyStore, MemoryKeyStore

NAME = "OD4064C9"
KEY = bytes(range(16))


def _run(tmp_path: Path, *argv: str) -> None:
    args = cli._build_parser().parse_args(["key", *argv, "--key-store", str(tmp_path / "keys.json")])
    args.func(args)


def _store(tmp_path: Path) -> JsonFileKeyStore:
    return JsonFileKeyStore(tmp_path / "keys.json")


def _out(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    captured = capsys.readouterr()
    return captured.out, " ".join(captured.err.split())


def test_export_prints_the_confirmed_key_after_the_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(tmp_path).promote(NAME, KEY)
    prompts: list[str] = []
    monkeypatch.setattr(cli_sec2, "_input", lambda prompt: prompts.append(prompt) or "y")

    _run(tmp_path, "export", "--device", NAME)

    out, _ = _out(capsys)
    assert out.strip() == KEY.hex()
    assert "read your WiFi password and FindMy keys" in " ".join(prompts[0].split())


def test_export_without_confirmation_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(tmp_path).promote(NAME, KEY)
    monkeypatch.setattr(cli_sec2, "_input", lambda prompt: "")

    with pytest.raises(SystemExit):
        _run(tmp_path, "export", "--device", NAME)

    assert KEY.hex() not in capsys.readouterr().out


def test_export_with_yes_does_not_ask(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(tmp_path).promote(NAME, KEY)
    monkeypatch.setattr(cli_sec2, "_input", pytest.fail)

    _run(tmp_path, "export", "--device", NAME, "--yes")

    assert capsys.readouterr().out.strip() == KEY.hex()


def test_export_refuses_a_key_that_never_opened_a_session(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _store(tmp_path).add_candidate(NAME, KEY, "user")

    with pytest.raises(SystemExit):
        _run(tmp_path, "export", "--device", NAME, "--yes")

    out, err = _out(capsys)
    assert KEY.hex() not in out
    assert "not confirmed" in err


def test_export_of_an_unknown_display_says_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _run(tmp_path, "export", "--device", NAME, "--yes")

    assert "no key" in _out(capsys)[1].lower()


def test_import_of_hex_stores_a_candidate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run(tmp_path, "import", "--device", NAME, KEY.hex().upper())

    store = _store(tmp_path)
    assert store.keys_to_try(NAME) == [KEY]
    assert "first time it opens a session" in _out(capsys)[1]


def test_import_of_a_landing_link_with_a_key(tmp_path: Path) -> None:
    link = build_landing_url(0, bytes.fromhex("4064c9"), KEY, 1)

    _run(tmp_path, "import", "--device", NAME, link)

    assert _store(tmp_path).keys_to_try(NAME) == [KEY]


def test_import_refuses_a_link_without_a_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The claimed E1002's boot-screen QR, key hidden (show-key off).
    with pytest.raises(SystemExit):
        _run(tmp_path, "import", "--device", NAME, "https://opendisplay.org/l/?AABAZMkAAAAAAAAAAAAAAAAAAAAAAAE")

    assert "carries no key" in _out(capsys)[1]
    assert _store(tmp_path).keys_to_try(NAME) == []


def test_import_refuses_a_link_for_another_display(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    link = build_landing_url(0, bytes.fromhex("58d951"), KEY, 1)

    with pytest.raises(SystemExit):
        _run(tmp_path, "import", "--device", NAME, link)

    assert "OD58D951" in _out(capsys)[1]
    assert _store(tmp_path).keys_to_try(NAME) == []


def test_import_refuses_malformed_input(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _run(tmp_path, "import", "--device", NAME, "abc")
    assert _store(tmp_path).keys_to_try(NAME) == []


async def test_an_imported_key_is_confirmed_by_its_first_session() -> None:
    device = RefDevice(k_dev=KEY)
    store = MemoryKeyStore()
    store.add_candidate(NAME, KEY, "import")

    async with opendisplay.connect(transport=device, key_store=store) as dev:
        assert dev.security.sec2_session_open

    assert store.confirmed(NAME) == KEY


async def test_a_rotation_warns_that_other_clients_need_the_new_key(caplog: pytest.LogCaptureFixture) -> None:
    device = RefDevice(k_dev=KEY)
    store = MemoryKeyStore()
    store.promote(NAME, KEY, uid=device.uid)

    async with opendisplay.connect(transport=device, key_store=store) as dev:
        await dev.security.rotate_key()

    assert "other clients" in caplog.text and "new key" in caplog.text


def test_the_no_key_error_mentions_a_rotation_by_another_client() -> None:
    assert "another client" in str(Sec2KeyRequiredError(1))


def test_import_refuses_hex_of_the_wrong_length(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _run(tmp_path, "import", "--device", NAME, KEY.hex()[:30])
    assert _store(tmp_path).keys_to_try(NAME) == []
