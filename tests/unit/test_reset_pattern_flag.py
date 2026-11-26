"""0x27 flags bit 6 NO_RESET_PATTERN (Firmware_Unified PROTOCOL_CHANGES_3.0 row 20, provisional)."""

from __future__ import annotations

import dataclasses
import logging
from types import SimpleNamespace
from typing import Any

import pytest

from opendisplay.device.api import ConfigView
from opendisplay.models.config import GlobalConfig, SecurityConfig
from opendisplay.sec2.wire import SecState


def _security(flags: int, reset_pin: int = 0xFF) -> SecurityConfig:
    return SecurityConfig(
        encryption_enabled=0,
        encryption_key=bytes(16),
        session_timeout_seconds=0,
        flags=flags,
        reset_pin=reset_pin,
        reserved=bytes(43),
    )


@pytest.mark.parametrize(
    ("flags", "reset_pin", "no_pattern", "only_wired"),
    [
        (0x00, 0xFF, False, False),  # the default: the pattern is on
        (0x40, 0xFF, True, True),  # pattern off, no reset pin
        (0x40 | 0x04, 0x05, True, False),  # pattern off, but an enabled reset pin on GPIO 5
        (0x40 | 0x04, 0xFF, True, True),  # the enable bit without a pin is no reset pin
        (0x40, 0x05, True, True),  # a pin without its enable bit is not used either
    ],
)
def test_the_flag_and_what_it_leaves_for_recovery(
    flags: int, reset_pin: int, no_pattern: bool, only_wired: bool
) -> None:
    security = _security(flags, reset_pin)

    assert security.no_reset_pattern is no_pattern
    assert security.only_wired_recovery is only_wired


def _view(state: SecState | None, written: list[GlobalConfig]) -> ConfigView:
    async def write_config(config: GlobalConfig) -> None:
        written.append(config)

    async def refresh() -> None:
        return None

    status = None if state is None else SimpleNamespace(state=state)
    device: Any = SimpleNamespace(
        security=SimpleNamespace(status=status),
        legacy=SimpleNamespace(write_config=write_config),
        refresh=refresh,
    )
    return ConfigView(device)


def _config(security: SecurityConfig) -> GlobalConfig:
    from test_profile import _config as base

    return dataclasses.replace(base(), security_config=security)


@pytest.mark.parametrize(
    ("state", "flags", "warned"),
    [
        (SecState.CLAIMED, 0x40, True),
        (SecState.UNCLAIMED, 0x40, False),  # an open device has no key to lose
        (None, 0x40, False),  # 2.x: no SEC2 state
        (SecState.CLAIMED, 0x00, False),
    ],
)
async def test_writing_warns_before_a_claimed_display_is_left_with_only_a_wired_recovery(
    caplog: pytest.LogCaptureFixture, state: SecState | None, flags: int, warned: bool
) -> None:
    written: list[GlobalConfig] = []
    view = _view(state, written)

    with caplog.at_level(logging.WARNING, logger="opendisplay.device.api"):
        await view.write(_config(_security(flags)))

    assert len(written) == 1  # a warning, never a refusal
    assert ("only a wired reflash" in caplog.text) is warned
