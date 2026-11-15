"""How the device object, its channel and its uploader fit together across connections."""

from __future__ import annotations

from opendisplay import OpenDisplayDevice
from opendisplay.device.channel import LegacySession
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.enums import ColorScheme


def _device() -> OpenDisplayDevice:
    caps = DeviceCapabilities(width=2, height=2, color_scheme=ColorScheme.MONO)
    return OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", capabilities=caps)


def test_second_connection_starts_clean(fake_transport) -> None:
    device = _device()
    device._attach(fake_transport())
    device._channel.session = LegacySession(session_key=b"\x01" * 16, session_id=b"\x02" * 8)
    device._uploader.pipe_probed = True
    device._uploader.pipe_supported = True
    device._uploader.pipe_partial_supported = True

    device._on_disconnect()

    assert device._channel.session is None
    uploader = device._uploader
    assert (uploader.pipe_probed, uploader.pipe_supported, uploader.pipe_partial_supported) == (False, False, None)

    device._attach(fake_transport())

    assert device._channel.session is None
    assert device._uploader.pipe_probed is False
