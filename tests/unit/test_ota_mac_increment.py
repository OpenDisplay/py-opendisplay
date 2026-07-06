"""Test nRF DFU MAC increment wraps the last octet without carry (§4).

Nordic DFU bootloaders advertise at ``original + 1``, incrementing only the last
octet as a ``uint8`` (wrap 0xFF -> 0x00, no carry into the previous octet). This
matches the standalone ``nrf-ota`` scanner's convention.
"""

from __future__ import annotations

import pytest

from opendisplay.ota import _increment_mac


@pytest.mark.parametrize(
    ("addr", "expected"),
    [
        ("AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"),
        ("AA:BB:CC:DD:EE:FF", "AA:BB:CC:DD:EE:00"),  # wraps, no carry
        ("AA:BB:CC:DD:FF:FF", "AA:BB:CC:DD:FF:00"),  # only the last octet wraps
        ("FF:FF:FF:FF:FF:FF", "FF:FF:FF:FF:FF:00"),  # previous octets untouched
    ],
)
def test_increment_mac_no_carry(addr: str, expected: str) -> None:
    assert _increment_mac(addr) == expected
