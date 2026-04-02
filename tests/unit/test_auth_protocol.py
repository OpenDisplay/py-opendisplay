"""Tests for authenticate command builders and response parsers."""

import pytest

from opendisplay.exceptions import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    AuthenticationSessionExistsError,
    InvalidResponseError,
)
from opendisplay.protocol.commands import build_authenticate_step1, build_authenticate_step2
from opendisplay.protocol.responses import parse_authenticate_challenge, parse_authenticate_success

_SERVER_NONCE = bytes(range(16))
_CLIENT_NONCE = bytes(range(16, 32))
_CHALLENGE = bytes(range(32, 48))
_SERVER_PROOF = bytes(range(48, 64))
_DEFAULT_DEVICE_ID = bytes([0x00, 0x00, 0x00, 0x01])
_CUSTOM_DEVICE_ID = bytes([0xDE, 0xAD, 0xBE, 0xEF])


class TestBuildAuthenticateStep1:
    """Tests for build_authenticate_step1."""

    def test_returns_3_bytes(self):
        """Step-1 command is exactly 3 bytes."""
        cmd = build_authenticate_step1()
        assert len(cmd) == 3

    def test_exact_bytes(self):
        """Step-1 command is [0x00, 0x50, 0x00]."""
        assert build_authenticate_step1() == b"\x00\x50\x00"


class TestBuildAuthenticateStep2:
    """Tests for build_authenticate_step2."""

    def test_returns_34_bytes(self):
        """Step-2 command is cmd(2) + client_nonce(16) + challenge(16) = 34 bytes."""
        cmd = build_authenticate_step2(_CLIENT_NONCE, _CHALLENGE)
        assert len(cmd) == 34

    def test_command_prefix(self):
        """First two bytes are the AUTHENTICATE command code."""
        cmd = build_authenticate_step2(_CLIENT_NONCE, _CHALLENGE)
        assert cmd[:2] == b"\x00\x50"

    def test_nonce_and_challenge_embedded(self):
        """client_nonce and challenge_response appear verbatim in output."""
        cmd = build_authenticate_step2(_CLIENT_NONCE, _CHALLENGE)
        assert cmd[2:18] == _CLIENT_NONCE
        assert cmd[18:34] == _CHALLENGE

    def test_wrong_nonce_length_raises(self):
        """Raises ValueError when client_nonce is not 16 bytes."""
        with pytest.raises(ValueError, match="client_nonce"):
            build_authenticate_step2(b"\x00" * 15, _CHALLENGE)

    def test_wrong_challenge_length_raises(self):
        """Raises ValueError when challenge_response is not 16 bytes."""
        with pytest.raises(ValueError, match="challenge_response"):
            build_authenticate_step2(_CLIENT_NONCE, b"\x00" * 8)


class TestParseAuthenticateChallenge:
    """Tests for parse_authenticate_challenge."""

    def _old_format(self, nonce: bytes = _SERVER_NONCE) -> bytes:
        """19-byte (old firmware) challenge."""
        return b"\x00\x50\x00" + nonce

    def _new_format(self, nonce: bytes = _SERVER_NONCE, device_id: bytes = _CUSTOM_DEVICE_ID) -> bytes:
        """23-byte (new firmware) challenge with device_id."""
        return b"\x00\x50\x00" + nonce + device_id

    def test_valid_returns_nonce(self):
        """Valid old-format response returns correct 16-byte server nonce."""
        nonce, _ = parse_authenticate_challenge(self._old_format())
        assert nonce == _SERVER_NONCE

    def test_valid_with_high_bit(self):
        """High-bit echo (ACK flag) is also accepted."""
        data = b"\x80\x50\x00" + _SERVER_NONCE
        nonce, _ = parse_authenticate_challenge(data)
        assert nonce == _SERVER_NONCE

    def test_nonce_is_16_bytes(self):
        """Returned nonce is always 16 bytes."""
        nonce, _ = parse_authenticate_challenge(self._old_format())
        assert len(nonce) == 16

    def test_old_format_uses_default_device_id(self):
        """19-byte format falls back to default device ID [0,0,0,1]."""
        _, device_id = parse_authenticate_challenge(self._old_format())
        assert device_id == _DEFAULT_DEVICE_ID

    def test_new_format_extracts_device_id(self):
        """23-byte format extracts device_id from bytes [19:23]."""
        _, device_id = parse_authenticate_challenge(self._new_format(device_id=_CUSTOM_DEVICE_ID))
        assert device_id == _CUSTOM_DEVICE_ID

    def test_new_format_nonce_unaffected_by_device_id(self):
        """New format still returns correct nonce regardless of device_id."""
        nonce, _ = parse_authenticate_challenge(self._new_format())
        assert nonce == _SERVER_NONCE

    def test_status_already_authenticated_raises_session_exists(self):
        """Status 0x02 (existing session) raises AuthenticationSessionExistsError."""
        data = b"\x00\x50\x02" + _SERVER_NONCE
        with pytest.raises(AuthenticationSessionExistsError):
            parse_authenticate_challenge(data)

    def test_status_wrong_key_raises_auth_failed(self):
        """Status 0x01 (wrong key) raises AuthenticationFailedError."""
        data = b"\x00\x50\x01" + _SERVER_NONCE
        with pytest.raises(AuthenticationFailedError):
            parse_authenticate_challenge(data)

    def test_status_not_configured_raises_auth_required(self):
        """Status 0x03 (not configured) raises AuthenticationRequiredError."""
        data = b"\x00\x50\x03" + _SERVER_NONCE
        with pytest.raises(AuthenticationRequiredError):
            parse_authenticate_challenge(data)

    def test_status_rate_limited_raises_auth_failed(self):
        """Status 0x04 (rate limited) raises AuthenticationFailedError."""
        data = b"\x00\x50\x04" + _SERVER_NONCE
        with pytest.raises(AuthenticationFailedError):
            parse_authenticate_challenge(data)

    def test_too_short_raises_invalid(self):
        """Response shorter than 19 bytes raises InvalidResponseError."""
        with pytest.raises(InvalidResponseError):
            parse_authenticate_challenge(b"\x00\x50\x00" + b"\x00" * 10)

    def test_empty_raises_invalid(self):
        """Empty response raises InvalidResponseError."""
        with pytest.raises(InvalidResponseError):
            parse_authenticate_challenge(b"")


class TestParseAuthenticateSuccess:
    """Tests for parse_authenticate_success."""

    def _valid(self, proof: bytes = _SERVER_PROOF) -> bytes:
        return b"\x00\x50\x00" + proof

    def test_valid_returns_proof(self):
        """Valid response returns 16-byte server proof."""
        proof = parse_authenticate_success(self._valid())
        assert proof == _SERVER_PROOF

    def test_valid_with_high_bit(self):
        """High-bit echo is accepted."""
        data = b"\x80\x50\x00" + _SERVER_PROOF
        proof = parse_authenticate_success(data)
        assert proof == _SERVER_PROOF

    def test_proof_is_16_bytes(self):
        """Returned proof is 16 bytes."""
        assert len(parse_authenticate_success(self._valid())) == 16

    def test_status_wrong_key_raises_auth_failed(self):
        """Status 0x01 raises AuthenticationFailedError with 'wrong' in message."""
        data = b"\x00\x50\x01" + _SERVER_PROOF
        with pytest.raises(AuthenticationFailedError, match="wrong"):
            parse_authenticate_success(data)

    def test_status_rate_limited_raises_auth_failed(self):
        """Status 0x04 raises AuthenticationFailedError."""
        data = b"\x00\x50\x04" + _SERVER_PROOF
        with pytest.raises(AuthenticationFailedError):
            parse_authenticate_success(data)

    def test_too_short_raises_invalid(self):
        """Response shorter than 19 bytes raises InvalidResponseError."""
        with pytest.raises(InvalidResponseError):
            parse_authenticate_success(b"\x00\x50\x00" + b"\x00" * 5)

    def test_empty_raises_invalid(self):
        """Empty response raises InvalidResponseError."""
        with pytest.raises(InvalidResponseError):
            parse_authenticate_success(b"")
