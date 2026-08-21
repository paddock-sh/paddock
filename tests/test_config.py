"""Where paddock looks for its config and keeps its run state."""

from pathlib import Path

import pytest

from paddock import config_dir as resolve_config_dir
from paddock import state_dir as resolve_state_dir

DEFAULT = Path("~/.config/paddock").expanduser()
DEFAULT_STATE = Path("~/.local/state/paddock").expanduser()


def test_the_override_wins(config_dir: Path) -> None:
    assert resolve_config_dir() == config_dir


def test_an_unset_override_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PADDOCK_CONFIG_DIR")
    assert resolve_config_dir() == DEFAULT


def test_an_empty_override_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty value must not resolve to the current directory."""
    monkeypatch.setenv("PADDOCK_CONFIG_DIR", "")
    assert resolve_config_dir() == DEFAULT


def test_the_state_override_wins(state_dir: Path) -> None:
    assert resolve_state_dir() == state_dir


def test_an_unset_state_override_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PADDOCK_STATE_DIR")
    assert resolve_state_dir() == DEFAULT_STATE


def test_an_empty_state_override_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADDOCK_STATE_DIR", "")
    assert resolve_state_dir() == DEFAULT_STATE
