"""`opendisplay setup`, `upgrade` and `update`: the setup flow's requests as terminal prompts."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_profile import _config, _display
from test_setup_runner import KEY, MANIFEST, _factory, _keyed, _migration, _World  # noqa: F401 (autouse fixture)

from opendisplay import cli, cli_setup
from opendisplay.firmware import UpdateOutcome, UpdateResult
from opendisplay.models.config_json import config_to_json
from opendisplay.models.ident import FirmwareTarget


class _Releases:
    def __init__(self) -> None:
        self.asked = 0

    async def manifest(self) -> Any:
        self.asked += 1
        return MANIFEST

    async def fetch(self, name: str) -> bytes:
        raise AssertionError("no download expected")


def _args(argv: list[str]) -> Any:
    return cli._build_parser().parse_args(argv)


def _answers(monkeypatch: pytest.MonkeyPatch, *typed: str) -> list[str]:
    prompts: list[str] = []
    queue = list(typed)

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        if not queue:
            raise AssertionError(f"Unexpected prompt: {prompt}")
        return queue.pop(0)

    monkeypatch.setattr(cli_setup, "_input", fake_input)
    return prompts


def test_the_key_choice_options() -> None:
    assert _args(["setup", "--device", "OD1"]).want_key is None
    assert _args(["setup", "--device", "OD1", "--claim"]).want_key is True
    assert _args(["setup", "--device", "OD1", "--no-key"]).want_key is False
    with pytest.raises(SystemExit):
        _args(["setup", "--device", "OD1", "--claim", "--no-key"])


async def test_an_upgrade_pauses_for_the_wired_install_and_resumes_from_the_state_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _World(major=2, target=FirmwareTarget.ESP32, config=_keyed())
    state, backup = tmp_path / "run.json", tmp_path / "backup.json"
    argv = [
        "upgrade",
        "--device",
        "OD1",
        "--no-key",
        "--board",
        "s3-n32r8-extuart",
        "--state-file",
        str(state),
        "--backup",
        str(backup),
        "--yes-untested",
    ]
    releases = _Releases()

    await cli_setup._setup(_args(argv), open_device=_factory(world), releases=releases)

    out = capsys.readouterr().err
    assert "opendisplay-s3-n32r8-extuart-merged.bin" in out and "opendisplay.org/firmware/install" in out
    assert "run the same command again" in out
    assert state.exists() and KEY.hex() not in state.read_text()
    assert KEY.hex() in backup.read_text()
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600

    world.major, world.claim_mode, world.fresh, world.config = 3, True, True, None  # flashed over USB
    _answers(monkeypatch, "y")  # the panel check
    await cli_setup._setup(_args(argv), open_device=_factory(world), releases=releases)

    assert "use_without_key" in world.calls and "config_write" in world.calls
    assert "set up" in capsys.readouterr().err.lower()
    assert releases.asked == 1  # the second run went on from the saved state without a manifest


async def test_a_backup_never_overwrites_an_earlier_one(tmp_path: Path) -> None:
    first = tmp_path / "backup.json"
    first.write_text("earlier")
    sink = cli_setup._backup_sink(first)

    sink(b"{}")

    assert first.read_text() == "earlier"
    assert (tmp_path / "backup-1.json").read_text() == "{}"


async def test_a_bg22_install_needs_the_word_yes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from opendisplay.setup import requests as rq  # pylint: disable=import-outside-toplevel

    _answers(monkeypatch, "y")
    request = rq.NeedConfirmation(reason="no_rollback", detail="no old image", subject="bg22")
    assert cli_setup._answer(request, _args(["setup", "--device", "OD1", "--yes-untested"])) is False

    _answers(monkeypatch, "yes")
    assert cli_setup._answer(request, _args(["setup", "--device", "OD1"])) is True


async def test_a_config_file_answers_the_config_request(tmp_path: Path) -> None:
    from opendisplay.setup import requests as rq  # pylint: disable=import-outside-toplevel

    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_to_json(_config(displays=[_display(panel_ic_type=0x0037)]))))

    config = cli_setup._answer(rq.NeedConfig(), _args(["setup", "--device", "OD1", "--config", str(path)]))

    assert config.displays[0].panel_ic_type == 0x0037


async def test_a_claim_reads_the_code_from_the_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _World(claim_mode=True, config=_keyed())
    _answers(monkeypatch, "123 456")

    await cli_setup._setup(
        _args(["setup", "--device", "OD1", "--claim"]), open_device=_factory(world), releases=_Releases()
    )

    assert "claim_confirm" in world.calls


async def test_update_says_whether_the_device_can_roll_back_and_asks_on_a_bg22(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    image = tmp_path / "ota.gbl"
    image.write_bytes(b"\xeb\x17\xa6\x03" + bytes(10))
    sent: list[bytes] = []

    async def update(data: bytes, **kw: Any) -> UpdateResult:
        sent.append(data)
        return UpdateResult(UpdateOutcome.UPDATED, "3.0.1", "3.0.2", "The display runs 3.0.2.")

    class _Conn:
        async def __aenter__(self) -> Any:
            return SimpleNamespace(firmware=SimpleNamespace(can_roll_back=False, update=update))

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(cli_setup, "connect", lambda **kw: _Conn())
    _answers(monkeypatch, "no")
    with pytest.raises(SystemExit):
        await cli_setup._update(_args(["update", "--device", "OD1", "--file", str(image)]))
    assert not sent and "cannot return to its old image" in capsys.readouterr().err

    _answers(monkeypatch, "yes")
    await cli_setup._update(_args(["update", "--device", "OD1", "--file", str(image)]))
    assert sent and "The display runs 3.0.2." in capsys.readouterr().err
