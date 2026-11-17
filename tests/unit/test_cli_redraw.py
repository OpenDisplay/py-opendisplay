"""The CLI allows for a redraw by default: a person just touched the display, and is watching."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from opendisplay import OpenDisplayDevice, cli, cli_migrate, redraw

NAME = "OD4064C9"
LINE = "Waiting for the display to finish redrawing..."


@pytest.fixture(autouse=True)
def _no_sleep_no_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    async def sleep(_: float) -> None:
        return None

    monkeypatch.setattr(redraw, "_sleep", sleep)
    monkeypatch.setattr(redraw, "waiting_hook", None)


def _prepare(*argv: str) -> Any:
    args = cli._build_parser().parse_args(list(argv))
    cli._prepare(args)
    return args


def test_cli_marks_the_target_before_connecting() -> None:
    _prepare("info", "--device", NAME)

    assert redraw.remaining(NAME) > 0


def test_cli_marks_a_lan_host_too() -> None:
    _prepare("info", "--host", "10.0.0.5")

    assert redraw.remaining("10.0.0.5") > 0


def test_no_wait_marks_nothing() -> None:
    _prepare("info", "--device", NAME, "--no-wait")

    assert redraw.remaining(NAME) == 0


def _scans(monkeypatch: pytest.MonkeyPatch, found_on: int) -> None:
    calls: list[float] = []

    async def discover_devices(timeout: float = 10.0, **_: Any) -> dict[str, str]:
        calls.append(timeout)
        return {NAME: "AA:BB"} if len(calls) >= found_on else {}

    monkeypatch.setattr("opendisplay.discovery.discover_devices", discover_devices)


@pytest.mark.parametrize(("found_on", "printed"), [(2, 1), (3, 1), (1, 0)])
async def test_the_waiting_line_is_printed_once_and_only_when_waiting(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], found_on: int, printed: int
) -> None:
    _scans(monkeypatch, found_on)
    _prepare("info", "--device", NAME)

    await OpenDisplayDevice(device_name=NAME)._resolve_name()

    assert " ".join(capsys.readouterr().err.split()).count(LINE) == printed


def test_migrate_no_longer_prints_the_spectra_warning() -> None:
    assert "Some panels (Spectra)" not in inspect.getsource(cli_migrate)
