"""Show Music Assistant album art on an OpenDisplay panel via Sendspin.

:class:`SendspinArtworkBridge` runs an artwork-only Sendspin client for one
display. It finds the Sendspin server (Music Assistant) over mDNS or uses an
explicit URL, asks for square album art sized to the panel's shorter side, and
hands each new cover to an async callback as a PIL image. What happens with the
image (uploading, queueing for a sleeping device) is up to the caller.

Requires the optional ``sendspin`` extra (``pip install py-opendisplay[sendspin]``).
The extra pins the aiosendspin major that Music Assistant ships, because Sendspin
9.x and 10.x are not wire compatible.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from PIL import Image

from .models.config import GlobalConfig

_MISSING_SENDSPIN_MSG = (
    "Sendspin support requires the 'aiosendspin' package. Install it with: pip install py-opendisplay[sendspin]"
)

try:
    from aiosendspin.client import SendspinClient
    from aiosendspin.models.artwork import ArtworkChannel, ClientHelloArtworkSupport
    from aiosendspin.models.core import DeviceInfo
    from aiosendspin.models.types import ArtworkSource, PictureFormat, Roles
    from aiosendspin.noise.keys import Identity
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
except ImportError as err:
    raise ImportError(_MISSING_SENDSPIN_MSG) from err

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from aiosendspin.noise.trust_store import ClientPairingStore

_LOGGER = logging.getLogger(__name__)

_SERVER_SERVICE_TYPE = "_sendspin-server._tcp.local."
_DEFAULT_PATH = "/sendspin"
_DISCOVERY_TIMEOUT_S = 10.0
_RESOLVE_TIMEOUT_MS = 3000
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0

ArtworkHandler = Callable[[Image.Image | None], Awaitable[None]]
"""Called with each new cover, or ``None`` when the server clears the artwork."""


class SendspinClientLike(Protocol):
    """The part of :class:`aiosendspin.client.SendspinClient` the bridge uses."""

    def add_artwork_listener(self, callback: Callable[[int, bytes], None]) -> Callable[[], None]:
        """Register a callback for artwork frames."""

    def add_disconnect_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback for when the connection closes."""

    def add_group_update_listener(self, callback: Callable[[Any], None]) -> Callable[[], None]:
        """Register a callback for group/update messages."""

    async def send_available(self, *, available: bool) -> None:
        """Send a client/state reporting availability."""

    async def connect(self, url: str) -> None:
        """Dial the server and return once admitted."""

    async def disconnect(self) -> None:
        """Say goodbye and close the connection."""


def artwork_edge(config: GlobalConfig) -> int:
    """Return the edge length in pixels of the square artwork to request.

    The cover is requested as a square sized to the panel's shorter side, so it
    is never cropped and looks the same however the panel is rotated. The
    upload's ``FitMode.CONTAIN`` then centres it with white bars.

    Raises:
        ValueError: The device config has no display.
    """
    if not config.displays:
        raise ValueError("Device config has no display")
    display = config.displays[0]
    return min(display.pixel_width, display.pixel_height)


def load_or_create_identity(path: Path) -> Identity:
    """Load the Sendspin identity stored at *path*, creating it on first use.

    The identity is the client's long-term key pair. Its public half is the
    ``client_id``, so keeping it stable keeps the display the same player in
    Music Assistant. Blocking file I/O: call it off the event loop.
    """
    if path.exists():
        return Identity.from_private_bytes(path.read_bytes())
    identity = Identity.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(identity.private_bytes)
    return identity


def _server_url(host: str, port: int, path: str | None) -> str:
    """Build the WebSocket URL for a server advertised over mDNS."""
    if ":" in host:
        host = f"[{host}]"
    path = path or _DEFAULT_PATH
    if not path.startswith("/"):
        path = "/" + path
    return f"ws://{host}:{port}{path}"


def _pick_address(addresses: list[str]) -> str:
    """Prefer an IPv4 address; link-local IPv6 needs a scope the URL cannot carry."""
    return next((a for a in addresses if ":" not in a), addresses[0])


async def discover_server_url(zeroconf: AsyncZeroconf, timeout: float = _DISCOVERY_TIMEOUT_S) -> str | None:
    """Return the URL of the first Sendspin server found over mDNS, or ``None``."""
    found: asyncio.Queue[str] = asyncio.Queue()

    def _on_change(
        zeroconf: object,  # noqa: ARG001  # pylint: disable=unused-argument
        service_type: str,  # noqa: ARG001  # pylint: disable=unused-argument
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        # zeroconf fires handlers with keyword arguments, so these parameter
        # names are part of the contract and must not be renamed.
        if state_change is ServiceStateChange.Added:
            found.put_nowait(name)

    browser = AsyncServiceBrowser(zeroconf.zeroconf, _SERVER_SERVICE_TYPE, handlers=[_on_change])
    try:
        async with asyncio.timeout(timeout):
            while True:
                name = await found.get()
                info = AsyncServiceInfo(_SERVER_SERVICE_TYPE, name)
                if not await info.async_request(zeroconf.zeroconf, _RESOLVE_TIMEOUT_MS):
                    continue
                addresses = info.parsed_addresses()
                if not addresses or info.port is None:
                    continue
                raw_path = info.properties.get(b"path")
                path = raw_path.decode("utf-8", "replace") if raw_path else None
                return _server_url(_pick_address(addresses), info.port, path)
    except TimeoutError:
        return None
    finally:
        await browser.async_cancel()


def _decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


class _ArtworkSink:
    """Turns raw artwork frames into handler calls.

    Frames are handled one at a time. A frame that arrives while the handler is
    busy replaces any frame still waiting, so a slow upload never builds a
    backlog. An image identical to the last one handled is skipped (Music
    Assistant resends the current cover whenever the display rejoins a group),
    and a clear does not reset that check. The hash is only recorded once the
    handler succeeds, so a failed upload is retried when the cover comes again.
    """

    def __init__(self, handler: ArtworkHandler, last_hash: str | None = None) -> None:
        self._handler = handler
        self.last_hash = last_hash
        self._pending: bytes | None = None
        self._task: asyncio.Task[None] | None = None

    def feed(self, data: bytes) -> None:
        """Queue a frame (empty bytes means clear). Must run on the event loop."""
        self._pending = data
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._drain())

    async def join(self) -> None:
        """Wait until every queued frame has been handled."""
        while self._task is not None and not self._task.done():
            await asyncio.shield(self._task)

    async def close(self) -> None:
        """Drop any waiting frame and stop handling."""
        self._pending = None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _drain(self) -> None:
        while self._pending is not None:
            data, self._pending = self._pending, None
            await self._deliver(data)

    async def _deliver(self, data: bytes) -> None:
        if not data:
            try:
                await self._handler(None)
            except Exception:
                _LOGGER.exception("Artwork handler failed on clear")
            return
        digest = hashlib.sha256(data).hexdigest()
        if digest == self.last_hash:
            _LOGGER.debug("Skipping artwork identical to the last one")
            return
        try:
            image = await asyncio.to_thread(_decode, data)
        except Exception:
            _LOGGER.warning("Received artwork that could not be decoded (%d bytes)", len(data))
            return
        try:
            await self._handler(image)
        except Exception:
            _LOGGER.exception("Artwork handler failed")
            return
        self.last_hash = digest


class SendspinArtworkBridge:  # pylint: disable=too-many-instance-attributes
    """Keeps an artwork-only Sendspin client connected for one display.

    ``start()`` returns at once; connecting, reconnecting with backoff and
    rediscovering the server all happen in a background task, and failures are
    logged rather than raised. ``stop()`` disconnects and may be called twice.

    Args:
        config: The display's device config, used to size the artwork.
        name: Player name shown in Music Assistant.
        identity: Long-term Sendspin identity, see :func:`load_or_create_identity`.
        pairing_store: Where pairing state is persisted.
        on_artwork: Called with each new cover, or ``None`` on a clear.
        url: Server URL. When omitted the server is discovered over mDNS.
        zeroconf: Shared zeroconf instance for discovery. One is created (and
            closed on ``stop()``) when omitted.
        session: aiohttp session for the WebSocket. Never closed by the bridge.
        unpaired_access: Let a server that has not paired with this client
            connect. Artwork is all this client can receive, so the default is on.
        last_hash: Hash of the cover already on the panel, to skip resending it.
        client_factory: Builds the Sendspin client; for tests.
        sleep: Backoff sleep; for tests.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        config: GlobalConfig,
        *,
        name: str,
        identity: Identity,
        pairing_store: ClientPairingStore,
        on_artwork: ArtworkHandler,
        url: str | None = None,
        zeroconf: AsyncZeroconf | None = None,
        session: ClientSession | None = None,
        unpaired_access: bool = True,
        last_hash: str | None = None,
        client_factory: Callable[[], SendspinClientLike] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        edge = artwork_edge(config)
        self._name = name
        self._identity = identity
        self._pairing_store = pairing_store
        self._url = url
        self._zeroconf = zeroconf
        self._owns_zeroconf = False
        self._session = session
        self._unpaired_access = unpaired_access
        self._client_factory = client_factory or self._default_client_factory
        self._sleep = sleep
        self._artwork_support = ClientHelloArtworkSupport(
            channels=[ArtworkChannel(ArtworkSource.ALBUM, PictureFormat.PNG, edge, edge)]
        )
        self._sink = _ArtworkSink(on_artwork, last_hash)
        self._client: SendspinClientLike | None = None
        self._task: asyncio.Task[None] | None = None
        self._state_tasks: set[asyncio.Task[None]] = set()

    @property
    def client_id(self) -> str:
        """The Sendspin ``client_id``, derived from the identity."""
        return self._identity.peer_id

    @property
    def last_hash(self) -> str | None:
        """Hash of the last cover handed to ``on_artwork`` successfully."""
        return self._sink.last_hash

    async def start(self) -> None:
        """Apply the unpaired-access setting and start connecting in the background."""
        if self._task is not None:
            return
        pairing_config = await self._pairing_store.get_pairing_config()
        if pairing_config.unpaired_access_enabled != self._unpaired_access:
            await self._pairing_store.store_pairing_config(
                replace(pairing_config, unpaired_access_enabled=self._unpaired_access)
            )
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        """Disconnect and stop reconnecting. Safe to call more than once."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                _LOGGER.debug("Error while disconnecting from Sendspin server", exc_info=True)
        await self._sink.close()
        if self._owns_zeroconf and self._zeroconf is not None:
            await self._zeroconf.async_close()
            self._zeroconf = None
            self._owns_zeroconf = False

    def _default_client_factory(self) -> SendspinClientLike:
        return SendspinClient(
            self._identity,
            self._name,
            # metadata@v1 makes Music Assistant register a display rather than a
            # speaker: an artwork-only client falls through to its audio player.
            [Roles.ARTWORK, Roles.METADATA],
            pairing_store=self._pairing_store,
            device_info=DeviceInfo(product_name="OpenDisplay", manufacturer="OpenDisplay"),
            artwork_support=self._artwork_support,
            session=self._session,
        )

    async def _run(self) -> None:
        delay = BACKOFF_MIN_S
        while True:
            if await self._session_once():
                delay = BACKOFF_MIN_S
            await self._sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)

    def _report_state(self, client: SendspinClientLike) -> None:
        """Send client/state after the server (re)activates us.

        The server holds artwork back until it has the client's initial
        client/state once the artwork role is active. aiosendspin 9.x only sends
        that state when the player or source role is activated, so a display that
        is grouped after it connected would never get artwork (and one grouped
        before connecting only gets it after the server's 5 s timeout). A
        group/update follows every activation, so answer each one with our state.
        """

        async def _send() -> None:
            try:
                await client.send_available(available=True)
            except Exception:
                _LOGGER.debug("Could not send client/state", exc_info=True)

        task = asyncio.get_running_loop().create_task(_send())
        self._state_tasks.add(task)
        task.add_done_callback(self._state_tasks.discard)

    async def _resolve_url(self) -> str | None:
        if self._url is not None:
            return self._url
        if self._zeroconf is None:
            self._zeroconf = AsyncZeroconf()
            self._owns_zeroconf = True
        return await discover_server_url(self._zeroconf)

    async def _session_once(self) -> bool:
        """Connect once and wait for the connection to end. True if it connected."""
        url = await self._resolve_url()
        if url is None:
            _LOGGER.debug("No Sendspin server found")
            return False
        client = self._client_factory()
        gone = asyncio.Event()
        client.add_disconnect_listener(gone.set)
        client.add_artwork_listener(lambda _channel, data: self._sink.feed(data))
        client.add_group_update_listener(lambda _payload: self._report_state(client))
        self._client = client
        try:
            await client.connect(url)
        except Exception as err:
            _LOGGER.warning("Could not connect to Sendspin server %s: %s", url, err)
            self._client = None
            with contextlib.suppress(Exception):
                await client.disconnect()
            return False
        _LOGGER.info("Connected to Sendspin server %s as %s", url, self.client_id)
        await gone.wait()
        self._client = None
        _LOGGER.info("Disconnected from Sendspin server %s", url)
        return True
