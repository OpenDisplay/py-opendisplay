"""Test NFC write API on OpenDisplayDevice."""

from __future__ import annotations

import pytest

from opendisplay import OpenDisplayDevice
from opendisplay.crypto import encrypt_command
from opendisplay.exceptions import BLETimeoutError, InvalidResponseError, NfcNotSupportedError, NfcWriteError
from opendisplay.models.enums import NfcRecordType


class _FakeConnection:
    def __init__(self, response: bytes | list[bytes], timeout_on: int | None = None):
        if isinstance(response, list):
            self._responses = response[:]
        else:
            self._responses = [response]
        self.written: list[bytes] = []
        self.read_timeouts: list[float] = []
        self._timeout_on = timeout_on
        self._read_count = 0

    async def write_command(self, cmd: bytes, response: bool = True) -> None:
        self.written.append(cmd)

    async def read_response(self, timeout: float) -> bytes:
        self._read_count += 1
        self.read_timeouts.append(timeout)
        if self._timeout_on is not None and self._read_count == self._timeout_on:
            raise BLETimeoutError("Timed out waiting for response")
        if not self._responses:
            raise RuntimeError("No fake responses left")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_write_nfc_inline_happy_path() -> None:
    """A short payload should be sent as a single inline write frame."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x83\x81")
    device._connection = fake

    await device.write_nfc(NfcRecordType.TEXT, b"hello")

    expected_cmd = b"\x00\x83" + bytes([0x01, int(NfcRecordType.TEXT)]) + len(b"hello").to_bytes(2, "big") + b"hello"
    assert fake.written == [expected_cmd]
    assert fake.read_timeouts == [device.TIMEOUT_NFC_WRITE]


@pytest.mark.asyncio
async def test_write_nfc_chunked_121_bytes_exact_frame_sequence() -> None:
    """A 121-byte payload should use start/data(120)/data(1)/end with the right ACKs."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    payload = bytes(range(121))
    fake = _FakeConnection(response=[b"\x00\x83\x82", b"\x00\x83\x82", b"\x00\x83\x82", b"\x00\x83\x81"])
    device._connection = fake

    await device.write_nfc(NfcRecordType.URI, payload)

    expected_start = b"\x00\x83" + bytes([0x10, int(NfcRecordType.URI)]) + len(payload).to_bytes(2, "big")
    expected_data1 = b"\x00\x83" + bytes([0x11]) + payload[:120]
    expected_data2 = b"\x00\x83" + bytes([0x11]) + payload[120:121]
    expected_end = b"\x00\x83" + bytes([0x12])

    assert fake.written == [expected_start, expected_data1, expected_data2, expected_end]
    # First 3 reads (start ack, chunk1 ack, chunk2 ack) use TIMEOUT_ACK; final end read uses TIMEOUT_NFC_WRITE.
    assert fake.read_timeouts == [
        device.TIMEOUT_ACK,
        device.TIMEOUT_ACK,
        device.TIMEOUT_ACK,
        device.TIMEOUT_NFC_WRITE,
    ]


@pytest.mark.asyncio
async def test_write_nfc_512_bytes_accepted() -> None:
    """Exactly 512 bytes (NFC_WRITE_MAX_TOTAL) should be accepted."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    payload = bytes(512)
    responses = [b"\x00\x83\x82"] * 6 + [b"\x00\x83\x81"]
    fake = _FakeConnection(response=responses)
    device._connection = fake

    await device.write_nfc(NfcRecordType.TEXT, payload)

    # start + 5 chunks (4x120 + 1x32) + end = 7 writes
    assert len(fake.written) == 7


@pytest.mark.asyncio
async def test_write_nfc_513_bytes_rejected() -> None:
    """A payload larger than NFC_WRITE_MAX_TOTAL should raise ValueError."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x83\x81")
    device._connection = fake

    with pytest.raises(ValueError):
        await device.write_nfc(NfcRecordType.TEXT, bytes(513))


@pytest.mark.asyncio
async def test_write_nfc_empty_payload_rejected() -> None:
    """An empty payload should raise ValueError."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x83\x81")
    device._connection = fake

    with pytest.raises(ValueError):
        await device.write_nfc(NfcRecordType.TEXT, b"")


@pytest.mark.asyncio
async def test_write_nfc_error_frame_raises_nfc_write_error() -> None:
    """A firmware error frame should raise NfcWriteError with the error code."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\xff\x83\xff\x03")
    device._connection = fake

    with pytest.raises(NfcWriteError) as exc_info:
        await device.write_nfc(NfcRecordType.TEXT, b"hello")
    assert exc_info.value.error_code == 3


@pytest.mark.asyncio
async def test_write_nfc_mid_stream_chunk_nack_aborts() -> None:
    """A NACK mid-stream should abort immediately with no further writes."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    payload = bytes(range(121))
    fake = _FakeConnection(response=[b"\x00\x83\x82", b"\xff\x83\xff\x08"])
    device._connection = fake

    with pytest.raises(NfcWriteError) as exc_info:
        await device.write_nfc(NfcRecordType.URI, payload)
    assert exc_info.value.error_code == 8
    # start + first data chunk only; second data chunk and end never sent
    assert len(fake.written) == 2


@pytest.mark.asyncio
async def test_write_nfc_stage_ack_where_ok_expected_raises() -> None:
    """A chunk-ack status where the final OK status is expected should raise InvalidResponseError."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x83\x82")
    device._connection = fake

    with pytest.raises(InvalidResponseError):
        await device.write_nfc(NfcRecordType.TEXT, b"hello")


@pytest.mark.asyncio
async def test_write_nfc_read_timeout_on_first_read_raises_not_supported() -> None:
    """A BLE read timeout on the first read of a write sequence should map to NfcNotSupportedError."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x83\x81", timeout_on=1)
    device._connection = fake

    with pytest.raises(NfcNotSupportedError):
        await device.write_nfc(NfcRecordType.TEXT, b"hello")


@pytest.mark.asyncio
async def test_write_nfc_read_timeout_on_later_read_propagates() -> None:
    """A BLE read timeout on a later read (not the first) should propagate as BLETimeoutError."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    payload = bytes(range(121))
    fake = _FakeConnection(response=[b"\x00\x83\x82", b"\x00\x83\x82"], timeout_on=3)
    device._connection = fake

    with pytest.raises(BLETimeoutError):
        await device.write_nfc(NfcRecordType.URI, payload)


@pytest.mark.asyncio
async def test_write_nfc_url_encodes_utf8_and_uses_uri_record_type() -> None:
    """write_nfc_url should send the URL verbatim as UTF-8 with record type URI."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x83\x81")
    device._connection = fake

    await device.write_nfc_url("https://example.com")

    payload = "https://example.com".encode("utf-8")
    expected_cmd = b"\x00\x83" + bytes([0x01, int(NfcRecordType.URI)]) + len(payload).to_bytes(2, "big") + payload
    assert fake.written == [expected_cmd]


@pytest.mark.asyncio
async def test_write_nfc_text_encodes_utf8_and_uses_text_record_type() -> None:
    """write_nfc_text should send the text verbatim as UTF-8 with record type TEXT."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x83\x81")
    device._connection = fake

    await device.write_nfc_text("hello world")

    payload = "hello world".encode("utf-8")
    expected_cmd = b"\x00\x83" + bytes([0x01, int(NfcRecordType.TEXT)]) + len(payload).to_bytes(2, "big") + payload
    assert fake.written == [expected_cmd]


@pytest.mark.asyncio
async def test_write_nfc_mime_builds_length_prefixed_payload() -> None:
    """write_nfc_mime should prefix the payload with a one-byte MIME type length."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x83\x81")
    device._connection = fake

    await device.write_nfc_mime("text/vcard", b"BEGIN:VCARD")

    body_prefix = b"\x0atext/vcard"
    payload = body_prefix + b"BEGIN:VCARD"
    expected_cmd = b"\x00\x83" + bytes([0x01, int(NfcRecordType.MIME)]) + len(payload).to_bytes(2, "big") + payload
    assert fake.written == [expected_cmd]


@pytest.mark.asyncio
async def test_write_nfc_mime_type_too_long_rejected() -> None:
    """A MIME type longer than 255 bytes should raise ValueError."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    fake = _FakeConnection(response=b"\x00\x83\x81")
    device._connection = fake

    with pytest.raises(ValueError):
        await device.write_nfc_mime("x" * 256, b"body")


@pytest.mark.asyncio
async def test_write_nfc_inline_happy_path_encrypted_session() -> None:
    """An encrypted OK frame from the device must be decrypted and normalized correctly.

    _read() decrypts an encrypted response and returns cmd_code.to_bytes(2, "big") + payload,
    i.e. exactly the same shape as the plaintext ack (b"\\x00\\x83\\x81"). This locks in that
    normalization contract for the encrypted-session code path, which no other test exercises.
    """
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    session_key = bytes(range(16))
    session_id = bytes(range(8))
    device._session_key = session_key
    device._session_id = session_id
    device._nonce_counter = 0

    # Build a genuinely encrypted device-to-host OK response using the library's own
    # encrypt routine. The wire format (cmd(2) + nonce(16) + ciphertext + tag(12)) is
    # identical in both directions, as proven by TestEncryptDecryptCommand.test_round_trip
    # in test_crypto.py, so reusing encrypt_command here is not a fragile reimplementation.
    encrypted_ok = encrypt_command(session_key, session_id, counter=1, cmd=b"\x00\x83", payload=b"\x81")

    fake = _FakeConnection(response=encrypted_ok)
    device._connection = fake

    await device.write_nfc(NfcRecordType.TEXT, b"hello")

    # _write() also encrypts outgoing frames once a session key is set, so the frame
    # recorded here is ciphertext too; what this test locks in is that the encrypted
    # *response* was accepted and decrypted without error.
    assert len(fake.written) == 1


@pytest.mark.asyncio
async def test_write_nfc_error_frame_plaintext_during_encrypted_session() -> None:
    """A plaintext firmware error frame must still raise NfcWriteError even with a session key set.

    Error frames {0xFF, 0x83, 0xFF, err} are only 4 bytes -- shorter than the 31-byte
    encrypted-frame minimum -- so the firmware sends them unencrypted even mid-session,
    and _read() must not attempt to decrypt them.
    """
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")
    device._session_key = bytes(range(16))
    device._session_id = bytes(range(8))
    device._nonce_counter = 0

    fake = _FakeConnection(response=b"\xff\x83\xff\x03")
    device._connection = fake

    with pytest.raises(NfcWriteError) as exc_info:
        await device.write_nfc(NfcRecordType.TEXT, b"hello")
    assert exc_info.value.error_code == 3


@pytest.mark.asyncio
async def test_write_nfc_requires_connection() -> None:
    """write_nfc should raise RuntimeError when not connected."""
    device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF")

    with pytest.raises(RuntimeError, match="not connected"):
        await device.write_nfc(NfcRecordType.TEXT, b"hello")
