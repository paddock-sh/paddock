"""`paddock run`: the same sandboxed session, in the terminal it was typed in.

No tab is opened and no herdr is asked for anything. The tests stand in for the exec, so
the process running them is never replaced, and check what it would have become.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from paddock import herdr_client, sessions, standalone
from paddock.backends import LAUNCH_SCRIPT, RUN_SCRIPT, SHELL_SCRIPT
from paddock.profiles import Profile


@dataclass
class FakeRun:
    """What a backend hands back: where the run lives, where it starts, and its VM if any."""

    run_dir: Path
    workdir: Path
    vm_handle: str = ""


class FakeBackend:
    """A backend whose scripts are already on disk, so only the exec is left to check.

    A `vm_handle` is what makes a session outlive the process that started it, which is
    the whole difference between the two backends here.
    """

    def __init__(self, run_dir: Path, vm_handle: str = "") -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "work").mkdir(exist_ok=True)
        for name in (LAUNCH_SCRIPT, SHELL_SCRIPT):
            (run_dir / name).write_text("#!/bin/sh\n")
        self.run = FakeRun(run_dir, run_dir / "work", vm_handle)
        self.prepared: list[Profile] = []
        self.loaded: list[Path] = []
        self.collected: list[tuple[Path, str]] = []

    def prepare(self, profile: Profile) -> FakeRun:
        self.prepared.append(profile)
        return self.run

    def load_run(self, run_dir: Path) -> FakeRun:
        self.loaded.append(run_dir)
        return self.run

    def collect(self, run_dir: Path, vm_handle: str = "") -> None:
        self.collected.append((run_dir, vm_handle))


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway home, so nothing here reads or links the developer's own config."""
    where = tmp_path / "home"
    where.mkdir()
    monkeypatch.setenv("HOME", str(where))
    return where


@pytest.fixture
def execs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record the exec instead of becoming the run. Each entry is the argv it was given."""
    done: list[list[str]] = []
    monkeypatch.setattr(os, "execv", lambda path, argv: done.append([path, *argv]))
    return done


@pytest.fixture
def moved(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Where the run would have started, without moving the process running the tests."""
    where: list[Path] = []
    monkeypatch.setattr(os, "chdir", lambda path: where.append(Path(path)))
    return where


@pytest.fixture
def no_herdr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every way out to herdr, wired to fail: this path may not take any of them."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the standalone path asked herdr for something")

    for name in ("create_tab", "run_in_pane", "list_pane_ids", "reload_config", "check_config"):
        monkeypatch.setattr(herdr_client, name, refuse)


def a_vm_backend(monkeypatch: pytest.MonkeyPatch, run_dir: Path) -> FakeBackend:
    """Stand a backend with a VM in for msb, under the name sessions dispatches on."""
    backend = FakeBackend(run_dir, vm_handle="paddock-fake")
    monkeypatch.setitem(sessions.BACKENDS, "msb", backend)
    return backend


def artifacts(run_dir: Path) -> dict[str, str]:
    """Every file a run left on disk, with the run's own path taken out of the text.

    Two runs live in differently named directories, so the paths inside their settings and
    scripts differ by that name alone. Taking it out is what makes them comparable.
    """
    found = {}
    for path in sorted(run_dir.rglob("*")):
        name = str(path.relative_to(run_dir))
        if path.is_symlink():
            found[name] = f"-> {os.readlink(path)}"
        elif path.is_dir():
            found[name] = "<a directory>"
        else:
            found[name] = path.read_text().replace(str(run_dir), "<run>")
    return found


# --- the run dir is the one a launch would have made ------------------------


def test_a_run_prepares_the_run_dir_a_launch_would_have(which, execs, moved) -> None:
    """Same profile, same policy: `paddock run` is a launch that skips the tab."""
    profile = Profile(name="p", agent="shell", tools=["git"], network_presets=[])
    launched = Path(sessions.create_session(profile).run_dir)

    standalone.start(profile)

    ran = Path(execs[0][-1]).parent
    assert ran != launched
    assert artifacts(ran) == artifacts(launched)


def test_a_run_execs_the_launch_script_in_place(which, execs, moved) -> None:
    """The same script a pane is sent, so a failed launch is held here exactly as there."""
    standalone.start(Profile(name="p", agent="shell", tools=[], network_presets=[]))

    script = execs[0][-1]
    assert execs == [["/bin/sh", "/bin/sh", script]]
    assert script.endswith(f"/{LAUNCH_SCRIPT}")


def test_a_run_starts_in_the_run_s_own_workdir(which, execs, moved) -> None:
    """The sandbox may write there and the terminal is in it, as a pane would have been."""
    standalone.start(Profile(name="p", agent="shell", tools=[], network_presets=[]))

    assert moved == [Path(execs[0][-1]).parent / "work"]


def test_nothing_on_the_run_path_asks_herdr(which, execs, moved, no_herdr) -> None:
    """This is paddock without herdr. An import is not the point: the calls are."""
    standalone.start(Profile(name="p", agent="shell", tools=[], network_presets=[]))

    assert len(execs) == 1


# --- what is registered, and what is not ------------------------------------


def test_an_srt_run_registers_no_session(which, execs, moved) -> None:
    """Nothing outlives the terminal, so there is nothing for a registry entry to end."""
    standalone.start(Profile(name="p", agent="shell", tools=[], network_presets=[]))

    assert sessions.list_sessions() == []


def test_a_run_with_a_vm_registers_a_session_with_no_panes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, execs, moved
) -> None:
    """A microVM outlives the process, so a sweep in another terminal must see it claimed."""
    a_vm_backend(monkeypatch, tmp_path / "run")

    standalone.start(Profile(name="p"), backend="msb")

    live = sessions.list_sessions()
    assert [(one.backend, one.vm_handle, one.pane_ids) for one in live] == [
        ("msb", "paddock-fake", [])
    ]


def test_a_run_with_a_vm_execs_a_wrapper_that_collects_on_the_way_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, execs, moved
) -> None:
    """No paddock is left to notice the exit, so the script that runs it ends the session."""
    a_vm_backend(monkeypatch, tmp_path / "run")

    standalone.start(Profile(name="p"), backend="msb")

    wrapper = Path(execs[0][-1])
    session = sessions.list_sessions()[0]
    assert wrapper.name == RUN_SCRIPT
    assert f"collect {session.session_id}" in wrapper.read_text()
    assert str(tmp_path / "run" / LAUNCH_SCRIPT) in wrapper.read_text()


def test_a_run_kept_alive_is_not_collected_on_the_way_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, execs, moved
) -> None:
    """Keeping it alive is asking for the sandbox to be there afterwards, so nothing ends it."""
    a_vm_backend(monkeypatch, tmp_path / "run")

    standalone.start(Profile(name="p"), backend="msb", keep_alive=True)

    assert Path(execs[0][-1]).name == LAUNCH_SCRIPT
    assert sessions.list_sessions()[0].keep_alive is True


def test_a_run_that_cannot_start_leaves_no_session_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, moved
) -> None:
    """The exec is the last thing that can fail, and after it nothing would collect the VM."""
    backend = a_vm_backend(monkeypatch, tmp_path / "run")

    def refuse(path: str, argv: list[str]) -> None:
        raise OSError("no /bin/sh")

    monkeypatch.setattr(os, "execv", refuse)

    with pytest.raises(OSError):
        standalone.start(Profile(name="p"), backend="msb")

    assert sessions.list_sessions() == []
    assert [handle for _, handle in backend.collected] == ["paddock-fake"]


# --- joining a session that is already running ------------------------------


def test_attaching_execs_the_session_s_own_launch_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, execs, moved
) -> None:
    a_vm_backend(monkeypatch, tmp_path / "run")
    session = sessions.create_session(Profile(name="p"), backend="msb")

    standalone.attach(session)

    assert execs == [["/bin/sh", "/bin/sh", str(tmp_path / "run" / LAUNCH_SCRIPT)]]


def test_attaching_a_shell_execs_the_run_s_shell_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, execs, moved
) -> None:
    a_vm_backend(monkeypatch, tmp_path / "run")
    session = sessions.create_session(Profile(name="p"), backend="msb")

    standalone.attach(session, shell=True)

    assert execs == [["/bin/sh", "/bin/sh", str(tmp_path / "run" / SHELL_SCRIPT)]]


def test_attaching_adds_no_pane_to_the_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, execs, moved
) -> None:
    """A terminal is not a tab: closing herdr's last tab still means the same thing."""
    a_vm_backend(monkeypatch, tmp_path / "run")
    session = sessions.create_session(Profile(name="p"), backend="msb")

    standalone.attach(session)

    assert sessions.list_sessions()[0].pane_ids == []


def test_a_run_prepared_before_shell_tabs_says_so_rather_than_failing_at_the_exec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, execs, moved
) -> None:
    a_vm_backend(monkeypatch, tmp_path / "run")
    session = sessions.create_session(Profile(name="p"), backend="msb")
    (tmp_path / "run" / SHELL_SCRIPT).unlink()

    with pytest.raises(RuntimeError, match=SHELL_SCRIPT):
        standalone.attach(session, shell=True)


# --- how the wrapper calls paddock back -------------------------------------


def test_the_collect_command_names_this_paddock_and_this_session() -> None:
    """A run dir script has no PATH to trust, so it calls back through this interpreter."""
    session = sessions.Session("s1", "one", "p", "shell", "", "/runs/s1", False, [])

    argv = standalone.collect_argv(session)

    assert argv[1:] == ["-m", "paddock", "collect", "s1"]
    assert Path(argv[0]).name.startswith("python")
