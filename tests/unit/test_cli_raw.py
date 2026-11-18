"""`opendisplay raw`: one command frame out, its reply printed as hex."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opendisplay import cli, cli_raw


def _args(argv: list[str]) -> Any:
    return cli._build_parser().parse_args(argv)


def _fake_connect(monkeypatch: pytest.MonkeyPatch, reply: bytes | None) -> list[tuple[bytes, bool]]:
    sent: list[tuple[bytes, bool]] = []

    async def send_raw(frame: bytes, *, reply: bool = True, timeout: float = 10.0) -> bytes | None:
        sent.append((frame, reply))
        return reply_bytes if reply else None

    reply_bytes = reply

    class _Conn:
        async def __aenter__(self) -> Any:
            return SimpleNamespace(legacy=SimpleNamespace(send_raw=send_raw))

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(cli_raw, "connect", lambda **kw: _Conn())
    return sent


async def test_the_command_and_payload_are_sent_as_one_frame_and_the_reply_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent = _fake_connect(monkeypatch, bytes.fromhex("00450000"))

    await cli_raw._raw(_args(["raw", "--device", "OD1", "--cmd", "0x0045", "--payload", "01 02"]))

    assert sent == [(bytes.fromhex("00450102"), True)]
    assert "00 45 00 00" in capsys.readouterr().out


async def test_no_reply_waits_for_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sent = _fake_connect(monkeypatch, None)

    await cli_raw._raw(_args(["raw", "--device", "OD1", "--cmd", "000f", "--no-reply"]))

    assert sent == [(bytes.fromhex("000f"), False)]


@pytest.mark.parametrize("cmd", ["12345", "zz", "0x10000"])
def test_a_command_that_is_not_two_bytes_of_hex_is_refused(cmd: str) -> None:
    with pytest.raises(SystemExit):
        _args(["raw", "--device", "OD1", "--cmd", cmd])
