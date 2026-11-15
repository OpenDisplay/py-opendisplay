"""Known answers for the firmware 2.x session: handshake values and sealed frames.

The other crypto tests check structure (which inputs feed which value). These pin the actual
bytes, so a change to the key derivation, the proof, the nonce layout or the frame format fails
here before it fails against a device.

The expected values were produced by this library at the commit that had just authenticated
and uploaded against firmware 2.2.1 (EFR32BG22) and 2.26.7 (ESP32-S3) on 2026-11-15. They are
a record of that behaviour, not vectors published by the firmware.
"""

from __future__ import annotations

import pytest

from opendisplay.crypto import (
    compute_challenge_response,
    compute_server_proof,
    decrypt_response,
    derive_session_id,
    derive_session_key,
    encrypt_command,
)
from opendisplay.device.channel import LegacySession

MASTER_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
SERVER_NONCE = bytes.fromhex("101112131415161718191a1b1c1d1e1f")
CLIENT_NONCE = bytes.fromhex("202122232425262728292a2b2c2d2e2f")
DEVICE_ID = bytes.fromhex("a1b2c3d4")

SESSION_KEY = bytes.fromhex("2267b3573b2c92a53b3047321b1d1361")
SESSION_ID = bytes.fromhex("b9c1b4d5ab94229b")

# (counter, plaintext frame = opcode + payload, sealed frame)
FRAMES = [
    (
        0,
        bytes.fromhex("0071") + bytes(range(8)),
        bytes.fromhex("0071b9c1b4d5ab94229b0000000000000000c234e1db56d23c66ded269f70833b4f1bff3b92a2d"),
    ),
    (
        5,
        bytes.fromhex("0043"),
        bytes.fromhex("0043b9c1b4d5ab94229b0000000000000005f62a3043c1d465f05fff1b977b"),
    ),
]


def test_challenge_response() -> None:
    response = compute_challenge_response(MASTER_KEY, SERVER_NONCE, CLIENT_NONCE, DEVICE_ID)

    assert response.hex() == "9fe732df32d8b28c7fe087d7d2d31212"


def test_session_key_and_id() -> None:
    session_key = derive_session_key(MASTER_KEY, CLIENT_NONCE, SERVER_NONCE, DEVICE_ID)

    assert session_key == SESSION_KEY
    assert derive_session_id(session_key, CLIENT_NONCE, SERVER_NONCE) == SESSION_ID


def test_server_proof() -> None:
    proof = compute_server_proof(SESSION_KEY, SERVER_NONCE, CLIENT_NONCE, DEVICE_ID)

    assert proof.hex() == "c800aa33c60cd6945116424da1dc7a8b"


@pytest.mark.parametrize(("counter", "plain", "sealed"), FRAMES)
def test_sealed_frame(counter: int, plain: bytes, sealed: bytes) -> None:
    assert encrypt_command(SESSION_KEY, SESSION_ID, counter, plain[:2], plain[2:]) == sealed


@pytest.mark.parametrize(("counter", "plain", "sealed"), FRAMES)
def test_opened_frame(counter: int, plain: bytes, sealed: bytes) -> None:
    opcode, payload = decrypt_response(SESSION_KEY, sealed)

    assert opcode.to_bytes(2, "big") + payload == plain


def test_session_seals_with_its_running_counter() -> None:
    session = LegacySession(session_key=SESSION_KEY, session_id=SESSION_ID)

    assert session.seal(FRAMES[0][1]) == FRAMES[0][2]
    assert session.nonce_counter == 1

    session.nonce_counter = 5
    assert session.seal(FRAMES[1][1]) == FRAMES[1][2]
    assert session.open(FRAMES[1][2]) == FRAMES[1][1]
