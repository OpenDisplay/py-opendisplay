"""SEC2 primitives against the shared vectors (SECURITY_V2.md § 2; vector group V0_primitives).

Vectors: tests/fixtures/sec2_vectors.json, vendored from Web v3/website b7df514
(schema opendisplay-sec2-vectors/1, spec revision 5).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from cryptography.exceptions import InvalidTag

from opendisplay.sec2 import primitives
from opendisplay.sec2.errors import Sec2BadPointError
from opendisplay.sec2.primitives import (
    P256_P,
    ccm_decrypt,
    ccm_encrypt,
    cmac,
    ecdh,
    generate_key_pair,
    is_valid_point,
    key_pair_from_scalar,
)

VECTORS = json.loads((Path(__file__).parents[2] / "fixtures" / "sec2_vectors.json").read_text())
V0 = VECTORS["V0_primitives"]
_h = bytes.fromhex


@pytest.mark.parametrize("row", V0["cmac_rfc4493"])
def test_cmac_rfc4493(row: dict[str, str]) -> None:
    assert cmac(_h(row["key"]), _h(row["msg"])) == _h(row["mac"])


@pytest.mark.parametrize("row", V0["ccm_m12_l2"])
def test_ccm_encrypt_and_decrypt(row: dict[str, str]) -> None:
    key, nonce, aad, pt = _h(row["key"]), _h(row["nonce"]), _h(row["aad"]), _h(row["pt"])

    assert ccm_encrypt(key, nonce, aad, pt) == _h(row["ct_tag"])
    assert ccm_decrypt(key, nonce, aad, _h(row["ct_tag"])) == pt


def test_ccm_decrypt_rejects_a_flipped_tag_bit() -> None:
    row = V0["ccm_m12_l2"][1]
    ct_tag = bytearray(_h(row["ct_tag"]))
    ct_tag[-1] ^= 0x01

    with pytest.raises(InvalidTag):
        ccm_decrypt(_h(row["key"]), _h(row["nonce"]), _h(row["aad"]), bytes(ct_tag))


def test_ccm_needs_a_13_byte_nonce() -> None:
    with pytest.raises(ValueError, match="13"):
        ccm_encrypt(bytes(16), bytes(12), b"", b"")


def test_generator_from_scalar_one() -> None:
    _, q = key_pair_from_scalar(_h(V0["p256_generator"]["scalar"]))

    assert q == _h(V0["p256_generator"]["Q"])


def test_ecdh_vector_from_both_sides() -> None:
    row = V0["p256_ecdh"]
    d_a, q_a = key_pair_from_scalar(_h(row["d_a"]))
    d_b, q_b = key_pair_from_scalar(_h(row["d_b"]))

    assert (q_a, q_b) == (_h(row["Q_a"]), _h(row["Q_b"]))
    assert ecdh(d_a, q_b) == ecdh(d_b, q_a) == _h(row["Z"])


def test_generated_key_pairs_are_valid_and_distinct() -> None:
    (_, q1), (_, q2) = generate_key_pair(), generate_key_pair()

    assert len(q1) == 64 and is_valid_point(q1)
    assert q1 != q2


def test_point_validation_rejects_zero_out_of_range_and_off_curve() -> None:
    good = _h(V0["p256_generator"]["Q"])
    x_too_big = (P256_P).to_bytes(32, "big") + good[32:]
    off_curve = good[:32] + (int.from_bytes(good[32:], "big") ^ 1).to_bytes(32, "big")
    y_too_big = good[:32] + (P256_P + 1).to_bytes(32, "big")

    assert is_valid_point(good)
    assert not is_valid_point(bytes(64))
    assert not is_valid_point(x_too_big)
    assert not is_valid_point(off_curve)
    assert not is_valid_point(y_too_big)
    assert not is_valid_point(good[:63])


def test_ecdh_validates_the_peer_point_before_the_library_sees_it() -> None:
    private, _ = generate_key_pair()

    with mock.patch.object(primitives, "_exchange") as exchange, pytest.raises(Sec2BadPointError):
        ecdh(private, bytes(64))

    exchange.assert_not_called()


def test_an_x_coordinate_of_p_or_more_is_rejected_even_when_congruent_to_a_valid_one() -> None:
    # (5, y) is on the curve; x = 5 + p reduces to the same point mod p but is out of range.
    y = bytes.fromhex("459243b9aa581806fe913bce99817ade11ca503c64d9a3c533415c083248fbcc")
    assert is_valid_point((5).to_bytes(32, "big") + y)
    assert not is_valid_point((5 + P256_P).to_bytes(32, "big") + y)
    # The Y < p check has no such test: it would need an on-curve point with y < 2^224
    # (about 1 in 2^32). It stays because SECURITY_V2.md § 2 makes it normative.
