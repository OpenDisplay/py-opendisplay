"""AES-128-CCM/CMAC crypto helpers for OpenDisplay BLE encryption.

Implements the application-layer encryption protocol used by firmware >= commit b04a22b.
All operations match the firmware's mbedtls/CryptoCell implementations exactly.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.cmac import CMAC

# Firmware placeholder device ID (hardcoded in firmware, never changes)
_DEVICE_ID = bytes([0x00, 0x00, 0x00, 0x01])

# CCM auth tag length used by firmware
_TAG_LEN = 12


def aes_cmac(key: bytes, data: bytes) -> bytes:
    """Compute AES-128-CMAC."""
    c = CMAC(algorithms.AES(key))
    c.update(data)
    return c.finalize()


def aes_ecb_encrypt(key: bytes, block: bytes) -> bytes:
    """Encrypt a single 16-byte block with AES-ECB (used in KDF only)."""
    cipher = Cipher(algorithms.AES(key), modes.ECB())  # noqa: S305 — intentional single-block KDF step
    enc = cipher.encryptor()
    return enc.update(block) + enc.finalize()


def derive_session_key(master_key: bytes, client_nonce: bytes, server_nonce: bytes) -> bytes:
    """Derive per-session AES-128 key from master key and nonces.

    Matches firmware deriveSessionKey():
      1. CMAC(master_key, label || 0x00 || device_id || client_nonce || server_nonce || 0x00 0x80)
      2. AES-ECB(master_key, counter_be(1, 8 bytes) || intermediate[0:8])
    """
    label = b"OpenDisplay session"
    cmac_input = label + b"\x00" + _DEVICE_ID + client_nonce + server_nonce + bytes([0x00, 0x80])
    intermediate = aes_cmac(master_key, cmac_input)

    # counter = 1, big-endian 8 bytes
    counter_be = (1).to_bytes(8, "big")
    final_input = counter_be + intermediate[:8]
    return aes_ecb_encrypt(master_key, final_input)


def derive_session_id(session_key: bytes, client_nonce: bytes, server_nonce: bytes) -> bytes:
    """Derive 8-byte session ID from session key and nonces.

    Matches firmware deriveSessionId():
      AES-CMAC(session_key, client_nonce || server_nonce)[0:8]
    """
    return aes_cmac(session_key, client_nonce + server_nonce)[:8]


def compute_challenge_response(master_key: bytes, server_nonce: bytes, client_nonce: bytes) -> bytes:
    """Compute CMAC challenge proof sent to device in step 2 of auth.

    CMAC(master_key, server_nonce || client_nonce || device_id)
    """
    return aes_cmac(master_key, server_nonce + client_nonce + _DEVICE_ID)


def get_nonce(session_id: bytes, counter: int) -> bytes:
    """Build the 16-byte full nonce: session_id(8) || counter_be(8)."""
    return session_id + counter.to_bytes(8, "big")


def encrypt_command(session_key: bytes, session_id: bytes, counter: int, cmd: bytes, payload: bytes) -> bytes:
    """Encrypt a command payload for sending to the device.

    Returns full BLE write bytes: [cmd:2][nonce_full:16][ciphertext][tag:12]

    The CCM nonce is nonce_full[3:16] (13 bytes).
    AD = cmd bytes (2 bytes).
    Plaintext = [len(payload):1][payload].
    """
    nonce_full = get_nonce(session_id, counter)
    ccm_nonce = nonce_full[3:]  # 13 bytes
    ad = cmd  # 2-byte command code as AAD
    plaintext = bytes([len(payload)]) + payload

    aesccm = AESCCM(session_key, tag_length=_TAG_LEN)
    ciphertext_and_tag = aesccm.encrypt(ccm_nonce, plaintext, ad)
    ciphertext = ciphertext_and_tag[:-_TAG_LEN]
    tag = ciphertext_and_tag[-_TAG_LEN:]

    return cmd + nonce_full + ciphertext + tag


def decrypt_response(session_key: bytes, raw: bytes) -> tuple[int, bytes]:
    """Decrypt an encrypted response notification from the device.

    Parses [cmd:2][nonce_full:16][ciphertext][tag:12].
    Returns (cmd_code, plaintext_payload).

    Raises:
        ValueError: If data is too short or tag verification fails.
    """
    min_len = 2 + 16 + 1 + _TAG_LEN  # cmd + nonce + 1-byte payload + tag
    if len(raw) < min_len:
        raise ValueError(f"Encrypted response too short: {len(raw)} bytes")

    cmd_code = int.from_bytes(raw[:2], "big")
    nonce_full = raw[2:18]
    ciphertext = raw[18:-_TAG_LEN]
    tag = raw[-_TAG_LEN:]
    ccm_nonce = nonce_full[3:]  # 13 bytes
    ad = raw[:2]  # cmd bytes as AAD

    aesccm = AESCCM(session_key, tag_length=_TAG_LEN)
    # cryptography library expects ciphertext+tag concatenated
    decrypted = aesccm.decrypt(ccm_nonce, ciphertext + tag, ad)

    # First byte is payload length
    payload_len = decrypted[0]
    payload = decrypted[1 : 1 + payload_len]
    return cmd_code, payload


def generate_client_nonce() -> bytes:
    """Generate 16 cryptographically random bytes for the client nonce."""
    return os.urandom(16)
