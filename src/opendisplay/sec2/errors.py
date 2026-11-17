"""Typed SEC2 errors (SECURITY_V2.md § 3.2)."""

from __future__ import annotations

from ..exceptions import ProtocolError


class Sec2Error(ProtocolError):
    """A SEC2 handshake or session failed."""


class Sec2BadPointError(Sec2Error):
    """A received P-256 public key is not a valid point (§ 2, point validation)."""

    def __init__(self, message: str = "The peer's P-256 public key is not a valid point; refusing it.") -> None:
        super().__init__(message)
