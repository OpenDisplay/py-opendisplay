"""The State column of ``opendisplay scan``: what an advertisement says about claim and config."""

from __future__ import annotations

import pytest

from opendisplay.cli import _scan_state
from opendisplay.models.advertisement import parse_advertisement

FRESH_3_0 = "ffffffffffffffffffffff860016"  # no config, claim mode, not claimed (captured from hardware)
CONFIGURED_OPEN = "0a000000000000000000007e8a10"  # ordinary 2.x device without a key
CONFIGURED_KEYED = "0a000000000000000000007e8a18"  # status bit 3: a session (2.x: the key) is required
CLAIMED_UNCONFIGURED = "ffffffffffffffffffffff86001a"  # claimed 3.0 device whose config lacks drive data


@pytest.mark.parametrize(
    ("payload", "state"),
    [
        (FRESH_3_0, "awaiting claim, unconfigured"),
        (CONFIGURED_OPEN, ""),
        (CONFIGURED_KEYED, "key required"),
        (CLAIMED_UNCONFIGURED, "key required, unconfigured"),
    ],
)
def test_scan_state(payload: str, state: str) -> None:
    assert _scan_state(parse_advertisement(bytes.fromhex(payload))) == state


def test_scan_state_without_an_advertisement() -> None:
    assert _scan_state(None) == ""
