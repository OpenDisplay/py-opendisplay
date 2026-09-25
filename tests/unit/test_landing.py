"""Test landing (deep-link) URL encoding."""

import base64

import pytest

from opendisplay.landing import (
    LANDING_URL_PREFIX,
    LandingInfo,
    build_landing_payload,
    build_landing_url,
    parse_landing_payload,
    parse_landing_url,
)

# Canonical vector from issue #40 / the firmware on-screen QR code.
# Decodes to: tag_type=0, device id 0x4B3F63 (name "OD4B3F63"),
# key all-0x12, manufacturer_id=3 (SOLUM).
CANONICAL_PAYLOAD = "AABLP2MSEhISEhISEhISEhISEhISAAM"
CANONICAL_URL = LANDING_URL_PREFIX + CANONICAL_PAYLOAD


def _b64url_decode(payload: str) -> bytes:
    """Decode a stripped base64url payload back to bytes."""
    padded = payload + "=" * (-len(payload) % 4)
    return base64.urlsafe_b64decode(padded)


class TestBuildLandingUrl:
    """Test build_landing_url / build_landing_payload."""

    def test_canonical_vector(self):
        """Known fields produce the exact firmware/web payload."""
        url = build_landing_url(0, b"\x4b\x3f\x63", b"\x12" * 16, 3)
        assert url == CANONICAL_URL

    def test_payload_layout(self):
        """Each field lands at the documented offset, big-endian."""
        payload = build_landing_payload(0x0102, b"\xaa\xbb\xcc", bytes(range(16)), 0x0003)
        assert len(payload) == 23
        assert payload[0:2] == b"\x01\x02"  # tag_type, BE
        assert payload[2:5] == b"\xaa\xbb\xcc"  # device id
        assert payload[5:21] == bytes(range(16))  # key
        assert payload[21:23] == b"\x00\x03"  # manufacturer_id, BE

    def test_no_key_is_zeroed(self):
        """A missing key emits 16 zero bytes."""
        payload = build_landing_payload(0, b"\x01\x02\x03", None, 4)
        assert payload[5:21] == b"\x00" * 16

    def test_round_trip(self):
        """The URL's payload decodes back to the original bytes."""
        payload = build_landing_payload(0, b"\x4b\x3f\x63", b"\x12" * 16, 3)
        url = build_landing_url(0, b"\x4b\x3f\x63", b"\x12" * 16, 3)
        assert _b64url_decode(url.removeprefix(LANDING_URL_PREFIX)) == payload

    def test_invalid_device_id_length(self):
        with pytest.raises(ValueError, match="device_id must be 3 bytes"):
            build_landing_payload(0, b"\x01\x02", b"\x12" * 16, 3)

    def test_invalid_key_length(self):
        with pytest.raises(ValueError, match="encryption_key must be 16 bytes"):
            build_landing_payload(0, b"\x01\x02\x03", b"\x12" * 15, 3)


class TestParseLandingUrl:
    """Test parse_landing_url / parse_landing_payload (the inverse direction)."""

    def test_canonical_vector(self):
        """The firmware QR vector decodes to its documented fields."""
        info = parse_landing_url(CANONICAL_URL)
        assert info == LandingInfo(
            tag_type=0,
            device_id=b"\x4b\x3f\x63",
            encryption_key=b"\x12" * 16,
            manufacturer_id=3,
        )
        assert info.device_name == "OD4B3F63"

    def test_round_trip(self):
        """build -> parse returns the original fields."""
        url = build_landing_url(0x0102, b"\xaa\xbb\xcc", bytes(range(16)), 4)
        info = parse_landing_url(url)
        assert (info.tag_type, info.device_id, info.encryption_key, info.manufacturer_id) == (
            0x0102,
            b"\xaa\xbb\xcc",
            bytes(range(16)),
            4,
        )

    def test_zero_key_is_none(self):
        """An all-zero key slot means "no key" and decodes to None."""
        info = parse_landing_url(build_landing_url(0, b"\x01\x02\x03", None, 0))
        assert info.encryption_key is None

    @pytest.mark.parametrize(
        "url",
        [
            f"  {CANONICAL_URL}\n",
            f"http://opendisplay.org/l/?{CANONICAL_PAYLOAD}",
            f"https://opendisplay.org/l?{CANONICAL_PAYLOAD}",
            f"https://OpenDisplay.org/l/?{CANONICAL_PAYLOAD}=",
            f"https://www.opendisplay.org/l/?{CANONICAL_PAYLOAD}",
        ],
    )
    def test_tolerated_variants(self, url):
        """Whitespace, http, missing slash, case and padding are accepted."""
        assert parse_landing_url(url).device_name == "OD4B3F63"

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "aabbccddee112233aabbccddee112233",
            f"https://example.com/l/?{CANONICAL_PAYLOAD}",
            f"https://opendisplay.org/x/?{CANONICAL_PAYLOAD}",
            "https://opendisplay.org/l/?",
            "https://opendisplay.org/l/?AAAA",  # too short
            "https://opendisplay.org/l/?!!!notbase64!!!",
        ],
    )
    def test_rejects_invalid(self, url):
        """Anything that isn't a well-formed landing URL raises ValueError."""
        with pytest.raises(ValueError):
            parse_landing_url(url)

    def test_payload_wrong_size(self):
        """parse_landing_payload enforces the 23-byte layout."""
        with pytest.raises(ValueError):
            parse_landing_payload(b"\x00" * 22)
