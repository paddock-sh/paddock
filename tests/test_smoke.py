"""Smoke test: the package imports and reports the expected version."""

import re
from pathlib import Path

import paddock

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version() -> None:
    assert paddock.__version__ == "0.2.0"


def test_pyproject_version_matches_the_package() -> None:
    """A release tags one number. The two version strings must not drift apart."""
    match = re.search(r'(?m)^version = "([^"]+)"', PYPROJECT.read_text())
    assert match is not None, "no version line in pyproject.toml"
    assert match.group(1) == paddock.__version__
