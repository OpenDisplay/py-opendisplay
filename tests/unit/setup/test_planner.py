"""The setup planner: device states (SECURITY_V2 § 5.3.1 / § 15.1) and the steps for each.

Ported case by case from the website's ``tests/od-wizard.test.js`` (Web ``v3/website`` b7df514),
plus the rules our own hardware work added: a display with no config is configured before it is
claimed (it cannot show the code), a 2.x device is backed up and installed first, and a device in
claim mode whose owner wants no key leaves claim mode.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opendisplay.protocol.responses import parse_read_ident
from opendisplay.sec2.wire import SecState, SecStatus
from opendisplay.setup import DeviceState, Step, device_state, plan_setup

S = Step

_FLAGS = {
    "claim_mode": 0x01,
    "legacy_secrets_open": 0x08,
    "rotation_suggested": 0x10,
    "claim_mode_fresh": 0x20,
    "config_locked": 0x40,
}


def _status(*, state: SecState = SecState.UNCLAIMED, key_legacy: bool = False, **flags: bool) -> SecStatus:
    bits = sum(_FLAGS[name] for name, on in flags.items() if on)
    return SecStatus(
        sec_ver=2,
        state=state,
        flags=bits,
        window_s=300 if flags.get("claim_mode") else 0,
        uid=bytes(8),
        timeout_s=3600,
        dflags=0x04 if key_legacy else 0,
        renders_left=3,
    )


def _fw(major: int) -> dict[str, Any]:
    return {"major": major, "minor": 0, "patch": 2, "sha": ""}


def _ident(flags: int, displays: int) -> Any:
    record = "37002003e00103" if displays else ""  # one display: 0x0037, 800x480, scheme 3
    return parse_read_ident(bytes.fromhex(f"00460401{displays:02x}{record}{flags:02x}a00f800003"))


def _adv(*, claim_mode: bool, session_required: bool, unconfigured: bool) -> Any:
    return SimpleNamespace(claim_mode=claim_mode, session_required=session_required, unconfigured=unconfigured)


def _profile(*, configured: bool = True, needs_migration: bool = False) -> Any:
    return SimpleNamespace(configured=configured, needs_migration=needs_migration)


def _state(**kw: Any) -> DeviceState:
    return device_state(
        firmware=kw.get("firmware", _fw(3)),
        ident=kw.get("ident"),
        sec_status=kw.get("sec_status"),
        advertisement=kw.get("advertisement"),
    )


# ── device_state ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "ident_displays", "want"),
    [
        (_status(claim_mode=True, claim_mode_fresh=True), 0, DeviceState.VIRGIN),
        (_status(claim_mode=True, claim_mode_fresh=True), 1, DeviceState.FRESH),
        (_status(claim_mode=True), 1, DeviceState.GESTURE),
        (_status(), 1, DeviceState.OPEN),
        (_status(state=SecState.CLAIMED), 1, DeviceState.CLAIMED),
        (_status(state=SecState.CLAIMED, claim_mode=True), 1, DeviceState.CLAIMED_IN_CLAIM_MODE),
        (_status(state=SecState.LOCKED), 1, DeviceState.LOCKED),
    ],
)
def test_sec_status_decides_the_state(status: SecStatus, ident_displays: int, want: DeviceState) -> None:
    assert _state(sec_status=status, ident=_ident(0x09, ident_displays)) is want


def test_firmware_2x_and_a_tail_without_sec_v2_are_legacy() -> None:
    assert _state(firmware=_fw(2)) is DeviceState.LEGACY
    assert _state(ident=_ident(0x01, 0)) is DeviceState.LEGACY  # a 3.0 tail without SEC_V2


def test_without_sec_status_the_ident_bits_decide() -> None:
    assert _state(ident=_ident(0x29, 0)) is DeviceState.VIRGIN  # the E1002 just flashed
    assert _state(ident=_ident(0x29, 1)) is DeviceState.GESTURE
    assert _state(ident=_ident(0x19, 1)) is DeviceState.CLAIMED
    assert _state(ident=_ident(0x09, 1)) is DeviceState.OPEN


def test_without_either_the_advertisement_decides() -> None:
    adv = _adv(claim_mode=True, session_required=False, unconfigured=True)
    assert _state(advertisement=adv) is DeviceState.VIRGIN
    assert (
        _state(advertisement=_adv(claim_mode=True, session_required=False, unconfigured=False)) is DeviceState.GESTURE
    )
    assert (
        _state(advertisement=_adv(claim_mode=False, session_required=True, unconfigured=False)) is DeviceState.CLAIMED
    )
    assert _state() is DeviceState.UNKNOWN


def test_an_unconfigured_advertisement_makes_a_fresh_claim_mode_virgin() -> None:
    adv = _adv(claim_mode=True, session_required=False, unconfigured=True)
    assert _state(sec_status=_status(claim_mode=True, claim_mode_fresh=True), advertisement=adv) is DeviceState.VIRGIN


# ── plan_setup ───────────────────────────────────────────────────────────────


def _plan(state: DeviceState, *, status: SecStatus | None = None, profile: Any = None, **kw: Any) -> Any:
    return plan_setup(state=state, profile=profile or _profile(), sec_status=status, **kw)


def test_virgin_device_is_configured_verified_then_claimed() -> None:
    p = _plan(DeviceState.VIRGIN, profile=_profile(configured=False), want_key=None)

    assert p.steps == (S.CONFIGURE_DISPLAY, S.VERIFY_PANEL, S.CLAIM, S.SECRETS, S.DONE)
    assert "awaiting_claim" in p.notes


def test_fresh_after_config_verifies_then_claims_and_skips_verify_once_the_code_was_drawn() -> None:
    a = _plan(DeviceState.FRESH, status=_status(claim_mode=True, claim_mode_fresh=True))
    assert a.steps == (S.VERIFY_PANEL, S.CLAIM, S.SECRETS, S.DONE)

    b = _plan(DeviceState.FRESH, status=_status(claim_mode=True, claim_mode_fresh=True, config_locked=True))
    assert b.steps == (S.CLAIM, S.SECRETS, S.DONE)


def test_a_locked_config_is_not_written_even_when_it_needs_migration() -> None:
    # Claim mode refuses config writes once the code was drawn (§ 7.4, config_locked).
    status = _status(claim_mode=True, claim_mode_fresh=True, config_locked=True)
    p = _plan(DeviceState.FRESH, status=status, profile=_profile(needs_migration=True))

    assert S.CONFIGURE_DISPLAY not in p.steps and S.VERIFY_PANEL not in p.steps


def test_gesture_claim_mode_goes_straight_to_claim() -> None:
    p = _plan(DeviceState.GESTURE, status=_status(claim_mode=True))

    assert p.steps == (S.CLAIM, S.SECRETS, S.DONE)
    assert "awaiting_claim" in p.notes


def test_an_open_device_offers_the_choice_then_follows_it() -> None:
    choice = _plan(DeviceState.OPEN, status=_status(), want_key=None)
    assert choice.steps == ()
    assert "claim_choice" in choice.notes

    claim = _plan(DeviceState.OPEN, status=_status(), want_key=True)
    assert claim.steps == (S.CLAIM, S.SECRETS, S.DONE)
    assert "gesture" in claim.notes  # two power cycles first

    no_key = _plan(DeviceState.OPEN, status=_status(), want_key=False)
    assert no_key.steps == ()  # nothing to do
    assert "use_without_key" in no_key.notes


def test_an_open_device_that_needs_migration_is_configured_first() -> None:
    p = _plan(DeviceState.OPEN, status=_status(), profile=_profile(needs_migration=True), want_key=True)

    assert p.steps == (S.CONFIGURE_DISPLAY, S.VERIFY_PANEL, S.CLAIM, S.SECRETS, S.DONE)
    assert "needs_migration" in p.notes


@pytest.mark.parametrize("profile", [_profile(configured=False), _profile(needs_migration=True)])
def test_no_config_and_needs_migration_both_lead_to_configure_display(profile: Any) -> None:
    # The EN05 after use_without_key(): open, unclaimed, no config at all.
    p = _plan(DeviceState.OPEN, status=_status(), profile=profile, want_key=False)

    assert p.steps == (S.CONFIGURE_DISPLAY, S.VERIFY_PANEL, S.DONE)


def test_a_device_with_no_config_is_configured_before_it_is_claimed() -> None:
    # It cannot show the claim code without a display.
    p = _plan(DeviceState.GESTURE, status=_status(claim_mode=True), profile=_profile(configured=False))

    assert p.steps.index(S.CONFIGURE_DISPLAY) < p.steps.index(S.CLAIM)


@pytest.mark.parametrize("state", [DeviceState.VIRGIN, DeviceState.FRESH, DeviceState.GESTURE])
def test_claim_mode_without_a_key_leaves_claim_mode_first(state: DeviceState) -> None:
    p = _plan(state, status=_status(claim_mode=True), profile=_profile(configured=False), want_key=False)

    assert p.steps == (S.USE_WITHOUT_KEY, S.CONFIGURE_DISPLAY, S.VERIFY_PANEL, S.DONE)
    assert S.CLAIM not in p.steps and S.SECRETS not in p.steps


def test_claimed_in_claim_mode_only_has_secrets_left() -> None:
    assert _plan(DeviceState.CLAIMED_IN_CLAIM_MODE, status=_status(state=SecState.CLAIMED, claim_mode=True)).steps == (
        S.SECRETS,
        S.DONE,
    )


def test_claimed_locked_unknown() -> None:
    assert _plan(DeviceState.CLAIMED, status=_status(state=SecState.CLAIMED)).steps == ()
    locked = _plan(DeviceState.LOCKED, status=_status(state=SecState.LOCKED))
    assert (locked.steps, locked.notes) == ((), ("locked", "factory_reset"))
    assert _plan(DeviceState.UNKNOWN).steps == ()


def test_a_claimed_device_with_a_2x_key_rotates_unless_asked_not_to() -> None:
    status = _status(state=SecState.CLAIMED, rotation_suggested=True)
    p = _plan(DeviceState.CLAIMED, status=status, profile=_profile(needs_migration=True))
    assert p.steps == (S.CONFIGURE_DISPLAY, S.VERIFY_PANEL, S.ROTATE_KEY, S.DONE)
    assert "rotation_one_way" in p.notes

    kept = _plan(DeviceState.CLAIMED, status=_status(state=SecState.CLAIMED, key_legacy=True), rotate_legacy_key=False)
    assert kept.steps == ()
    assert "rotation_pending" in kept.notes


def test_an_open_device_holding_2x_secrets_is_told_and_claiming_changes_them() -> None:
    status = _status(legacy_secrets_open=True)
    assert "legacy_secrets" in _plan(DeviceState.OPEN, status=status, want_key=None).notes

    p = _plan(DeviceState.OPEN, status=status, want_key=True)
    assert p.steps == (S.CLAIM, S.SECRETS, S.DONE)
    assert "change_wifi" in p.notes


def test_a_2x_device_is_backed_up_installed_and_confirmed_before_the_3_0_steps() -> None:
    p = plan_setup(state=DeviceState.LEGACY, profile=None, sec_status=None, want_key=True)

    assert p.steps == (
        S.BACK_UP,
        S.INSTALL_3_0,
        S.CONFIRM_IMAGE,
        S.CONFIGURE_DISPLAY,
        S.VERIFY_PANEL,
        S.CLAIM,
        S.SECRETS,
        S.DONE,
    )
    assert "replan_after_install" in p.notes

    # A wired install leaves it in claim mode: without a key it leaves claim mode first.
    no_key = plan_setup(state=DeviceState.LEGACY, profile=None, sec_status=None, want_key=False)
    assert no_key.steps[3:] == (S.USE_WITHOUT_KEY, S.CONFIGURE_DISPLAY, S.VERIFY_PANEL, S.DONE)

    choice = plan_setup(state=DeviceState.LEGACY, profile=None, sec_status=None, want_key=None)
    assert choice.steps == (S.BACK_UP, S.INSTALL_3_0, S.CONFIRM_IMAGE, S.CONFIGURE_DISPLAY, S.VERIFY_PANEL, S.DONE)
    assert "claim_choice" in choice.notes
