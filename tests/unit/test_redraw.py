"""The in-process record of displays that are redrawing (no I/O)."""

from __future__ import annotations

import pytest

from opendisplay import redraw


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    t = [1000.0]
    monkeypatch.setattr(redraw, "_clock", lambda: t[0])
    return t


def test_a_note_makes_its_keys_wait_until_the_deadline(clock: list[float]) -> None:
    redraw.mark_redrawing("OD4064C9", "AA:BB")

    assert redraw.remaining("aa:bb") == 60.0
    clock[0] += 59
    assert redraw.remaining("OD4064C9") == 1.0
    clock[0] += 2
    assert redraw.remaining("OD4064C9") == 0.0


def test_a_later_mark_extends_and_never_shortens(clock: list[float]) -> None:
    redraw.mark_redrawing("OD4064C9")
    clock[0] += 10
    redraw.mark_redrawing("OD4064C9", allowance=5)

    assert redraw.remaining("OD4064C9") == 50.0


def test_link_shares_the_latest_deadline(clock: list[float]) -> None:
    redraw.mark_redrawing("OD4064C9")
    redraw.link("OD4064C9", "AA:BB")

    assert redraw.remaining("AA:BB") == 60.0


def test_empty_and_none_keys_are_ignored(clock: list[float]) -> None:
    redraw.mark_redrawing(None, "")

    assert redraw.remaining(None) == 0.0
    assert redraw.remaining("") == 0.0


def test_wait_timeout_is_never_shorter_than_the_normal_timeout(clock: list[float]) -> None:
    assert redraw.wait_timeout(["x"], 5.0) == 5.0
    redraw.mark_redrawing("x")
    assert redraw.wait_timeout(["x"], 5.0) == 60.0
    clock[0] += 58
    assert redraw.wait_timeout(["x"], 5.0) == 5.0
