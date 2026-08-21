"""Smoke test: the package imports and reports the expected version."""

import paddock


def test_version() -> None:
    assert paddock.__version__ == "0.1.0"
