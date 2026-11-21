"""``plan_setup()``: the ordered steps for a device (Web ``planWizard`` and ``planMigrationAssistant``).

The website's wizard, with the rules our hardware work added:

- a display with no config, or one that needs migration, is configured (and its panel checked)
  before it is claimed: it cannot show the claim code otherwise;
- a 2.x device is backed up, given 3.0 and confirmed first; the plan is made again once it runs 3.0;
- a device in claim mode whose owner wants no key leaves claim mode first.

``notes`` carry the wizard's text keys unchanged (``awaiting_claim``, ``claim_choice``, ...), so a
caller can show its own texts. An empty ``steps`` means nothing to do, or a choice first
(``claim_choice``: claim, or use without a key).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from .state import DeviceState

if TYPE_CHECKING:
    from ..models.profile import DeviceProfile
    from ..sec2.wire import SecStatus


class Step(Enum):
    """One step of the flow, in the order they run."""

    BACK_UP = "back_up"
    INSTALL_3_0 = "install_3_0"
    CONFIRM_IMAGE = "confirm_image"
    USE_WITHOUT_KEY = "use_without_key"  # leave claim mode, unclaimed
    CONFIGURE_DISPLAY = "configure_display"
    VERIFY_PANEL = "verify_panel"
    CLAIM = "claim"
    ROTATE_KEY = "rotate_key"
    SECRETS = "secrets"
    DONE = "done"


@dataclass(frozen=True)
class SetupPlan:
    """What to do: the state it was planned from, the steps, and the text keys to show."""

    state: DeviceState
    steps: tuple[Step, ...]
    notes: tuple[str, ...]


_CLAIM_MODE_STATES = (DeviceState.VIRGIN, DeviceState.FRESH, DeviceState.GESTURE)
_CONFIGURE = (Step.CONFIGURE_DISPLAY, Step.VERIFY_PANEL)


def _configure_needed(profile: DeviceProfile | None) -> bool:
    return profile is not None and (not profile.configured or profile.needs_migration)


def _claim_mode_steps(
    state: DeviceState, profile: DeviceProfile | None, status: SecStatus | None, want_key: bool | None
) -> tuple[list[Step], list[str]]:
    configure = _configure_needed(profile)
    if want_key is False:
        return [Step.USE_WITHOUT_KEY, *(_CONFIGURE if configure else ())], ["use_without_key"]
    locked = status is not None and status.config_locked
    steps: list[Step] = []
    if not locked and (configure or state is DeviceState.VIRGIN):
        steps.append(Step.CONFIGURE_DISPLAY)
    if not locked and (configure or state in (DeviceState.VIRGIN, DeviceState.FRESH)):
        steps.append(Step.VERIFY_PANEL)
    return [*steps, Step.CLAIM, Step.SECRETS], ["awaiting_claim"]


def _open_steps(
    profile: DeviceProfile | None, status: SecStatus | None, want_key: bool | None
) -> tuple[list[Step], list[str]]:
    configure = list(_CONFIGURE) if _configure_needed(profile) else []
    notes = ["needs_migration"] if profile is not None and profile.needs_migration else []
    secrets = status is not None and status.legacy_secrets_open
    if secrets:
        notes.append("legacy_secrets")
    if want_key is None:
        return [], [*notes, "claim_choice"]
    if want_key is False:
        return configure, [*notes, "use_without_key"]
    return [*configure, Step.CLAIM, Step.SECRETS], [*notes, "gesture", *(["change_wifi"] if secrets else [])]


def _claimed_steps(
    profile: DeviceProfile | None, status: SecStatus | None, rotate_legacy_key: bool
) -> tuple[list[Step], list[str]]:
    steps = list(_CONFIGURE) if _configure_needed(profile) else []
    notes = ["needs_migration"] if profile is not None and profile.needs_migration else []
    if status is not None and (status.rotation_suggested or status.key_legacy):
        if rotate_legacy_key:
            steps.append(Step.ROTATE_KEY)
            notes.append("rotation_one_way")
        else:
            notes.append("rotation_pending")
    return steps, notes


def _legacy_steps(want_key: bool | None) -> tuple[list[Step], list[str]]:
    install = [Step.BACK_UP, Step.INSTALL_3_0, Step.CONFIRM_IMAGE]
    notes = ["replan_after_install"]
    if want_key is False:  # a wired install leaves it in claim mode
        return [*install, Step.USE_WITHOUT_KEY, *_CONFIGURE], [*notes, "use_without_key"]
    if want_key is None:
        return [*install, *_CONFIGURE], [*notes, "claim_choice"]
    return [*install, *_CONFIGURE, Step.CLAIM, Step.SECRETS], notes


def plan_setup(
    *,
    state: DeviceState,
    profile: DeviceProfile | None,
    sec_status: SecStatus | None,
    want_key: bool | None = None,
    rotate_legacy_key: bool = True,
) -> SetupPlan:
    """The steps for a device in ``state``.

    Args:
        state: From :func:`device_state`.
        profile: The device's profile (whether it is configured, whether it needs migration);
            ``None`` when it is not known yet, as before a 2.x device runs 3.0.
        sec_status: SEC_STATUS, when read.
        want_key: ``True`` to claim, ``False`` to use the device without a key, ``None`` not
            decided yet (an open device then asks).
        rotate_legacy_key: Replace a key carried over from 2.x on a claimed device.
    """
    steps: list[Step]
    notes: list[str]
    if state in _CLAIM_MODE_STATES:
        steps, notes = _claim_mode_steps(state, profile, sec_status, want_key)
    elif state is DeviceState.OPEN:
        steps, notes = _open_steps(profile, sec_status, want_key)
    elif state is DeviceState.CLAIMED:
        steps, notes = _claimed_steps(profile, sec_status, rotate_legacy_key)
    elif state is DeviceState.CLAIMED_IN_CLAIM_MODE:
        steps, notes = [Step.SECRETS], []
    elif state is DeviceState.LEGACY:
        steps, notes = _legacy_steps(want_key)
    elif state is DeviceState.LOCKED:
        steps, notes = [], ["locked", "factory_reset"]
    else:
        steps, notes = [], ["identify"]
    if steps:
        steps.append(Step.DONE)
    return SetupPlan(state=state, steps=tuple(steps), notes=tuple(notes))
