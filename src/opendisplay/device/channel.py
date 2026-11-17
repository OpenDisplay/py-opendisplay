"""The secure channel: every command frame goes through here on its way to the transport.

A :class:`SecureChannel` exists once per connection and holds an optional :class:`Session`.
Without a session, frames pass through in plaintext. :class:`LegacySession` is the
challenge-response scheme of firmware 2.x (``CMD_AUTHENTICATE`` 0x0050, AES-CCM frames).
"""

from __future__ import annotations

import hmac
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..crypto import (
    compute_challenge_response,
    compute_server_proof,
    decrypt_response,
    derive_session_id,
    derive_session_key,
    encrypt_command,
    generate_client_nonce,
)
from ..exceptions import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    AuthenticationSessionExistsError,
    IntegrityCheckError,
    ProtocolError,
)
from ..protocol import (
    CHUNK_SIZE,
    DEFAULT_MAX_FRAME,
    ENCRYPTED_CHUNK_SIZE,
    MAX_START_PAYLOAD,
    PIPE_FRAME_OVERHEAD,
    build_authenticate_step1,
    build_authenticate_step2,
)
from ..protocol.responses import (
    is_authenticate_challenge_frame,
    parse_authenticate_challenge,
    parse_authenticate_success,
    unpack_command_code,
)
from ..sec2.errors import Sec2RecordError
from ..transport import Transport

_LOGGER = logging.getLogger(__name__)

# Step-1 challenge frames tolerated while waiting for the step-2 auth response.
MAX_STRAY_AUTH_FRAMES = 4

# Timeout for each handshake reply (seconds).
TIMEOUT_AUTH = 5.0

# Frames a session may discard while one answer is awaited.
_MAX_DISCARDED_FRAMES = 8


class Session(Protocol):
    """An established security session: how frames are wrapped and what that costs."""

    direct_chunk_size: int
    """Data bytes per 0x71 DIRECT_WRITE chunk inside this session."""

    established_at: float
    """``time.monotonic()`` of the handshake that created the session."""

    def seal(self, frame: bytes) -> bytes:
        """Wrap one plaintext command frame (opcode + payload) for the wire."""

    def is_sealed(self, raw: bytes) -> bool:
        """Whether a received frame is wrapped by this session (some replies are not)."""

    def open(self, raw: bytes) -> bytes:
        """Unwrap a received frame for which :meth:`is_sealed` is true."""

    def pipe_data_size(self, frame_eff: int) -> int:
        """Chunk data capacity of a PIPE_WRITE frame of ``frame_eff`` bytes."""

    def allows_plaintext(self, raw: bytes) -> bool:
        """Whether an unsealed frame may be accepted while this session is live."""


@dataclass
class LegacySession:
    """A firmware 2.x session: AES-CCM frames under a key derived by challenge-response."""

    session_key: bytes = field(repr=False)
    session_id: bytes = field(repr=False)
    nonce_counter: int = 0
    established_at: float = 0.0
    direct_chunk_size: int = ENCRYPTED_CHUNK_SIZE

    # cmd(2) + nonce(16) + payload(1) + tag(12)
    ENCRYPTED_RESPONSE_MIN_LEN = 31

    @classmethod
    async def establish(cls, transport: Transport, key: bytes) -> LegacySession:
        """Perform two-step challenge-response authentication with the device.

        Args:
            transport: The connected transport; the handshake itself is plaintext
            key: 16-byte AES-128 master key

        Raises:
            AuthenticationFailedError: If the device rejects the key or is rate-limited
            InvalidResponseError: If device sends malformed response
        """
        # Step 1: Request server nonce (retry once if device reports existing session)
        for attempt in range(2):
            await transport.write_command(build_authenticate_step1())
            challenge_data = await transport.read_response(timeout=TIMEOUT_AUTH)
            try:
                server_nonce, device_id = parse_authenticate_challenge(challenge_data)
                break
            except AuthenticationSessionExistsError:
                if attempt == 1:
                    raise
                _LOGGER.debug("Device has active session, retrying for fresh challenge")

        # Step 2: Prove key knowledge, receive server proof
        client_nonce = generate_client_nonce()
        challenge = compute_challenge_response(key, server_nonce, client_nonce, device_id)
        await transport.write_command(build_authenticate_step2(client_nonce, challenge))
        server_proof = parse_authenticate_success(await cls._read_step2(transport))  # raises on wrong key / error

        # Derive session key and ID
        session_key = derive_session_key(key, client_nonce, server_nonce, device_id)

        # Verify the device's mutual-auth proof so we authenticate the device, not
        # just the other way around. A device (or MITM) that returns status OK
        # without knowing the master key cannot produce this CMAC. Constant-time
        # compare to avoid leaking a timing side channel.
        expected_proof = compute_server_proof(session_key, server_nonce, client_nonce, device_id)
        if not hmac.compare_digest(server_proof, expected_proof):
            raise AuthenticationFailedError("Device failed mutual authentication (server proof mismatch)")

        return cls(
            session_key=session_key,
            session_id=derive_session_id(session_key, client_nonce, server_nonce),
            nonce_counter=0,
            established_at=time.monotonic(),
        )

    @staticmethod
    async def _read_step2(transport: Transport) -> bytes:
        """Read the step-2 AUTHENTICATE response, skipping repeated step-1 challenges.

        A challenge and a step-2 success share status 0x00 and differ only in
        length. A challenge frame that arrives twice (the notification queue has
        no request/response correlation) would otherwise be taken for the
        success, its server nonce for the proof, and the handshake would fail
        with a misleading server-proof mismatch.

        Raises:
            ProtocolError: If more than ``MAX_STRAY_AUTH_FRAMES`` challenge
                frames arrive instead of the step-2 response
        """
        for _ in range(MAX_STRAY_AUTH_FRAMES + 1):
            response = await transport.read_response(timeout=TIMEOUT_AUTH)
            if not is_authenticate_challenge_frame(response):
                return response
            _LOGGER.warning("Ignoring a repeated auth challenge frame while waiting for the step-2 response")
        raise ProtocolError(
            f"Authentication aborted: more than {MAX_STRAY_AUTH_FRAMES} challenge frames "
            "arrived instead of the step-2 response"
        )

    def seal(self, frame: bytes) -> bytes:
        """Encrypt one plaintext command frame under this session.

        Advances the nonce counter by one so every transmission — including a
        PIPE_WRITE retransmission — carries a fresh, higher nonce.
        """
        sealed = encrypt_command(self.session_key, self.session_id, self.nonce_counter, frame[:2], frame[2:])
        self.nonce_counter += 1
        return sealed

    def is_sealed(self, raw: bytes) -> bool:
        """Whether ``raw`` is an encrypted frame.

        Encrypted packets are at least cmd(2)+nonce(16)+payload(1)+tag(12)=31 bytes.
        Shorter responses are sent unencrypted by the firmware even during a session:
        direct-write ACKs (0x0070-0x0073) are always 2-byte plaintext; error frames
        like {0xFF, 0xFF} (compressed buffer unavailable) are also unencrypted.
        """
        return len(raw) >= self.ENCRYPTED_RESPONSE_MIN_LEN

    def open(self, raw: bytes) -> bytes:
        """Decrypt a frame and return it as plaintext opcode + payload."""
        cmd_code, payload = decrypt_response(self.session_key, raw)
        return cmd_code.to_bytes(2, "big") + payload

    def pipe_data_size(self, frame_eff: int) -> int:
        """Encrypted: frame_eff - CCM envelope (31) - seq (1) = 212 @ 244."""
        return frame_eff - 31 - 1

    def allows_plaintext(self, raw: bytes) -> bool:  # pylint: disable=unused-argument
        """2.x firmware answers some commands in plaintext during a session (see ``is_sealed``)."""
        return True


class SecureChannel:
    """Writes and reads command frames on one connection, through the session if there is one.

    Args:
        transport: The connected transport.
        reauth_key: Master key used to re-authenticate a session that is about to
            expire. ``None`` disables proactive re-authentication.
        session_timeout: Returns the device's ``session_timeout_seconds``, or 0 when
            it is unknown or the device has no timeout.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        reauth_key: bytes | None = None,
        session_timeout: Callable[[], int] = lambda: 0,
    ) -> None:
        self.transport = transport
        self.session: Session | None = None
        self._reauth_key = reauth_key
        self._session_timeout = session_timeout

    async def authenticate(self, key: bytes) -> None:
        """Establish a 2.x session with ``key``; later frames are encrypted and decrypted.

        The current session, if any, stays in place until the new handshake succeeds.
        """
        self.session = await LegacySession.establish(self.transport, key)
        _LOGGER.info("Authentication successful, session established")

    def reset(self) -> None:
        """Drop the session.

        Called on disconnect so a reused device object does not encrypt against
        a session the firmware has already torn down.
        """
        self.session = None

    def _seal(self, frame: bytes) -> bytes:
        """Wrap ``frame`` under the active session, or return it unchanged without one.

        Does NOT re-authenticate (callers handle re-auth once before a stream,
        never mid-stream).
        """
        return self.session.seal(frame) if self.session is not None else frame

    async def write(self, frame: bytes, response: bool = True) -> None:
        """Write a command, encrypting it if an active session exists.

        Args:
            frame: Plaintext command frame (opcode + payload).
            response: Passed through to the transport. False requests a BLE Write
                Without Response (used for 0x71 data chunks); applies whether or not
                the frame is encrypted.
        """
        if self.session is not None:
            await self._reauthenticate_if_needed()
        await self.transport.write_command(self._seal(frame), response=response)

    async def write_stream_frame(self, frame: bytes, *, response: bool) -> None:
        """Encrypt (no re-auth) and write a live PIPE_WRITE stream frame.

        Passes ``drain_stale=False`` so queued sliding-window ACKs are preserved.
        Used for every 0x0081 DATA frame (response=False) and the 0x0082 END
        (response=True). Re-authentication is intentionally skipped: it runs once
        before 0x0080 and never mid-stream (Part 1 §1.6).
        """
        await self.transport.write_command(self._seal(frame), response=response, drain_stale=False)

    async def _reauthenticate_if_needed(self) -> None:
        """Re-authenticate proactively at 90% of session_timeout_seconds."""
        if self._reauth_key is None or self.session is None:
            return
        timeout = self._session_timeout()
        if timeout == 0:
            return
        elapsed = time.monotonic() - self.session.established_at
        if elapsed >= timeout * 0.9:
            _LOGGER.info(
                "Session approaching timeout (%.0fs / %ds), re-authenticating",
                elapsed,
                timeout,
            )
            await self.authenticate(self._reauth_key)

    async def read(self, timeout: float) -> bytes:
        """Read a response, decrypting it if an active session exists.

        Raises:
            AuthenticationRequiredError: If device returns 0xFE (encryption required, no active session)
            IntegrityCheckError: If device returns 0xFF (decrypt/integrity check failed, command not executed)
        """
        raw, opened = await self._read_session_frame(timeout)
        if opened:
            return raw
        # Firmware returns [cmd_high, cmd_low, 0xFE] (3 bytes) when a command
        # requires authentication but no session is active.
        if len(raw) == 3 and raw[2] == 0xFE:
            raise AuthenticationRequiredError(
                "Device requires an encryption key — pass encryption_key=bytes.fromhex('...') to OpenDisplayDevice"
            )
        # Firmware returns [cmd_high, cmd_low, 0xFF] (3 bytes) when an encrypted
        # command fails AES-GCM decryption / tag verification. The command was
        # NOT executed, so it must not be treated as an ACK — the 2-byte echo
        # otherwise matches the expected command code and passes validation.
        # Distinct from the 2-byte {0xFF, 0xFF} compressed-failure frame and
        # 4-byte NACK frames, which have different lengths.
        if len(raw) == 3 and raw[2] == 0xFF:
            raise IntegrityCheckError(
                f"Device rejected command 0x{unpack_command_code(raw):04x}: "
                "decryption/integrity check failed (command not executed) — likely a dropped or corrupted packet"
            )
        return raw

    async def _read_session_frame(self, timeout: float) -> tuple[bytes, bool]:
        """The next frame the session accepts, and whether it was opened (unsealed) by the session.

        Without a session every frame is accepted as it is.

        Inside a SEC2 session an unsealed frame outside the plaintext set, and a sealed frame
        with a stale counter or a bad tag, are discarded (SECURITY_V2.md § 9.5); the third bad
        tag raises ``Sec2SessionLostError`` from the session.
        """
        for _ in range(_MAX_DISCARDED_FRAMES + 1):
            raw = await self.transport.read_response(timeout=timeout)
            session = self.session
            if session is None:
                return raw, False
            if session.is_sealed(raw):
                try:
                    return session.open(raw), True
                except Sec2RecordError as err:
                    _LOGGER.warning("Discarding a sealed frame: %s", err)
                    continue
            if session.allows_plaintext(raw):
                return raw, False
            _LOGGER.warning("Discarding an unsealed %d-byte frame the session does not allow", len(raw))
        raise ProtocolError(f"More than {_MAX_DISCARDED_FRAMES} frames were discarded while waiting for an answer")

    def start_payload_limit(self) -> int:
        """The largest plaintext START command (opcode included) the current session can carry.

        Inside a session it is the session's encrypted chunk budget, which depends on the scheme
        (2.x and SEC2 both 154 today); without one, ``MAX_START_PAYLOAD``.
        """
        return self.session.direct_chunk_size if self.session is not None else MAX_START_PAYLOAD

    def direct_chunk_size(self) -> int:
        """Data bytes per 0x71 DIRECT_WRITE chunk for the current session and transport.

        Encrypted BLE session -> ENCRYPTED_CHUNK_SIZE (154); plain BLE ->
        CHUNK_SIZE (230); a large-frame LAN transport -> ``max_frame - 2`` (the
        2-byte opcode header), e.g. 4092 for a 4094-byte payload / 4096-byte
        wire frame.
        """
        if self.session is not None:
            return self.session.direct_chunk_size
        max_frame = self.transport.max_frame
        if max_frame > DEFAULT_MAX_FRAME:
            return max_frame - 2
        return CHUNK_SIZE

    def pipe_data_size(self, frame_eff: int) -> int:
        """Chunk data capacity for a pipe frame at ``frame_eff`` bytes.

        Encrypted: frame_eff - CCM envelope (31) - seq (1) = 212 @ 244.
        Plaintext: frame_eff - PIPE_FRAME_OVERHEAD (cmd 2 + seq 1) = 241 @ 244.
        """
        if self.session is not None:
            return self.session.pipe_data_size(frame_eff)
        return frame_eff - PIPE_FRAME_OVERHEAD
