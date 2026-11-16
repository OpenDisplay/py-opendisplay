"""`opendisplay backup-config` and `opendisplay migrate`, against a fake Device."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from test_profile import FW3, IDENT_NEEDS_MIGRATION, _config, _display

from opendisplay import cli, cli_migrate
from opendisplay.exceptions import ConfigSecretNeedsClaimError
from opendisplay.models.config import GlobalConfig, SecurityConfig, WifiConfig
from opendisplay.models.config_json import config_to_json
from opendisplay.models.profile import DeviceProfile, build_profile
from opendisplay.panels import PanelDatabase, TargetClass
from opendisplay.protocol.config_serializer import serialize_config

DB = PanelDatabase.load()
KEY = bytes(range(1, 17))
GLASS_PANEL = 0x001E
EQUIVALENT_PANEL = 0x0001


def _security(**overrides: Any) -> SecurityConfig:
    base: dict[str, Any] = dict(
        encryption_enabled=1,
        encryption_key=KEY,
        session_timeout_seconds=0,
        flags=0x02,  # show key on screen
        reset_pin=0xFF,
        reserved=b"\x07" + bytes(42),
    )
    base.update(overrides)
    return SecurityConfig(**base)


def _wifi(password: bytes = b"hunter2".ljust(32, b"\x00")) -> WifiConfig:
    return WifiConfig(
        ssid=b"home".ljust(32, b"\x00"),
        password=password,
        encryption_type=3,
        server_url=bytes(64),
        server_port=0,
        reserved=bytes(29),
    )


def _plain(panel: int = EQUIVALENT_PANEL, **overrides: Any) -> GlobalConfig:
    return _config(displays=[_display(panel_ic_type=panel)], **overrides)


class _FakeConfigView:
    def __init__(self, device: _FakeDevice, cached: GlobalConfig | None) -> None:
        self._device = device
        self.cached = cached

    async def write(self, config: GlobalConfig) -> None:
        if self._device.write_error is not None:
            raise self._device.write_error
        self._device.written.append(config)
        stored = self._device.stored_after_write or config
        if self._device.unclaimed and stored.security_config is not None:
            # od_cfg_policy.c unclaimed_form: what an unclaimed 3.0 device stores for every 0x27.
            sec = stored.security_config
            sec = dataclasses.replace(
                sec,
                encryption_enabled=0,
                encryption_key=bytes(16),
                flags=sec.flags & ~0x02,
                reserved=bytes(1) + sec.reserved[1:],
            )
            stored = dataclasses.replace(stored, security_config=sec)
        self.cached = stored

    async def read(self) -> GlobalConfig:
        assert self.cached is not None
        return self.cached

    def plan_migration(self, db: PanelDatabase | None = None) -> Any:
        from opendisplay.panels import plan_migration

        return plan_migration(self.cached, self._device.profile, db or DB)


class _FakeDevice:
    def __init__(self, config: GlobalConfig | None, profile: DeviceProfile) -> None:
        self.config = _FakeConfigView(self, config)
        self.profile = profile
        self.written: list[GlobalConfig] = []
        self.write_error: Exception | None = None
        self.stored_after_write: GlobalConfig | None = None
        self.unclaimed = True

    async def __aenter__(self) -> _FakeDevice:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _patch(monkeypatch: pytest.MonkeyPatch, device: _FakeDevice) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def connect(**kwargs: Any) -> _FakeDevice:
        calls.append(kwargs)
        return device

    monkeypatch.setattr(cli_migrate, "connect", connect)
    return calls


def _migrate(*argv: str) -> None:
    args = cli._build_parser().parse_args(["migrate", "--device", "OD4064C9", *argv])
    args.func(args)


def _backup(tmp_path: Path, config: GlobalConfig) -> Path:
    path = tmp_path / "backup.json"
    path.write_text(json.dumps(config_to_json(config)))
    return path


def _device(config: GlobalConfig | None, ident: Any = IDENT_NEEDS_MIGRATION) -> _FakeDevice:
    return _FakeDevice(config, build_profile(FW3, config, ident))


# ── migrate ──────────────────────────────────────────────────────────────────


def test_dry_run_prints_the_plan_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    device = _device(_plain())
    _patch(monkeypatch, device)

    _migrate("--dry-run")

    out = capsys.readouterr()
    text = out.out + out.err
    assert device.written == []
    assert "config has no drive packets" in text
    assert "equivalent" in text  # the verification level is always shown in a dry run
    assert "0xB1" in text and "bytes" in text


def test_blocked_plan_exits_1_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    device = _device(_plain(panel=768))
    _patch(monkeypatch, device)

    with pytest.raises(SystemExit) as exit_info:
        _migrate("--yes-untested")

    assert exit_info.value.code == 1
    assert device.written == []
    assert "panel_ic_type 768" in " ".join(capsys.readouterr().err.split())


def test_nothing_to_do_exits_0_without_writing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _plain()
    from opendisplay.panels import apply_migration, plan_migration

    migrated = apply_migration(config, plan_migration(config, build_profile(FW3, config, None), DB))
    device = _device(migrated, ident=None)
    _patch(monkeypatch, device)

    _migrate()

    assert device.written == []
    assert "already has drive data" in " ".join(capsys.readouterr().err.split())


def test_migrates_the_devices_own_config_and_verifies_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    device = _device(_plain(panel=GLASS_PANEL))
    _patch(monkeypatch, device)

    _migrate()

    assert len(device.written) == 1
    written = device.written[0]
    assert written.length_prefixed_packets == list(DB.entry_for(GLASS_PANEL, TargetClass.DEFAULT).packets)  # type: ignore[union-attr]
    assert "verified" in capsys.readouterr().err.lower()


def test_own_config_secrets_are_written_back_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _plain(panel=GLASS_PANEL, wifi_config=_wifi())
    device = _device(config)
    _patch(monkeypatch, device)

    _migrate()

    assert device.written[0].wifi_config == config.wifi_config


def test_equivalent_panel_is_not_written_when_the_confirmation_is_declined(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    device = _device(_plain())
    _patch(monkeypatch, device)
    prompts: list[str] = []
    monkeypatch.setattr(cli_migrate, "_ask", lambda prompt: prompts.append(prompt) or False)

    with pytest.raises(SystemExit) as exit_info:
        _migrate()

    assert exit_info.value.code == 1
    assert device.written == []
    assert "EP42_400x300" in prompts[0] and "0x0001" in prompts[0]
    assert "byte-identically" in prompts[0] and "nobody has watched" in prompts[0]


def test_equivalent_panel_is_written_when_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    device = _device(_plain())
    _patch(monkeypatch, device)
    monkeypatch.setattr(cli_migrate, "_ask", lambda prompt: True)

    _migrate()

    assert len(device.written) == 1


def test_yes_untested_skips_the_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    device = _device(_plain())
    _patch(monkeypatch, device)
    monkeypatch.setattr(cli_migrate, "_ask", lambda prompt: pytest.fail("asked despite --yes-untested"))

    _migrate("--yes-untested")

    assert len(device.written) == 1


def test_read_back_mismatch_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:  # Review Focus 5
    device = _device(_plain(panel=GLASS_PANEL))
    device.stored_after_write = _plain(panel=GLASS_PANEL)  # the device kept the old config
    _patch(monkeypatch, device)

    with pytest.raises(SystemExit) as exit_info:
        _migrate()

    err = " ".join(capsys.readouterr().err.split())
    assert exit_info.value.code == 1
    assert "does not match" in err
    assert str(len(serialize_config(_plain(panel=GLASS_PANEL)))) in err


def test_secret_refusal_from_the_device_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    device = _device(_plain(panel=GLASS_PANEL))
    device.write_error = ConfigSecretNeedsClaimError()
    _patch(monkeypatch, device)

    with pytest.raises(SystemExit) as exit_info:
        _migrate()

    assert exit_info.value.code == 1
    assert "claim it first" in " ".join(capsys.readouterr().err.split())


# ── migrate --from-backup ────────────────────────────────────────────────────


def test_backup_with_secrets_is_refused_before_connecting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backup = _backup(tmp_path, _plain(panel=GLASS_PANEL, security_config=_security(), wifi_config=_wifi()))
    calls = _patch(monkeypatch, _device(None))

    with pytest.raises(SystemExit) as exit_info:
        _migrate("--from-backup", str(backup))

    err = " ".join(capsys.readouterr().err.split())  # Rich wraps long lines
    assert exit_info.value.code == 1
    assert calls == []
    assert "encryption key" in err and "WiFi password" in err
    assert "claim it first, then enter WiFi / FindMy settings again" in err
    assert KEY.hex() not in err and "hunter2" not in err


def test_strip_secrets_writes_the_backup_without_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    findmy = bytes([0, 0x2D]) + bytes(range(1, 129))
    backup = _backup(
        tmp_path,
        _plain(panel=GLASS_PANEL, security_config=_security(), wifi_config=_wifi(), findmy_config_raw=findmy),
    )
    device = _device(None)
    _patch(monkeypatch, device)

    _migrate("--from-backup", str(backup), "--strip-secrets")

    written = device.written[0]
    assert written.security_config is not None
    assert (written.security_config.encryption_enabled, written.security_config.encryption_key) == (0, bytes(16))
    # The form an unclaimed device stores (od_cfg_policy.c unclaimed_form), so the read-back matches.
    assert written.security_config.flags & 0x02 == 0 and written.security_config.reserved[0] == 0
    assert written.wifi_config is not None and written.wifi_config.password == bytes(32)
    assert written.wifi_config.ssid == _wifi().ssid
    assert written.findmy_config_raw is not None  # FindMy settings kept, only the key fields zeroed
    body = written.findmy_config_raw[2:]
    assert body[8:40] == bytes(32) and body[40:68] == bytes(28) and body[76:96] == bytes(20)
    assert body[:8] == bytes(range(1, 9)) and body[68:76] == bytes(range(69, 77)) and body[96:] == bytes(range(97, 129))
    assert written.length_prefixed_packets == list(DB.entry_for(GLASS_PANEL, TargetClass.DEFAULT).packets)  # type: ignore[union-attr]
    err = " ".join(capsys.readouterr().err.split())
    assert "encryption key" in err and "FindMy" in err


def test_backup_replaces_the_devices_packets_and_says_which_are_lost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backup = _backup(tmp_path, _plain(panel=GLASS_PANEL))
    on_device = _config(displays=[], wifi_config=_wifi(password=bytes(32)))
    device = _device(on_device)
    _patch(monkeypatch, device)

    _migrate("--from-backup", str(backup))

    err = " ".join(capsys.readouterr().err.split())
    assert "replaced by the backup" in err
    assert "0x26" in err  # the device has WiFi settings the backup lacks
    assert device.written[0].wifi_config is None


def test_dry_run_with_a_backup_needs_no_device_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    backup = _backup(tmp_path, _plain(panel=GLASS_PANEL))
    device = _device(None)
    _patch(monkeypatch, device)

    _migrate("--from-backup", str(backup), "--dry-run")

    assert device.written == []


def test_panel_option_accepts_hex(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    device = _device(_plain())
    _patch(monkeypatch, device)

    _migrate("--panel", "0x001E", "--dry-run")

    assert "EP" in capsys.readouterr().err  # the glass panel's name is printed


# ── backup-config ────────────────────────────────────────────────────────────


def _backup_cmd(path: Path) -> None:
    args = cli._build_parser().parse_args(["backup-config", str(path), "--device", "OD4064C9"])
    args.func(args)


def test_backup_config_writes_the_file_and_warns_about_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _plain(security_config=_security())
    _patch(monkeypatch, _device(config))
    path = tmp_path / "backup.json"

    _backup_cmd(path)

    def without_timestamp(doc: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in doc.items() if k != "exported_at"}

    assert without_timestamp(json.loads(path.read_text())) == without_timestamp(config_to_json(config))
    err = " ".join(capsys.readouterr().err.split())
    assert "encryption key" in err and "keep this file private" in err.lower()


def test_backup_config_refuses_an_existing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "backup.json"
    path.write_text("keep me")
    calls = _patch(monkeypatch, _device(_plain()))

    with pytest.raises(SystemExit) as exit_info:
        _backup_cmd(path)

    assert exit_info.value.code == 1
    assert path.read_text() == "keep me"
    assert calls == []


def test_backup_config_of_a_device_without_config_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch(monkeypatch, _device(None))

    with pytest.raises(SystemExit):
        _backup_cmd(tmp_path / "backup.json")

    assert not (tmp_path / "backup.json").exists()


def test_connect_gets_the_cli_addressing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _patch(monkeypatch, _device(_plain()))

    _backup_cmd(tmp_path / "b.json")

    assert calls[0]["name"] == "OD4064C9" and calls[0].get("address") is None


def test_parser_registers_both_commands() -> None:
    parser = cli._build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))  # noqa: SLF001

    assert {"backup-config", "migrate"} <= set(sub.choices)


def test_device_config_view_plans_a_migration() -> None:
    from opendisplay.device.api import ConfigView

    class _Dev:
        profile = build_profile(FW3, _plain(), IDENT_NEEDS_MIGRATION)

    view = ConfigView(_Dev())  # type: ignore[arg-type]
    view._cached = _plain()  # noqa: SLF001

    plan = view.plan_migration(DB)

    assert plan.needed and plan.add == DB.entry_for(EQUIVALENT_PANEL, TargetClass.DEFAULT).packets  # type: ignore[union-attr]


def test_strip_secrets_gives_the_unclaimed_form_of_the_security_packet() -> None:
    stripped = cli_migrate.strip_secrets(_plain(security_config=_security(flags=0x02 | 0x04)))

    security = stripped.security_config
    assert security is not None
    assert (security.encryption_enabled, security.encryption_key) == (0, bytes(16))
    assert security.flags == 0x04  # show-key cleared, the reset-pin bit kept
    assert security.reserved == bytes(43)


def test_a_key_without_the_enable_flag_is_still_a_secret() -> None:
    config = _plain(security_config=_security(encryption_enabled=0))

    assert cli_migrate.find_secrets(config) == ["encryption key"]
    assert (
        cli_migrate.find_secrets(_plain(security_config=_security(encryption_enabled=0, encryption_key=bytes(16))))
        == []
    )


# ── review fixes (piece 3a review, I1 I2 M1 M7 M8 M9) ─────────────────────────

ALPHA = Path(__file__).parent.parent / "fixtures" / "configs" / "bg22_alpha_drive_packets.bin"


def _alpha_config() -> GlobalConfig:
    from opendisplay.protocol.config_parser import parse_config_response

    return parse_config_response(ALPHA.read_bytes())


def test_already_migrated_backup_is_restored_to_a_device_without_config(  # I1
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    backup_config = _alpha_config()
    device = _device(None, ident=None)  # no needs-migration flag: the backup's drive data is kept
    _patch(monkeypatch, device)

    # The fixture's key is zeroed but encryption_enabled is 1, which the firmware counts as a secret.
    _migrate("--from-backup", str(_backup(tmp_path, backup_config)), "--strip-secrets")

    err = " ".join(capsys.readouterr().err.split())
    assert len(device.written) == 1
    assert device.written[0].length_prefixed_packets == backup_config.length_prefixed_packets
    assert "verified" in err


def test_backup_the_device_already_stores_is_not_written_again(  # I1
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from opendisplay.models.config_json import config_from_json

    path = _backup(tmp_path, _alpha_config())
    stored = cli_migrate.strip_secrets(config_from_json(json.loads(path.read_text())))
    device = _device(stored, ident=None)
    _patch(monkeypatch, device)

    _migrate("--from-backup", str(path), "--strip-secrets")

    assert device.written == []
    assert "already stored, nothing written" in " ".join(capsys.readouterr().err.split())


def test_backup_of_a_panel_without_drive_data_is_still_restored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:  # I1
    device = _device(None)
    _patch(monkeypatch, device)

    _migrate("--from-backup", str(_backup(tmp_path, _plain(panel=1500))))

    assert len(device.written) == 1


def test_backup_security_packet_is_written_in_the_unclaimed_form(  # I2
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    security = _security(encryption_enabled=0, encryption_key=bytes(16), flags=0x02 | 0x04)  # not a secret
    device = _device(None)
    _patch(monkeypatch, device)

    _migrate("--from-backup", str(_backup(tmp_path, _plain(panel=GLASS_PANEL, security_config=security))))

    written = device.written[0].security_config
    assert written is not None and written.flags == 0x04
    err = " ".join(capsys.readouterr().err.split())
    assert "verified" in err and "unclaimed" in err


def test_findmy_without_key_material_is_not_a_secret() -> None:  # M1
    findmy = bytes([0, 0x2D]) + bytes(8) + bytes(32) + bytes(28) + b"\x05" * 8 + bytes(20) + b"\x01" * 32
    assert cli_migrate.find_secrets(_plain(findmy_config_raw=findmy)) == []
    keyed = findmy[: 2 + 8] + b"\x01" + findmy[2 + 9 :]
    assert cli_migrate.find_secrets(_plain(findmy_config_raw=keyed)) == ["FindMy keys"]


def test_every_findmy_packet_is_checked_and_stripped() -> None:  # M1
    clean = bytes([0, 0x2D]) + bytes(128)
    keyed = bytes([1, 0x2D]) + bytes(76) + b"\x09" * 20 + bytes(32)
    config = _plain(findmy_config_raw=clean + keyed)

    assert cli_migrate.find_secrets(config) == ["FindMy keys"]
    stripped = cli_migrate.strip_secrets(config).findmy_config_raw
    assert stripped == clean + bytes([1, 0x2D]) + bytes(128)


def test_backup_file_is_private(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # M9
    _patch(monkeypatch, _device(_plain(security_config=_security())))
    path = tmp_path / "backup.json"

    _backup_cmd(path)

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_blocked_reason_is_printed_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:  # M7
    _patch(monkeypatch, _device(_plain(panel=768)))

    with pytest.raises(SystemExit):
        _migrate()

    assert " ".join(capsys.readouterr().err.split()).count("panel_ic_type 768") == 1


def test_strip_secrets_needs_from_backup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:  # M8
    calls = _patch(monkeypatch, _device(_plain()))  # never a real connect, even if the check is missing

    with pytest.raises(SystemExit) as exit_info:
        _migrate("--strip-secrets")

    assert exit_info.value.code == 2
    assert "--from-backup" in capsys.readouterr().err
    assert calls == []
