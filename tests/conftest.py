"""Every test runs against throwaway directories, and never shells out for real."""

import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from paddock import herdr_client
from tests import fake_sessions as fake_sessions_module


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
def fake_sessions(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Stands in for sessions: records what the chooser and the CLI asked of it.

    They are tested on which call they make with what, not on what a session does
    about it, which `tests/test_sessions.py` covers.
    """
    from paddock import cli, tui

    fake_sessions_module.reset()
    monkeypatch.setattr(cli, "sessions", fake_sessions_module)
    monkeypatch.setattr(tui, "sessions", fake_sessions_module)
    return fake_sessions_module


@pytest.fixture
def real_subprocess() -> None:
    """Ask for this to run a command for real: a stub script, never herdr or srt."""


@pytest.fixture(autouse=True)
def keychain(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The login Keychain a test sees: empty, unless the test puts a token in it by service.

    synth_config asks macOS `security` for the agent's token, so faking that command keeps
    the developer's own Keychain out of the tests. Any other command is a bug: CI has no
    herdr and no srt.
    """
    entries: dict[str, str] = {}
    if "real_subprocess" in request.fixturenames:
        return entries

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if list(argv[:2]) != ["security", "find-generic-password"]:
            raise AssertionError(f"the test ran a real subprocess: {argv!r}")
        service = argv[argv.index("-s") + 1]
        if service not in entries:
            raise subprocess.CalledProcessError(44, argv)
        return subprocess.CompletedProcess(argv, 0, entries[service] + "\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    return entries


class FakeClient:
    """Stands in for herdr_client: records what would have been asked of herdr."""

    def __init__(self) -> None:
        self.tabs: list[tuple[Path, str, dict[str, str]]] = []
        self.commands: list[tuple[str, str]] = []
        # The panes herdr would list: every tab this fake opened, less the ones a test closed.
        self.live: set[str] = set()
        # Set this to make herdr unreachable, which every caller has to survive.
        self.unreachable = False

    def create_tab(self, cwd: Path, label: str = "", env: dict[str, str] | None = None) -> str:
        self.tabs.append((cwd, label, dict(env or {})))
        pane_id = f"wA:p{len(self.tabs) + 1}"
        self.live.add(pane_id)
        return pane_id

    def run_in_pane(self, pane_id: str, command: str) -> None:
        self.commands.append((pane_id, command))

    def list_pane_ids(self) -> set[str]:
        if self.unreachable:
            raise herdr_client.HerdrError("herdr pane list failed: no herdr server")
        return set(self.live)

    def close_pane(self, pane_id: str) -> None:
        """A tab the user closed: herdr stops listing it, and nothing tells paddock."""
        self.live.discard(pane_id)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    """Everything that opens a pane goes through herdr_client, so faking it covers them all."""
    fake = FakeClient()
    monkeypatch.setattr(herdr_client, "create_tab", fake.create_tab)
    monkeypatch.setattr(herdr_client, "run_in_pane", fake.run_in_pane)
    monkeypatch.setattr(herdr_client, "list_pane_ids", fake.list_pane_ids)
    return fake


@pytest.fixture
def which(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Control what the code finds on the host PATH."""
    found = {
        "srt": "/opt/bin/srt",
        "npx": "/opt/bin/npx",
        "msb": "/opt/bin/msb",
        "git": "/usr/bin/git",
    }
    monkeypatch.setattr(shutil, "which", found.get)
    return found
