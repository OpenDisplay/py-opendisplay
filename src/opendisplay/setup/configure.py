"""Writing a config in the setup flow: the secrets a config carries, and a checked write.

Moved from the CLI's ``migrate`` so the setup runner and the CLI share it. An unclaimed firmware 3.0
device does not store secrets (SECURITY_V2.md § 10.4, § 10.5): a config for one is written without
them, and its 0x27 in the form the device stores.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from ..exceptions import OpenDisplayError
from ..models.config import GlobalConfig, SecurityConfig, WifiConfig
from ..protocol import config_parser as packet_ids
from ..protocol.config_serializer import serialize_config

if TYPE_CHECKING:
    from ..device.api import Device

_SHOW_KEY_ON_SCREEN = 0x02  # SecurityConfig.flags bit 1
# 0x2D FindMyConfig key fields (od_cfg_policy.c S2D_*): eik[32] at 8, net_a_adv_key[28] at 40,
# advertisement_key[20] at 76; the check applies to bodies of at least 96 bytes.
_FINDMY_KEY_FIELDS = ((8, 32), (40, 28), (76, 20))
_FINDMY_MIN_BODY = 96
_FINDMY_PACKET = 2 + packet_ids.FINDMY_CONFIG_SIZE  # [number][0x2D][body]


class ConfigMismatchError(OpenDisplayError):
    """The config read back after a write differs from what was written."""


def _findmy_packets(raw: bytes) -> list[bytes]:
    """Split ``findmy_config_raw`` (every 0x2D packet, header included, in order) into packets."""
    return [raw[i : i + _FINDMY_PACKET] for i in range(0, len(raw), _FINDMY_PACKET)]


def _findmy_has_keys(packet: bytes) -> bool:
    body = packet[2:]
    return len(body) >= _FINDMY_MIN_BODY and any(any(body[o : o + n]) for o, n in _FINDMY_KEY_FIELDS)


def _findmy_without_keys(packet: bytes) -> bytes:
    if len(packet) - 2 < _FINDMY_MIN_BODY:
        return packet
    out = bytearray(packet)
    for offset, size in _FINDMY_KEY_FIELDS:
        out[2 + offset : 2 + offset + size] = bytes(size)
    return bytes(out)


def find_secrets(config: GlobalConfig) -> list[str]:
    """The secrets a config carries, by name; never their values."""
    found = []
    security = config.security_config
    if security is not None and (security.encryption_enabled or any(security.encryption_key)):
        found.append("encryption key")
    if config.wifi_config is not None and any(config.wifi_config.password):
        found.append("WiFi password")
    if config.findmy_config_raw and any(_findmy_has_keys(p) for p in _findmy_packets(config.findmy_config_raw)):
        found.append("FindMy keys")
    return found


def unclaimed_form(config: GlobalConfig) -> GlobalConfig:
    """The config with its 0x27 as an unclaimed 3.0 device stores every 0x27 it is written.

    The firmware's ``unclaimed_form`` (od_cfg_policy.c) clears ``encryption_enabled``, the key,
    the show-key flag and the first reserved byte on every write to an unclaimed device.
    """
    security: SecurityConfig | None = config.security_config
    if security is None:
        return config
    return dataclasses.replace(
        config,
        security_config=dataclasses.replace(
            security,
            encryption_enabled=0,
            encryption_key=bytes(16),
            flags=security.flags & ~_SHOW_KEY_ON_SCREEN,
            reserved=bytes(1) + security.reserved[1:],
        ),
    )


def strip_secrets(config: GlobalConfig) -> GlobalConfig:
    """The config without secrets, in the form an unclaimed 3.0 device stores.

    The 0x27 in its unclaimed form (``unclaimed_form``), the 0x26 password zeroed, and the key
    fields of every 0x2D zeroed in place; the other FindMy settings stay.
    """
    config = unclaimed_form(config)
    wifi: WifiConfig | None = config.wifi_config
    if wifi is not None:
        wifi = dataclasses.replace(wifi, password=bytes(len(wifi.password)))
    findmy = config.findmy_config_raw
    if findmy:
        findmy = b"".join(_findmy_without_keys(p) for p in _findmy_packets(findmy))
    return dataclasses.replace(config, wifi_config=wifi, findmy_config_raw=findmy)


# ── a checked write ──────────────────────────────────────────────────────────


def without_security(config: GlobalConfig) -> bytes:
    """The serialised config without its 0x27, which the firmware rewrites on store."""
    return serialize_config(dataclasses.replace(config, security_config=None))


async def write_and_verify(dev: Device, config: GlobalConfig) -> bool:
    """Write ``config``, read it back and compare.

    Returns:
        Whether the device stored the security packet (0x27) in its own form, which the firmware
        does on every store (``unclaimed_form`` unclaimed, the key at rest claimed; od_cfg_policy.c).

    Raises:
        ConfigMismatchError: Anything but the 0x27 differs; the write may have been interrupted.
    """
    await dev.config.write(config)
    stored = await dev.config.read()
    if without_security(stored) != without_security(config):
        raise ConfigMismatchError(
            f"The config read back ({len(serialize_config(stored))} bytes) does not match what was written "
            f"({len(serialize_config(config))} bytes). Keep your backup; the write may have been interrupted."
        )
    return serialize_config(stored) != serialize_config(config)
