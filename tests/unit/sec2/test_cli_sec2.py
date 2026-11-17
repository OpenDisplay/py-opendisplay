"""`opendisplay claim`, `unclaim` and `rotate-key`, against the SEC2 reference device."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from refdevice import RefDevice

import opendisplay
from opendisplay import cli, cli_sec2
from opendisplay.sec2.keystore import JsonFileKeyStore

NAME = "OD4064C9"


def _patch(monkeypatch: pytest.MonkeyPatch, device: RefDevice) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def connect(**kwargs: Any) -> opendisplay.Device:
        calls.append(kwargs)
        return opendisplay.connect(
            transport=device, key_store=kwargs.get("key_store"), encryption_key=kwargs.get("encryption_key")
        )

    monkeypatch.setattr(cli_sec2, "connect", connect)
    return calls


def _run(tmp_path: Path, *argv: str) -> None:
    args = cli._build_parser().parse_args(
        [argv[0], "--device", NAME, "--key-store", str(tmp_path / "keys.json"), *argv[1:]]
    )
    args.func(args)


def test_claim_asks_for_the_code_and_stores_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    device = RefDevice(claim_mode=True)
    _patch(monkeypatch, device)
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        assert device.shown_code is not None
        return device.shown_code[:3] + " " + device.shown_code[3:]

    monkeypatch.setattr(cli_sec2, "_input", answer)

    _run(tmp_path, "claim")

    assert device.claimed
    assert JsonFileKeyStore(tmp_path / "keys.json").keys_to_try(NAME) == [device.k_dev]
    assert "6-digit code" in prompts[0] and "someone else is claiming" in prompts[0]
    assert "claimed" in " ".join(capsys.readouterr().err.split()).lower()


def test_a_wrong_code_aborts_and_keeps_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    device = RefDevice(claim_mode=True)
    _patch(monkeypatch, device)
    monkeypatch.setattr(cli_sec2, "_input", lambda prompt: "000000" if device.shown_code != "000000" else "111111")

    with pytest.raises(SystemExit) as exit_info:
        _run(tmp_path, "claim")

    assert exit_info.value.code == 1
    assert not device.claimed
    assert JsonFileKeyStore(tmp_path / "keys.json").keys_to_try(NAME) == []
    assert "does not match" in " ".join(capsys.readouterr().err.split())


def test_claim_outside_claim_mode_says_how_to_start_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch, RefDevice())

    with pytest.raises(SystemExit):
        _run(tmp_path, "claim")

    assert "power-cycle it twice" in " ".join(capsys.readouterr().err.split()).lower()


def test_claim_without_a_screen_needs_the_acknowledgement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    device = RefDevice(claim_mode=True, dflags=0x00)  # HAS_PANEL clear
    _patch(monkeypatch, device)
    monkeypatch.setattr(cli_sec2, "_input", lambda prompt: "no")

    with pytest.raises(SystemExit):
        _run(tmp_path, "claim")
    assert not device.claimed

    monkeypatch.setattr(cli_sec2, "_input", lambda prompt: "yes")
    _run(tmp_path, "claim")
    assert device.claimed


def test_rotate_key_replaces_the_stored_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    device = RefDevice(k_dev=b"\x01" * 16)
    store = JsonFileKeyStore(tmp_path / "keys.json")
    store.promote(NAME, device.k_dev, uid=device.uid)  # type: ignore[arg-type]
    _patch(monkeypatch, device)

    _run(tmp_path, "rotate-key")

    assert device.k_dev != b"\x01" * 16
    assert store.keys_to_try(NAME) == [device.k_dev]


def test_unclaim_asks_first_and_forgets_the_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    device = RefDevice(k_dev=b"\x02" * 16)
    store = JsonFileKeyStore(tmp_path / "keys.json")
    store.promote(NAME, device.k_dev, uid=device.uid)  # type: ignore[arg-type]
    _patch(monkeypatch, device)
    monkeypatch.setattr(cli_sec2, "_input", lambda prompt: "n")

    with pytest.raises(SystemExit):
        _run(tmp_path, "unclaim")
    assert device.claimed

    _run(tmp_path, "unclaim", "--yes")
    assert not device.claimed and store.keys_to_try(NAME) == []


def test_a_claimed_device_without_a_known_key_is_explained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch, RefDevice(k_dev=b"\x03" * 16))

    with pytest.raises(SystemExit):
        _run(tmp_path, "rotate-key")

    assert "factory-reset" in " ".join(capsys.readouterr().err.split())
