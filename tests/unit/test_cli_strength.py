"""CLI parsing of --tone / --gamut strengths."""

import pytest

from opendisplay import cli


def test_number_accepted() -> None:
    assert cli._parse_compression_value("--tone", "0.5") == 0.5


@pytest.mark.parametrize("value", ["auto", "off", "1.5", "-0.1"])
def test_non_numbers_and_out_of_range_rejected(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli._parse_compression_value("--gamut", value)
    assert exc.value.code == 1
    assert "--gamut must be a number from 0.0 to 1.0" in capsys.readouterr().err
