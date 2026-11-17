"""Where SEC2 device keys live: the caller's ``KeyStore`` (SECURITY_V2.md § 8.8).

Per device (by advertised name, e.g. ``"OD4064C9"``; the uid is recorded once known) a store
keeps one **confirmed** key and a list of **candidates**. The rules, mirrored from the website's
od-keystore.js:

- a key is removed only by :meth:`KeyStore.forget` (an explicit user action, or an authenticated
  unclaim) or when another key has completed an authenticated handshake (:meth:`promote`);
  a refusal, silence or any unauthenticated reply never removes a key;
- :meth:`stage` stores a key that is about to become the device's only key (claim, rotation) and
  proves it is stored before the caller lets the device take it;
- :meth:`keys_to_try` returns at most three keys, the confirmed one first.

Two stores ship: :class:`MemoryKeyStore`, and :class:`JsonFileKeyStore` for the CLI
(``~/.config/opendisplay/keys.json``, mode 0600). Home Assistant implements the protocol on its
config entry.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import time
from pathlib import Path
from typing import Any, Final, Protocol

from .errors import Sec2StorageError

KEY_LEN: Final = 16
MAX_TRY: Final = 3
FORMAT: Final = 1
DEFAULT_PATH: Final = Path("~/.config/opendisplay/keys.json")


class KeyStore(Protocol):
    """The storage a caller provides for SEC2 device keys."""

    def keys_to_try(self, name: str) -> list[bytes]:
        """At most three keys for ``name``: the confirmed one first, then candidates newest first."""

    def uid(self, name: str) -> bytes | None:
        """The uid stored with the confirmed key (every later handshake uses it, § 4.3)."""

    def add_candidate(self, name: str, key: bytes, source: str) -> None:
        """Remember a key that might work (typed by the user, carried over from 2.x)."""

    def stage(self, name: str, key: bytes, kind: str) -> None:
        """Store a key the device may now hold, and prove it is stored.

        Raises:
            Sec2StorageError: The key is not durably stored; do not confirm the claim or rotation.
        """

    def promote(self, name: str, key: bytes, *, uid: bytes | None = None) -> None:
        """``key`` opened an authenticated session: it is confirmed, and every other key goes."""

    def forget(self, name: str) -> None:
        """Remove every key for ``name`` (explicit user action, or an authenticated unclaim)."""


def _check_key(key: bytes) -> str:
    if len(key) != KEY_LEN:
        raise ValueError(f"A device key is {KEY_LEN} bytes, got {len(key)}")
    return key.hex()


class _DocumentKeyStore:
    """The retention rules over one JSON-shaped document; subclasses load and save it."""

    def _load(self) -> dict[str, Any] | None:
        """The stored document, ``None`` when there is none yet."""
        raise NotImplementedError

    def _save(self, document: dict[str, Any]) -> None:
        raise NotImplementedError

    def _document(self) -> dict[str, Any]:
        document = self._load()
        return document if document is not None else {"format": FORMAT, "devices": {}}

    def _record(self, name: str) -> dict[str, Any] | None:
        record = self._document()["devices"].get(name)
        return copy.deepcopy(record) if record is not None else None

    def _update(self, name: str, change: Any) -> None:
        if not name:
            raise ValueError("device name required")
        document = self._document()
        record = document["devices"].get(name) or {"name": name, "uid": None, "confirmed": None, "candidates": []}
        change(record)
        document["devices"][name] = record
        self._save(document)

    def keys_to_try(self, name: str) -> list[bytes]:
        """See :class:`KeyStore`."""
        record = self._record(name)
        if record is None:
            return []
        hexes = ([record["confirmed"]] if record["confirmed"] else []) + [
            c["key"] for c in sorted(record["candidates"], key=lambda c: c["added"], reverse=True)
        ]
        unique = list(dict.fromkeys(hexes))
        return [bytes.fromhex(k) for k in unique[:MAX_TRY]]

    def uid(self, name: str) -> bytes | None:
        """See :class:`KeyStore`."""
        record = self._record(name)
        return bytes.fromhex(record["uid"]) if record and record.get("uid") else None

    def add_candidate(self, name: str, key: bytes, source: str) -> None:
        """See :class:`KeyStore`."""
        hex_key = _check_key(key)

        def change(record: dict[str, Any]) -> None:
            if record["confirmed"] != hex_key and all(c["key"] != hex_key for c in record["candidates"]):
                record["candidates"].append({"key": hex_key, "source": source, "added": time.time_ns()})

        self._update(name, change)

    def stage(self, name: str, key: bytes, kind: str) -> None:
        """See :class:`KeyStore`."""
        hex_key = _check_key(key)

        def change(record: dict[str, Any]) -> None:
            record["candidates"] = [c for c in record["candidates"] if c["key"] != hex_key]
            record["candidates"].append({"key": hex_key, "source": f"pending-{kind}", "added": time.time_ns()})

        self._update(name, change)
        record = self._record(name)
        if record is None or all(c["key"] != hex_key for c in record["candidates"]):
            raise Sec2StorageError("the staged key did not read back")

    def promote(self, name: str, key: bytes, *, uid: bytes | None = None) -> None:
        """See :class:`KeyStore`."""
        hex_key = _check_key(key)

        def change(record: dict[str, Any]) -> None:
            record["confirmed"] = hex_key
            record["candidates"] = []
            if uid is not None:
                record["uid"] = uid.hex()

        self._update(name, change)

    def forget(self, name: str) -> None:
        """See :class:`KeyStore`."""
        document = self._document()
        if document["devices"].pop(name, None) is not None:
            self._save(document)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({len(self._document()['devices'])} devices)"


class MemoryKeyStore(_DocumentKeyStore):
    """Keys in memory only: for tests and callers that persist keys themselves."""

    def __init__(self) -> None:
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data)

    def _save(self, document: dict[str, Any]) -> None:
        self._data = copy.deepcopy(document)


class JsonFileKeyStore(_DocumentKeyStore):
    """Keys in a JSON file, mode 0600, replaced atomically and fsynced on every change."""

    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        self.path = Path(path).expanduser()

    def _load(self) -> dict[str, Any] | None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as err:
            raise Sec2StorageError(f"the key store {self.path} is unreadable: {err}") from err
        try:
            document = json.loads(text)
        except ValueError as err:
            raise Sec2StorageError(f"the key store {self.path} is unreadable; refusing to overwrite it") from err
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            raise Sec2StorageError(f"the key store {self.path} has an unknown format; refusing to overwrite it")
        return document

    def _save(self, document: dict[str, Any]) -> None:
        text = json.dumps(document, indent=2, sort_keys=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.path)
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as err:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise Sec2StorageError(f"the key store {self.path} could not be written: {err}") from err
        if self.path.read_text(encoding="utf-8") != text:
            raise Sec2StorageError(f"the key store {self.path} did not read back")
