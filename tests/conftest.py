"""Every test runs against throwaway directories, never the developer's own."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config"
    monkeypatch.setenv("PADDOCK_CONFIG_DIR", str(path))
    return path


@pytest.fixture(autouse=True)
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state"
    monkeypatch.setenv("PADDOCK_STATE_DIR", str(path))
    return path


@pytest.fixture(autouse=True)
def no_herdr_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test must not inherit the herdr session the developer happens to be sitting in."""
    monkeypatch.delenv("HERDR_ACTIVE_WORKSPACE_ID", raising=False)
