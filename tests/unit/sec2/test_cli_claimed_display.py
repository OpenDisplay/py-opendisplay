"""The older CLI commands open a SEC2 session on a claimed firmware 3.0 display, with the key store."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import pytest
from refdevice import RefDevice

import opendisplay
from opendisplay import cli, cli_common
from opendisplay.sec2.keystore import JsonFileKeyStore

NAME = "OD4064C9"


def _claimed(tmp_path: Path) -> RefDevice:
    key = secrets.token_bytes(16)
    device = RefDevice(k_dev=key)
    JsonFileKeyStore(tmp_path / "keys.json").promote(NAME, key, uid=device.uid)
    return device


def _patch(monkeypatch: pytest.MonkeyPatch, device: RefDevice) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def connect(**kwargs: Any) -> opendisplay.Device:
        calls.append(kwargs)
        return opendisplay.connect(
            transport=device, key_store=kwargs["key_store"], encryption_key=kwargs.get("encryption_key")
        )

    monkeypatch.setattr(cli_common, "connect", connect)
    return calls


def _run(tmp_path: Path, *argv: str) -> None:
    args = cli._build_parser().parse_args(
        [argv[0], "--device", NAME, "--no-wait", "--key-store", str(tmp_path / "keys.json"), *argv[1:]]
    )
    args.func(args)


def _sealed(device: RefDevice) -> list[bytes]:
    """Requests after the handshake that went out sealed: [cmd][ctr:4][ciphertext][tag:12]."""
    return [f for f in device.written if f[:2] not in (b"\x00\x43", b"\x00\x54", b"\x00\x56") and len(f) >= 18]


def test_info_on_a_claimed_display_reads_it_in_a_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    device = _claimed(tmp_path)
    calls = _patch(monkeypatch, device)

    _run(tmp_path, "info")

    assert "CLAIMED" in capsys.readouterr().err
    assert calls and calls[0]["key_store"].path == tmp_path / "keys.json"
    assert _sealed(device)  # the config read went sealed


def test_reboot_on_a_claimed_display_is_sent_sealed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    device = _claimed(tmp_path)
    _patch(monkeypatch, device)

    _run(tmp_path, "reboot")

    assert any(f[:2] == b"\x00\x0f" for f in _sealed(device))


def test_a_claimed_display_without_its_key_is_explained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch, RefDevice(k_dev=secrets.token_bytes(16)))

    with pytest.raises(SystemExit):
        _run(tmp_path, "info")

    assert "key" in capsys.readouterr().err.lower()
