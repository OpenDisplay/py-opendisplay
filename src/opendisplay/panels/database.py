"""The panel drive database: which drive packets make a panel draw on firmware 3.0.

A port of the database half of the website's ``httpdocs/js/od-v3.js`` (Web ``v3/website``
``b7df514``). Each function names the JS function it ports, so the two stay comparable. The data
is Firmware_Unified's generated drive presets, vendored gzip-compressed (see ``SOURCE.md``).
"""

from __future__ import annotations

import gzip
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from importlib.resources import files
from pathlib import Path
from typing import Any, Final

from ..models.ident import FirmwareTarget
from ..models.panel_drive import (
    PACKET_TYPE_DISPLAY_CONTROLLER,
    PACKET_TYPE_DISPLAY_MODE,
    PACKET_TYPE_DISPLAY_PANEL,
    LengthPrefixedPacket,
)

_SCHEMA: Final = "opendisplay.drive_presets"
_MAIN_FILE: Final = "drive_presets.json.gz"
_CLASS_FILES: Final = {"nrf52811": "drive_presets_nrf52811.json.gz"}
_USABLE_STATUS: Final = frozenset({"glass", "equivalent"})
NRF52811_PANEL_MIN: Final = 0xE000
NRF52811_PANEL_MAX: Final = 0xE1FF


class TargetClass(Enum):
    """Which interpreter limits apply (od-v3.js ``TARGET_CLASS_LIMITS`` keys)."""

    DEFAULT = "default"  # ESP32, nRF52840, nRF54
    BG22 = "bg22"
    NRF52811 = "nrf52811"


@dataclass(frozen=True)
class _Limits:
    max_steps: int
    window: bool
    sleep_max: int


# od-v3.js TARGET_CLASS_LIMITS: the client's own caps, not the file's, so the check does not
# trust the data it checks.
_LIMITS: Final = {
    TargetClass.DEFAULT: _Limits(max_steps=128, window=True, sleep_max=20),
    TargetClass.BG22: _Limits(max_steps=128, window=False, sleep_max=20),
    TargetClass.NRF52811: _Limits(max_steps=128, window=False, sleep_max=20),
}
_CLASS_LABEL: Final = {
    TargetClass.DEFAULT: "ESP32 / nRF",
    TargetClass.BG22: "EFR32BG22",
    TargetClass.NRF52811: "nRF52811",
}
RESTRICTIVE_CLASS: Final = TargetClass.BG22  # BG22 data runs on every target; default data does not run on BG22


def target_class_for(target: FirmwareTarget | int | None) -> TargetClass:
    """The drive class of a firmware target (od-v3.js ``targetClassFromIdent`` + ``normalizeClass``).

    An unknown or missing target gets the restrictive class.
    """
    if target in (FirmwareTarget.ESP32, FirmwareTarget.NRF54, FirmwareTarget.NRF52840):
        return TargetClass.DEFAULT
    if target == FirmwareTarget.EFR32BG22:
        return TargetClass.BG22
    if target == FirmwareTarget.NRF52811:
        return TargetClass.NRF52811
    return RESTRICTIVE_CLASS


def needs_drive_data(panel_ic_type: int) -> bool:
    """Whether a panel needs drive packets on 3.x (od-v3.js ``panelNeedsDriveData``).

    bb_epaper SPI panels (ids below 1000) do, and so do the Firmware_NRF 3.0 panels
    0xE000-0xE1FF. IT8951 / parallel panels (3000-3005) are driven by their own path.
    """
    return 0 < panel_ic_type < 1000 or NRF52811_PANEL_MIN <= panel_ic_type <= NRF52811_PANEL_MAX


# ── drive sequence checks ────────────────────────────────────────────────────

_ESCAPE_ARGS: Final = {1: 0, 3: 0, 4: 0, 5: 2, 6: 2, 7: 2, 8: 1, 9: 2, 0x0A: 3, 0x0B: 4, 0x0D: 3}
_ESC_SEQUENCE: Final = 0x02
_ESC_WINDOW: Final = 0x0C
_ESC_FILL: Final = 0x0D
_MODE_HEAD: Final = 37  # 0xAE body head; seq_len at bytes 35-36
_PANEL_HEAD: Final = 13  # 0xB1 body head; sleep_seq_len at byte 12


@dataclass(frozen=True)
class _Walk:
    ok: bool
    size: int
    steps: int
    window: bool
    error: str


def _walk_sequence(seq: bytes) -> _Walk:  # pylint: disable=too-many-return-statements
    """Walk one drive sequence (od-v3.js ``walkSequence``; the od_epd.h grammar).

    ``steps`` counts END, like the firmware's cap. An unknown escape is an error: the caller
    must not write what it cannot vouch for.
    """
    i = steps = 0
    window = False
    while i < len(seq):
        n = seq[i]
        if n:
            if i + 1 + n > len(seq):
                return _Walk(False, len(seq), steps, window, "step overruns the sequence")
            steps += 1
            i += 1 + n
            continue
        if i + 1 >= len(seq):
            return _Walk(False, len(seq), steps, window, "truncated escape")
        escape = seq[i + 1]
        if escape == 0:
            return _Walk(True, len(seq), steps + 1, window, "")
        if escape in (_ESC_SEQUENCE, _ESC_WINDOW):
            if i + 2 >= len(seq):
                return _Walk(False, len(seq), steps, window, "truncated escape")
            length = 3 + 2 * seq[i + 2]
        elif escape in _ESCAPE_ARGS:
            length = 2 + _ESCAPE_ARGS[escape]
        else:
            return _Walk(False, len(seq), steps, window, f"unknown escape 0x{escape:x}")
        if i + length > len(seq):
            return _Walk(False, len(seq), steps, window, "truncated escape")
        if escape in (_ESC_WINDOW, _ESC_FILL):
            window = True
        steps += 1
        i += length
    return _Walk(False, len(seq), steps, window, "sequence has no END")


def check_drive_packets_fit(
    packets: Iterable[LengthPrefixedPacket],
    target_class: TargetClass,
    *,
    max_steps: int | None = None,
    window: bool | None = None,
) -> list[str]:
    """Problems that keep ``packets`` from running on ``target_class``; empty when they fit.

    A port of od-v3.js ``checkDrivePacketsFit``: each 0xAE mode sequence (steps, WINDOW/FILL)
    and each 0xB1 sleep sequence (bytes). ``max_steps`` and ``window`` override the class table,
    for a device whose identity states its own limits.
    """
    base = _LIMITS[target_class]
    limits = _Limits(
        max_steps=base.max_steps if max_steps is None else max_steps,
        window=base.window if window is None else window,
        sleep_max=base.sleep_max,
    )
    problems: list[str] = []
    for packet in packets:
        body = packet.body
        cap: int | None
        if packet.packet_type == PACKET_TYPE_DISPLAY_MODE:
            if len(body) < _MODE_HEAD:
                problems.append("0xAE body shorter than its 37-byte head")
                continue
            seq_len = body[35] | (body[36] << 8)
            if _MODE_HEAD + seq_len > len(body):
                problems.append(f"0xAE mode {body[1]}: seq_len {seq_len} does not match the body")
                continue
            # Bytes after the sequence are the mode tail (schema 1.4), read by length and ignored here.
            seq, what, cap = body[_MODE_HEAD : _MODE_HEAD + seq_len], f"mode {body[1]}", None  # streamed: no byte cap
        elif packet.packet_type == PACKET_TYPE_DISPLAY_PANEL:
            if len(body) < _PANEL_HEAD:
                problems.append("0xB1 body shorter than its 13-byte head")
                continue
            if _PANEL_HEAD + body[12] > len(body):
                problems.append(f"0xB1: sleep_seq_len {body[12]} does not match the body")
                continue
            if not body[12]:
                continue
            # Bytes after the sleep sequence are the panel tail (schema 1.4).
            seq, what, cap = body[_PANEL_HEAD : _PANEL_HEAD + body[12]], "sleep sequence", limits.sleep_max
        else:
            continue
        walk = _walk_sequence(seq)
        if not walk.ok:
            problems.append(f"{what}: {walk.error}")
            continue
        if cap is not None and walk.size > cap:
            problems.append(f"{what} needs a {walk.size} B sequence, limit {cap} B")
        if packet.packet_type == PACKET_TYPE_DISPLAY_MODE and walk.steps > limits.max_steps:
            problems.append(f"{what} needs {walk.steps} steps, limit {limits.max_steps}")
        if walk.window and not limits.window:
            problems.append(f"{what} uses WINDOW/FILL, which this target does not implement")
    return problems


# ── entries ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PanelEntry:  # pylint: disable=too-many-instance-attributes
    """One panel's drive data for one target class.

    Attributes:
        verified: ``"glass"`` (seen drawing on a real panel), ``"equivalent"`` (replays
            byte-identically to the 2.x driver, not seen drawing) or ``"untested"``.
        packets: The drive packets to write; empty when the entry is not usable.
        blocked_reason: Why the entry cannot be used on this class; ``None`` when it can.
        legacy_panel_ic_type: The 2.x panel id this entry replaces, when the file names one.
        legacy_model_id: The 2.x driver model the entry was derived from, when it was.
    """

    panel_ic_type: int
    name: str
    width: int | None
    height: int | None
    color_scheme: int | None
    status: str
    verified: str
    target_class: TargetClass
    packets: tuple[LengthPrefixedPacket, ...]
    drive_bytes: int
    requires_escapes: tuple[str, ...]
    blocked_reason: str | None
    legacy_panel_ic_type: int | None = None
    legacy_model_id: int | None = None

    @property
    def usable(self) -> bool:
        """Whether the packets may be written to a device of this class."""
        return self.blocked_reason is None


@dataclass(frozen=True)
class NativeController:
    """A board with a built-in controller driver: one 0xB3 packet, no 0xAE-0xB1."""

    panel_ic_type: int
    name: str
    board: str
    controller_class: str
    packets: tuple[LengthPrefixedPacket, ...]


def _num(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        return None


def _normalize_verified(value: Any) -> str:
    """od-v3.js ``normalizeVerified``: "glass 2026-11-13: ..." counts as glass."""
    text = "" if value is None else str(value).lower()
    if text in ("glass", "verified", "true") or re.match(r"glass\b", text):
        return "glass"
    if text in ("equivalent", "derived"):
        return "equivalent"
    return "untested"


def _packets(raw: Iterable[Mapping[str, Any]], only: int | None = None) -> tuple[LengthPrefixedPacket, ...]:
    """od-v3.js ``normalizePacketList``: drive presets are length-prefixed (0x80+) packets only."""
    out = []
    for item in raw:
        packet_type = _num(item.get("id", item.get("pid")))
        body_hex = item.get("body_hex", item.get("raw"))
        if packet_type is None or body_hex is None or not 0x80 <= packet_type <= 0xFF:
            continue
        if only is not None and packet_type != only:
            continue
        body = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", str(body_hex)))
        out.append(LengthPrefixedPacket(number=_num(item.get("number")) or 0, packet_type=packet_type, body=body))
    return tuple(out)


def _blocked_reason(src: Mapping[str, Any], status: str, target_class: TargetClass | None) -> str:
    """The reason text of od-v3.js ``driveView`` for an entry that is not usable."""
    needs = ", ".join(src.get("blocked_on") or [])
    if status == "blocked" and target_class is not None:
        return (
            f"Not available on this target ({_CLASS_LABEL[target_class]}): "
            + (f"the drive data needs {needs}" if needs else "no drive data for this target")
            + "."
        )
    if status == "blocked":
        return (
            "Not yet available for firmware 3.x: the drive sequence needs a firmware change "
            f"({needs or 'unspecified'})."
        )
    if status == "differs":
        return "Not yet available for firmware 3.x: the converted drive data does not match the 2.x driver."
    if status in ("corrected", "extracted"):
        return (
            "Not yet available for firmware 3.x: the drive data exists but has not been checked on a "
            f"real panel or against a compiled driver ({status})."
        )
    return f"Not yet available for firmware 3.x: no drive data ({status})."


def _drive_view(meta: Mapping[str, Any], src: Mapping[str, Any], target_class: TargetClass | None) -> PanelEntry:
    """od-v3.js ``driveView``: the drive fields of one view (top level or ``targets.<class>``)."""
    packets = _packets(src.get("packets") or [])
    status = str(src.get("status") or ("equivalent" if packets else "blocked"))
    usable = status in _USABLE_STATUS and bool(packets)
    reason: str | None = None
    if usable and target_class is not None:
        # Second line of defence: the generator says these fit; check it anyway.
        problems = check_drive_packets_fit(packets, target_class)
        if problems:
            usable, reason = False, "Not available on this target: " + "; ".join(problems) + "."
    if not usable and reason is None:
        reason = _blocked_reason(src, status, target_class)
    drive_bytes = _num(src.get("drive_bytes"))
    return PanelEntry(
        panel_ic_type=meta["panel_ic_type"],
        name=meta["name"],
        width=meta["width"],
        height=meta["height"],
        color_scheme=meta["color_scheme"],
        status=status,
        verified=_normalize_verified(src.get("verified", status)),
        target_class=target_class or TargetClass.DEFAULT,
        packets=packets if usable else (),
        drive_bytes=drive_bytes if drive_bytes is not None else sum(len(p.to_bytes()) for p in packets),
        requires_escapes=tuple(src.get("requires_escapes") or ()),
        blocked_reason=None if usable else reason,
        legacy_panel_ic_type=meta["legacy_panel_ic_type"],
        legacy_model_id=meta["legacy_model_id"],
    )


@dataclass(frozen=True)
class _Panel:
    top: PanelEntry
    classes: Mapping[TargetClass, PanelEntry]


def _class_of(name: str) -> TargetClass | None:
    return next((c for c in TargetClass if c.value == name), None)


def _panel_from_generated(key: str, entry: Mapping[str, Any], panels: Mapping[str, Any]) -> _Panel | None:
    """od-v3.js ``entryFromGenerated``: one panel, with a view per target class."""
    legacy = entry.get("legacy") or {}
    panel_ic_type = _num(entry.get("panel_ic_type", legacy.get("panel_ic_type", key)))
    if panel_ic_type is None:
        return None
    alias = entry.get("alias_of")
    src = panels[alias] if alias and alias in panels else entry
    meta = {
        "panel_ic_type": panel_ic_type,
        "name": str(entry.get("name") or ""),
        "width": _num(entry.get("width")),
        "height": _num(entry.get("height")),
        "color_scheme": _num(entry.get("color_scheme")),
        "legacy_panel_ic_type": _num(legacy.get("panel_ic_type")),
        "legacy_model_id": _num(legacy.get("model_id")),
    }
    classes: dict[TargetClass, PanelEntry] = {}
    for name, view in (src.get("targets") or {}).items():
        target_class = _class_of(name)
        if target_class is not None and view:
            classes[target_class] = _drive_view(meta, view, target_class)
    top = _drive_view(meta, src, TargetClass.DEFAULT if classes else None)
    return _Panel(top=top, classes=classes)


def _check_schema(doc: Mapping[str, Any], label: str) -> None:
    if doc.get("schema") not in (None, _SCHEMA):
        raise ValueError(f"drive presets ({label}): unexpected schema {doc.get('schema')}")
    if doc.get("schema_version") not in (None, 1):
        raise ValueError(f"drive presets ({label}): unsupported schema_version {doc.get('schema_version')}")


class PanelDatabase:
    """Panel id -> drive data, as od-v3.js ``buildDriveDb`` builds it."""

    def __init__(
        self,
        generated: Mapping[str, Any],
        class_files: Mapping[TargetClass, Mapping[str, Any]] | None = None,
    ) -> None:
        """Build the database from the parsed JSON documents.

        Args:
            generated: ``drive_presets.json``.
            class_files: Class-only files (``drive_presets_nrf52811.json``): their panels exist on
                that class only. ``generated`` wins for an id both have.
        """
        _check_schema(generated, "main")
        self.schema_minor: int = int(generated.get("schema_minor", 0))
        self.caps: Mapping[str, int] = dict(generated.get("caps") or {})
        self._panels: dict[int, _Panel] = {}
        for target_class, doc in (class_files or {}).items():
            _check_schema(doc, target_class.value)
            panels = doc.get("panels") or {}
            for key, raw in panels.items():
                # A class file names a panel by its key when the entry does not (buildDriveDb).
                raw = {**raw, "panel_ic_type": raw.get("panel_ic_type", key)}
                panel = _panel_from_generated(key, raw, panels)
                if panel is None or panel.top.panel_ic_type in self._panels:
                    continue
                meta_top = panel.top
                view = _drive_view(
                    {
                        "panel_ic_type": meta_top.panel_ic_type,
                        "name": meta_top.name,
                        "width": meta_top.width,
                        "height": meta_top.height,
                        "color_scheme": meta_top.color_scheme,
                        "legacy_panel_ic_type": meta_top.legacy_panel_ic_type,
                        "legacy_model_id": meta_top.legacy_model_id,
                    },
                    raw,
                    target_class,
                )
                blocked = PanelEntry(
                    **{
                        **meta_top.__dict__,
                        "packets": (),
                        "blocked_reason": f"Only on {_CLASS_LABEL[target_class]} firmware 3.x.",
                    }
                )
                self._panels[meta_top.panel_ic_type] = _Panel(top=blocked, classes={target_class: view})
        panels = generated.get("panels") or {}
        for key, raw in panels.items():
            panel = _panel_from_generated(key, raw, panels)
            if panel is not None:
                self._panels[panel.top.panel_ic_type] = panel
        self._natives: dict[int, NativeController] = {}
        for key, raw in (generated.get("native_controllers") or {}).items():
            panel_ic_type = _num(raw.get("panel_ic_type", key)) if raw else None
            packets = _packets((raw or {}).get("packets") or [], only=PACKET_TYPE_DISPLAY_CONTROLLER)
            if panel_ic_type is None or not packets:
                continue
            self._natives[panel_ic_type] = NativeController(
                panel_ic_type=panel_ic_type,
                name=str(raw.get("name") or ""),
                board=str(raw.get("board") or ""),
                controller_class=str(raw.get("controller_class") or ""),
                packets=packets,
            )

    @classmethod
    def load(cls, path: Path | None = None) -> PanelDatabase:
        """Load the vendored database, or a ``drive_presets.json`` (plain or ``.gz``) at ``path``.

        With ``path`` only that file is loaded, without the nRF52811 class file.
        """
        if path is not None:
            data = path.read_bytes()
            return cls(json.loads(gzip.decompress(data) if path.suffix == ".gz" else data))
        package = files(__package__)
        main = json.loads(gzip.decompress(package.joinpath(_MAIN_FILE).read_bytes()))
        class_files = {
            TargetClass(name): json.loads(gzip.decompress(package.joinpath(file).read_bytes()))
            for name, file in _CLASS_FILES.items()
        }
        return cls(main, class_files)

    def panel_ids(self) -> Iterator[int]:
        """Every panel id the database has an entry for, in ascending order."""
        return iter(sorted(self._panels))

    def entry_for(self, panel_ic_type: int, target_class: TargetClass) -> PanelEntry | None:
        """The drive entry of a panel for a target class (od-v3.js ``driveEntryFor``).

        Per-class data when the file has it. Without it, the class-independent packets, and
        only if they fit the class. A panel blocked on the class comes back not usable, with
        ``blocked_reason`` saying what it needs. ``None`` when the panel is unknown.
        """
        panel = self._panels.get(panel_ic_type)
        if panel is None:
            return None
        view = panel.classes.get(target_class)
        if view is not None:
            return view
        top = PanelEntry(**{**panel.top.__dict__, "target_class": target_class})
        if top.usable:
            problems = check_drive_packets_fit(top.packets, target_class)
            if problems:
                return PanelEntry(
                    **{
                        **top.__dict__,
                        "packets": (),
                        "blocked_reason": "Not available on this target: " + "; ".join(problems) + ".",
                    }
                )
        return top

    def nrf52811_panel_for(self, legacy_panel_ic_type: int) -> int | None:
        """The Firmware_NRF 3.0 panel id for a 2.x Firmware_NRF id 1000-2999 (od-v3.js ``nrf52811PanelFor``).

        The nRF52811 entry whose legacy id matches; entries derived from the 2.x driver (with a
        ``model_id``) win over others, then the lowest id. ``None`` when there is none.
        """
        if not 1000 <= legacy_panel_ic_type <= 2999:
            return None
        best: tuple[int, int] | None = None
        for panel_ic_type, panel in self._panels.items():
            view = panel.classes.get(TargetClass.NRF52811)
            if view is None or view.legacy_panel_ic_type != legacy_panel_ic_type:
                continue
            rank = (0 if view.legacy_model_id is not None else 1, panel_ic_type)
            if best is None or rank < best:
                best = rank
        return None if best is None else best[1]

    def native_controller_for(self, panel_ic_type: int) -> NativeController | None:
        """The 0xB3 entry of a native-controller panel id (od-v3.js ``nativeControllerFor``)."""
        return self._natives.get(panel_ic_type)

    def needs_drive_data(self, panel_ic_type: int) -> bool:
        """See the module function ``needs_drive_data``."""
        return needs_drive_data(panel_ic_type)
