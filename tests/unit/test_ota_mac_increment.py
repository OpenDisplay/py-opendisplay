"""Test nRF DFU MAC increment carries across octets (§4)."""

from __future__ import annotations

import pytest

from opendisplay.ota import _increment_mac


@pytest.mark.parametrize(
    ("addr", "expected"),
    [
        ("AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"),
        ("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EF:00"),  # carry into previous octet
        ("AA:BB:CC:DD:FF:FF", "AA:BB:CC:DE:00:00"),  # double carry
        ("FF:FF:FF:FF:FF:FF", "00:00:00:00:00:00"),  # wraps around
    ],
)
def test_increment_mac_carries(addr: str, expected: str) -> None:
    assert _increment_mac(addr) == expected
