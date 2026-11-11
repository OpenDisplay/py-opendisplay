"""py-opendisplay's own wire-value ColorScheme."""

import pytest

from opendisplay import ColorScheme


def test_wire_values() -> None:
    assert [(s.name, int(s)) for s in ColorScheme] == [
        ("MONO", 0),
        ("BWR", 1),
        ("BWY", 2),
        ("BWRY", 3),
        ("BWGBRY", 4),
        ("GRAYSCALE_4", 5),
        ("GRAYSCALE_16", 6),
        ("SEVEN_COLOR", 7),
        ("BWGBRY_SPLIT", 8),
    ]


def test_from_value_rejects_library_local_9() -> None:
    with pytest.raises(ValueError, match="Invalid color scheme value: 9"):
        ColorScheme.from_value(9)


def test_from_value_round_trips() -> None:
    assert ColorScheme.from_value(3) is ColorScheme.BWRY


def test_not_epaper_dithering_enum() -> None:
    assert ColorScheme.__module__ == "opendisplay.models.enums"
