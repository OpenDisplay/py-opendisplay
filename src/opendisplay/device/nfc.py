"""Writing an NDEF record to the device's NFC tag (NFC_ENDPOINT 0x0083)."""

from __future__ import annotations

from ..exceptions import NfcNotSupportedError, OpenDisplayTimeoutError
from ..models.enums import NfcRecordType
from ..protocol import (
    NFC_CHUNK_SIZE,
    NFC_INLINE_MAX,
    NFC_WRITE_MAX_TOTAL,
    build_nfc_write_data_command,
    build_nfc_write_end_command,
    build_nfc_write_inline_command,
    build_nfc_write_start_command,
)
from ..protocol.responses import NFC_STATUS_CHUNK_ACK, NFC_STATUS_WRITE_OK, validate_nfc_response
from .channel import SecureChannel


async def write_nfc_record(
    channel: SecureChannel,
    rec_type: NfcRecordType | int,
    payload: bytes,
    *,
    commit_timeout: float,
    ack_timeout: float,
) -> None:
    """Send one NDEF record, inline or chunked. See ``OpenDisplayDevice.write_nfc`` for the contract.

    Args:
        channel: The connection's secure channel.
        rec_type: NDEF record type.
        payload: Record payload bytes, 1..NFC_WRITE_MAX_TOTAL.
        commit_timeout: Timeout for the read that follows the EEPROM commit.
        ack_timeout: Timeout for the intermediate chunk-stage ACKs.
    """
    if not 1 <= len(payload) <= NFC_WRITE_MAX_TOTAL:
        raise ValueError(f"payload length must be 1..{NFC_WRITE_MAX_TOTAL}, got {len(payload)}")

    first_read = True

    async def _read_nfc(read_timeout: float) -> bytes:
        nonlocal first_read
        try:
            response = await channel.read(read_timeout)
        except OpenDisplayTimeoutError:
            if first_read:
                raise NfcNotSupportedError() from None
            raise
        finally:
            first_read = False
        return response

    if len(payload) <= NFC_INLINE_MAX:
        await channel.write(build_nfc_write_inline_command(int(rec_type), payload))
        response = await _read_nfc(commit_timeout)
        validate_nfc_response(response, NFC_STATUS_WRITE_OK)
        return

    await channel.write(build_nfc_write_start_command(int(rec_type), len(payload)))
    response = await _read_nfc(ack_timeout)
    validate_nfc_response(response, NFC_STATUS_CHUNK_ACK)

    for offset in range(0, len(payload), NFC_CHUNK_SIZE):
        chunk = payload[offset : offset + NFC_CHUNK_SIZE]
        await channel.write(build_nfc_write_data_command(chunk))
        response = await _read_nfc(ack_timeout)
        validate_nfc_response(response, NFC_STATUS_CHUNK_ACK)

    await channel.write(build_nfc_write_end_command())
    response = await _read_nfc(commit_timeout)
    validate_nfc_response(response, NFC_STATUS_WRITE_OK)
