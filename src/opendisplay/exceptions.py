"""Exceptions for opendisplay package."""

from __future__ import annotations


class OpenDisplayError(Exception):
    """Base exception for all opendisplay errors."""

    pass


class OpenDisplayConnectionError(OpenDisplayError):
    """Transport-neutral connection failure.

    Superclass for connection errors on any transport (BLE or TCP/LAN). Callers
    that don't care which transport failed should catch this; BLE-specific code
    can still catch the ``BLEConnectionError`` subclass. Named to avoid shadowing
    the builtin ``ConnectionError``.
    """

    pass


class OpenDisplayTimeoutError(OpenDisplayError):
    """Transport-neutral operation timeout.

    Superclass for read/connect timeouts on any transport. Named to avoid
    shadowing the builtin ``TimeoutError``.
    """

    pass


class BLEConnectionError(OpenDisplayConnectionError):
    """BLE connection failed."""

    pass


class BLETimeoutError(OpenDisplayTimeoutError):
    """Operation timed out."""

    pass


class ProtocolError(OpenDisplayError):
    """Protocol communication error."""

    pass


class RefreshTimeoutError(ProtocolError):
    """Display refresh timed out (device sent 0x74) after the image was delivered.

    Distinct from other ProtocolErrors because it fires *after* the transfer
    completed: the device already cleared its displayed etag, so callers must
    not treat it as a cleanly-aborted transfer (e.g. the pipe-partial path
    re-raises instead of falling back to a full upload).
    """

    pass


class ConfigParseError(ProtocolError):
    """Failed to parse device configuration."""

    pass


class NoStoredConfigError(ProtocolError):
    """The device has no configuration stored (READ_CONFIG answered with an error frame).

    Normal for a freshly flashed device, and for firmware 3.0 after an install that erased
    the config: the device is reachable but cannot drive a display until a config is written.
    """

    def __init__(
        self, message: str = "Device has no stored configuration (READ_CONFIG returned an error frame)"
    ) -> None:
        super().__init__(message)


class TruncatedConfigError(ConfigParseError):
    """Device stopped sending config chunks before the advertised length.

    Raised during interrogate() when a config-read transfer stalls — a chunk
    read times out mid-transfer, or the device sends an empty chunk making no
    progress toward the total length — instead of hanging or returning a
    partial config.
    """

    pass


class InvalidResponseError(ProtocolError):
    """Device returned invalid response."""

    pass


class AuthenticationError(ProtocolError):
    """Base class for authentication errors."""

    pass


class AuthenticationFailedError(AuthenticationError):
    """Authentication was attempted but rejected by the device.

    Raised when the device returns a bad-key or rate-limit status during the
    challenge-response handshake. The configured key is likely wrong.
    """

    pass


class AuthenticationSessionExistsError(AuthenticationError):
    """Device still has an active session from a previous connection.

    Raised when the device returns status 0x02 in the step-1 challenge response.
    The caller should retry the authentication request to get a fresh challenge.
    """

    pass


class AuthenticationRequiredError(AuthenticationError):
    """Command rejected because no authenticated session exists.

    Raised when the device returns 0xFE — encryption is enabled but no session
    has been established. Either no key was provided or the session expired.
    """

    pass


class IntegrityCheckError(ProtocolError):
    """Device rejected a command because its decrypt/integrity check failed.

    Raised when the firmware returns the 3-byte frame ``{0x00, cmd, 0xFF}``.
    The encrypted command was received but failed AES-GCM decryption or tag
    verification (e.g. a dropped/corrupted packet), so the firmware did NOT
    execute it. The command must be retried, not treated as acknowledged.
    """

    pass


class NeedsMigrationError(ProtocolError):
    """The device refused an upload because it has no usable drive data for its display.

    Firmware 3.0 drives a panel from data in the config. A config written by a client that
    does not know those packets leaves the device unable to draw until one that includes
    them is written.
    """

    def __init__(
        self,
        message: str = (
            "The device has no drive data for its display (firmware 3.0 needs a migrated config); "
            "write a config that includes the panel's drive packets."
        ),
    ) -> None:
        super().__init__(message)


class PanelBusyError(ProtocolError):
    """The device refused an upload because the panel was still busy. Transient: retry."""

    def __init__(
        self, message: str = "The panel was still busy when the upload started; retry in a few seconds."
    ) -> None:
        super().__init__(message)


class ConfigWriteBusyError(ProtocolError):
    """The device did not store a config because a display pass was running. Transient: retry."""

    def __init__(
        self,
        message: str = "The device is refreshing its display and did not store the config; retry after the refresh.",
    ) -> None:
        super().__init__(message)


class ConfigSecretNeedsClaimError(ProtocolError):
    """An unclaimed firmware 3.0 device refused a config that carries a secret; nothing was stored.

    Secrets are a 0x27 key or ``encryption_enabled``, a 0x26 WiFi password, and 0x2D FindMy key
    material (SECURITY_V2.md § 10.4). Claim the device first, or write the config without them.
    """

    def __init__(
        self,
        message: str = (
            "The device is not claimed and does not store secrets (encryption key, WiFi password, "
            "FindMy keys); claim it first, or write the config without them."
        ),
    ) -> None:
        super().__init__(message)


class ConfigKeyMismatchError(ProtocolError):
    """A claimed firmware 3.0 device refused a config whose 0x27 key is neither zero nor its own key."""

    def __init__(
        self,
        message: str = (
            "The config carries an encryption key that is not the device's key; "
            "write it with the key zeroed (the device keeps its own)."
        ),
    ) -> None:
        super().__init__(message)


class NfcWriteError(ProtocolError):
    """Device rejected an NFC_ENDPOINT (0x0083) write with an error frame.

    Raised when the firmware returns ``{0xFF, 0x83, 0xFF, err}``. ``error_code``
    carries the raw firmware error byte (see ``NFC_ERROR_MESSAGES``) so callers
    can distinguish e.g. a too-large record from NFC being disabled in config.
    """

    def __init__(self, message: str, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class IdentNotSupportedError(ProtocolError):
    """Device does not implement READ_IDENT (0x0046).

    Firmware before 3.0 (EFR32BG22 before 2.3) does not know the command. Depending on the
    build it stays silent, answers with the 3-byte "session required" frame, or sends a NACK.
    """

    def __init__(
        self,
        message: str = "Device does not answer READ_IDENT (firmware before 3.0 does not implement it)",
        *,
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        #: True when the device simply did not answer. Old firmware does that, but so does a
        #: slow or dropping link, so silence alone does not prove the firmware is old.
        self.timed_out = timed_out


class NfcNotSupportedError(ProtocolError):
    """Device did not respond to an NFC_ENDPOINT (0x0083) command.

    Firmware older than the NFC write feature stays silent on unknown opcodes,
    so a missing response is inconclusive rather than a confirmed rejection;
    it could also be a dropped packet. Raised when a write times out waiting
    for any 0x0083 response.
    """

    def __init__(self, message: str = "Device may not support NFC write (no response to NFC command)") -> None:
        super().__init__(message)


class ImageEncodingError(OpenDisplayError):
    """Failed to encode image."""

    pass


class OTANotSupportedError(OpenDisplayError):
    """OTA firmware update is not supported for this IC type."""

    pass


class OTAError(OpenDisplayError):
    """Firmware update failed."""

    pass
