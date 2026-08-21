"""Every test runs against throwaway directories, and never shells out for real."""

import subprocess
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


@pytest.fixture
def real_subprocess() -> None:
    """Ask for this to run a command for real — a stub script, never herdr or srt."""


@pytest.fixture(autouse=True)
def no_subprocess(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """CI has no herdr and no srt, so a test that shells out to either is a bug."""
    if "real_subprocess" in request.fixturenames:
        return

    def fail(argv: object, **kwargs: object) -> None:
        raise AssertionError(f"the test ran a real subprocess: {argv!r}")

    monkeypatch.setattr(subprocess, "run", fail)
