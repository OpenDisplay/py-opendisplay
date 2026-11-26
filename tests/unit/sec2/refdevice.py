"""A SEC2 reference device for tests: enough of firmware 3.0's security layer to drive the client.

Plays the device side of SECURITY_V2.md § 5.1, § 8, § 9 and § 11 over the Transport interface
(the parts the website's tests/helpers/sec2-refdevice.js covers that these tests need). Not a
model of the firmware: no rate limiter, no timers, one connection.
"""

from __future__ import annotations

import secrets
from collections import deque
from typing import Any

from opendisplay.protocol.config_serializer import serialize_config
from opendisplay.sec2 import kdf, wire
from opendisplay.sec2.primitives import ccm_encrypt, ecdh, generate_key_pair
from opendisplay.sec2.session import Sec2Session, open_h2d, seal_d2h

FW_3_0_2 = b"\x00\x43\x03\x00\x28" + b"0" * 40 + b"\x02"
IDENT_ESP32 = bytes.fromhex("004604010029a00f800003")  # no display records, flags 0x29


class RefDevice:  # pylint: disable=too-many-instance-attributes
    """One SEC2 device on one connection."""

    max_frame = 244
    supports_write_without_response = True

    def __init__(
        self,
        *,
        uid: bytes = bytes.fromhex("0000441bf6c96440"),
        k_dev: bytes | None = None,
        claim_mode: bool = False,
        key_legacy: bool = False,
        config: Any = None,
        name: str = "OD4064C9",
        dflags: int = 0x01,
    ) -> None:
        self.uid, self.k_dev, self.claim_mode, self.key_legacy = uid, k_dev, claim_mode, key_legacy
        self.config, self.device_name, self.dflags = config, name, dflags
        self.written: list[bytes] = []
        self.session: Sec2Session | None = None
        self.tx_ctr = 0
        self._parts: list[bytes] = []
        self._queue: deque[bytes] = deque()
        self._pending: dict[str, Any] = {}
        self._rotation: bytes | None = None
        self.shown_code: str | None = None
        self._connected = False
        #: Refuse this many HELLO / BEGIN with ``refusal`` first (a display pass holds the device).
        self.busy_replies = 0
        self.refusal = wire.SecErr.BUSY
        self.handshake_starts = 0
        #: Answer these opcodes inside a session with the given refusal frame instead of OK.
        self.refusals: dict[int, bytes] = {}
        #: Seal those refusals (Firmware_Unified 6ad0ee0 on); False sends them unsealed as before.
        self.seal_refusals = True

    # ── Transport ────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        self.session = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    def drain_notifications(self) -> int:
        return 0

    async def clear_cache(self) -> bool:
        return False

    async def read_response(self, timeout: float = 5.0) -> bytes:
        if not self._queue:
            from opendisplay.exceptions import OpenDisplayTimeoutError

            raise OpenDisplayTimeoutError("RefDevice: nothing to send")
        return self._queue.popleft()

    async def write_command(self, data: bytes, response: bool = True, drain_stale: bool = True) -> None:
        self.written.append(data)
        cmd = int.from_bytes(data[:2], "big")
        if cmd in (wire.CMD_SEC_CLAIM, wire.CMD_SEC_AUTH):
            self._fragment(data)
        elif wire.is_plaintext_opcode(cmd):
            self._plaintext(cmd)
        elif self.session is not None:
            self._sealed(cmd, open_h2d(self.session.k_h2d, data))
        elif self.k_dev is not None:
            self._queue.append(bytes([0x00, data[1], 0xFE]))  # SESSION_REQUIRED
        else:
            self._reply_plain(cmd, data[2:])

    # ── answers ──────────────────────────────────────────────────────────────

    @property
    def claimed(self) -> bool:
        return self.k_dev is not None

    def status_frame(self) -> bytes:
        state = wire.SecState.CLAIMED if self.claimed else wire.SecState.UNCLAIMED
        flags = (0x01 if self.claim_mode else 0) | (0x02 if self.session else 0) | (0x10 if self.key_legacy else 0)
        dflags = self.dflags | (wire.DFLAG_KEY_LEGACY if self.key_legacy else 0)
        return (
            bytes([0x00, 0x54, 0x02, state, flags])
            + (300 if self.claim_mode else 0).to_bytes(2, "little")
            + self.uid
            + (3600).to_bytes(2, "little")
            + bytes([dflags, 3 if self.claim_mode else 0])
        )

    def _plaintext(self, cmd: int) -> None:
        if cmd == 0x0043:
            self._queue.append(FW_3_0_2)
        elif cmd == 0x0054:
            self._queue.append(self.status_frame())
        elif cmd == 0x0046:
            self._queue.append(IDENT_ESP32)
        elif cmd == 0x0050:  # the 2.x handshake: 3.0 refuses it cleanly (§ 3.1)
            self._queue.append(bytes([0x00, 0x50, wire.AUTH_STATUS_V2_REQUIRED if self.claimed else 0x03]))

    def _answer(self, cmd: int, payload: bytes) -> bytes:
        if cmd == 0x0040:
            if self.config is None:
                return bytes.fromhex("ff400000")
            blob = serialize_config(self.config)
            return b"\x00\x40\x00\x00" + len(blob).to_bytes(2, "little") + blob
        return bytes([0x00, cmd & 0xFF]) + b"OK" + payload

    def _reply_plain(self, cmd: int, payload: bytes) -> None:
        self._queue.append(self._answer(cmd, payload))

    def _seal(self, frame: bytes) -> bytes:
        assert self.session is not None
        self.tx_ctr += 1
        return seal_d2h(self.session.k_d2h, self.tx_ctr, frame)

    def _sealed(self, cmd: int, payload: bytes) -> None:
        if cmd in self.refusals:
            frame = self.refusals[cmd]
            self._queue.append(self._seal(frame) if self.seal_refusals else frame)
            return
        if cmd != wire.CMD_SEC_KEY:
            self._queue.append(self._seal(self._answer(cmd, payload)))
            return
        sub = payload[0]
        if sub == wire.KeySub.ROTATE:
            self._rotation = secrets.token_bytes(16)
            self._queue.append(self._seal(b"\x00\x57\x01" + self._rotation))
        elif sub == wire.KeySub.ROTATE_CONFIRM:
            assert self._rotation is not None and self.session is not None
            k_conf_new = kdf.key_hierarchy(self._rotation, self.uid).k_conf
            if payload[1:17] != kdf.kdf(k_conf_new, "OD2/rotate-c", self.uid + self.session.sid):
                self._queue.append(self._seal(bytes([0xFF, 0x57, 0x02, wire.SecErr.AUTH_FAILED])))
                return
            self._queue.append(self._seal(b"\x00\x57\x02"))
            self.k_dev, self.key_legacy, self.session = self._rotation, False, None
        elif sub == wire.KeySub.UNCLAIM and payload[1:3] == wire.UNCLAIM_GUARD:
            self._queue.append(self._seal(b"\x00\x57\x03"))
            self.k_dev, self.session = None, None

    # ── handshakes ───────────────────────────────────────────────────────────

    def _fragment(self, data: bytes) -> None:
        self._parts.append(data[4:])
        if not data[3] & wire.FRAG_LAST:
            return
        body, self._parts = b"".join(self._parts), []
        cmd_lo, sub = data[1], data[2]
        if sub == 1:  # HELLO / BEGIN
            self.handshake_starts += 1
            if self.busy_replies:
                self.busy_replies -= 1
                self._error(cmd_lo, sub, self.refusal)
                return
        if cmd_lo == wire.CMD_SEC_AUTH & 0xFF:
            self._auth(sub, body)
        else:
            self._claim(sub, body)

    def _error(self, cmd_lo: int, sub: int, err: wire.SecErr) -> None:
        self._queue.append(bytes([0xFF, cmd_lo, sub, int(err)]))

    def _auth(self, sub: int, body: bytes) -> None:
        if not self.claimed:
            self._error(0x56, sub, wire.SecErr.NOT_CLAIMED)
            return
        assert self.k_dev is not None
        if sub == wire.AuthSub.HELLO:
            self.session = None
            qc, nc = body[2:66], body[66:82]
            dd, qd = generate_key_pair()
            nd = secrets.token_bytes(16)
            dflags = self.dflags | (wire.DFLAG_KEY_LEGACY if self.key_legacy else 0)
            ta = kdf.auth_transcript(dflags, self.uid, qc, qd, nc, nd)
            prk = kdf.extract(kdf.key_hierarchy(self.k_dev, self.uid).k_ext, ecdh(dd, qc))
            self._pending = {"ta": ta, "prk": prk, "frag_max": body[1]}
            self._queue.extend(wire.fragment(0x56, 1, bytes([dflags]) + qd + nd, body[1]))
            return
        ta, prk = self._pending["ta"], self._pending["prk"]
        if body != kdf.kdf(prk, "OD2/auth-c", ta):
            self._error(0x56, sub, wire.SecErr.AUTH_FAILED)
            return
        keys = kdf.session_keys(prk, ta)
        self.session, self.tx_ctr = Sec2Session(k_h2d=keys.k_h2d, k_d2h=keys.k_d2h, sid=keys.sid), 0
        tag_d = kdf.kdf(prk, "OD2/auth-d", ta)
        self._queue.extend(wire.fragment(0x56, 2, tag_d + (3600).to_bytes(2, "little"), self._pending["frag_max"]))

    def _claim(self, sub: int, body: bytes) -> None:
        if self.claimed and sub != wire.ClaimSub.EXIT:
            self._error(0x55, sub, wire.SecErr.ALREADY_CLAIMED)
            return
        if not self.claim_mode:
            self._error(0x55, sub, wire.SecErr.NOT_IN_CLAIM_MODE)
            return
        p = self._pending
        if sub == wire.ClaimSub.BEGIN:
            dd, qd = generate_key_pair()
            p.clear()
            p.update(
                frag_max=body[1], qc=body[2:66], dd=dd, qd=qd, nd=secrets.token_bytes(16), k_new=secrets.token_bytes(16)
            )
            cd = kdf.commitment(p["nd"], self.uid, qd, p["qc"])
            self._queue.extend(wire.fragment(0x55, 1, bytes([self.dflags]) + qd + cd, p["frag_max"]))
        elif sub == wire.ClaimSub.NONCE:
            nc = body
            p["tc"] = tc = kdf.claim_transcript(self.dflags, self.uid, p["qc"], p["qd"], nc, p["nd"])
            p["prk"] = prk = kdf.extract(bytes(16), ecdh(p["dd"], p["qc"]))
            wrapped = ccm_encrypt(kdf.kdf(prk, "OD2/wrap", tc), kdf.WRAP_NONCE, kdf.wrap_aad(self.uid), p["k_new"])
            self.shown_code = kdf.code_from_raw(kdf.code_raw(nc, self.uid, p["qc"], p["qd"], p["nd"]))
            self._queue.extend(wire.fragment(0x55, 2, p["nd"] + wrapped, p["frag_max"]))
        elif sub == wire.ClaimSub.CONFIRM:
            k_conf = kdf.key_hierarchy(p["k_new"], self.uid).k_conf
            if body != kdf.kdf(k_conf, "OD2/claim-c", p["tc"]):
                self._error(0x55, sub, wire.SecErr.AUTH_FAILED)
                return
            self.k_dev = p["k_new"]
            keys = kdf.session_keys(p["prk"], p["tc"])
            self.session, self.tx_ctr = Sec2Session(k_h2d=keys.k_h2d, k_d2h=keys.k_d2h, sid=keys.sid), 0
            tag_d = kdf.kdf(k_conf, "OD2/claim-d", p["tc"])
            self._queue.extend(wire.fragment(0x55, 3, tag_d + (3600).to_bytes(2, "little"), p["frag_max"]))
        else:  # ABORT, EXIT
            if sub == wire.ClaimSub.EXIT:
                self.claim_mode = False
            p.clear()
            self._queue.extend(wire.fragment(0x55, sub, b"", 20))
