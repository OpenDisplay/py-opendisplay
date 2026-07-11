"""Exceptions for opendisplay package."""

from __future__ import annotations


class OpenDisplayError(Exception):
    """Base exception for all opendisplay errors."""

    pass


class BLEConnectionError(OpenDisplayError):
    """BLE connection failed."""

    pass


class BLETimeoutError(OpenDisplayError):
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


class ImageEncodingError(OpenDisplayError):
    """Failed to encode image."""

    pass


class OTANotSupportedError(OpenDisplayError):
    """OTA firmware update is not supported for this IC type."""

    pass


class OTAError(OpenDisplayError):
    """Firmware update failed."""

    pass
