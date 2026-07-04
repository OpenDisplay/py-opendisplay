"""Test that NFC (0x2A) packets are emitted after packets firmware parses (M4)."""

from __future__ import annotations

from opendisplay.models.config import (
    DataExtended,
    FlashConfig,
    GlobalConfig,
    ManufacturerData,
    NfcConfig,
    PowerOption,
    SystemConfig,
)
from opendisplay.protocol.config_parser import _get_packet_size
from opendisplay.protocol.config_serializer import (
    PACKET_TYPE_DATA_EXTENDED,
    PACKET_TYPE_FLASH_CONFIG,
    PACKET_TYPE_NFC_CONFIG,
    serialize_config,
)


def _packet_type_order(blob: bytes) -> list[int]:
    """Return the packet-type IDs in the serialized TLV stream, in order."""
    data = blob[3:-2]  # strip 3-byte wrapper header and 2-byte CRC
    order: list[int] = []
    off = 0
    while off < len(data) - 1:
        ptype = data[off + 1]
        size = _get_packet_size(ptype)
        if size is None:
            break
        order.append(ptype)
        off += 2 + size
    return order


def test_nfc_emitted_after_flash_and_data_extended() -> None:
    config = GlobalConfig(
        system=SystemConfig(ic_type=0, communication_modes=0, device_flags=0, pwr_pin=0xFF, reserved=b"\x00" * 15),
        manufacturer=ManufacturerData(manufacturer_id=0, board_type=0, board_revision=0, reserved=b"\x00" * 6),
        power=PowerOption(
            power_mode=0,
            battery_capacity_mah=b"\x00\x00\x00",
            sleep_timeout_ms=0,
            tx_power=0,
            sleep_flags=0,
            battery_sense_pin=0xFF,
            battery_sense_enable_pin=0xFF,
            battery_sense_flags=0,
            capacity_estimator=0,
            voltage_scaling_factor=0,
            deep_sleep_current_ua=0,
            deep_sleep_time_seconds=0,
            reserved=b"\x00" * 10,
        ),
        nfc_configs=[NfcConfig.from_bytes(bytes(32))],
        flash_configs=[FlashConfig.from_bytes(bytes(32))],
        data_extended=DataExtended(),
    )

    order = _packet_type_order(serialize_config(config))

    assert PACKET_TYPE_NFC_CONFIG in order
    assert PACKET_TYPE_FLASH_CONFIG in order
    assert PACKET_TYPE_DATA_EXTENDED in order
    nfc_pos = order.index(PACKET_TYPE_NFC_CONFIG)
    assert nfc_pos > order.index(PACKET_TYPE_FLASH_CONFIG)
    assert nfc_pos > order.index(PACKET_TYPE_DATA_EXTENDED)
