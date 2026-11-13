"""The package must report the version it was released as."""

from importlib.metadata import version

import opendisplay


def test_version_matches_package_metadata() -> None:
    assert opendisplay.__version__ == version("py-opendisplay")
