"""Every test runs against throwaway directories, and never shells out for real."""

import shutil
import subprocess
from pathlib import Path

import pytest

from paddock import herdr_client


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


class FakeClient:
    """Stands in for herdr_client: records what would have been asked of herdr."""

    def __init__(self) -> None:
        self.tabs: list[tuple[Path, str, dict[str, str]]] = []
        self.commands: list[tuple[str, str]] = []

    def create_tab(self, cwd: Path, label: str = "", env: dict[str, str] | None = None) -> str:
        self.tabs.append((cwd, label, dict(env or {})))
        return f"wA:p{len(self.tabs) + 1}"

    def run_in_pane(self, pane_id: str, command: str) -> None:
        self.commands.append((pane_id, command))


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Everything that opens a pane goes through herdr_client, so faking it covers them all."""
    fake = FakeClient()
    monkeypatch.setattr(herdr_client, "create_tab", fake.create_tab)
    monkeypatch.setattr(herdr_client, "run_in_pane", fake.run_in_pane)
    return fake


@pytest.fixture
def which(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Control what the code finds on the host PATH."""
    found = {"srt": "/opt/bin/srt", "npx": "/opt/bin/npx", "git": "/usr/bin/git"}
    monkeypatch.setattr(shutil, "which", found.get)
    return found
