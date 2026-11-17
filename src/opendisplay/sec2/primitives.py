"""The SEC2 primitives (SECURITY_V2.md § 2): AES-128-CMAC, AES-128-CCM, P-256 ECDH.

CCM uses a 13-byte nonce (L = 2) and a 12-byte tag (M = 12); its output is ``c || t``. ECDH
returns the 32-byte big-endian x-coordinate. A public key on the wire is ``X || Y`` (64 bytes),
the SEC1 uncompressed form without its 0x04 prefix. A received key is validated here, before any
library call, as the spec requires of every implementation.
"""

from __future__ import annotations

import secrets
from typing import Final

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from ..crypto import aes_cmac
from .errors import Sec2BadPointError

PrivateKey = ec.EllipticCurvePrivateKey

CCM_NONCE_LEN: Final = 13
CCM_TAG_LEN: Final = 12
POINT_LEN: Final = 64
# P-256 (secp256r1) domain parameters, FIPS 186-4 D.1.2.3.
P256_P: Final = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
P256_B: Final = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
P256_N: Final = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def cmac(key: bytes, message: bytes) -> bytes:
    """AES-128-CMAC, the full 16-byte output (RFC 4493)."""
    return aes_cmac(key, message)


def _check_nonce(nonce: bytes) -> None:
    if len(nonce) != CCM_NONCE_LEN:
        raise ValueError(f"SEC2 CCM needs a {CCM_NONCE_LEN}-byte nonce, got {len(nonce)}")


def ccm_encrypt(key: bytes, nonce: bytes, aad: bytes, plaintext: bytes) -> bytes:
    """AES-128-CCM with a 13-byte nonce and a 12-byte tag; returns ``ciphertext || tag``."""
    _check_nonce(nonce)
    return AESCCM(key, tag_length=CCM_TAG_LEN).encrypt(nonce, plaintext, aad)


def ccm_decrypt(key: bytes, nonce: bytes, aad: bytes, ct_tag: bytes) -> bytes:
    """Open ``ciphertext || tag``.

    Raises:
        cryptography.exceptions.InvalidTag: The tag does not verify.
    """
    _check_nonce(nonce)
    return AESCCM(key, tag_length=CCM_TAG_LEN).decrypt(nonce, ct_tag, aad)


def _public_bytes(private: PrivateKey) -> bytes:
    numbers = private.public_key().public_numbers()
    return numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")


def generate_key_pair() -> tuple[PrivateKey, bytes]:
    """A fresh P-256 key pair: the private key and its public ``X || Y``."""
    private = ec.derive_private_key(secrets.randbelow(P256_N - 1) + 1, ec.SECP256R1())
    return private, _public_bytes(private)


def key_pair_from_scalar(scalar: bytes) -> tuple[PrivateKey, bytes]:
    """The key pair of a given scalar. For tests and vectors only."""
    private = ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())
    return private, _public_bytes(private)


def is_valid_point(q: bytes) -> bool:
    """Whether ``q`` (``X || Y``) is a valid P-256 public key: X < p, Y < p, and on the curve.

    P-256 has cofactor 1, so this is full public-key validation (SP 800-56A § 5.6.2.3.3); the
    identity has no ``X || Y`` encoding and (0, 0) fails the curve equation.
    """
    if len(q) != POINT_LEN:
        return False
    x, y = int.from_bytes(q[:32], "big"), int.from_bytes(q[32:], "big")
    if x >= P256_P or y >= P256_P:
        return False
    return (y * y - (x * x * x - 3 * x + P256_B)) % P256_P == 0


def _exchange(private: PrivateKey, peer_q: bytes) -> bytes:
    peer = ec.EllipticCurvePublicNumbers(
        int.from_bytes(peer_q[:32], "big"), int.from_bytes(peer_q[32:], "big"), ec.SECP256R1()
    ).public_key()
    return private.exchange(ec.ECDH(), peer)


def ecdh(private: PrivateKey, peer_q: bytes) -> bytes:
    """P-256 ECC CDH: the 32-byte x-coordinate of ``d * Q``.

    Raises:
        Sec2BadPointError: ``peer_q`` is not a valid point; checked before the library is called.
    """
    if not is_valid_point(peer_q):
        raise Sec2BadPointError()
    return _exchange(private, peer_q)
