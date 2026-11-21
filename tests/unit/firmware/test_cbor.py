"""The minimal CBOR an SMP client needs: maps of text keys, ints, bytes, text, bools, arrays."""

from __future__ import annotations

import pytest

from opendisplay.firmware import _cbor


def test_encode_matches_the_bytes_the_esp32_server_parses() -> None:
    # od_smp.cpp parse_req: a flat map of text keys; heads as put_head() writes them.
    encoded = _cbor.encode({"off": 0, "len": 1229968, "data": b"\x01\x02", "confirm": False})

    assert encoded == (
        b"\xa4" + b"\x63off\x00" + b"\x63len\x1a\x00\x12\xc4\x90" + b"\x64data\x42\x01\x02" + b"\x67confirm\xf4"
    )


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        (23, b"\x17"),
        (24, b"\x18\x18"),
        (255, b"\x18\xff"),
        (256, b"\x19\x01\x00"),
        (65536, b"\x1a\x00\x01\x00\x00"),
        (-1, b"\x20"),
        (-500, b"\x39\x01\xf3"),
        (True, b"\xf5"),
        ("3.0.2", b"\x653.0.2"),
        (b"\xab" * 32, b"\x58\x20" + b"\xab" * 32),
        ([1, 2], b"\x82\x01\x02"),
    ],
)
def test_values_round_trip(value: object, encoded: bytes) -> None:
    assert _cbor.encode(value) == encoded
    assert _cbor.decode(encoded) == value


def test_an_image_state_reply_decodes() -> None:
    # The shape od_smp.cpp reply_state() and Zephyr img_mgmt send.
    reply = _cbor.encode(
        {
            "images": [
                {"slot": 0, "version": "3.0.2", "hash": b"\xfc" * 32, "active": True, "confirmed": True},
                {"slot": 1, "version": "3.0.2", "hash": b"\xfc" * 32, "pending": False},
            ],
            "splitStatus": 0,
        }
    )

    decoded = _cbor.decode(reply)

    assert decoded["images"][1]["version"] == "3.0.2"
    assert decoded["images"][0]["active"] is True


def test_indefinite_length_maps_and_arrays_decode() -> None:
    # Zephyr's zcbor may send indefinite-length containers.
    assert _cbor.decode(b"\xbf\x62rc\x00\x64list\x9f\x01\x02\xff\xff") == {"rc": 0, "list": [1, 2]}


@pytest.mark.parametrize("data", [b"", b"\xa1\x63off", b"\x5a\x00\x00\x01\x00", b"\xc1\x00", b"\xf9\x00\x00"])
def test_malformed_or_unsupported_input_raises(data: bytes) -> None:
    with pytest.raises(ValueError):
        _cbor.decode(data)


def test_trailing_bytes_are_refused() -> None:
    with pytest.raises(ValueError, match="trailing"):
        _cbor.decode(b"\x00\x00")
