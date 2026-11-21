"""What a setup run can pause on: one request at a time, answered with ``SetupRun.answer()``.

Each request is a frozen dataclass and saves as plain JSON (``to_dict`` / ``from_dict``), so a run
paused on one survives a Home Assistant restart. The answer each takes is in its docstring.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, ClassVar


@dataclass(frozen=True)
class NeedKeyChoice:
    """Claim the display (answer ``True``) or use it without a key (``False``)."""

    kind: ClassVar[str] = "key_choice"


@dataclass(frozen=True)
class NeedGesture:
    """The user must act on the device; answer ``None`` once done.

    ``gesture`` is ``"claim_mode"``: power-cycle it twice quickly to enter claim mode.
    """

    gesture: str = "claim_mode"
    kind: ClassVar[str] = "gesture"


@dataclass(frozen=True)
class NeedClaimCode:
    """The six-digit code the display shows; answer it as typed. ``retry``: the last one was wrong."""

    has_panel: bool = True
    retry: bool = False
    kind: ClassVar[str] = "claim_code"


@dataclass(frozen=True)
class NeedPanelChoice:
    """Which panel the display has; answer its ``panel_ic_type``. ``reason`` says why it is asked."""

    reason: str = ""
    candidates: tuple[int, ...] = ()
    kind: ClassVar[str] = "panel_choice"


@dataclass(frozen=True)
class NeedConfirmation:
    """A yes/no before going on; ``False`` stops the run (it asks again when run next).

    ``reason`` is one of ``untested_panel`` (drive data nobody has seen draw), ``no_rollback`` (a
    BG22 install: no old image to return to), ``panel_ok`` (does the test pattern look right).
    """

    reason: str
    detail: str = ""
    subject: str = ""
    kind: ClassVar[str] = "confirmation"


@dataclass(frozen=True)
class NeedBoardChoice:
    """Which board the display is; answer the board name. Several boards share one chip."""

    candidates: tuple[str, ...] = ()
    kind: ClassVar[str] = "board_choice"


@dataclass(frozen=True)
class NeedWiredInstall:
    """Install 3.0 over USB with these files, then answer ``None``.

    The library does not flash over USB: ``url`` is the website flasher, ``protocol`` the method
    (``None``: the flasher; ``adafruit-serial-dfu``; ``swd``). After the install the display may
    advertise under another name.
    """

    board: str
    files: tuple[str, ...]
    protocol: str | None
    erases_config: bool | None
    url: str
    release_url: str = ""
    kind: ClassVar[str] = "wired_install"


@dataclass(frozen=True)
class NeedConfig:
    """A config to start from: the display has none and no backup was taken. Answer a
    ``GlobalConfig`` (for example ``config_from_json()`` of a Config Builder export)."""

    reason: str = "no_config"
    kind: ClassVar[str] = "config"


Request = (
    NeedKeyChoice
    | NeedGesture
    | NeedClaimCode
    | NeedPanelChoice
    | NeedConfirmation
    | NeedBoardChoice
    | NeedWiredInstall
    | NeedConfig
)

_KINDS: dict[str, type[Any]] = {
    cls.kind: cls
    for cls in (
        NeedKeyChoice,
        NeedGesture,
        NeedClaimCode,
        NeedPanelChoice,
        NeedConfirmation,
        NeedBoardChoice,
        NeedWiredInstall,
        NeedConfig,
    )
}


def to_dict(request: Request) -> dict[str, Any]:
    """The request as plain JSON values."""
    return {"kind": request.kind, **dataclasses.asdict(request)}


def from_dict(data: dict[str, Any]) -> Request:
    """The request ``to_dict`` saved.

    Raises:
        ValueError: An unknown kind.
    """
    fields = dict(data)
    cls = _KINDS.get(fields.pop("kind", ""))
    if cls is None:
        raise ValueError(f"Unknown setup request {data!r}")
    for name, value in fields.items():
        if isinstance(value, list):
            fields[name] = tuple(value)
    request: Request = cls(**fields)
    return request
