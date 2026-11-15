"""How the device object, its channel and its uploader fit together across connections."""

from __future__ import annotations

from opendisplay import OpenDisplayDevice, RefreshMode
from opendisplay.device.channel import LegacySession
from opendisplay.models.capabilities import DeviceCapabilities
from opendisplay.models.enums import ColorScheme

ACK_START, ACK_DATA, ACK_END, ACK_REFRESH = b"\x00\x70", b"\x00\x71", b"\x00\x72", b"\x00\x73"


def _device() -> OpenDisplayDevice:
    caps = DeviceCapabilities(width=2, height=2, color_scheme=ColorScheme.MONO)
    return OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", capabilities=caps, max_queue_size=1)


def _mark_probed(device: OpenDisplayDevice) -> None:
    device._uploader.pipe_probed = True
    device._uploader.pipe_supported = True
    device._uploader.pipe_partial_supported = True


def _probe_state(device: OpenDisplayDevice) -> tuple[bool, bool, bool | None]:
    uploader = device._uploader
    return uploader.pipe_probed, uploader.pipe_supported, uploader.pipe_partial_supported


def test_disconnect_callback_drops_the_session_and_the_probe_result(fake_transport) -> None:
    device = _device()
    device._attach(fake_transport())
    device._channel.session = LegacySession(session_key=b"\x01" * 16, session_id=b"\x02" * 8)
    _mark_probed(device)

    device._on_disconnect()

    assert device._channel.session is None
    assert _probe_state(device) == (False, False, None)


def test_reconnect_starts_without_a_session(fake_transport) -> None:
    device = _device()
    device._attach(fake_transport())
    device._channel.session = LegacySession(session_key=b"\x01" * 16, session_id=b"\x02" * 8)

    device._attach(fake_transport())  # a second connection on the same object

    assert device._channel.session is None


def test_probe_result_survives_a_reconnect_when_no_disconnect_was_reported(fake_transport) -> None:
    """Only the disconnect callback clears the PIPE_WRITE probe result, as before the split.

    Not every backend fires the callback for a disconnect the caller asked for, and an
    explicit ``transport=`` has none; such a device is not probed again on the next connection.
    """
    device = _device()
    device._attach(fake_transport())
    _mark_probed(device)

    device._attach(fake_transport())

    assert _probe_state(device) == (True, True, True)


async def test_timeout_set_on_the_device_applies_to_its_uploads(fake_transport) -> None:
    device = _device()
    device.TIMEOUT_REFRESH = 1.5  # type: ignore[misc]  # e.g. a caller tuning for one slow panel
    fake = fake_transport([ACK_START, ACK_DATA, ACK_END, ACK_REFRESH])
    device._attach(fake)

    await device._uploader.execute_upload(b"\x00" * 10, RefreshMode.FULL, use_compression=False)

    assert fake.timeouts[3] == 1.5
