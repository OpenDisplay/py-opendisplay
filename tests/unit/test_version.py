"""The package must report the version it was released as."""

from importlib.metadata import PackageNotFoundError, version

import pytest

import opendisplay


def test_version_matches_package_metadata() -> None:
    assert opendisplay.__version__ == version("py-opendisplay")


def test_version_is_unknown_without_package_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(opendisplay, "version", missing)

    assert opendisplay._package_version() == "unknown"
