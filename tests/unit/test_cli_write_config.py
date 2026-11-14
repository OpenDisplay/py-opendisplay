"""`opendisplay write-config` must not silently delete config packets it does not recognise.

A JSON file edited in a tool that does not know the ``unparsed_tail`` field loses
it. Writing that file to a device that has such packets would delete them, so
the command refuses unless told whether to keep or discard them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest
from test_config_json_roundtrip import _base_config, _display

from opendisplay import GlobalConfig, cli
from opendisplay.models import config_to_json
from opendisplay.models.panel_drive import LengthPrefixedPacket

_TAIL = bytes([5, 0xB1, 3, 0, 0xAA, 0xBB, 0xCC])
_OTHER_TAIL = bytes([5, 0xB1, 1, 0, 0x11])


def _config(tail: bytes = b"") -> GlobalConfig:
    return dataclasses.replace(_base_config(displays=[_display()]), unparsed_tail=tail)


def _write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    device_tail: bytes,
    file_tail: bytes,
    unknown_packets: str | None = None,
) -> list[GlobalConfig]:
    """Run write-config against a fake device; return the configs it was asked to write."""
    written: list[GlobalConfig] = []

    class FakeDevice:
        import_config_json = staticmethod(cli.OpenDisplayDevice.import_config_json)

        def __init__(self, **kwargs: object) -> None:
            self.config = _config(device_tail)

        async def __aenter__(self) -> FakeDevice:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def write_config(self, config: GlobalConfig) -> None:
            written.append(config)

    monkeypatch.setattr(cli, "OpenDisplayDevice", FakeDevice)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_to_json(_config(file_tail))))
    asyncio.run(cli._write_config({}, str(path), unknown_packets))
    return written


def test_refuses_when_the_file_would_delete_the_devices_unknown_packets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    written: list[GlobalConfig] = []
    with pytest.raises(SystemExit) as exc:
        written = _write(monkeypatch, tmp_path, device_tail=_TAIL, file_tail=b"")

    assert exc.value.code == 1
    assert written == []
    err = capsys.readouterr().err
    assert "--keep-unknown-packets" in err and "--discard-unknown-packets" in err


def test_keep_carries_the_devices_unknown_packets_over(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    written = _write(monkeypatch, tmp_path, device_tail=_TAIL, file_tail=b"", unknown_packets="keep")

    assert [c.unparsed_tail for c in written] == [_TAIL]


def test_discard_writes_the_file_as_it_is(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    written = _write(monkeypatch, tmp_path, device_tail=_TAIL, file_tail=b"", unknown_packets="discard")

    assert [c.unparsed_tail for c in written] == [b""]


@pytest.mark.parametrize("unknown_packets", [None, "keep", "discard"])
def test_a_file_with_its_own_unknown_packets_is_written_as_it_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unknown_packets: str | None
) -> None:
    written = _write(monkeypatch, tmp_path, device_tail=_TAIL, file_tail=_OTHER_TAIL, unknown_packets=unknown_packets)

    assert [c.unparsed_tail for c in written] == [_OTHER_TAIL]


def test_length_prefixed_packets_are_guarded_like_the_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A device whose packets are all length-prefixed (no tail) is protected the same way."""
    packets = [LengthPrefixedPacket(0, 0xB1, b"\x01\x02\x03")]
    written: list[GlobalConfig] = []

    class FakeDevice:
        import_config_json = staticmethod(cli.OpenDisplayDevice.import_config_json)

        def __init__(self, **kwargs: object) -> None:
            self.config = dataclasses.replace(_config(), length_prefixed_packets=packets)

        async def __aenter__(self) -> FakeDevice:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def write_config(self, config: GlobalConfig) -> None:
            written.append(config)

    monkeypatch.setattr(cli, "OpenDisplayDevice", FakeDevice)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config_to_json(_config())))

    with pytest.raises(SystemExit):
        asyncio.run(cli._write_config({}, str(path)))
    assert written == []
    assert "--keep-unknown-packets" in capsys.readouterr().err

    asyncio.run(cli._write_config({}, str(path), "keep"))
    assert [c.length_prefixed_packets for c in written] == [packets]


def test_a_device_without_unknown_packets_needs_no_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    written = _write(monkeypatch, tmp_path, device_tail=b"", file_tail=b"")

    assert len(written) == 1


def test_the_two_flags_are_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["write-config", "--device", "X", "--keep-unknown-packets", "--discard-unknown-packets", "c.json"]
        )
    assert "not allowed with" in capsys.readouterr().err


def test_flags_parse_to_the_choice() -> None:
    parser = cli._build_parser()
    assert parser.parse_args(["write-config", "--device", "X", "c.json"]).unknown_packets is None
    assert (
        parser.parse_args(["write-config", "--device", "X", "--keep-unknown-packets", "c.json"]).unknown_packets
        == "keep"
    )
