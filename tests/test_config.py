"""Where paddock looks for its config."""

from pathlib import Path

import pytest

from paddock import config_dir as resolve_config_dir

DEFAULT = Path("~/.config/paddock").expanduser()


def test_the_override_wins(config_dir: Path) -> None:
    assert resolve_config_dir() == config_dir


def test_an_unset_override_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADDOCK_CONFIG_DIR")
    assert resolve_config_dir() == DEFAULT


def test_an_empty_override_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty value must not resolve to the current directory."""
    monkeypatch.setenv("PADDOCK_CONFIG_DIR", "")
    assert resolve_config_dir() == DEFAULT
