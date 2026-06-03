"""OpenDisplay device deep-link ("landing") URL encoding.

A landing URL points the user at the per-device config page and has the form:

    https://opendisplay.org/l/?<base64url(payload)>

``payload`` is a fixed 23-byte, big-endian identity blob -- the same bytes the
firmware renders as an on-screen QR code (see Firmwares/*/boot_screen.*). The
web page at /l/ base64url-decodes the query string back into these fields:

    offset  size  field            notes
    0       2     tag_type         u16, BE (DisplayConfig.tag_type; 0 if none)
    2       3     device id        low 3 bytes of the MAC == the "OD######" name
    5       16    AES key          encryption key, or all-zero if unknown
    21      2     manufacturer_id  u16, BE (OpenDisplay enum 0..4, NOT BLE 9286)

The result is base64url-encoded (RFC 4648 sec. 5: '+'->'-', '/'->'_') with the
trailing '=' padding stripped, then appended to ``LANDING_URL_PREFIX``.
"""

from __future__ import annotations

import base64

LANDING_URL_PREFIX = "https://opendisplay.org/l/?"

_PAYLOAD_SIZE = 23


def build_landing_payload(
    tag_type: int,
    device_id: bytes,
    encryption_key: bytes | None,
    manufacturer_id: int,
) -> bytes:
    """Build the 23-byte device-identity payload (see module docstring layout).

    Args:
        tag_type: Display tag type, encoded as a big-endian u16.
        device_id: The 3 identity bytes (lower 3 bytes of the MAC / "OD######").
        encryption_key: 16-byte AES key, or None to emit a zero key.
        manufacturer_id: OpenDisplay manufacturer enum (0..4), big-endian u16.

    Raises:
        ValueError: If device_id is not 3 bytes or the key is not 16 bytes.
    """
    if len(device_id) != 3:
        raise ValueError("device_id must be 3 bytes")
    key = encryption_key if encryption_key else b"\x00" * 16
    if len(key) != 16:
        raise ValueError("encryption_key must be 16 bytes")

    payload = bytearray(_PAYLOAD_SIZE)
    payload[0] = (tag_type >> 8) & 0xFF
    payload[1] = tag_type & 0xFF
    payload[2:5] = device_id
    payload[5:21] = key
    payload[21] = (manufacturer_id >> 8) & 0xFF
    payload[22] = manufacturer_id & 0xFF
    return bytes(payload)


def build_landing_url(
    tag_type: int,
    device_id: bytes,
    encryption_key: bytes | None,
    manufacturer_id: int,
) -> str:
    """Build the full ``https://opendisplay.org/l/?...`` deep link.

    See :func:`build_landing_payload` for the field semantics.
    """
    payload = build_landing_payload(tag_type, device_id, encryption_key, manufacturer_id)
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{LANDING_URL_PREFIX}{encoded}"
