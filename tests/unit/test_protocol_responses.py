"""Test protocol response parsing."""

import re

import pytest

from opendisplay.exceptions import InvalidResponseError, NfcNotSupportedError, NfcWriteError, ProtocolError
from opendisplay.protocol.commands import CommandCode
from opendisplay.protocol.responses import (
    NFC_ERROR_MESSAGES,
    NFC_STATUS_CHUNK_ACK,
    NFC_STATUS_WRITE_OK,
    check_response_type,
    parse_firmware_version,
    strip_command_echo,
    unpack_command_code,
    validate_ack_response,
    validate_nfc_response,
)


class TestUnpackCommandCode:
    """Test command code extraction from responses."""

    def test_unpack_command_code_basic(self):
        """Test extracting 2-byte big-endian command code."""
        data = b"\x00\x40"  # 0x0040
        code = unpack_command_code(data)
        assert code == 0x0040

    def test_unpack_command_code_with_high_bit(self):
        """Test extracting command code with ACK high bit set."""
        data = b"\x80\x43"  # 0x8043 (0x0043 with high bit)
        code = unpack_command_code(data)
        assert code == 0x8043

    def test_unpack_command_code_with_offset(self):
        """Test extracting code from offset position."""
        data = b"\xff\xff\x00\x70\x00"
        code = unpack_command_code(data, offset=2)
        assert code == 0x0070

    def test_unpack_command_code_from_real_response(self, real_firmware_response):
        """Test extracting code from real device response."""
        if real_firmware_response and len(real_firmware_response) >= 2:
            code = unpack_command_code(real_firmware_response)
            # Should be 0x0043 or 0x8043 (with ACK bit)
            assert code in [0x0043, 0x8043]


class TestStripCommandEcho:
    """Test command echo removal from responses."""

    def test_strip_echo_exact_match(self):
        """Test stripping exact command echo."""
        response = b"\x00\x40\x01\x02\x03"  # Echo + data
        stripped = strip_command_echo(response, CommandCode.READ_CONFIG)
        assert stripped == b"\x01\x02\x03"

    def test_strip_echo_with_high_bit(self):
        """Test stripping echo with ACK high bit set."""
        response = b"\x80\x43\x01\x05"  # Echo with high bit + data
        stripped = strip_command_echo(response, CommandCode.READ_FW_VERSION)
        assert stripped == b"\x01\x05"

    def test_strip_echo_no_match(self):
        """Test returns original data when echo doesn't match."""
        response = b"\x00\x99\x01\x02"  # Wrong echo
        stripped = strip_command_echo(response, CommandCode.READ_CONFIG)
        assert stripped == response  # Unchanged

    def test_strip_echo_too_short(self):
        """Test returns original data when response too short."""
        response = b"\x40"  # Only 1 byte
        stripped = strip_command_echo(response, CommandCode.READ_CONFIG)
        assert stripped == response


class TestCheckResponseType:
    """Test response type checking and ACK detection."""

    def test_check_response_type_without_ack(self):
        """Test detecting command without ACK bit."""
        response = b"\x00\x40\x01\x02"
        command, is_ack = check_response_type(response)
        assert command == CommandCode.READ_CONFIG
        assert is_ack is False

    def test_check_response_type_with_ack(self):
        """Test detecting command with ACK bit set."""
        response = b"\x80\x43\x01\x05"  # 0x8043 = 0x0043 | 0x8000
        command, is_ack = check_response_type(response)
        assert command == CommandCode.READ_FW_VERSION
        assert is_ack is True

    def test_check_response_type_real_firmware_response(self, real_firmware_response):
        """Test checking real firmware response."""
        if real_firmware_response and len(real_firmware_response) >= 2:
            command, is_ack = check_response_type(real_firmware_response)
            assert command == CommandCode.READ_FW_VERSION
            # Most responses have ACK bit set
            assert isinstance(is_ack, bool)


class TestValidateAckResponse:
    """Test ACK response validation."""

    def test_validate_ack_exact_match(self):
        """Test validating ACK with exact command echo."""
        response = b"\x00\x40"
        # Should not raise
        validate_ack_response(response, CommandCode.READ_CONFIG)

    def test_validate_ack_with_high_bit(self):
        """Test validating ACK with high bit set."""
        response = b"\x80\x43"
        # Should not raise
        validate_ack_response(response, CommandCode.READ_FW_VERSION)

    def test_validate_ack_mismatch(self):
        """Test validation fails on command mismatch."""
        response = b"\x00\x40"  # READ_CONFIG
        with pytest.raises(InvalidResponseError, match="ACK mismatch"):
            validate_ack_response(response, CommandCode.READ_FW_VERSION)

    def test_validate_ack_too_short(self):
        """Test validation fails on short response."""
        response = b"\x40"  # Only 1 byte
        with pytest.raises(InvalidResponseError, match="too short"):
            validate_ack_response(response, CommandCode.READ_CONFIG)

    def test_validate_ack_real_firmware_response(self, real_firmware_response):
        """Test validating real firmware ACK response."""
        if real_firmware_response and len(real_firmware_response) >= 2:
            # Should not raise
            validate_ack_response(real_firmware_response, CommandCode.READ_FW_VERSION)


class TestParseFirmwareVersion:
    """Test firmware version parsing."""

    def test_parse_firmware_version_basic(self):
        """Test parsing firmware version with echo and SHA."""
        # Format: [echo:2][major:1][minor:1][shaLength:1][sha:variable]
        data = b"\x00\x43\x01\x05\x07gaberin"  # Version 1.5, SHA "gaberin"
        result = parse_firmware_version(data)
        assert result == {"major": 1, "minor": 5, "sha": "gaberin"}

    def test_parse_firmware_version_with_ack_bit(self):
        """Test parsing with ACK bit set in echo."""
        data = b"\x80\x43\x02\x03\x04test"  # Version 2.3 with ACK, SHA "test"
        result = parse_firmware_version(data)
        assert result == {"major": 2, "minor": 3, "sha": "test"}

    def test_parse_firmware_version_real_data(self, real_firmware_response):
        """Test parsing real firmware response from device."""
        if real_firmware_response and len(real_firmware_response) >= 5:
            result = parse_firmware_version(real_firmware_response)

            # Verify structure
            assert "major" in result
            assert "minor" in result
            assert "sha" in result

            # Versions should be reasonable (0-255)
            assert 0 <= result["major"] <= 255
            assert 0 <= result["minor"] <= 255

            # SHA should be a non-empty string
            assert isinstance(result["sha"], str)
            assert len(result["sha"]) > 0

            print(f"Real device firmware: {result['major']}.{result['minor']} (SHA: {result['sha'][:8]}...)")

    def test_parse_firmware_version_too_short(self):
        """Test parsing fails on truncated response."""
        data = b"\x00\x43\x01\x05"  # Only 4 bytes, need at least 5 (missing shaLength)
        with pytest.raises(InvalidResponseError, match="too short"):
            parse_firmware_version(data)

    def test_parse_firmware_version_wrong_echo(self):
        """Test parsing fails on wrong command echo."""
        data = b"\x00\x40\x01\x05\x00"  # Wrong command (0x0040 not 0x0043)
        with pytest.raises(InvalidResponseError, match="echo mismatch"):
            parse_firmware_version(data)

    def test_parse_firmware_version_long_sha(self):
        """Test parsing firmware version with long SHA hash."""
        sha_string = "6761626572696e31323334353637383930"  # 34 chars
        sha_bytes = sha_string.encode("ascii")
        sha_length = len(sha_bytes)

        data = b"\x80\x43\x01\x00" + bytes([sha_length]) + sha_bytes

        result = parse_firmware_version(data)

        assert result["major"] == 1
        assert result["minor"] == 0
        assert result["sha"] == sha_string
        assert len(result["sha"]) == 34

    def test_parse_firmware_version_incomplete_sha(self):
        """Test error when SHA length exceeds actual data."""
        # Claims 10 bytes of SHA but only provides 5
        data = b"\x80\x43\x01\x05\x0a12345"  # shaLength=10, actual=5

        with pytest.raises(InvalidResponseError, match="incomplete.*expected 15 bytes.*got 10"):
            parse_firmware_version(data)

    def test_parse_firmware_version_invalid_sha_encoding(self):
        """Test error when SHA contains non-ASCII bytes."""
        # Invalid UTF-8 sequence in SHA
        data = b"\x80\x43\x01\x05\x03\xff\xfe\xfd"

        with pytest.raises(InvalidResponseError, match="Invalid SHA hash encoding"):
            parse_firmware_version(data)

    def test_parse_firmware_version_missing_sha(self):
        """Test error when SHA hash is missing (shaLength=0)."""
        data = b"\x80\x43\x01\x05\x00"  # shaLength=0

        with pytest.raises(InvalidResponseError, match="missing SHA hash"):
            parse_firmware_version(data)


class TestValidateNfcResponse:
    """Test validate_nfc_response() for the NFC_ENDPOINT (0x0083) command."""

    def test_write_ok_status(self):
        """Test {0x00, 0x83, 0x81} (inline write / chunk end) is accepted."""
        data = bytes([0x00, 0x83, NFC_STATUS_WRITE_OK])
        assert validate_nfc_response(data, NFC_STATUS_WRITE_OK) is None

    def test_chunk_ack_status(self):
        """Test {0x00, 0x83, 0x82} (chunk start / chunk data ACK) is accepted."""
        data = bytes([0x00, 0x83, NFC_STATUS_CHUNK_ACK])
        assert validate_nfc_response(data, NFC_STATUS_CHUNK_ACK) is None

    def test_wrong_status_raises_invalid_response(self):
        """Test a well-formed OK frame with a status the caller didn't expect."""
        data = bytes([0x00, 0x83, NFC_STATUS_CHUNK_ACK])
        with pytest.raises(InvalidResponseError):
            validate_nfc_response(data, NFC_STATUS_WRITE_OK)

    @pytest.mark.parametrize("error_code", sorted(NFC_ERROR_MESSAGES))
    def test_error_frame_raises_nfc_write_error(self, error_code):
        """Test each documented error code maps to its message and is carried on the exception."""
        data = bytes([0xFF, 0x83, 0xFF, error_code])
        with pytest.raises(NfcWriteError, match=re.escape(NFC_ERROR_MESSAGES[error_code])) as exc_info:
            validate_nfc_response(data, NFC_STATUS_WRITE_OK)
        assert exc_info.value.error_code == error_code

    def test_error_frame_unmapped_code(self):
        """Test an error code outside the documented table still raises with the code attached."""
        data = bytes([0xFF, 0x83, 0xFF, 0x99])
        with pytest.raises(NfcWriteError) as exc_info:
            validate_nfc_response(data, NFC_STATUS_WRITE_OK)
        assert exc_info.value.error_code == 0x99

    def test_short_frame_raises_invalid_response(self):
        """Test a frame too short to be a valid response raises InvalidResponseError."""
        data = bytes([0x00, 0x83])
        with pytest.raises(InvalidResponseError):
            validate_nfc_response(data, NFC_STATUS_WRITE_OK)

    def test_empty_frame_raises_invalid_response(self):
        """Test an empty response raises InvalidResponseError."""
        with pytest.raises(InvalidResponseError):
            validate_nfc_response(b"", NFC_STATUS_WRITE_OK)

    def test_garbage_frame_raises_invalid_response(self):
        """Test a frame that is neither the expected OK shape nor the error shape."""
        data = bytes([0x12, 0x34, 0x56])
        with pytest.raises(InvalidResponseError):
            validate_nfc_response(data, NFC_STATUS_WRITE_OK)

    def test_short_error_frame_raises_invalid_response(self):
        """Test a truncated error frame (missing the error code byte) is not silently accepted."""
        data = bytes([0xFF, 0x83, 0xFF])
        with pytest.raises(InvalidResponseError):
            validate_nfc_response(data, NFC_STATUS_WRITE_OK)

    def test_all_error_codes_documented(self):
        """Test the error table covers the firmware's documented codes 1-9."""
        assert set(NFC_ERROR_MESSAGES) == set(range(1, 10))


class TestNfcExceptions:
    """Test the NFC-specific exception types added alongside validate_nfc_response()."""

    def test_nfc_write_error_carries_code(self):
        """Test NfcWriteError exposes the firmware error_code attribute."""
        exc = NfcWriteError("NFC write failed", error_code=3)
        assert exc.error_code == 3
        assert str(exc) == "NFC write failed"

    def test_nfc_write_error_defaults_code_to_none(self):
        """Test error_code defaults to None when not supplied."""
        exc = NfcWriteError("NFC write failed")
        assert exc.error_code is None

    def test_nfc_write_error_is_protocol_error(self):
        """Test NfcWriteError fits the existing ProtocolError hierarchy."""
        assert issubclass(NfcWriteError, ProtocolError)

    def test_nfc_not_supported_error_is_protocol_error(self):
        """Test NfcNotSupportedError fits the existing ProtocolError hierarchy."""
        assert issubclass(NfcNotSupportedError, ProtocolError)

    def test_nfc_not_supported_error_hedges_in_message(self):
        """Test the default guidance hedges rather than asserting firmware definitely lacks NFC.

        Pre-0x83 firmware stays silent on unknown opcodes, so a missing response
        is inconclusive (could be a dropped packet); the message must not overclaim.
        """
        exc = NfcNotSupportedError()
        assert "may not support" in str(exc).lower()
