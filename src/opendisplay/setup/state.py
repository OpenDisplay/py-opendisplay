"""``device_state()``: where a device stands (SECURITY_V2 § 5.3.1 / § 15.1; Web ``deviceState``).

SEC_STATUS is preferred, then the READ_IDENT tail bits, then the advertisement's status byte.
Every input is unauthenticated and so advisory: the state picks the steps to offer, and the
handshake makes the security decisions.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from ..sec2.wire import SecState

if TYPE_CHECKING:
    from ..models.advertisement import AdvertisementData
    from ..models.firmware import FirmwareVersion
    from ..models.ident import DeviceIdent
    from ..sec2.wire import SecStatus


class DeviceState(Enum):
    """Where a device stands before setup."""

    LEGACY = "legacy"  # firmware 2.x, or no SECURITY_V2
    VIRGIN = "virgin"  # just flashed: fresh claim mode, no display configured
    FRESH = "fresh"  # fresh claim mode with a display configured
    GESTURE = "gesture"  # claim mode entered by the two-power-cycle gesture
    OPEN = "open"  # unclaimed, normal mode
    CLAIMED = "claimed"
    CLAIMED_IN_CLAIM_MODE = "claimed_in_claim_mode"
    LOCKED = "locked"
    UNKNOWN = "unknown"


def _unconfigured(ident: DeviceIdent | None, advertisement: AdvertisementData | None) -> bool:
    if ident is not None and ident.has_tail and not ident.displays:
        return True
    return bool(advertisement is not None and advertisement.unconfigured)


def _from_status(status: SecStatus, unconfigured: bool) -> DeviceState:
    if status.state == SecState.LOCKED:
        return DeviceState.LOCKED
    if status.state == SecState.CLAIMED:
        return DeviceState.CLAIMED_IN_CLAIM_MODE if status.claim_mode else DeviceState.CLAIMED
    if status.claim_mode:
        if status.claim_mode_fresh:
            return DeviceState.VIRGIN if unconfigured else DeviceState.FRESH
        return DeviceState.GESTURE
    return DeviceState.OPEN


def device_state(
    *,
    firmware: FirmwareVersion | None,
    ident: DeviceIdent | None,
    sec_status: SecStatus | None,
    advertisement: AdvertisementData | None,
) -> DeviceState:
    """Classify a device from what it signals; any input may be missing."""
    if firmware is not None and firmware["major"] < 3:
        return DeviceState.LEGACY
    if ident is not None and ident.has_tail and not ident.sec2:
        return DeviceState.LEGACY
    unconfigured = _unconfigured(ident, advertisement)
    if sec_status is not None:
        return _from_status(sec_status, unconfigured)
    if ident is not None and ident.has_tail:
        if ident.claimed:
            return DeviceState.CLAIMED
        if ident.claim_mode:
            return DeviceState.VIRGIN if unconfigured else DeviceState.GESTURE
        return DeviceState.OPEN
    if advertisement is not None:
        if advertisement.session_required:
            return DeviceState.CLAIMED
        if advertisement.claim_mode:
            return DeviceState.VIRGIN if advertisement.unconfigured else DeviceState.GESTURE
    return DeviceState.UNKNOWN
