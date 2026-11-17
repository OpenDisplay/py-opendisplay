"""SEC2 key derivation, commitment, code and transcripts against the shared vectors.

SECURITY_V2.md § 2.1-2.3, § 8.2-8.4, § 9.3; vector groups V1_kdf, V2_key_hierarchy, V8_code and
the derived values in V3_claim and V4_auth. Every named intermediate is compared, so a wrong label
length or byte order is located, not just detected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from opendisplay.sec2 import kdf
from opendisplay.sec2.primitives import ccm_decrypt, ccm_encrypt, ecdh, key_pair_from_scalar

VECTORS = json.loads((Path(__file__).parents[2] / "fixtures" / "sec2_vectors.json").read_text())
_h = bytes.fromhex


@pytest.mark.parametrize("row", VECTORS["V1_kdf"], ids=lambda r: f"{r['label']}-{r['context_len']}")
def test_kdf_rows(row: dict[str, Any]) -> None:
    assert len(row["label"]) == row["label_len"]
    assert kdf.kdf_input(row["label"], _h(row["context"])) == _h(row["cmac_input"])
    assert kdf.kdf(_h(row["k_in"]), row["label"], _h(row["context"])) == _h(row["out"])


def test_a_context_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(ValueError, match="OD2/k-ext"):
        kdf.kdf_input("OD2/k-ext", bytes(9))


def test_an_unknown_label_is_refused() -> None:
    with pytest.raises(ValueError, match="label"):
        kdf.kdf_input("OD2/nope", bytes(8))


@pytest.mark.parametrize("row", VECTORS["V2_key_hierarchy"], ids=lambda r: r["origin"])
def test_key_hierarchy(row: dict[str, Any]) -> None:
    keys = kdf.key_hierarchy(_h(row["k_dev"]), _h(row["uid"]))

    assert (keys.k_ext, keys.k_conf, keys.k_lan, keys.k_v1_derived) == (
        _h(row["k_ext"]),
        _h(row["k_conf"]),
        _h(row["k_lan"]),
        _h(row["k_v1_derived"]),
    )
    legacy_unrotated = row["origin"] == "LEGACY"
    assert kdf.k_v1_at_rest(_h(row["k_dev"]), _h(row["uid"]), legacy_unrotated=legacy_unrotated) == _h(
        row["k_v1_at_rest"]
    )


def test_key_hierarchy_does_not_show_keys_in_its_repr() -> None:
    keys = kdf.key_hierarchy(bytes(range(16)), bytes(8))

    assert keys.k_ext.hex() not in repr(keys) and "k_ext" not in repr(keys)


def test_a_uid_of_the_wrong_length_is_refused() -> None:
    with pytest.raises(ValueError, match="uid"):
        kdf.key_hierarchy(bytes(16), bytes(7))


@pytest.mark.parametrize("row", VECTORS["V8_code"], ids=lambda r: r["case"])
def test_code_from_raw(row: dict[str, Any]) -> None:
    assert kdf.code_from_raw(_h(row["code_raw"])) == row["code"]
    assert kdf.format_code(row["code"]) == row["display"]


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("123456", "123456"),
        ("123 456", "123456"),
        ("123-456", "123456"),
        (" 12.34 56 ", "123456"),
        ("12345", None),
        ("1234567", None),
        ("12a456", None),
        ("", None),
    ],
)
def test_normalize_typed_code(typed: str, expected: str | None) -> None:
    assert kdf.normalize_typed_code(typed) == expected


@pytest.mark.parametrize("row", VECTORS["V3_claim"], ids=lambda r: f"frag{r['frag_max']}-{r['inputs']['claim_mode']}")
def test_claim_derivations(row: dict[str, Any]) -> None:
    i = row["inputs"]
    uid, dflags = _h(i["uid"]), int(i["dflags"], 16)
    dc, qc = key_pair_from_scalar(_h(i["dc"]))
    dd, qd = key_pair_from_scalar(_h(i["dd"]))
    nc, nd, k_new = _h(i["Nc"]), _h(i["Nd"]), _h(i["K_new"])
    assert (qc, qd) == (_h(row["Qc"]), _h(row["Qd"]))

    assert kdf.commitment(nd, uid, qd, qc) == _h(row["Cd"])
    tc = kdf.claim_transcript(dflags, uid, qc, qd, nc, nd)
    assert tc == _h(row["Tc"])
    z = ecdh(dc, qd)
    assert z == ecdh(dd, qc) == _h(row["Z"])
    prk = kdf.extract(bytes(16), z)
    assert prk == _h(row["PRK"])
    k_wrap = kdf.kdf(prk, "OD2/wrap", tc)
    assert k_wrap == _h(row["k_wrap"])
    w = ccm_encrypt(k_wrap, kdf.WRAP_NONCE, kdf.wrap_aad(uid), k_new)
    assert w == _h(row["W"])
    assert ccm_decrypt(k_wrap, kdf.WRAP_NONCE, kdf.wrap_aad(uid), w) == k_new
    raw = kdf.code_raw(nc, uid, qc, qd, nd)
    assert raw == _h(row["code_raw"])
    assert kdf.code_from_raw(raw) == row["code"] and kdf.format_code(row["code"]) == row["code_display"]
    k_conf = kdf.key_hierarchy(k_new, uid).k_conf
    assert k_conf == _h(row["K_conf"])
    assert kdf.kdf(k_conf, "OD2/claim-c", tc) == _h(row["tag_c"])
    assert kdf.kdf(k_conf, "OD2/claim-d", tc) == _h(row["tag_d"])
    keys = kdf.session_keys(prk, tc)
    assert (keys.k_h2d, keys.k_d2h, keys.sid) == (_h(row["k_h2d"]), _h(row["k_d2h"]), _h(row["sid"]))


@pytest.mark.parametrize("row", VECTORS["V4_auth"], ids=lambda r: f"frag{r['frag_max']}-{r['inputs']['origin']}")
def test_auth_derivations(row: dict[str, Any]) -> None:
    i = row["inputs"]
    uid, dflags, k_dev = _h(i["uid"]), int(i["dflags"], 16), _h(i["k_dev"])
    dc, qc = key_pair_from_scalar(_h(i["dc"]))
    _, qd = key_pair_from_scalar(_h(i["dd"]))
    nc, nd = _h(i["Nc"]), _h(i["Nd"])

    k_ext = kdf.key_hierarchy(k_dev, uid).k_ext
    assert k_ext == _h(row["K_ext"])
    assert (qc, qd) == (_h(row["Qc"]), _h(row["Qd"]))
    ta = kdf.auth_transcript(dflags, uid, qc, qd, nc, nd)
    assert ta == _h(row["Ta"])
    z = ecdh(dc, qd)
    assert z == _h(row["Z"])
    prk = kdf.extract(k_ext, z)
    assert prk == _h(row["PRK"])
    assert kdf.kdf(prk, "OD2/auth-c", ta) == _h(row["tag_c"])
    assert kdf.kdf(prk, "OD2/auth-d", ta) == _h(row["tag_d"])
    keys = kdf.session_keys(prk, ta)
    assert (keys.k_h2d, keys.k_d2h, keys.sid) == (_h(row["k_h2d"]), _h(row["k_d2h"]), _h(row["sid"]))


def test_transcripts_have_their_normative_lengths() -> None:
    q, n = bytes(64), bytes(16)

    assert len(kdf.claim_transcript(1, bytes(8), q, q, n, n)) == 179
    assert len(kdf.auth_transcript(1, bytes(8), q, q, n, n)) == 178
    with pytest.raises(ValueError, match="179"):
        kdf.claim_transcript(1, bytes(7), q, q, n, n)
