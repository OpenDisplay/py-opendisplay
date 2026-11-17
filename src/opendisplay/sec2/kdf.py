"""SEC2 key derivation, the key hierarchy, the claim commitment and code, and the transcripts.

SECURITY_V2.md revision 5, § 2.1-2.3, § 8.2-8.4 and § 9.3; a port of the matching part of the
website's od-sec2.js (``kdfInput``, ``kdf``, ``extract``, ``keyHierarchy``, ``kV1AtRest``,
``commitment``, ``codeRaw``, ``codeFromRaw``, ``normalizeTypedCode``, ``claimTranscript``,
``authTranscript``, ``wrapAad``). Implemented exactly as the spec says: it has not been reviewed
by a cryptographer, and it must not be "improved" here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from .primitives import cmac

# Label -> the Context lengths it may take (§ 2.3; the lengths are normative). A wrong length is a
# programming error and is refused.
LABELS: Final[dict[str, tuple[int, ...]]] = {
    "OD2/k-ext": (8,),
    "OD2/k-conf": (8,),
    "OD2/k-lan": (8,),
    "OD2/v1-compat": (8,),
    "OD2/wrap": (179,),
    "OD2/h2d": (179, 178),
    "OD2/d2h": (179, 178),
    "OD2/sid": (179, 178),
    "OD2/claim-c": (179,),
    "OD2/claim-d": (179,),
    "OD2/auth-c": (178,),
    "OD2/auth-d": (178,),
    "OD2/rotate-c": (24,),
}
UID_LEN: Final = 8
CLAIM_TRANSCRIPT_LEN: Final = 179
AUTH_TRANSCRIPT_LEN: Final = 178
VERSION: Final = 0x02
WRAP_NONCE: Final = bytes(13)  # W's nonce: k_wrap is single-use (§ 8.2, § 19 E10)
_CODE_MODULUS: Final = 1_000_000


def kdf_input(label: str, context: bytes) -> bytes:
    """``0x01 || Label || 0x00 || Context || 0x00 0x80`` (§ 2.1).

    Raises:
        ValueError: An unknown label, or a context of a length the label does not take.
    """
    allowed = LABELS.get(label)
    if allowed is None:
        raise ValueError(f"Unknown KDF label {label!r}")
    if len(context) not in allowed:
        raise ValueError(f"KDF {label}: context of {len(context)} bytes, want {' or '.join(map(str, allowed))}")
    return b"\x01" + label.encode("ascii") + b"\x00" + context + b"\x00\x80"


def kdf(k_in: bytes, label: str, context: bytes) -> bytes:
    """``KDF(K_in, Label, Context) = CMAC(K_in, kdf_input(Label, Context))``: one 16-byte block."""
    return cmac(k_in, kdf_input(label, context))


def extract(salt: bytes, z: bytes) -> bytes:
    """``PRK = CMAC(salt, Z)`` (§ 2.1, SP 800-56C two-step extraction)."""
    return cmac(salt, z)


def _check_uid(uid: bytes) -> None:
    if len(uid) != UID_LEN:
        raise ValueError(f"uid must be {UID_LEN} bytes, got {len(uid)}")


@dataclass(frozen=True)
class KeyHierarchy:
    """The keys derived from K_dev (§ 2.2). K_dev itself is never a CMAC or CCM key."""

    k_ext: bytes = field(repr=False)  # salt for session extraction
    k_conf: bytes = field(repr=False)  # key-confirmation tags (claim, rotation)
    k_lan: bytes = field(repr=False)  # TLS-PSK for the LAN transport (§ 12)
    k_v1_derived: bytes = field(repr=False)  # the 0x27 key at rest once rotated (§ 10.2)


def key_hierarchy(k_dev: bytes, uid: bytes) -> KeyHierarchy:
    """K_ext, K_conf, K_lan and K_v1 from a device key and the device's uid."""
    _check_uid(uid)
    return KeyHierarchy(
        k_ext=kdf(k_dev, "OD2/k-ext", uid),
        k_conf=kdf(k_dev, "OD2/k-conf", uid),
        k_lan=kdf(k_dev, "OD2/k-lan", uid),
        k_v1_derived=kdf(k_dev, "OD2/v1-compat", uid),
    )


def k_v1_at_rest(k_dev: bytes, uid: bytes, *, legacy_unrotated: bool) -> bytes:
    """The 0x27 key at rest (§ 10.2): K_dev itself while LEGACY and never rotated, else derived."""
    _check_uid(uid)
    return bytes(k_dev) if legacy_unrotated else kdf(k_dev, "OD2/v1-compat", uid)


def commitment(nd: bytes, uid: bytes, qd: bytes, qc: bytes) -> bytes:
    """``Cd = CMAC(Nd, "OD2/commit" || uid || Qd || Qc)``: binding because Nd is the key (§ 8.3)."""
    return cmac(nd, b"OD2/commit" + uid + qd + qc)


def code_raw(nc: bytes, uid: bytes, qc: bytes, qd: bytes, nd: bytes) -> bytes:
    """``CMAC(Nc, "OD2/code" || uid || Qc || Qd || Nd)``, the code's source (§ 8.4)."""
    return cmac(nc, b"OD2/code" + uid + qc + qd + nd)


def code_from_raw(raw: bytes) -> str:
    """The six-digit code: ``uint32 big-endian of raw[12..15] mod 1 000 000``, zero-padded."""
    return f"{int.from_bytes(raw[12:16], 'big') % _CODE_MODULUS:06d}"


def format_code(code: str) -> str:
    """The code as the device shows it: ``"123 456"``."""
    return f"{code[:3]} {code[3:]}"


def normalize_typed_code(text: str) -> str | None:
    """What the user typed, as six digits; ``None`` for anything else.

    Spaces, dashes and dots between digits are allowed ("508 279", "508-279"); letters or a
    seventh digit are refused, so a typo is never silently corrected.
    """
    digits = re.sub(r"[\s.\-]", "", text.strip())
    return digits if re.fullmatch(r"[0-9]{6}", digits) else None


def _transcript(  # pylint: disable=too-many-arguments
    prefix: bytes, dflags: int, uid: bytes, qc: bytes, qd: bytes, nc: bytes, nd: bytes, length: int
) -> bytes:
    transcript = prefix + bytes([VERSION, dflags & 0xFF]) + uid + qc + qd + nc + nd
    if len(transcript) != length:
        raise ValueError(f"{prefix.decode()} transcript must be {length} bytes, got {len(transcript)}")
    return transcript


def claim_transcript(dflags: int, uid: bytes, qc: bytes, qd: bytes, nc: bytes, nd: bytes) -> bytes:
    """``Tc = "OD2/claim" || 0x02 || dflags || uid || Qc || Qd || Nc || Nd`` (179 bytes)."""
    return _transcript(b"OD2/claim", dflags, uid, qc, qd, nc, nd, CLAIM_TRANSCRIPT_LEN)


def auth_transcript(dflags: int, uid: bytes, qc: bytes, qd: bytes, nc: bytes, nd: bytes) -> bytes:
    """``Ta = "OD2/auth" || 0x02 || dflags || uid || Qc || Qd || Nc || Nd`` (178 bytes)."""
    return _transcript(b"OD2/auth", dflags, uid, qc, qd, nc, nd, AUTH_TRANSCRIPT_LEN)


def wrap_aad(uid: bytes) -> bytes:
    """W's associated data: ``"OD2/wrap" || uid``."""
    return b"OD2/wrap" + uid


@dataclass(frozen=True)
class SessionKeys:
    """The record keys and session id of one claim or session (§ 8.2, § 9.3)."""

    k_h2d: bytes = field(repr=False)
    k_d2h: bytes = field(repr=False)
    sid: bytes = field(repr=False)  # binds SEC_KEY; never on the wire


def session_keys(prk: bytes, transcript: bytes) -> SessionKeys:
    """``k_h2d``, ``k_d2h`` and ``sid`` from a claim (Tc) or session (Ta) PRK and transcript."""
    return SessionKeys(
        k_h2d=kdf(prk, "OD2/h2d", transcript),
        k_d2h=kdf(prk, "OD2/d2h", transcript),
        sid=kdf(prk, "OD2/sid", transcript),
    )
