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


class ConfigParseError(ProtocolError):
    """Failed to parse device configuration."""

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


class AuthenticationRequiredError(AuthenticationError):
    """Command rejected because no authenticated session exists.

    Raised when the device returns 0xFE — encryption is enabled but no session
    has been established. Either no key was provided or the session expired.
    """

    pass


class ImageEncodingError(OpenDisplayError):
    """Failed to encode image."""

    pass
