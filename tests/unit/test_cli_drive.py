"""`opendisplay upload --mode NAME`: an upload in a named drive mode, against a fake Device."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from opendisplay import cli, cli_drive
from opendisplay.models.profile import build_profile
from opendisplay.protocol.config_parser import parse_config_response

ALPHA = parse_config_response(
    (Path(__file__).parent.parent / "fixtures" / "configs" / "bg22_alpha_drive_packets.bin").read_bytes()
)
FW3 = {"major": 3, "minor": 0, "patch": 2, "sha": "bg22-dev"}


class _FakeDisplay:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[Image.Image, dict[str, Any]]] = []
        self.error = error

    def drive_mode(self, name: str) -> Any:
        mode = next((m for m in ALPHA.display_modes if m.name.lower() == name.lower()), None)
        if mode is None:
            raise ValueError(f"Unknown drive mode '{name}'; the device has: full, colour16")
        return mode

    def nominal_colours(self, mode: Any) -> bool:
        return bool(mode.passes > 1)

    async def show(self, image: Image.Image, **kwargs: Any) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append((image, kwargs))


class _FakeConfig:
    cached = ALPHA


class _FakeDevice:
    def __init__(self, error: Exception | None = None) -> None:
        self.display = _FakeDisplay(error)
        self.config = _FakeConfig()
        self.profile = build_profile(FW3, ALPHA, None)  # type: ignore[arg-type]

    async def __aenter__(self) -> _FakeDevice:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _patch(monkeypatch: pytest.MonkeyPatch, device: _FakeDevice) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def connect(**kwargs: Any) -> _FakeDevice:
        calls.append(kwargs)
        return device

    monkeypatch.setattr(cli_drive, "connect", connect)
    return calls


def _upload(tmp_path: Path, *argv: str) -> None:
    path = tmp_path / "img.png"
    Image.new("RGB", (184, 360), "white").save(path)
    args = cli._build_parser().parse_args(["upload", str(path), "--device", "OD58D951", *argv])
    args.func(args)


def test_mode_upload_goes_through_the_display_view(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    device = _FakeDevice()
    calls = _patch(monkeypatch, device)

    _upload(tmp_path, "--mode", "colour16", "--dither-mode", "none", "--no-compress")

    assert calls[0]["name"] == "OD58D951"
    ((image, kwargs),) = device.display.calls
    assert image.size == (184, 360)
    assert kwargs["mode"] == "colour16"
    assert kwargs["compress"] is False
    assert kwargs["dither_mode"].name == "NONE"


def test_nominal_colours_are_announced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch, _FakeDevice())

    _upload(tmp_path, "--mode", "colour16")

    assert "nominal" in " ".join(capsys.readouterr().err.split())


def test_a_single_pass_mode_says_nothing_about_nominal_colours(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch, _FakeDevice())

    _upload(tmp_path, "--mode", "full")

    assert "nominal" not in capsys.readouterr().err


def test_an_unknown_mode_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch, _FakeDevice())

    with pytest.raises(SystemExit) as exit_info:
        _upload(tmp_path, "--mode", "sepia")

    assert exit_info.value.code == 1
    assert "full, colour16" in " ".join(capsys.readouterr().err.split())


@pytest.mark.parametrize("extra", [["--tone", "0.5"], ["--state-file", "s.bin"], ["--refresh-mode", "partial"]])
def test_options_a_drive_mode_cannot_take_are_refused_before_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra: list[str]
) -> None:
    calls = _patch(monkeypatch, _FakeDevice())

    with pytest.raises(SystemExit):
        _upload(tmp_path, "--mode", "colour16", *extra)

    assert calls == []


def test_without_mode_the_upload_is_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _patch(monkeypatch, _FakeDevice())
    seen: list[Any] = []

    async def fake_upload(*args: Any) -> None:
        seen.append(args)

    monkeypatch.setattr(cli, "_upload", fake_upload)

    _upload(tmp_path)

    assert calls == [] and len(seen) == 1


def test_another_value_error_is_not_reported_as_a_mode_name_error(  # review M7
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch(monkeypatch, _FakeDevice(ValueError("image data broke")))

    with pytest.raises(ValueError, match="image data broke"):
        _upload(tmp_path, "--mode", "colour16")
