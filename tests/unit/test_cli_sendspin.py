"""Tests for the ``opendisplay sendspin`` CLI subcommand."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from test_sendspin import _config, _display

from opendisplay import cli
from opendisplay.now_playing import NowPlaying


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


async def test_now_playing_is_prepared_before_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refinement takes CPU time; the device must not sit connected (and silent) meanwhile."""
    events: list[tuple[str, object]] = []
    prepared = (b"raw", b"zip", Image.new("RGB", (4, 4)))

    def _prepare(now: NowPlaying, config: object, **kwargs: object) -> object:
        events.append(("prepare", {"now": now, "config": config, **kwargs}))
        return prepared

    class _FakeDevice:
        def __init__(self, **kwargs: object) -> None:
            events.append(("connect", kwargs))

        async def __aenter__(self) -> _FakeDevice:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def upload_prepared_image(self, data: object) -> None:
            events.append(("upload", data))

    monkeypatch.setattr(cli, "prepare_now_playing", _prepare)
    monkeypatch.setattr(cli, "OpenDisplayDevice", _FakeDevice)
    config = _config(_display(800, 480))
    now = NowPlaying(title="Song", artist="Band")
    await cli._upload_now_playing({"mac_address": "AA:BB:CC:DD:EE:FF"}, config, now)

    assert [name for name, _ in events] == ["prepare", "connect", "upload"]
    assert events[0][1] == {
        "now": now,
        "config": config,
        "use_measured_palettes": True,
        "dbs": True,
        "compress": False,
    }
    assert events[1][1] == {"mac_address": "AA:BB:CC:DD:EE:FF", "config": config}
    assert events[2][1] is prepared
