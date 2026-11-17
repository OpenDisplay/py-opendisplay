"""Sending an encoded frame to the device: partial, pipe/streaming and direct write."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

from PIL import (
    Image,
)

from ..encoding import (
    FIRMWARE_ZLIB_WINDOW_BITS,
    compress_image_data,
    zlib_window_bits,
)
from ..exceptions import (
    AuthenticationError,
    IntegrityCheckError,
    InvalidResponseError,
    OpenDisplayTimeoutError,
    ProtocolError,
    RefreshTimeoutError,
)
from ..models.config import (
    DisplayConfig,
    GlobalConfig,
)
from ..models.enums import (
    ColorScheme,
    RefreshMode,
)
from ..partial import (
    PARTIAL_FLAG_COMPRESSED,
    PartialRegion,
    PartialState,
    _generate_etag,
    build_partial_logical_stream,
    compute_partial_region,
    encode_segment_wire,
    parse_nack,
)
from ..protocol import (
    DEFAULT_MAX_FRAME,
    MAX_COMPRESSED_SIZE,
    MAX_PTO,
    PIPE_FLAG_PARTIAL,
    PIPE_MAX_RETX_FRACTION,
    PIPE_RETX_ACK_SPACING,
    TIMEOUT_PIPE_START,
    CommandCode,
    PipeParams,
    PipePartialRequest,
    build_direct_write_data_command,
    build_direct_write_end_command,
    build_direct_write_end_with_etag,
    build_direct_write_partial_start,
    build_direct_write_start_compressed,
    build_direct_write_start_uncompressed,
    build_pipe_write_data_command,
    build_pipe_write_end_command,
    build_pipe_write_start_command,
    classify_pipe_frame,
    parse_pipe_data_ack,
    parse_pipe_data_nack,
    parse_pipe_start_response,
    unpack_ack_ranges,
    validate_ack_response,
)
from ..protocol.responses import (
    PIPE_FRAME_ACK,
    PIPE_FRAME_END_ACK,
    PIPE_FRAME_END_NACK,
    PIPE_FRAME_NACK,
    PIPE_START_NACK_COMPRESSION,
    PIPE_START_NACK_ETAG_MISMATCH,
    PIPE_START_NACK_PARTIAL_UNSUPPORTED,
    PIPE_START_NACK_RECT_INVALID,
    check_response_type,
    is_compressed_failure_frame,
    raise_for_upload_start_refusal,
)
from .channel import SecureChannel

_LOGGER = logging.getLogger(__name__)


class _PipePartialEtagMismatch(Exception):
    """Internal: the device NACKed a pipe-partial START with 0x05 (etag mismatch).

    The device has already cleared its displayed_etag, so a 0x76 retry would
    mismatch again — the caller clears PartialState and goes straight to full.
    """


class _PipePartialRejected(Exception):
    """Internal: the device NACKed a pipe-partial START with 0x06/0x07.

    0x06 = partial unsupported (bpp/driver), 0x07 = rect invalid. The 0x76
    fallback runs identical checks and would fail identically, so the caller
    clears PartialState and goes straight to full.
    """


@dataclass(frozen=True)
class UploadTarget:
    """What the uploader needs to know about the device it is sending to, read at upload time."""

    config: GlobalConfig | None
    color_scheme: ColorScheme | None
    """``None`` when the device's capabilities are not known."""
    name: str
    """The device address, for log lines."""


class UploadTimeouts(Protocol):  # pylint: disable=too-few-public-methods
    """The timeouts an upload reads (seconds). ``Uploader`` and ``OpenDisplayDevice`` both carry them."""

    TIMEOUT_FIRST_CHUNK: float
    TIMEOUT_ACK: float
    TIMEOUT_DIRECT_WRITE_DATA_ACK: float
    TIMEOUT_UNCOMPRESSED_END_ACK: float
    TIMEOUT_COMPRESSED_END_ACK: float
    TIMEOUT_REFRESH: float
    TIMEOUT_PIPE_DATA_COMPRESSED: float
    TIMEOUT_PIPE_DATA_UNCOMPRESSED: float
    TIMEOUT_PIPE_TAIL_FLUSH: float


class Uploader:
    """Sends encoded frames over a :class:`SecureChannel`.

    Holds the per-connection PIPE_WRITE probe state. It takes no lock: its methods run
    inside the device's serialized upload commands.

    Args:
        channel: The connection's secure channel.
        target: Returns the current :class:`UploadTarget`; called when a decision needs it,
            because the device config can arrive after the uploader is built.
        blocks_per_ack: Requested PIPE_WRITE ACK cadence.
        max_queue_size: Requested PIPE_WRITE window; 1 or less disables PIPE_WRITE.
        timeouts: Where the ``TIMEOUT_*`` values are read from, at the moment each is needed.
            The device passes itself, so a timeout changed on the device object applies to
            its uploads. Defaults to the values defined on this class.
    """

    # Operation timeouts (seconds)
    TIMEOUT_FIRST_CHUNK = 10.0  # First chunk may take longer
    TIMEOUT_ACK = 5.0  # Command acknowledgments
    # DIRECT_WRITE DATA (0x0071), both protocols. The ACK follows the firmware's
    # bbepWriteData() call, which blocks SPI for up to ~60s on Spectra/ACeP. The
    # compressed path does that same blocking write plus an inflate — one 4KB frame
    # expands to ~90KB of panel data — so it needs the budget at least as much as
    # the uncompressed one. It previously fell through to TIMEOUT_ACK (5s), which
    # made a healthy-but-slow transfer look dead mid-stream.
    TIMEOUT_DIRECT_WRITE_DATA_ACK = 90.0
    TIMEOUT_UNCOMPRESSED_END_ACK = 90.0  # Uncompressed END: some firmware variants refresh before replying (~60s max)
    TIMEOUT_COMPRESSED_END_ACK = 90.0  # Compressed END: decompression + full SPI write to IC (~60s on Spectra/ACeP)
    TIMEOUT_REFRESH = 90.0  # Display refresh (firmware spec: up to 60s)

    # PIPE_WRITE per-path progress timeouts (Part 1 §1.4): a compressed chunk lands
    # fast; an uncompressed chunk can block bbepWriteData on SPI for the Spectra/ACeP
    # ~60s SPI-block budget, so 90s preserves it.
    TIMEOUT_PIPE_DATA_COMPRESSED = 5.0
    TIMEOUT_PIPE_DATA_UNCOMPRESSED = 90.0
    # Compressed tail-flush: firmware ACKs only every N_eff accepted frames, so a
    # tail of < N_eff unacked frames never earns a cadence ACK on its own. Rather
    # than stalling chunk_timeout (5 s) waiting for one, block briefly and then
    # dup-probe (resend the oldest unacked chunk) — the duplicate elicits an
    # immediate ACK from firmware. Never applied to the uncompressed path, whose
    # 90 s budget covers legitimate SPI stalls.
    TIMEOUT_PIPE_TAIL_FLUSH = 0.5

    def __init__(
        self,
        channel: SecureChannel,
        *,
        target: Callable[[], UploadTarget],
        blocks_per_ack: int,
        max_queue_size: int,
        timeouts: UploadTimeouts | None = None,
    ) -> None:
        self._channel = channel
        self._target = target
        self._timeouts: UploadTimeouts = timeouts if timeouts is not None else self
        self._blocks_per_ack = blocks_per_ack
        self._max_queue_size = max_queue_size
        self.pipe_probed: bool = False  # capability determined this connection
        self.pipe_supported: bool = False  # probe result (valid iff pipe_probed)
        # Pipe-partial support is inferred, then confirmed by the 0x0080 ACK flags
        # bit1: None = unknown, True = confirmed, False = rejected this connection
        # (older pipe-capable firmware NACKs the partial flag with 0x02).
        self.pipe_partial_supported: bool | None = None

    def reset_pipe_state(self) -> None:
        """Forget what this connection learned about PIPE_WRITE.

        All pipe negotiation/capability state is per-connection (Part 1 §1.1).
        """
        self.pipe_probed = False
        self.pipe_supported = False
        self.pipe_partial_supported = None

    @staticmethod
    def _raise_for_start_refusal(response: bytes) -> None:
        """Raise the typed error if ``response`` is a START NACK that names its reason.

        Checked before the generic handling so that "needs migration" or "panel busy" on a
        compressed START is reported as such instead of triggering the uncompressed retry.
        """
        nack = parse_nack(response)
        if nack is not None:
            raise_for_upload_start_refusal(*nack)

    def _color_scheme(self) -> ColorScheme:
        """The device's color scheme, raising RuntimeError when capabilities are unknown."""
        color_scheme = self._target().color_scheme
        if color_scheme is None:
            raise RuntimeError("Device capabilities unknown - interrogate first or provide config/capabilities")
        return color_scheme

    @property
    def _display_config(self) -> DisplayConfig | None:
        """The first display's config, or None when no config (or no display) is known."""
        return config.displays[0] if (config := self._target().config) and config.displays else None

    @property
    def supports_compression(self) -> bool:
        """Whether a compressed upload is worth attempting.

        Post-2.0 configs may advertise only streaming decompression (bit 0x01,
        historically ZIPXL) without the plain ZIP bit; firmware 2.0 accepts
        compressed uploads either way (<=1.81 NACKs without the ZIP bit and the
        upload falls back to uncompressed). Without a config, assume yes: the
        compressed START falls back to uncompressed when the device rejects it.
        """
        display = self._display_config
        return display is None or display.supports_zip or display.supports_streaming_decompression

    def pipe_write_allowed(self, display: DisplayConfig | None) -> bool:
        """Whether a PIPE_WRITE (0x0080) attempt may be made on this connection.

        Gated on the device config advertising pipe support (transmission_modes
        bit 0x10) — a device whose config lacks the bit never sees a 0x0080 probe.
        Also off when disabled by the caller (max_queue_size <= 1), when this
        connection already probed negative, and on LAN: PIPE_WRITE (0x0080-0x82)
        is forbidden there (SECTION 9 rule 2), so a large-frame transport streams
        via plain DIRECT_WRITE instead.
        """
        return (
            display is not None
            and display.supports_pipe_write
            and self._max_queue_size > 1
            and not (self.pipe_probed and not self.pipe_supported)
            and self._channel.transport.max_frame <= DEFAULT_MAX_FRAME
        )

    async def upload_prepared(
        self,
        image_data: bytes,
        compressed_data: bytes | None,
        processed_image: Image.Image,
        refresh_mode: RefreshMode,
        compress: bool,
        progress_callback: Callable[[int, int], None] | None,
        state: PartialState | None,
    ) -> None:
        """Send an encoded frame: partial when ``state`` allows it, otherwise full.

        With a ``state`` a full upload commits a new etag, so the next upload can
        be a partial one against this frame. It refreshes in the requested mode;
        PARTIAL is not a mode for a whole new frame, so that request refreshes FULL.
        """
        if state is None:
            await self.dispatch_upload(image_data, refresh_mode, compress, compressed_data, progress_callback)
            _LOGGER.info("Image upload complete")
            return

        partial_outcome = await self.maybe_upload_partial(processed_image, state, progress_callback)
        if partial_outcome == "success":
            _LOGGER.info("Image upload complete (partial path)")
            return
        if partial_outcome == "no_change":
            _LOGGER.info("No pixels changed; skipping upload")
            return
        _LOGGER.info("Partial path unavailable or etag mismatch; continuing with full upload")

        full_refresh_mode = RefreshMode.FULL if refresh_mode is RefreshMode.PARTIAL else refresh_mode
        new_etag = _generate_etag()
        etag_committed = await self.dispatch_upload(
            image_data, full_refresh_mode, compress, compressed_data, progress_callback, new_etag=new_etag
        )
        _LOGGER.info("Image upload complete")
        self.update_partial_state(state, processed_image, new_etag if etag_committed else None)

    async def dispatch_upload(
        self,
        image_data: bytes,
        refresh_mode: RefreshMode,
        compress: bool,
        compressed_data: bytes | None,
        progress_callback: Callable[[int, int], None] | None,
        new_etag: int | None = None,
    ) -> bool:
        """Choose compressed or uncompressed upload protocol and execute it.

        Returns True if the device committed ``new_etag`` (END-with-etag was
        sent), False if the firmware auto-completed the upload.
        """
        display_cfg = self._display_config
        supports_compression = self.supports_compression
        streaming_decompression = bool(display_cfg and display_cfg.supports_streaming_decompression)
        if (
            compress
            and supports_compression
            and (
                compressed_data is None
                or (streaming_decompression and zlib_window_bits(compressed_data) != FIRMWARE_ZLIB_WINDOW_BITS)
            )
        ):
            # Firmware only accepts zlib streams with a <=9-bit window (see
            # prepare_image), so the lazy deferred/partial-fallback compression
            # must use it too.
            compressed_data = compress_image_data(image_data, level=6, window_bits=FIRMWARE_ZLIB_WINDOW_BITS)

        within_compressed_limit = compressed_data is not None and (
            # The 50 KB cap protects the old buffered decompressor; streaming
            # decompression (bit 0x01) has no whole-blob buffer.
            streaming_decompression or len(compressed_data) < MAX_COMPRESSED_SIZE
        )
        if compress and supports_compression and compressed_data and within_compressed_limit:
            _LOGGER.info(
                "Using compressed upload protocol (size: %d bytes, zlib window: %d bits)",
                len(compressed_data),
                zlib_window_bits(compressed_data) or 0,
            )
            return await self.execute_upload(
                image_data,
                refresh_mode,
                use_compression=True,
                compressed_data=compressed_data,
                uncompressed_size=len(image_data),
                progress_callback=progress_callback,
                new_etag=new_etag,
            )

        # 4-gray ships the same two split planes (plane0 ++ plane1) over either
        # transport; the firmware streams them to PLANE_0/PLANE_1 whether they
        # arrive compressed or as raw 0x71 chunks, so no special-casing here.
        if compress and not supports_compression:
            _LOGGER.info("Device does not support compressed uploads, using uncompressed protocol")
        elif compress and compressed_data:
            _LOGGER.info("Compressed size exceeds %d bytes, using uncompressed protocol", MAX_COMPRESSED_SIZE)
        else:
            _LOGGER.info("Compression disabled or no compressed data, using uncompressed protocol")
        return await self.execute_upload(
            image_data,
            refresh_mode,
            use_compression=False,
            progress_callback=progress_callback,
            new_etag=new_etag,
        )

    @staticmethod
    def update_partial_state(state: PartialState, processed_image: Image.Image, etag: int | None) -> None:
        """After a full upload, refresh state to reflect what's now on the panel.

        ``etag`` is the etag committed to the device via END-with-etag, or None
        if the upload auto-completed (no etag was committed). When None, the
        device's displayed_etag was never set, so populating state.etag would
        make the next partial attempt fail ERR_ETAG_MISMATCH (or match a stale
        device etag and render against the wrong old plane). In that case
        invalidate the partial state so the next upload goes full.
        """
        if etag is None:
            state.invalidate()
            return
        palette_image = processed_image.convert("P") if processed_image.mode != "P" else processed_image
        state.commit(etag, palette_image.tobytes(), *processed_image.size)

    async def send_partial_chunks(
        self,
        remaining: bytes,
        stream_bytes: bytes,
        state: PartialState,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> str:
        """Send remaining 0x71 chunks and update upload progress.

        Returns "success", or "fallback_full" if the device NACKed a chunk
        before the refresh started (firmware aborts the partial on such a NACK,
        so a subsequent full upload is safe).
        """
        chunk_size = self._channel.direct_chunk_size()
        total_stream_bytes = len(stream_bytes)
        bytes_sent = total_stream_bytes - len(remaining)
        offset = 0
        while offset < len(remaining):
            chunk = remaining[offset : offset + chunk_size]
            # Write Without Response; the per-chunk ACK read below keeps flow control.
            await self._channel.write(build_direct_write_data_command(chunk, max_data_len=chunk_size), response=False)
            ack = await self._channel.read(self._timeouts.TIMEOUT_ACK)
            nack = parse_nack(ack)
            if nack is not None:
                opcode, err = nack
                _LOGGER.info(
                    "Partial upload rejected mid-stream (opcode=0x%02x err=0x%02x); falling back to full upload",
                    opcode,
                    err,
                )
                state.invalidate()
                return "fallback_full"
            validate_ack_response(ack, CommandCode.DIRECT_WRITE_DATA)
            offset += len(chunk)
            bytes_sent += len(chunk)
            if progress_callback is not None:
                progress_callback(bytes_sent, total_stream_bytes)
        return "success"

    async def maybe_upload_partial(
        self,
        processed_image: Image.Image,
        state: PartialState,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> str:
        """Try a partial upload using the 0x76 single-rectangle protocol."""
        # Resolve all partial-update preconditions in one pass (support checks,
        # state validation, diff computation, and region alignment).
        region = compute_partial_region(processed_image, state, self._target().config, self._color_scheme())
        if isinstance(region, str):
            return region

        display = region.display
        _LOGGER.debug(
            "Partial path diff: old_etag=0x%08x, image=%dx%d, rect=(%d,%d,%d,%d)",
            state.etag,
            region.width,
            region.height,
            region.rx,
            region.ry,
            region.rw,
            region.rh,
        )

        # Build logical stream from the changed rectangle only, then compress
        # when it reduces the transfer size.
        old_palette_image = region.palette_image.copy()
        old_palette_image.frombytes(region.old_palette)
        old_rect_bytes = encode_segment_wire(
            old_palette_image,
            region.rx,
            region.ry,
            region.rw,
            region.rh,
            region.color_scheme,
        )
        new_rect_bytes = encode_segment_wire(
            region.palette_image,
            region.rx,
            region.ry,
            region.rw,
            region.rh,
            region.color_scheme,
        )

        logical_stream = build_partial_logical_stream(old_rect_bytes, new_rect_bytes)
        # A partial stream rides inside the 0x76 initial bytes; firmware only
        # accepts a <= 9-bit zlib window, so always use a 9-bit window (a 15-bit
        # window would be NACKed with ERR_PARTIAL_STREAM by 9-bit firmware).
        compressed_stream = compress_image_data(logical_stream, level=6, window_bits=FIRMWARE_ZLIB_WINDOW_BITS)
        use_compression = self.supports_compression and len(compressed_stream) < len(logical_stream)
        stream_bytes = compressed_stream if use_compression else logical_stream

        flags = 0
        if use_compression:
            flags |= PARTIAL_FLAG_COMPRESSED

        _LOGGER.debug(
            "Partial stream: rect=(%d,%d,%d,%d), uncompressed=%d, wire=%d, compressed=%s",
            region.rx,
            region.ry,
            region.rw,
            region.rh,
            len(logical_stream),
            len(stream_bytes),
            use_compression,
        )

        new_etag = _generate_etag()
        _LOGGER.debug("Partial upload: old_etag=0x%08x new_etag=0x%08x", state.etag, new_etag)

        # Pipe-first: ride the sliding window when PIPE_WRITE is allowed and
        # pipe-partial has not been disabled this connection. partial_update_support
        # != NONE is already guaranteed (compute_partial_region gated on it). Falls
        # back to the legacy 0x76 flow below on any None result.
        if self.pipe_write_allowed(display) and self.pipe_partial_supported is not False:
            total_size = len(logical_stream)
            try:
                params = await self.negotiate_pipe_partial(use_compression, total_size, state.etag, region)
            except _PipePartialEtagMismatch:
                # Device already cleared its etag — a 0x76 retry would mismatch
                # again, so skip straight to a full upload.
                _LOGGER.info("pipe-partial etag mismatch (0x05); clearing state, falling back to full")
                state.invalidate()
                return "fallback_full"
            except _PipePartialRejected as exc:
                # 0x76 runs identical bpp/rect checks and would fail identically.
                _LOGGER.info("pipe-partial rejected (%s); clearing state, falling back to full", exc)
                state.invalidate()
                return "fallback_full"
            if params is not None:
                # Negotiation may have downgraded compressed→uncompressed (NACK 0x02).
                payload = compressed_stream if params.compressed else logical_stream
                try:
                    await self.run_pipe_upload(payload, params, RefreshMode.PARTIAL, progress_callback, new_etag)
                except RefreshTimeoutError:
                    # 0x74 after END_ACK: firmware already cleared its etag on
                    # the failed refresh; re-raise (parity with the 0x76 path).
                    # Note a read timeout on the post-DATA reads deliberately
                    # propagates too (not caught below): the END may already have
                    # committed on the device, so falling back to full and
                    # re-baselining state on an unknown outcome would be unsafe.
                    raise
                except (AuthenticationError, IntegrityCheckError):
                    # An auth/session failure or a decrypt-integrity rejection is NOT
                    # a clean, safe-to-retry protocol abort. Auth errors MUST surface
                    # so the caller (e.g. Home Assistant) can trigger reauth instead
                    # of silently retrying; an integrity failure signals an out-of-sync
                    # encrypted channel that a full upload over the same session would
                    # likely hit again. Masking either as a full-upload fallback would
                    # hide it, so re-raise. Only the genuine protocol NACKs below are
                    # safe to recover from with a full upload.
                    raise
                except ProtocolError as exc:
                    # Mid-stream NACK / MAX_RETX / END NACK before any refresh — the
                    # transfer aborted cleanly, so a full upload is safe.
                    _LOGGER.info("pipe-partial upload failed (%s); clearing state, falling back to full", exc)
                    state.invalidate()
                    return "fallback_full"
                state.commit(new_etag, region.new_palette, region.width, region.height)
                return "success"
            # params is None → fall through to the legacy 0x76 flow unchanged.

        # Start partial upload (0x76), stream remaining 0x71 chunks, and finish
        # with partial refresh.
        max_start = self._channel.start_payload_limit()
        start_pkt, remaining = build_direct_write_partial_start(
            old_etag=state.etag,
            new_etag=new_etag,
            flags=flags,
            x=region.rx,
            y=region.ry,
            width=region.rw,
            height=region.rh,
            stream_bytes=stream_bytes,
            max_start_payload=max_start,
        )
        await self._channel.write(start_pkt)
        try:
            response = await self._channel.read(self._timeouts.TIMEOUT_ACK)
            nack = parse_nack(response)
            if nack is not None:
                opcode, err = nack
                raise_for_upload_start_refusal(opcode, err)
                # No partial data has been applied yet, so any pre-refresh 0x76
                # NACK (etag mismatch, rect OOB/align, flags, unsupported, ...) is
                # safe to recover from by falling back to a full upload rather than
                # raising and losing the upload entirely.
                _LOGGER.info(
                    "Partial upload rejected at START (opcode=0x%02x err=0x%02x); falling back to full upload",
                    opcode,
                    err,
                )
                state.invalidate()
                return "fallback_full"
            validate_ack_response(response, CommandCode.DIRECT_WRITE_PARTIAL_START)
        except (OpenDisplayTimeoutError, InvalidResponseError):
            _LOGGER.info("Partial upload start was not acknowledged; falling back to full upload")
            return "fallback_full"

        if await self.send_partial_chunks(remaining, stream_bytes, state, progress_callback) == "fallback_full":
            return "fallback_full"

        await self._channel.write(build_direct_write_end_command(RefreshMode.PARTIAL.value))
        response = await self._channel.read(self._timeouts.TIMEOUT_ACK)
        validate_ack_response(response, CommandCode.DIRECT_WRITE_END)
        await self.await_refresh_complete()

        state.commit(new_etag, region.new_palette, region.width, region.height)
        return "success"

    async def execute_upload(
        self,
        image_data: bytes,
        refresh_mode: RefreshMode,
        use_compression: bool = False,
        compressed_data: bytes | None = None,
        uncompressed_size: int | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        new_etag: int | None = None,
    ) -> bool:
        """Execute image upload using compressed or uncompressed protocol.

        Args:
            image_data: Raw uncompressed image data (always needed for uncompressed)
            refresh_mode: Display refresh mode
            use_compression: True to use compressed protocol
            compressed_data: Compressed data (required if use_compression=True)
            uncompressed_size: Original size (required if use_compression=True)

        Returns:
            True if ``new_etag`` was committed via END-with-etag, False if the
            firmware auto-completed the upload (no etag committed).

        Raises:
            ProtocolError: If upload fails
        """
        # 0. Sliding-window (PIPE_WRITE) attempt (see _pipe_write_allowed). The
        # 0x0080 negotiation below remains authoritative for the transfer
        # parameters when the gate passes.
        if self.pipe_write_allowed(self._display_config):
            total_size = len(image_data)
            params = await self.negotiate_pipe(use_compression, total_size)
            self.pipe_probed = True
            if params is not None:
                self.pipe_supported = True
                # Negotiation may have downgraded compressed→uncompressed (NACK 0x02).
                if params.compressed and compressed_data is not None:
                    payload = compressed_data
                else:
                    payload = image_data
                    if params.compressed:
                        # Contract: use_compression implies compressed_data; guard anyway.
                        params = PipeParams(params.window, params.ack_every, params.max_frame, params.selective, False)
                return await self.run_pipe_upload(payload, params, refresh_mode, progress_callback, new_etag)
            self.pipe_supported = False
            _LOGGER.info("PIPE_WRITE unavailable on %s; using legacy direct-write flow", self._target().name)
            # Fall through to the untouched legacy flow below.

        # 1. Send START command (different for each protocol)
        if use_compression:
            if uncompressed_size is None or compressed_data is None:
                raise ValueError("uncompressed_size and compressed_data are required when use_compression=True")
            max_start = self._channel.start_payload_limit()
            start_cmd, remaining_compressed = build_direct_write_start_compressed(
                uncompressed_size, compressed_data, max_start_payload=max_start
            )
        else:
            start_cmd = build_direct_write_start_uncompressed()
            remaining_compressed = None

        await self._channel.write(start_cmd)

        # 2. Wait for START ACK — firmware initializes display hardware here, which can be slow
        response = await self._channel.read(self._timeouts.TIMEOUT_FIRST_CHUNK)
        self._raise_for_start_refusal(response)
        try:
            validate_ack_response(response, CommandCode.DIRECT_WRITE_START)
        except InvalidResponseError:
            # If we weren't using compression there's nothing to fall back to.
            if not use_compression:
                raise
            # Device rejected the compressed START. Fall back to the uncompressed
            # protocol and retry; the same image_data (for 4-gray, the two split
            # planes) streams fine uncompressed. Firmware may report the failure as
            # the legacy {0xFF, 0xFF} frame or the spec-conformant {0xFF, 0x70}
            # ({0xFF, <cmd low byte>}); both are recognized as the same signal.
            if is_compressed_failure_frame(response):
                _LOGGER.warning(
                    "Device signalled compressed-write failure (0x%04x); falling back to uncompressed",
                    int.from_bytes(response[:2], "big"),
                )
            else:
                _LOGGER.warning(
                    "Compressed START rejected by device (0x%04x); falling back to uncompressed",
                    int.from_bytes(response[:2], "big"),
                )
            use_compression = False
            start_cmd = build_direct_write_start_uncompressed()
            await self._channel.write(start_cmd)
            response = await self._channel.read(self._timeouts.TIMEOUT_FIRST_CHUNK)
            self._raise_for_start_refusal(response)
            validate_ack_response(response, CommandCode.DIRECT_WRITE_START)

        # 3. Send data chunks: whatever the compressed START could not carry, or
        # the whole uncompressed frame.
        chunk_data = remaining_compressed if use_compression else image_data
        auto_completed = False
        if chunk_data:
            auto_completed = await self.send_data_chunks(chunk_data, progress_callback)

        # 4. Send END (unless device auto-triggered refresh), then wait for 0x73
        if not auto_completed:
            end_cmd = (
                build_direct_write_end_with_etag(refresh_mode.value, new_etag)
                if new_etag is not None
                else build_direct_write_end_command(refresh_mode.value)
            )
            await self._channel.write(end_cmd)

            # END triggers the SPI write and/or refresh on device before the ACK is sent.
            # Both paths can block up to ~60s on slow displays (Spectra/ACeP).
            end_ack_timeout = (
                self._timeouts.TIMEOUT_COMPRESSED_END_ACK
                if use_compression
                else self._timeouts.TIMEOUT_UNCOMPRESSED_END_ACK
            )
            response = await self._channel.read(end_ack_timeout)
            validate_ack_response(response, CommandCode.DIRECT_WRITE_END)
        await self.await_refresh_complete()

        # The etag is only committed on the device when we send END-with-etag;
        # a firmware auto-END (auto_completed) never sets displayed_etag.
        return not auto_completed and new_etag is not None

    async def await_refresh_complete(self) -> None:
        """Wait for the panel refresh that follows a completed transfer (0x73).

        Raises:
            RefreshTimeoutError: If the device reports a refresh timeout (0x74)
            ProtocolError: If any other frame arrives instead
        """
        _LOGGER.debug("Display refresh started, waiting for completion...")
        response = await self._channel.read(self._timeouts.TIMEOUT_REFRESH)
        command, _ = check_response_type(response)
        if command == CommandCode.DIRECT_WRITE_REFRESH_TIMEOUT:
            raise RefreshTimeoutError("Display refresh timed out (device sent 0x74)")
        if command != CommandCode.DIRECT_WRITE_REFRESH_COMPLETE:
            raise ProtocolError(f"Unexpected response waiting for refresh: {command.name} (0x{command:04x})")
        _LOGGER.info("Display refresh complete")

    async def send_data_chunks(
        self,
        image_data: bytes,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> bool:
        """Send image data chunks, waiting for ACK after each.

        Returns:
            True if the device sent 0x72 in place of a 0x71 ACK, meaning it
            auto-triggered the refresh (uncompressed protocol, buffer full).
            Caller must NOT send an explicit END in this case.
            False on normal completion — caller should send END.

        Raises:
            ProtocolError: If device responds with an unexpected code
            OpenDisplayTimeoutError: If no response within the DATA-ACK budget

        Reads use TIMEOUT_DIRECT_WRITE_DATA_ACK rather than the general
        TIMEOUT_ACK: every 0x0071 ACK waits on a blocking panel write, so the 5s
        command-ack budget is never right here.
        """
        bytes_sent = 0
        chunks_sent = 0

        chunk_size = self._channel.direct_chunk_size()
        while bytes_sent < len(image_data):
            chunk_data = image_data[bytes_sent : bytes_sent + chunk_size]

            # Send the data chunk without waiting for the ATT write confirmation
            # (Write Without Response). Flow control is preserved by the per-chunk
            # application ACK read below, so only one write is ever in flight.
            await self._channel.write(
                build_direct_write_data_command(chunk_data, max_data_len=chunk_size), response=False
            )
            bytes_sent += len(chunk_data)
            chunks_sent += 1

            if progress_callback is not None:
                progress_callback(bytes_sent, len(image_data))

            response = await self._channel.read(self._timeouts.TIMEOUT_DIRECT_WRITE_DATA_ACK)
            command, _ = check_response_type(response)

            if command == CommandCode.DIRECT_WRITE_END:
                # Device auto-triggered refresh (buffer full) and sent 0x72
                # instead of 0x71. No explicit END should follow.
                _LOGGER.debug(
                    "Device auto-completed upload after %d bytes (%d chunks)",
                    bytes_sent,
                    chunks_sent,
                )
                return True

            if command != CommandCode.DIRECT_WRITE_DATA:
                raise ProtocolError(f"Unexpected response during upload: {command.name} (0x{command:04x})")

            if chunks_sent % 50 == 0 or bytes_sent >= len(image_data):
                _LOGGER.debug(
                    "Sent %d/%d bytes (%.1f%%)",
                    bytes_sent,
                    len(image_data),
                    bytes_sent / len(image_data) * 100,
                )

        _LOGGER.debug("All data chunks sent (%d chunks total)", chunks_sent)
        return False

    async def negotiate_pipe(
        self, compressed: bool, total_size: int, _retry_uncompressed: bool = True
    ) -> PipeParams | None:
        """Probe + negotiate a sliding-window transfer via 0x0080.

        Sends PIPE_WRITE_START and waits ``TIMEOUT_PIPE_START`` for the response.
        Attempts are config-gated (transmission_modes bit 0x10), so this is a plain
        command timeout rather than a discovery probe. Silence (stale config bit on
        pipe-less firmware) or an unrecoverable NACK returns
        None → the caller falls back to the legacy 0x70 flow. A NACK err 0x02
        (compression unsupported) on a compressed request retries 0x0080 once
        uncompressed before giving up.

        Returns:
            PipeParams (effective, post-min-rule) on success, else None.
        """
        req_frame = self._channel.transport.max_frame  # transport frame ceiling; our client_max_frame (BLE=244)
        # The 0x0080 is the single pre-stream write; _write re-authenticates here
        # (once, never again mid-stream).
        await self._channel.write(
            build_pipe_write_start_command(
                compressed, self._max_queue_size, self._blocks_per_ack, req_frame, total_size
            )
        )
        try:
            resp = await self._channel.read(TIMEOUT_PIPE_START)
        except OpenDisplayTimeoutError:
            _LOGGER.debug("No 0x0080 response within %.1fs; firmware lacks PIPE_WRITE", TIMEOUT_PIPE_START)
            return None

        try:
            ok, payload = parse_pipe_start_response(resp)
        except InvalidResponseError as err:
            _LOGGER.debug("Garbled 0x0080 response (%s); falling back to legacy", err)
            return None

        if not ok:
            err_code = cast(int, payload)
            raise_for_upload_start_refusal(CommandCode.PIPE_WRITE_START & 0xFF, err_code)
            if err_code == PIPE_START_NACK_COMPRESSION and compressed and _retry_uncompressed:
                _LOGGER.info("Device rejected compressed PIPE_WRITE (err 0x02); retrying uncompressed")
                return await self.negotiate_pipe(False, total_size, _retry_uncompressed=False)
            _LOGGER.info("PIPE_WRITE START NACK (err 0x%02x); falling back to legacy", err_code)
            return None

        return self.effective_pipe_params(cast("tuple[int, int, int, int, int]", payload), compressed, partial=False)

    def effective_pipe_params(
        self, ack: tuple[int, int, int, int, int], compressed: bool, *, partial: bool
    ) -> PipeParams:
        """Apply the min-rule (Part 1 §1.1) to a 0x0080 ACK — computed identically to firmware."""
        ver, dev_max_window, dev_max_ack_every, dev_max_frame, flags = ack
        w_eff = max(1, min(self._max_queue_size, dev_max_window, 32))
        n_eff = max(1, min(self._blocks_per_ack, dev_max_ack_every, w_eff))
        frame_eff = min(self._channel.transport.max_frame, dev_max_frame)
        selective = bool(flags & 0x01)
        _LOGGER.info(
            "%s negotiated: W=%d N=%d frame=%d selective=%s compressed=%s (dev max %d/%d/%d, ver %d)",
            "pipe-partial" if partial else "PIPE_WRITE",
            w_eff,
            n_eff,
            frame_eff,
            selective,
            compressed,
            dev_max_window,
            dev_max_ack_every,
            dev_max_frame,
            ver,
        )
        return PipeParams(w_eff, n_eff, frame_eff, selective, compressed, partial=partial)

    async def negotiate_pipe_partial(
        self,
        compressed: bool,
        total_size: int,
        old_etag: int,
        region: PartialRegion,
        _retry_uncompressed: bool = True,
    ) -> PipeParams | None:
        """Probe + negotiate a partial-region sliding-window transfer via 0x0080.

        Sends an extended PIPE_WRITE_START (flags bit1 + 12-byte geometry) and
        interprets the response per Part 1 §1.2. A partial 0x0080 is a valid pipe
        probe, so this also updates ``pipe_probed`` / ``pipe_supported``.

        Returns:
            PipeParams(partial=True) on an ACK confirming partial (flags bit1),
            or None when the caller should fall back to the legacy 0x76 flow
            (silence / garble / other NACK / partial-flag rejected / bit1 clear).

        Raises:
            _PipePartialEtagMismatch: NACK 0x05 — caller skips 0x76, goes full.
            _PipePartialRejected: NACK 0x06/0x07 — caller skips 0x76, goes full.
        """
        req_frame = self._channel.transport.max_frame  # transport frame ceiling; our client_max_frame (BLE=244)
        partial = PipePartialRequest(old_etag=old_etag, x=region.rx, y=region.ry, w=region.rw, h=region.rh)
        # The 0x0080 is the single pre-stream write; _write re-authenticates here
        # (once, never again mid-stream).
        await self._channel.write(
            build_pipe_write_start_command(
                compressed,
                self._max_queue_size,
                self._blocks_per_ack,
                req_frame,
                total_size,
                partial=partial,
            )
        )
        try:
            resp = await self._channel.read(TIMEOUT_PIPE_START)
        except OpenDisplayTimeoutError:
            _LOGGER.debug("No 0x0080 partial response within %.1fs; firmware lacks PIPE_WRITE", TIMEOUT_PIPE_START)
            self.pipe_probed = True
            self.pipe_supported = False
            return None

        try:
            ok, payload = parse_pipe_start_response(resp)
        except InvalidResponseError as err:
            _LOGGER.debug("Garbled 0x0080 partial response (%s); falling back to legacy", err)
            self.pipe_probed = True
            self.pipe_supported = False
            return None

        if not ok:
            err_code = cast(int, payload)
            raise_for_upload_start_refusal(CommandCode.PIPE_WRITE_START & 0xFF, err_code)
            # The device answered 0x0080, so pipe write itself is supported.
            self.pipe_probed = True
            self.pipe_supported = True
            if err_code == PIPE_START_NACK_COMPRESSION and compressed and _retry_uncompressed:
                _LOGGER.info("Device rejected compressed pipe-partial (err 0x02); retrying uncompressed still-partial")
                return await self.negotiate_pipe_partial(False, total_size, old_etag, region, _retry_uncompressed=False)
            if err_code == PIPE_START_NACK_COMPRESSION:
                # A second 0x02 (or 0x02 on an already-uncompressed request) means
                # the partial flag bit itself is unknown — older pipe-capable
                # firmware. Cache the negative so we never re-send a partial 0x0080
                # this connection.
                _LOGGER.info("Device rejected the pipe-partial flag (err 0x02); disabling pipe-partial this connection")
                self.pipe_partial_supported = False
                return None
            if err_code == PIPE_START_NACK_ETAG_MISMATCH:
                raise _PipePartialEtagMismatch("Device rejected pipe-partial START: displayed etag mismatch (0x05)")
            if err_code in (PIPE_START_NACK_PARTIAL_UNSUPPORTED, PIPE_START_NACK_RECT_INVALID):
                if err_code == PIPE_START_NACK_PARTIAL_UNSUPPORTED:
                    # bpp/driver can't do partial at all — never retry this connection.
                    self.pipe_partial_supported = False
                raise _PipePartialRejected(f"Device rejected pipe-partial START (err 0x{err_code:02x})")
            _LOGGER.info("pipe-partial START NACK (err 0x%02x); falling back to legacy", err_code)
            return None

        ack = cast("tuple[int, int, int, int, int]", payload)
        # A valid ACK is a valid pipe probe.
        self.pipe_probed = True
        self.pipe_supported = True
        if not ack[4] & PIPE_FLAG_PARTIAL:
            # Requested partial but the device did not confirm bit1 → older
            # pipe-capable firmware. Abandon the pipe attempt; the subsequent 0x76
            # START resets the orphaned firmware session (Part 1 §1.2).
            _LOGGER.info("Device ACKed 0x0080 without the partial bit; pipe-partial unsupported this connection")
            self.pipe_partial_supported = False
            return None
        self.pipe_partial_supported = True
        return self.effective_pipe_params(ack, compressed, partial=True)

    async def run_pipe_upload(
        self,
        payload: bytes,
        params: PipeParams,
        refresh_mode: RefreshMode,
        progress_callback: Callable[[int, int], None] | None,
        new_etag: int | None,
    ) -> bool:
        """Split, stream, END, and await refresh for a sliding-window transfer.

        Returns True if ``new_etag`` was committed via END-with-etag, False if the
        firmware auto-completed the upload (no etag committed).
        """
        size = self._channel.pipe_data_size(params.max_frame)
        if size < 1:
            raise ProtocolError(f"Negotiated pipe frame {params.max_frame} too small for a data byte")
        # Always keep at least one frame so the receiver's total check + END
        # handshake run even for an empty payload (mirrors legacy START/END).
        chunks = [payload[i : i + size] for i in range(0, len(payload), size)] or [b""]
        if params.partial and new_etag is None:
            # An etag-less partial END would make firmware commit displayed_etag=0
            # on a successful refresh, silently desyncing the client PartialState.
            raise ProtocolError("PIPE_WRITE partial transfer requires a new_etag for the END commit")
        chunk_timeout = (
            self._timeouts.TIMEOUT_PIPE_DATA_COMPRESSED
            if params.compressed
            else self._timeouts.TIMEOUT_PIPE_DATA_UNCOMPRESSED
        )

        auto_completed = await self.send_pipe_chunks(chunks, params, chunk_timeout, progress_callback)
        # Uncompressed full-frame transfers ALWAYS auto-complete (firmware
        # resets pipe state and sends an unsolicited END_ACK once total_size is
        # reached), so the explicit END path below is skipped there. Compressed
        # and partial transfers use the explicit END (partial firmware never
        # auto-completes — Part 1 §1.5). Sending an END after auto-complete
        # would be NACKed and desync etag accounting.
        if params.partial and auto_completed:
            # Partial firmware must never auto-complete; an unsolicited END_ACK
            # here is a contract violation (would have refreshed FULL, not
            # REFRESH_PARTIAL, with no committed etag).
            raise ProtocolError("PIPE_WRITE partial transfer auto-completed unexpectedly (unsolicited END_ACK)")
        if not auto_completed:
            await self.await_pipe_end_ack(refresh_mode, new_etag, params)

        await self.await_refresh_complete()
        return not auto_completed and new_etag is not None

    async def send_pipe_chunks(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
        self,
        chunks: list[bytes],
        params: PipeParams,
        chunk_timeout: float,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> bool:
        """QUIC-style selective-repeat sender for PIPE_WRITE data frames.

        Keeps up to ``params.window`` frames in flight (span-based tokens), refunds
        tokens on ACK, retransmits only missing chunks (selective repeat) — or
        rewinds when the receiver does not buffer out-of-order (bit0 clear).

        Completion contract is set by ``explicit_end = params.compressed or
        params.partial``:
        - explicit-END path (compressed OR partial): returns once every chunk is
          acked; the caller sends the explicit 0x0082 END. Compressed firmware can
          only verify totals at zlib flush; partial firmware never auto-completes
          (Part 1 §1.5) because only the 0x0082 carries the refresh mode + new_etag.
          A tail of < N_eff unacked frames never earns a cadence ACK, so the tail
          wait uses TIMEOUT_PIPE_TAIL_FLUSH and a dup-probe instead of stalling
          on chunk_timeout.
        - auto-complete path (uncompressed full-frame only): the client sends
          exactly total_size bytes, so firmware ALWAYS auto-completes — flush-ACK,
          then an unsolicited {0x00,0x82}, resetting its pipe state before any
          explicit END could arrive. This sender therefore keeps reading past the
          flush-ACK until that END_ACK and returns True (mirroring legacy
          send_data_chunks' 0x72-in-place-of-0x71 handling); the caller must NOT
          send END.

        Returns:
            True if the device auto-completed (unsolicited END_ACK — only the
            uncompressed full-frame path), False on normal explicit-END completion
            (compressed or partial; caller sends END).

        Raises:
            ProtocolError: On a fatal NACK, MAX_RETX/MAX_PTO exhaustion, a missing
                auto-complete END_ACK, or an unexpected frame.
        """
        n = len(chunks)
        window = params.window
        # Partial transfers never auto-complete (Part 1 §1.5), so they use the
        # same explicit-END completion contract compressed transfers use today.
        explicit_end = params.compressed or params.partial
        # Flat 3*W is fine for small transfers but starves multi-thousand-chunk
        # uploads (E1004 ~960KB): BLE write-without-response drops are normal and
        # legitimately need more repairs. Match the web client's scaled budget.
        max_retx = max(3 * window, math.ceil(n * PIPE_MAX_RETX_FRACTION))
        acked: set[int] = set()
        window_base = 0  # lowest unacked
        next_to_send = 0
        pending_retx: dict[int, int] = {}  # missing idx → ACKs seen since last (re)transmit
        retx_count = 0
        pto_count = 0
        stall_acks = 0  # consecutive ACKs that neither advance window_base nor expose a hole
        bytes_total = sum(len(c) for c in chunks)
        bytes_sent_hw = 0

        async def _send(idx: int) -> None:
            await self._channel.write_stream_frame(
                build_pipe_write_data_command(idx % 256, chunks[idx]), response=False
            )

        while True:
            # 1. Send new chunks while span-tokens are available.
            while next_to_send < n and (next_to_send - window_base) < window:
                await _send(next_to_send)
                bytes_sent_hw += len(chunks[next_to_send])
                if progress_callback is not None:
                    progress_callback(min(bytes_sent_hw, bytes_total), bytes_total)
                next_to_send += 1

            # Explicit-END (compressed or partial): all acked → done, caller sends
            # the explicit END. Uncompressed full-frame: keep reading for the
            # unsolicited auto-complete END_ACK.
            if window_base >= n and explicit_end:
                break

            # 2. Block for an ACK (credit exhausted or tail pending). Compressed
            # tail (< N_eff unacked frames, no holes) will never earn a cadence
            # ACK, so wait only briefly before dup-probing — never send END with
            # unacked chunks (a genuinely lost tail chunk must be repaired by
            # retransmit, not surface as a fatal END NACK at zlib flush).
            tail_flush = (
                explicit_end
                and next_to_send >= n
                and 0 < n - window_base < params.ack_every
                and not (acked and max(acked) >= window_base)  # no known holes
            )
            read_timeout = self._timeouts.TIMEOUT_PIPE_TAIL_FLUSH if tail_flush else chunk_timeout
            try:
                resp = await self._channel.read(read_timeout)
            except OpenDisplayTimeoutError:
                if window_base >= n:
                    # Uncompressed with everything acked: firmware owes the
                    # unsolicited END_ACK; there is nothing left to probe.
                    raise ProtocolError("PIPE_WRITE aborted: auto-complete END_ACK never arrived") from None
                pto_count += 1
                if pto_count >= MAX_PTO:
                    raise ProtocolError(f"PIPE_WRITE aborted: no ACK progress after {MAX_PTO} PTO probes") from None
                # PTO / tail-flush dup-probe: resend the oldest unacked chunk
                # (fresh nonce); a duplicate elicits an immediate ACK.
                await _send(window_base)
                retx_count += 1
                if retx_count > max_retx:
                    raise ProtocolError(f"PIPE_WRITE aborted: MAX_RETX ({max_retx}) exceeded (PTO)") from None
                continue

            kind = classify_pipe_frame(resp)
            if kind == PIPE_FRAME_NACK:
                err, _hs, _mask = parse_pipe_data_nack(resp)
                raise ProtocolError(f"PIPE_WRITE data NACK err=0x{err:02x} (fatal)")
            if kind == PIPE_FRAME_END_ACK:
                # Unsolicited auto-complete: the receiver confirms it holds the full
                # image (accepted total reached total_size), so it is authoritative
                # and terminal — even if a final local ACK was lost. Mirrors legacy
                # 0x72 auto-finish; the client sends no explicit END.
                if window_base < n:
                    _LOGGER.debug("PIPE auto-complete END_ACK with %d/%d chunks locally acked", len(acked), n)
                return True
            if kind != PIPE_FRAME_ACK:
                raise ProtocolError(f"Unexpected pipe frame during send: {resp[:8].hex()}")

            # 3. Process the ACK — refund tokens over the contiguous acked prefix.
            pto_count = 0
            highest_seen, ack_mask = parse_pipe_data_ack(resp)
            acked |= unpack_ack_ranges(highest_seen, ack_mask, window_base)
            prev_base = window_base
            while window_base in acked:
                pending_retx.pop(window_base, None)
                window_base += 1
            # Progress bound: an ACK stream that never advances the cumulative
            # point and never exposes a hole would otherwise reset pto_count each
            # pass and loop forever (only reachable with pathological firmware —
            # real firmware ACKs in response to frames, not unsolicited).
            if window_base > prev_base:
                stall_acks = 0
            if window_base >= n:
                # Loop top: explicit-end (compressed or partial) breaks (caller
                # sends END); uncompressed full-frame keeps reading for the
                # unsolicited auto-complete END_ACK.
                stall_acks += 1
                if stall_acks > max_retx:
                    raise ProtocolError(f"PIPE_WRITE aborted: {stall_acks} ACKs without auto-complete END_ACK")
                continue

            # 4. Loss handling — holes below the highest received are definite losses.
            highest_recv = max(acked) if acked else window_base - 1
            missing = [i for i in range(window_base, min(highest_recv, next_to_send)) if i not in acked]
            if not missing:
                stall_acks += 1
                if stall_acks > max_retx:
                    raise ProtocolError(f"PIPE_WRITE aborted: {stall_acks} consecutive ACKs without progress")
                continue

            if params.selective:
                # Selective repeat, oldest first. pending_retx counts ACKs seen
                # since a chunk was last (re)sent — wait PIPE_RETX_ACK_SPACING
                # ACKs before repeating so an in-flight repair isn't double-counted.
                for m in missing:  # oldest first
                    seen = pending_retx.get(m)
                    if seen is None:
                        do_retx = True  # newly detected hole
                    else:
                        seen += 1
                        pending_retx[m] = seen
                        do_retx = seen >= PIPE_RETX_ACK_SPACING
                    if do_retx:
                        await _send(m)
                        pending_retx[m] = 0
                        retx_count += 1
                        if retx_count > max_retx:
                            raise ProtocolError(f"PIPE_WRITE aborted: MAX_RETX ({max_retx}) exceeded")
            else:
                # bit0 clear: rewind-style recovery (resend from window_base).
                next_to_send = window_base
                pending_retx.clear()
                retx_count += 1
                if retx_count > max_retx:
                    raise ProtocolError(f"PIPE_WRITE aborted: MAX_RETX ({max_retx}) exceeded (rewind)")

        return False

    async def await_pipe_end_ack(
        self,
        refresh_mode: RefreshMode,
        new_etag: int | None,
        params: PipeParams,
    ) -> None:
        """Send 0x0082 END and wait for the END_ACK (explicit-END transfers).

        Reached by compressed transfers and ALL partial transfers (partial never
        auto-completes — the END alone carries the refresh selector + new_etag).
        Uncompressed full-frame transfers never reach here: firmware always
        auto-completes them (see send_pipe_chunks), and an END sent after that
        reset would be NACKed. Called only after ``send_pipe_chunks`` has seen
        every chunk acked, so the transfer is complete; a trailing tail-flush
        PIPE_ACK may precede the END_ACK in the queue and is skipped. An
        END_NACK or data NACK aborts (the caller's existing retry-from-scratch
        recovers).
        """
        end_cmd = build_pipe_write_end_command(refresh_mode.value, new_etag)
        await self._channel.write_stream_frame(end_cmd, response=True)

        end_timeout = (
            self._timeouts.TIMEOUT_COMPRESSED_END_ACK
            if params.compressed
            else self._timeouts.TIMEOUT_UNCOMPRESSED_END_ACK
        )
        stray_acks = 0
        while True:
            resp = await self._channel.read(end_timeout)
            kind = classify_pipe_frame(resp)
            if kind == PIPE_FRAME_END_ACK:
                return
            if kind == PIPE_FRAME_END_NACK:
                raise ProtocolError("PIPE_WRITE END NACK (byte-total mismatch or incomplete transfer)")
            if kind == PIPE_FRAME_NACK:
                err, _hs, _mask = parse_pipe_data_nack(resp)
                raise ProtocolError(f"PIPE_WRITE data NACK during END: err=0x{err:02x}")
            if kind == PIPE_FRAME_ACK:
                # Tail-flush ACK preceding END_ACK — ignore, keep reading. Bounded:
                # firmware sends at most one flush ACK per END; an unending ACK
                # stream would otherwise renew end_timeout forever.
                stray_acks += 1
                if stray_acks > 32:
                    raise ProtocolError(f"PIPE_WRITE aborted: {stray_acks} ACK frames while awaiting END_ACK")
                continue
            raise ProtocolError(f"Unexpected frame awaiting END_ACK: {resp[:8].hex()}")
