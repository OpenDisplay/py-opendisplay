import pytest

from opendisplay.models.buzzer_activate import BuzzerActivateConfig
from opendisplay.models.enums import NfcRecordType
from opendisplay.models.led_flash import LedFlashConfig, LedFlashStep
from opendisplay.protocol.commands import (
    CHUNK_SIZE,
    CONFIG_CHUNK_SIZE,
    NFC_CHUNK_SIZE,
    NFC_INLINE_MAX,
    NFC_SUB_READ,
    NFC_SUB_WRITE_DATA,
    NFC_SUB_WRITE_END,
    NFC_SUB_WRITE_INLINE,
    NFC_SUB_WRITE_START,
    NFC_WRITE_MAX_TOTAL,
    CommandCode,
    build_buzzer_activate_command,
    build_deep_sleep_command,
    build_direct_write_data_command,
    build_direct_write_end_command,
    build_direct_write_start_compressed,
    build_direct_write_start_uncompressed,
    build_led_activate_command,
    build_nfc_write_data_command,
    build_nfc_write_end_command,
    build_nfc_write_inline_command,
    build_nfc_write_start_command,
    build_read_config_command,
    build_read_fw_version_command,
    build_reboot_command,
    build_write_config_command,
)


class TestCommandBuilders:
    """Test command builder functions against real protocol data."""

    def test_build_read_config_command(self, real_read_config_command):
        """Test READ_CONFIG command matches real captured data."""
        cmd = build_read_config_command()
        assert len(cmd) == 2
        assert cmd == b"\x00\x40"  # 0x0040 big-endian
        # Verify matches real device command
        if real_read_config_command:
            assert cmd == real_read_config_command

    def test_build_read_fw_version_command(self, real_firmware_command):
        """Test READ_FW_VERSION command matches real captured data."""
        cmd = build_read_fw_version_command()
        assert len(cmd) == 2
        assert cmd == b"\x00\x43"  # 0x0043 big-endian
        # Verify matches real device command
        if real_firmware_command:
            assert cmd == real_firmware_command

    def test_build_reboot_command(self):
        """Test REBOOT command builder."""
        cmd = build_reboot_command()
        assert len(cmd) == 2
        assert cmd == b"\x00\x0f"  # 0x000F big-endian

    def test_build_deep_sleep_command(self):
        """Test DEEP_SLEEP command builder."""
        cmd = build_deep_sleep_command()
        assert len(cmd) == 2
        assert cmd == b"\x00\x52"  # 0x0052 big-endian
        assert cmd == CommandCode.DEEP_SLEEP.to_bytes(2, "big")

    def test_build_direct_write_start_uncompressed(self, real_upload_start_command):
        """Test uncompressed START command matches real data."""
        cmd = build_direct_write_start_uncompressed()
        assert len(cmd) == 2
        assert cmd == b"\x00\x70"  # 0x0070 big-endian
        # Verify matches real device command
        if real_upload_start_command:
            assert cmd == real_upload_start_command

    def test_build_direct_write_start_compressed_small(self):
        """Test compressed START with payload that fits (≤194 bytes)."""
        compressed_data = b"\x78\x9c" + b"A" * 100  # 102 bytes total
        uncompressed_size = 500

        start_cmd, remaining = build_direct_write_start_compressed(uncompressed_size, compressed_data)

        # All data should fit in START
        assert len(remaining) == 0
        assert len(start_cmd) == 2 + 4 + 102  # cmd + size + data
        assert start_cmd[:2] == b"\x00\x70"  # Command code
        assert start_cmd[2:6] == uncompressed_size.to_bytes(4, "little")
        assert start_cmd[6:] == compressed_data

    def test_build_direct_write_start_compressed_large(self):
        """Test compressed START with payload exceeding 194 bytes."""
        compressed_data = b"\x78\x9c" + b"A" * 300  # 302 bytes total
        uncompressed_size = 1000

        start_cmd, remaining = build_direct_write_start_compressed(uncompressed_size, compressed_data)

        # Should split: 194 bytes in START, 108 bytes remaining
        assert len(remaining) == 302 - 194  # 108 bytes
        assert len(start_cmd) == 2 + 4 + 194  # cmd + size + max chunk
        assert start_cmd[:2] == b"\x00\x70"
        assert start_cmd[2:6] == uncompressed_size.to_bytes(4, "little")
        assert start_cmd[6:] == compressed_data[:194]
        assert remaining == compressed_data[194:]

    def test_build_direct_write_start_compressed_exact_boundary(self):
        """Test compressed START with exactly 194 bytes."""
        compressed_data = b"A" * 194
        uncompressed_size = 500

        start_cmd, remaining = build_direct_write_start_compressed(uncompressed_size, compressed_data)

        # Exactly fits, no remaining
        assert len(remaining) == 0
        assert len(start_cmd) == 200  # 2 + 4 + 194 = MAX_START_PAYLOAD

    def test_build_direct_write_start_compressed_one_over_boundary(self):
        """Test compressed START with 195 bytes (first case needing chunking)."""
        compressed_data = b"A" * 195
        uncompressed_size = 500

        start_cmd, remaining = build_direct_write_start_compressed(uncompressed_size, compressed_data)

        # Should split: 194 in START, 1 byte remaining
        assert len(remaining) == 1
        assert len(start_cmd) == 200  # 2 + 4 + 194
        assert remaining == b"A"

    def test_build_direct_write_data_command(self, real_data_chunk_command):
        """Test DATA command prepends command code to chunk."""
        chunk = b"A" * 100
        cmd = build_direct_write_data_command(chunk)

        assert cmd[:2] == b"\x00\x71"  # 0x0071 big-endian
        assert cmd[2:] == chunk

        # Verify structure matches real captured chunk
        if real_data_chunk_command:
            assert cmd[:2] == real_data_chunk_command[:2]  # Same command code

    def test_build_direct_write_data_command_max_size(self):
        """Test DATA command accepts max CHUNK_SIZE."""
        chunk = b"A" * CHUNK_SIZE
        cmd = build_direct_write_data_command(chunk)
        assert len(cmd) == CHUNK_SIZE + 2

    def test_build_direct_write_data_command_too_large(self):
        """Test DATA command rejects oversized chunks."""
        chunk = b"A" * (CHUNK_SIZE + 1)
        with pytest.raises(ValueError, match="exceeds maximum"):
            build_direct_write_data_command(chunk)

    def test_build_direct_write_end_command(self, real_upload_end_command):
        """Test END command includes refresh mode."""
        # Default refresh mode (0 = FULL)
        cmd = build_direct_write_end_command()
        assert len(cmd) == 3
        assert cmd == b"\x00\x72\x00"  # cmd + refresh 0

        # Verify matches real device command
        if real_upload_end_command:
            assert cmd == real_upload_end_command

        # Fast refresh mode (1)
        cmd = build_direct_write_end_command(refresh_mode=1)
        assert cmd == b"\x00\x72\x01"  # cmd + refresh 1

    def test_build_led_activate_command_with_flash_config(self):
        """Test LED activate command with typed flash config payload."""
        flash_config = LedFlashConfig(
            mode=1,
            brightness=8,
            step1=LedFlashStep(color=0xE0, flash_count=2, loop_delay_units=2, inter_delay_units=5),
            step2=LedFlashStep(color=0x1C, flash_count=2, loop_delay_units=2, inter_delay_units=3),
            step3=LedFlashStep(color=0x03, flash_count=2, loop_delay_units=2, inter_delay_units=1),
            group_repeats=1,
        )
        cmd = build_led_activate_command(led_instance=1, flash_config=flash_config)
        assert cmd == b"\x00\x73\x01" + flash_config.to_bytes()

    def test_build_led_activate_command_rejects_raw_bytes(self):
        """Test LED activate command requires typed flash config."""
        with pytest.raises(TypeError, match="must be LedFlashConfig"):
            build_led_activate_command(led_instance=0, flash_config=b"\x00" * 12)  # type: ignore[arg-type]


class TestCommandCode:
    """Test CommandCode enum values."""

    def test_command_code_values(self):
        """Test all command codes have correct values."""
        assert CommandCode.READ_CONFIG == 0x0040
        assert CommandCode.WRITE_CONFIG == 0x0041
        assert CommandCode.READ_FW_VERSION == 0x0043
        assert CommandCode.REBOOT == 0x000F
        assert CommandCode.DIRECT_WRITE_START == 0x0070
        assert CommandCode.DIRECT_WRITE_DATA == 0x0071
        assert CommandCode.DIRECT_WRITE_END == 0x0072
        assert CommandCode.LED_ACTIVATE == 0x0073
        assert CommandCode.BUZZER_ACTIVATE == 0x0077
        assert CommandCode.NFC_ENDPOINT == 0x0083

    def test_command_code_to_bytes(self):
        """Test command codes convert to correct big-endian bytes."""
        assert CommandCode.READ_CONFIG.to_bytes(2, "big") == b"\x00\x40"
        assert CommandCode.READ_FW_VERSION.to_bytes(2, "big") == b"\x00\x43"
        assert CommandCode.DIRECT_WRITE_START.to_bytes(2, "big") == b"\x00\x70"
        assert CommandCode.BUZZER_ACTIVATE.to_bytes(2, "big") == b"\x00\x77"


class TestBuildBuzzerActivateCommand:
    """Test build_buzzer_activate_command wire format."""

    def test_command_starts_with_0x0077(self):
        config = BuzzerActivateConfig.single_tone(frequency_hz=1000, duration_ms=100)
        cmd = build_buzzer_activate_command(0, config)
        assert cmd[:2] == b"\x00\x77"

    def test_instance_byte_at_position_2(self):
        config = BuzzerActivateConfig.single_tone(frequency_hz=1000, duration_ms=100)
        cmd = build_buzzer_activate_command(3, config)
        assert cmd[2] == 3

    def test_payload_appended_after_instance(self):
        config = BuzzerActivateConfig.single_tone(frequency_hz=1000, duration_ms=100)
        cmd = build_buzzer_activate_command(0, config)
        assert cmd[3:] == config.to_bytes()

    def test_total_length_for_single_tone(self):
        config = BuzzerActivateConfig.single_tone(frequency_hz=500, duration_ms=50)
        cmd = build_buzzer_activate_command(0, config)
        # 2 (cmd) + 1 (instance) + 5 (config: repeats+n_patterns+n_steps+freq+dur) = 8
        assert len(cmd) == 8

    def test_invalid_instance_raises(self):
        config = BuzzerActivateConfig.single_tone(frequency_hz=1000, duration_ms=100)
        with pytest.raises(ValueError, match="out of range"):
            build_buzzer_activate_command(256, config)


class TestWriteConfigChunking:
    """WRITE_CONFIG chunking must match the firmware's expectations (C4)."""

    def test_single_chunk_when_within_limit(self):
        data = b"\xab" * CONFIG_CHUNK_SIZE  # exactly 200 bytes -> single chunk
        first, chunks = build_write_config_command(data)
        assert chunks == []
        assert first == CommandCode.WRITE_CONFIG.to_bytes(2, "big") + data

    def test_first_chunk_carries_full_200_data_bytes(self):
        # >200 bytes triggers chunked mode; the first chunk must carry a full
        # 200 data bytes (payload = size(2) + 200 = 202 > 200) so the firmware
        # enters chunked mode instead of the single-chunk path.
        total = CONFIG_CHUNK_SIZE + 50  # 250 bytes
        data = bytes(range(256))[:total]
        first, chunks = build_write_config_command(data)

        cmd_write = CommandCode.WRITE_CONFIG.to_bytes(2, "big")
        # first = cmd(2) + total_size(2 LE) + 200 data
        assert first[:2] == cmd_write
        assert first[2:4] == total.to_bytes(2, "little")
        assert first[4:] == data[:CONFIG_CHUNK_SIZE]
        assert len(first[4:]) == CONFIG_CHUNK_SIZE

        # remaining 50 bytes in a single 0x42 continuation chunk
        cmd_chunk = CommandCode.WRITE_CONFIG_CHUNK.to_bytes(2, "big")
        assert len(chunks) == 1
        assert chunks[0] == cmd_chunk + data[CONFIG_CHUNK_SIZE:]

    def test_chunk_count_matches_ceil_total_over_200(self):
        import math

        for total in (201, 400, 401, 605):
            data = bytes((i % 256 for i in range(total)))
            first, chunks = build_write_config_command(data)
            # first chunk (200) + continuations (200 each) == ceil(total/200) chunks
            assert 1 + len(chunks) == math.ceil(total / CONFIG_CHUNK_SIZE)


class TestNfcSubcommandConstants:
    """Sub-opcode and size constants for the NFC endpoint (command 0x0083)."""

    def test_subcommand_values(self):
        assert NFC_SUB_READ == 0x00
        assert NFC_SUB_WRITE_INLINE == 0x01
        assert NFC_SUB_WRITE_START == 0x10
        assert NFC_SUB_WRITE_DATA == 0x11
        assert NFC_SUB_WRITE_END == 0x12

    def test_size_constants(self):
        assert NFC_INLINE_MAX == 120
        assert NFC_CHUNK_SIZE == 120
        assert NFC_WRITE_MAX_TOTAL == 512


class TestNfcRecordType:
    """NfcRecordType enum values (used with the 0x0083 NFC endpoint)."""

    def test_values(self):
        assert NfcRecordType.TEXT == 0
        assert NfcRecordType.URI == 1
        assert NfcRecordType.WELL_KNOWN_RAW == 2
        assert NfcRecordType.MIME == 3
        assert NfcRecordType.RAW_NDEF == 4


class TestBuildNfcWriteInlineCommand:
    """build_nfc_write_inline_command wire format: [0x00][0x83][0x01][rec_type:1][len:2 BE][payload]."""

    def test_wire_format(self):
        cmd = build_nfc_write_inline_command(NfcRecordType.URI, b"https://x")
        assert cmd == b"\x00\x83\x01\x01\x00\x09https://x"

    def test_accepts_plain_int_rec_type(self):
        cmd = build_nfc_write_inline_command(0, b"hi")
        assert cmd == b"\x00\x83\x01\x00\x00\x02hi"

    def test_empty_payload_raises(self):
        with pytest.raises(ValueError):
            build_nfc_write_inline_command(NfcRecordType.TEXT, b"")

    def test_payload_over_u16_limit_raises(self):
        with pytest.raises(ValueError):
            build_nfc_write_inline_command(NfcRecordType.TEXT, b"a" * 0x10000)

    def test_payload_at_u16_limit_is_accepted(self):
        payload = b"a" * 0xFFFF
        cmd = build_nfc_write_inline_command(NfcRecordType.TEXT, payload)
        assert cmd[4:6] == (0xFFFF).to_bytes(2, "big")

    def test_rec_type_below_range_raises(self):
        with pytest.raises(ValueError):
            build_nfc_write_inline_command(-1, b"x")

    def test_rec_type_above_range_raises(self):
        with pytest.raises(ValueError):
            build_nfc_write_inline_command(256, b"x")


class TestBuildNfcWriteStartCommand:
    """build_nfc_write_start_command wire format: [0x00][0x83][0x10][rec_type:1][total_len:2 BE]."""

    def test_wire_format(self):
        cmd = build_nfc_write_start_command(3, 300)
        assert cmd == b"\x00\x83\x10\x03\x01\x2c"

    def test_total_len_zero_raises(self):
        with pytest.raises(ValueError):
            build_nfc_write_start_command(0, 0)

    def test_total_len_over_max_raises(self):
        with pytest.raises(ValueError):
            build_nfc_write_start_command(0, NFC_WRITE_MAX_TOTAL + 1)

    def test_total_len_at_max_is_accepted(self):
        cmd = build_nfc_write_start_command(0, NFC_WRITE_MAX_TOTAL)
        assert cmd[4:6] == NFC_WRITE_MAX_TOTAL.to_bytes(2, "big")

    def test_total_len_at_one_is_accepted(self):
        cmd = build_nfc_write_start_command(0, 1)
        assert cmd[4:6] == (1).to_bytes(2, "big")

    def test_rec_type_below_range_raises(self):
        with pytest.raises(ValueError):
            build_nfc_write_start_command(-1, 10)

    def test_rec_type_above_range_raises(self):
        with pytest.raises(ValueError):
            build_nfc_write_start_command(256, 10)


class TestBuildNfcWriteDataCommand:
    """build_nfc_write_data_command wire format: [0x00][0x83][0x11][bytes]."""

    def test_wire_format(self):
        cmd = build_nfc_write_data_command(b"\x01\x02")
        assert cmd == b"\x00\x83\x11\x01\x02"

    def test_empty_chunk_raises(self):
        with pytest.raises(ValueError):
            build_nfc_write_data_command(b"")

    def test_chunk_over_max_raises(self):
        with pytest.raises(ValueError):
            build_nfc_write_data_command(b"a" * (NFC_CHUNK_SIZE + 1))

    def test_chunk_at_max_is_accepted(self):
        chunk = b"a" * NFC_CHUNK_SIZE
        cmd = build_nfc_write_data_command(chunk)
        assert cmd == b"\x00\x83\x11" + chunk


class TestBuildNfcWriteEndCommand:
    """build_nfc_write_end_command wire format: [0x00][0x83][0x12]."""

    def test_wire_format(self):
        assert build_nfc_write_end_command() == b"\x00\x83\x12"
