"""Tests for the ``opendisplay sendspin`` CLI subcommand."""

from __future__ import annotations

from pathlib import Path

import pytest

from opendisplay import cli


def test_default_state_dir_is_per_device() -> None:
    assert cli._sendspin_state_dir("AA:BB:CC:DD:EE:FF") == (
        Path.home() / ".config" / "opendisplay" / "sendspin" / "aabbccddeeff"
    )


def test_sendspin_subcommand_is_registered(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.argv", ["opendisplay", "sendspin", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--url" in out
    assert "--state-dir" in out
