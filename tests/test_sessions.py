"""Sandbox sessions: the registry, names, attaching tabs, and collecting what nobody uses."""

import fcntl
import json
import os
import shlex
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from paddock import herdr_client, sessions
from paddock.backends import SandboxGone, microsandbox, srt
from paddock.profiles import Profile
from tests.conftest import FakeClient, launch_command


@dataclass
class FakeRun:
    """What a backend hands back: sessions reads the run dir, and a VM handle if there is one."""

    run_dir: Path
    vm_handle: str = ""


class FakeBackend:
    """A backend that is not srt, so dispatch is tested on the name and not on one module.

    `fails_with` is what its `open_pane` raises instead of opening a tab.
    """

    def __init__(
        self,
        run_dir: str = "/state/runs/fake",
        vm_handle: str = "",
        fails_with: Exception | None = None,
    ) -> None:
        self.run = FakeRun(Path(run_dir), vm_handle)
        self.fails_with = fails_with
        self.prepared: list[Profile] = []
        self.loaded: list[Path] = []
        self.opened: list[tuple[object, str, Path | None, bool]] = []
        self.collected: list[tuple[Path, str]] = []

    def prepare(self, profile: Profile) -> FakeRun:
        self.prepared.append(profile)
        return self.run

    def load_run(self, run_dir: Path) -> FakeRun:
        self.loaded.append(run_dir)
        return self.run

    def open_pane(
        self, run: FakeRun, label: str = "", cwd: Path | None = None, shell: bool = False
    ) -> str:
        if self.fails_with is not None:
            raise self.fails_with
        self.opened.append((run, label, cwd, shell))
        # A new id per tab, as herdr gives: two tabs on one session are two panes.
        return f"wA:p{6 + len(self.opened)}"

    def collect(self, run_dir: Path, vm_handle: str = "") -> None:
        self.collected.append((run_dir, vm_handle))


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway home, so nothing here reads or links the developer's own config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def settings_path(command: str) -> Path:
    """The settings file out of a composed `srt --settings <file> -c ...` command."""
    return Path(shlex.split(command)[2])


def launch_script(session: sessions.Session) -> str:
    """The composed command back out of the script the pane runs."""
    return launch_command(Path(session.run_dir))


def write_registry(state_dir: Path, records: list[dict]) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "sessions.json"
    path.write_text(json.dumps(records))
    return path


def record(**overrides: object) -> dict:
    complete = asdict(
        sessions.Session(
            session_id="abc123",
            name="demo",
            profile_name="claude-default",
            agent="claude",
            created_at="2026-08-21T10:00:00+00:00",
            run_dir="/state/runs/one",
            keep_alive=False,
            pane_ids=[],
        )
    )
    return {**complete, **overrides}


# --- creating a session ----------------------------------------------------


def test_a_new_session_is_registered(which: dict[str, str], client: FakeClient) -> None:
    session = sessions.create_session(Profile(name="review", tools=["git"]), name="demo")

    assert [(s.name, s.session_id) for s in sessions.list_sessions()] == [
        ("demo", session.session_id)
    ]
    assert session.profile_name == "review"
    assert session.agent == "claude"
    assert session.pane_ids == []
    assert session.keep_alive is False
    assert session.created_at.startswith("20")


def test_a_new_session_has_its_run_dir_ready(
    which: dict[str, str], client: FakeClient, state_dir: Path
) -> None:
    session = sessions.create_session(Profile(tools=["git"]))

    run_dir = Path(session.run_dir)
    assert run_dir.parent == state_dir / "runs"
    assert json.loads((run_dir / "srt-settings.json").read_text())["network"]["allowedDomains"]
    assert (run_dir / "bin" / "git").is_symlink()
    assert (run_dir / "config" / ".mcp.json").is_file()
    assert (run_dir / "work").is_dir()


def test_creating_a_session_opens_no_pane(which: dict[str, str], client: FakeClient) -> None:
    """The chooser registers a session first; a tab attaches to it afterwards."""
    sessions.create_session(Profile(tools=[]))

    assert client.tabs == []
    assert client.commands == []


def test_a_name_already_in_use_is_refused(which: dict[str, str], client: FakeClient) -> None:
    sessions.create_session(Profile(tools=[]), name="demo")

    with pytest.raises(ValueError, match="demo"):
        sessions.create_session(Profile(tools=[]), name="demo")

    assert len(sessions.list_sessions()) == 1


def test_a_name_that_is_another_sessions_id_is_refused(
    which: dict[str, str], client: FakeClient
) -> None:
    """Both are references, so a name that is also an id would make lookups ambiguous."""
    session = sessions.create_session(Profile(tools=[]), name="demo")

    with pytest.raises(ValueError, match=session.session_id):
        sessions.create_session(Profile(tools=[]), name=session.session_id)


def test_an_empty_name_is_refused(which: dict[str, str], client: FakeClient) -> None:
    """The chooser sends None for a blank answer; a name of spaces is a mistake, not a blank."""
    with pytest.raises(ValueError, match="name"):
        sessions.create_session(Profile(tools=[]), name="  ")


def test_an_unnamed_session_is_named_after_its_profile(
    which: dict[str, str], client: FakeClient
) -> None:
    first = sessions.create_session(Profile(name="review", tools=[]))
    second = sessions.create_session(Profile(name="review", tools=[]))

    assert first.name.startswith("review-")
    assert second.name.startswith("review-")
    assert first.name != second.name


def test_a_failed_setup_registers_nothing(which: dict[str, str], client: FakeClient) -> None:
    with pytest.raises(ValueError, match="unknown agent"):
        sessions.create_session(Profile(agent="nope"))

    assert sessions.list_sessions() == []


# --- finding a session -----------------------------------------------------


def test_a_session_is_found_by_name_or_by_id(which: dict[str, str], client: FakeClient) -> None:
    session = sessions.create_session(Profile(tools=[]), name="demo")

    assert sessions.get_session("demo").session_id == session.session_id
    assert sessions.get_session(session.session_id).name == "demo"


def test_an_id_wins_over_a_name_that_looks_like_one(state_dir: Path) -> None:
    """create_session keeps these apart; a hand-edited registry may not."""
    write_registry(
        state_dir,
        [record(session_id="one", name="abc"), record(session_id="abc", name="two")],
    )

    assert sessions.get_session("abc").name == "two"


def test_an_unknown_reference_finds_nothing(which: dict[str, str], client: FakeClient) -> None:
    assert sessions.get_session("demo") is None


# --- attaching -------------------------------------------------------------


def test_attach_opens_a_pane_labelled_with_the_session_name(
    which: dict[str, str], client: FakeClient
) -> None:
    session = sessions.create_session(Profile(tools=[]), name="demo")

    pane_id = sessions.attach(session)

    cwd, label, env = client.tabs[0]
    assert label == "sbx:demo"
    assert cwd == Path(session.run_dir) / "work"
    assert env == {"CLAUDE_CONFIG_DIR": str(Path(session.run_dir) / "config")}
    assert [pane for pane, _ in client.commands] == [pane_id]


def test_attach_runs_the_sandboxed_command_of_that_session(
    which: dict[str, str], client: FakeClient
) -> None:
    session = sessions.create_session(Profile(tools=[]), name="demo")

    sessions.attach(session)

    _, command = client.commands[0]
    assert command == f"exec /bin/sh {session.run_dir}/launch.sh"
    assert shlex.split(launch_script(session))[0] == "srt"
    assert settings_path(launch_script(session)) == Path(session.run_dir) / "srt-settings.json"


def test_attach_records_the_pane_on_the_session(which: dict[str, str], client: FakeClient) -> None:
    session = sessions.create_session(Profile(tools=[]), name="demo")

    pane_id = sessions.attach(session)

    assert session.pane_ids == [pane_id]
    assert sessions.get_session("demo").pane_ids == [pane_id]


def test_a_second_tab_gets_the_same_settings_file_and_workdir(
    which: dict[str, str], client: FakeClient
) -> None:
    """srt attach is a new process under the same policy, never a shared process tree."""
    session = sessions.create_session(Profile(tools=[]), name="demo")

    first = sessions.attach(session)
    second = sessions.attach(session)

    assert first != second
    assert client.commands[0][1] == client.commands[1][1]
    assert {cwd for cwd, _, _ in client.tabs} == {Path(session.run_dir) / "work"}
    assert sessions.get_session("demo").pane_ids == [first, second]


def test_two_tabs_from_separate_loads_both_stay_attached(
    which: dict[str, str], client: FakeClient
) -> None:
    """Two popups each load the session before either attaches; neither pane may be lost."""
    sessions.create_session(Profile(tools=[]), name="demo")
    one, two = sessions.get_session("demo"), sessions.get_session("demo")

    first = sessions.attach(one)
    second = sessions.attach(two)

    assert sessions.get_session("demo").pane_ids == [first, second]


def test_attaching_to_a_sandbox_that_is_gone_drops_the_session(
    which: dict[str, str], client: FakeClient, monkeypatch: pytest.MonkeyPatch, state_dir: Path
) -> None:
    """A VM removed behind paddock's back leaves a session nothing can attach to."""
    gone = SandboxGone("the microVM paddock-1 is gone")
    fake = FakeBackend(vm_handle="paddock-1", fails_with=gone)
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)
    session = sessions.create_session(Profile(tools=[]), name="demo", backend="fake")

    with pytest.raises(SandboxGone, match="demo"):
        sessions.attach(session)

    assert sessions.list_sessions() == []
    assert json.loads((state_dir / "sessions.json").read_text()) == []


def test_attach_can_open_the_tab_somewhere_else(
    which: dict[str, str], client: FakeClient, tmp_path: Path
) -> None:
    session = sessions.create_session(Profile(tools=[]), name="demo")

    sessions.attach(session, cwd=tmp_path)

    assert client.tabs[0][0] == tmp_path


def test_launch_creates_a_session_and_attaches_to_it(
    which: dict[str, str], client: FakeClient
) -> None:
    session, pane_id = sessions.launch(Profile(name="review", tools=[]), name="demo")

    assert sessions.get_session("demo").pane_ids == [pane_id]
    assert client.tabs[0][1] == "sbx:demo"
    assert session.name == "demo"


def test_a_session_whose_first_tab_fails_is_not_left_behind(
    which: dict[str, str], client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the VM keeps running and the registry keeps a session with no tabs."""
    fake = FakeBackend(vm_handle="paddock-1", fails_with=RuntimeError("herdr said no"))
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)

    with pytest.raises(RuntimeError, match="paddock-1") as raised:
        sessions.launch(Profile(tools=[]), name="demo", backend="fake")

    assert "demo" in str(raised.value)
    assert "herdr said no" in str(raised.value)
    assert sessions.list_sessions() == []
    assert fake.collected == [(Path("/state/runs/fake"), "paddock-1")]


def test_a_first_tab_that_fails_on_a_backend_with_no_vm_is_rolled_back_too(
    which: dict[str, str], client: FakeClient, monkeypatch: pytest.MonkeyPatch, state_dir: Path
) -> None:
    def refuse(pane_id: str, command: str) -> None:
        raise RuntimeError("herdr pane run failed")

    monkeypatch.setattr(herdr_client, "run_in_pane", refuse)

    with pytest.raises(RuntimeError, match="demo"):
        sessions.launch(Profile(tools=[]), name="demo")

    assert sessions.list_sessions() == []
    assert json.loads((state_dir / "sessions.json").read_text()) == []


# --- backends --------------------------------------------------------------


def test_both_backends_are_registered_under_the_names_records_carry() -> None:
    assert sessions.BACKENDS == {"srt": srt, "msb": microsandbox}


def test_a_new_session_says_which_backend_it_runs_on(
    which: dict[str, str], client: FakeClient, state_dir: Path
) -> None:
    session = sessions.create_session(Profile(tools=[]), name="demo")

    assert session.backend == "srt"
    assert sessions.get_session("demo").backend == "srt"
    records = json.loads((state_dir / "sessions.json").read_text())
    assert records[0]["backend"] == "srt"


def test_a_record_written_before_the_backend_field_is_an_srt_session(state_dir: Path) -> None:
    """v1 wrote no backend, and every session it wrote was an srt one (SPEC §3.4)."""
    legacy = record()
    legacy.pop("backend")
    write_registry(state_dir, [legacy])

    assert [session.backend for session in sessions.list_sessions()] == ["srt"]


def test_a_session_on_a_backend_this_paddock_lacks_still_loads(state_dir: Path) -> None:
    """A registry written by a newer paddock is readable; only attaching to it is not."""
    write_registry(state_dir, [record(backend="microsandbox")])

    assert [session.name for session in sessions.list_sessions()] == ["demo"]


def test_attaching_on_an_unknown_backend_says_which_one(
    state_dir: Path, client: FakeClient
) -> None:
    write_registry(state_dir, [record(backend="microsandbox")])

    with pytest.raises(ValueError, match="microsandbox"):
        sessions.attach(sessions.get_session("demo"))

    assert client.tabs == []


def test_attach_goes_through_the_backend_the_session_names(
    state_dir: Path, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackend()
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)
    write_registry(state_dir, [record(backend="fake")])
    session = sessions.get_session("demo")

    pane_id = sessions.attach(session)

    assert fake.loaded == [Path(session.run_dir)]
    assert fake.opened == [(fake.run, "sbx:demo", None, False)]
    assert pane_id == "wA:p7"
    assert sessions.get_session("demo").pane_ids == ["wA:p7"]


def test_a_field_a_newer_paddock_wrote_survives_a_rewrite(
    which: dict[str, str], client: FakeClient, state_dir: Path
) -> None:
    """One registry, two paddocks: writing it back must not strip what the newer one added."""
    write_registry(
        state_dir,
        [record(session_id="newer", name="newer", backend="future", shard="b")],
    )

    session = sessions.create_session(Profile(tools=[]), name="demo")
    sessions.remove_pane(sessions.attach(session))

    records = json.loads((state_dir / "sessions.json").read_text())
    assert [entry.get("shard") for entry in records] == ["b"]


def test_a_field_written_while_a_tab_was_opening_is_kept(
    which: dict[str, str], client: FakeClient, state_dir: Path
) -> None:
    """The other writer's key is merged back the way its panes are, not written over."""
    session = sessions.create_session(Profile(tools=[]), name="demo")
    write_registry(
        state_dir,
        [
            record(
                session_id=session.session_id,
                name="demo",
                run_dir=session.run_dir,
                shard="b",
            )
        ],
    )

    sessions.attach(session)

    records = json.loads((state_dir / "sessions.json").read_text())
    assert [entry.get("shard") for entry in records] == ["b"]


def test_a_new_session_is_prepared_by_the_registered_backend(
    which: dict[str, str], client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackend()
    monkeypatch.setitem(sessions.BACKENDS, "srt", fake)
    profile = Profile(tools=[])

    session = sessions.create_session(profile, name="demo")

    assert fake.prepared == [profile]
    assert session.run_dir == "/state/runs/fake"


def test_a_new_session_can_be_asked_for_on_another_backend(
    which: dict[str, str], client: FakeClient, monkeypatch: pytest.MonkeyPatch, state_dir: Path
) -> None:
    """Two backends exist now, so the caller says which one, and the record keeps the answer."""
    fake = FakeBackend()
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)

    session = sessions.create_session(Profile(tools=[]), name="demo", backend="fake")

    assert session.backend == "fake"
    assert fake.prepared == [Profile(tools=[])]
    assert json.loads((state_dir / "sessions.json").read_text())[0]["backend"] == "fake"


def test_a_backend_this_paddock_lacks_prepares_nothing(
    which: dict[str, str], client: FakeClient
) -> None:
    with pytest.raises(ValueError, match="nope"):
        sessions.create_session(Profile(tools=[]), name="demo", backend="nope")

    assert sessions.list_sessions() == []


def test_launch_can_name_the_backend_too(
    which: dict[str, str], client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackend()
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)

    session, pane_id = sessions.launch(Profile(tools=[]), name="demo", backend="fake")

    assert session.backend == "fake"
    assert pane_id == "wA:p7"


# --- the VM a session runs in ----------------------------------------------


def test_a_session_on_a_vm_records_its_handle(
    which: dict[str, str], client: FakeClient, monkeypatch: pytest.MonkeyPatch, state_dir: Path
) -> None:
    """The handle every msb subcommand takes, kept where the registry can be reconciled."""
    fake = FakeBackend(vm_handle="paddock-1")
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)

    session = sessions.create_session(Profile(tools=[]), name="demo", backend="fake")

    assert session.vm_handle == "paddock-1"
    assert sessions.get_session("demo").vm_handle == "paddock-1"
    assert json.loads((state_dir / "sessions.json").read_text())[0]["vm_handle"] == "paddock-1"


def test_a_session_with_no_vm_names_none(which: dict[str, str], client: FakeClient) -> None:
    """An srt session is a settings file and a workdir, so there is no handle to keep."""
    session = sessions.create_session(Profile(tools=[]), name="demo")

    assert session.vm_handle == ""


def test_a_record_written_before_the_vm_handle_field_is_still_a_session(state_dir: Path) -> None:
    legacy = record()
    legacy.pop("vm_handle")
    write_registry(state_dir, [legacy])

    assert [session.vm_handle for session in sessions.list_sessions()] == [""]


# --- lifecycle -------------------------------------------------------------


def test_the_session_goes_when_its_last_pane_does(
    which: dict[str, str], client: FakeClient
) -> None:
    session = sessions.create_session(Profile(tools=[]), name="demo")
    pane_id = sessions.attach(session)

    sessions.remove_pane(pane_id)

    assert sessions.list_sessions() == []


def test_a_session_with_another_pane_stays(which: dict[str, str], client: FakeClient) -> None:
    session = sessions.create_session(Profile(tools=[]), name="demo")
    first = sessions.attach(session)
    second = sessions.attach(session)

    sessions.remove_pane(first)

    assert sessions.get_session("demo").pane_ids == [second]


def test_a_keep_alive_session_survives_its_last_pane(
    which: dict[str, str], client: FakeClient
) -> None:
    """An srt session is a settings file and a workdir, so keeping one costs only disk."""
    session = sessions.create_session(Profile(tools=[]), name="demo")
    session.keep_alive = True
    pane_id = sessions.attach(session)

    sessions.remove_pane(pane_id)

    assert sessions.get_session("demo").pane_ids == []


def test_keeping_a_session_running_is_written_down_where_it_is_read(
    which: dict[str, str], client: FakeClient
) -> None:
    """The chooser asks under Advanced, and the answer has to outlive the process that asked."""
    session = sessions.create_session(Profile(tools=[]), name="demo")
    sessions.attach(session)

    sessions.set_keep_alive(session, True)

    assert sessions.get_session("demo").keep_alive is True
    assert sessions.get_session("demo").pane_ids == session.pane_ids


def test_a_session_collected_while_it_was_used_is_not_brought_back(
    which: dict[str, str], client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """Writing a dead session back would revive one whose credentials have already gone."""
    session = sessions.create_session(Profile(tools=[]), name="demo")
    pane_id = sessions.attach(session)
    sessions.remove_pane(pane_id)  # the last tab closed, so it was collected

    sessions.set_keep_alive(session, True)

    assert sessions.list_sessions() == []
    assert "collected" in capsys.readouterr().err


def test_a_collected_session_loses_the_token_in_its_run_dir(
    which: dict[str, str], client: FakeClient, keychain: dict[str, str]
) -> None:
    """The run dir stays, because deleting a workdir would lose work, but the token in it goes."""
    keychain["Claude Code-credentials"] = '{"claudeAiOauth": {}}'
    session = sessions.create_session(Profile(tools=[]), name="demo")
    pane_id = sessions.attach(session)
    config = Path(session.run_dir) / "config"
    assert (config / ".credentials.json").is_file()

    sessions.remove_pane(pane_id)

    assert not (config / ".credentials.json").exists()
    assert (config / ".mcp.json").is_file()
    assert Path(session.run_dir).is_dir()


def test_a_session_that_survives_keeps_its_token(
    which: dict[str, str], client: FakeClient, keychain: dict[str, str]
) -> None:
    """A keep-alive session is still usable, so it still needs the agent to authenticate."""
    keychain["Claude Code-credentials"] = '{"claudeAiOauth": {}}'
    session = sessions.create_session(Profile(tools=[]), name="demo")
    session.keep_alive = True
    pane_id = sessions.attach(session)

    sessions.remove_pane(pane_id)

    assert (Path(session.run_dir) / "config" / ".credentials.json").is_file()


def test_collecting_a_session_lets_its_backend_tear_the_run_down(
    which: dict[str, str], client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is where a microVM is destroyed: nothing else knows the session ended."""
    fake = FakeBackend()
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)
    session = sessions.create_session(Profile(tools=[]), name="demo", backend="fake")

    sessions.remove_pane(sessions.attach(session))

    assert fake.collected == [(Path(session.run_dir), "")]


def test_the_backend_is_handed_the_handle_the_registry_kept(
    which: dict[str, str], client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run dir that lost its launch record still names its VM, in the registry."""
    fake = FakeBackend(vm_handle="paddock-1")
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)
    session = sessions.create_session(Profile(tools=[]), name="demo", backend="fake")

    sessions.remove_pane(sessions.attach(session))

    assert fake.collected == [(Path(session.run_dir), "paddock-1")]


def test_a_session_that_survives_its_last_pane_is_not_torn_down(
    which: dict[str, str], client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackend()
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)
    session = sessions.create_session(Profile(tools=[]), name="demo", backend="fake")
    session.keep_alive = True

    sessions.remove_pane(sessions.attach(session))

    assert fake.collected == []


def test_collecting_a_session_on_a_backend_this_paddock_lacks_says_so(
    state_dir: Path, client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    """A newer paddock's session leaves the registry here; only its own can tear it down."""
    write_registry(state_dir, [record(backend="future", pane_ids=["wA:p3"])])

    sessions.remove_pane("wA:p3")

    assert sessions.list_sessions() == []
    assert "future" in capsys.readouterr().err


def test_a_pane_nobody_registered_changes_nothing(
    which: dict[str, str], client: FakeClient
) -> None:
    session = sessions.create_session(Profile(tools=[]), name="demo")
    sessions.attach(session)

    sessions.remove_pane("wA:p99")

    assert sessions.get_session("demo").pane_ids == session.pane_ids


# --- reconciling with herdr ------------------------------------------------


def test_a_pane_herdr_no_longer_has_is_dropped(which: dict[str, str], client: FakeClient) -> None:
    """Nothing tells paddock a tab closed, so it compares the registry with `herdr pane list`."""
    session = sessions.create_session(Profile(tools=[]), name="demo")
    first = sessions.attach(session)
    second = sessions.attach(session)
    client.close_pane(first)

    sessions.reconcile()

    assert sessions.get_session("demo").pane_ids == [second]


def test_the_panes_that_are_still_open_are_kept(which: dict[str, str], client: FakeClient) -> None:
    session = sessions.create_session(Profile(tools=[]), name="demo")
    first = sessions.attach(session)
    second = sessions.attach(session)

    assert sessions.reconcile() == []
    assert sessions.get_session("demo").pane_ids == [first, second]


def test_a_session_whose_last_tab_closed_is_collected(
    which: dict[str, str], client: FakeClient
) -> None:
    session = sessions.create_session(Profile(tools=[]), name="demo")
    client.close_pane(sessions.attach(session))

    collected = sessions.reconcile()

    assert [session.name for session in collected] == ["demo"]
    assert sessions.list_sessions() == []


def test_a_collected_srt_session_loses_the_token_in_its_run_dir(
    which: dict[str, str], client: FakeClient, keychain: dict[str, str]
) -> None:
    """The guarantee the whole thing exists for: the token does not outlive the session."""
    keychain["Claude Code-credentials"] = '{"claudeAiOauth": {}}'
    session = sessions.create_session(Profile(tools=[]), name="demo")
    client.close_pane(sessions.attach(session))
    config = Path(session.run_dir) / "config"
    assert (config / ".credentials.json").is_file()

    sessions.reconcile()

    assert not (config / ".credentials.json").exists()
    assert Path(session.run_dir).is_dir()


def test_a_collected_vm_session_is_torn_down_with_the_handle_the_registry_kept(
    which: dict[str, str], client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is where an msb microVM is destroyed: nothing else knows the session ended."""
    fake = FakeBackend(vm_handle="paddock-1")
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)
    session = sessions.create_session(Profile(tools=[]), name="demo", backend="fake")
    pane_id = sessions.attach(session)
    client.live.add(pane_id)  # the fake backend opens its pane without going through the client
    client.close_pane(pane_id)

    sessions.reconcile()

    assert fake.collected == [(Path(session.run_dir), "paddock-1")]


def test_a_keep_alive_session_survives_with_no_panes_left(
    which: dict[str, str], client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackend()
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)
    session = sessions.create_session(Profile(tools=[]), name="demo", backend="fake")
    session.keep_alive = True
    pane_id = sessions.attach(session)
    client.live.add(pane_id)  # the fake backend opens its pane without going through the client
    client.close_pane(pane_id)

    assert sessions.reconcile() == []
    assert sessions.get_session("demo").pane_ids == []
    assert fake.collected == []


def test_a_herdr_that_cannot_be_reached_changes_nothing(
    which: dict[str, str], client: FakeClient, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No answer is not "no panes". Collecting the lot here would destroy every session."""
    fake = FakeBackend()
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)
    session = sessions.create_session(Profile(tools=[]), name="demo", backend="fake")
    sessions.attach(session)
    before = (state_dir / "sessions.json").read_text()
    client.unreachable = True

    assert sessions.reconcile() == []
    assert (state_dir / "sessions.json").read_text() == before
    assert fake.collected == []


def test_a_reconcile_with_nothing_to_do_does_not_rewrite_the_registry(
    which: dict[str, str], client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It runs at every paddock invocation, so the usual case has to be a read and no more."""
    sessions.attach(sessions.create_session(Profile(tools=[]), name="demo"))
    monkeypatch.setattr(os, "replace", _boom)

    assert sessions.reconcile() == []


def test_a_session_that_has_not_opened_its_first_tab_is_left_alone(
    which: dict[str, str], client: FakeClient
) -> None:
    """create_session registers before attach opens a pane, and another paddock may be there."""
    sessions.create_session(Profile(tools=[]), name="demo")

    assert sessions.reconcile() == []
    assert sessions.get_session("demo").pane_ids == []


def test_herdr_is_asked_while_the_registry_lock_is_held(
    which: dict[str, str], client: FakeClient, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two paddocks at once: a tab that attached a moment ago must not read as a dead pane.

    The lock is what makes that true. A pane list taken before the lock could miss a tab
    another paddock had already opened and registered, and this would then collect it.
    """
    held: list[bool] = []

    def list_pane_ids() -> set[str]:
        held.append(_lock_is_held(state_dir))
        return set(client.live)

    session = sessions.create_session(Profile(tools=[]), name="demo")
    sessions.attach(session)
    monkeypatch.setattr(herdr_client, "list_pane_ids", list_pane_ids)

    sessions.reconcile()

    assert held == [True]


def _lock_is_held(state_dir: Path) -> bool:
    """Whether something already holds the registry lock, asked without blocking on it."""
    with open(state_dir / "sessions.lock") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


# --- the registry file -----------------------------------------------------


def test_the_registry_lives_in_the_state_dir(
    which: dict[str, str], client: FakeClient, state_dir: Path
) -> None:
    session = sessions.create_session(Profile(tools=[]), name="demo")

    records = json.loads((state_dir / "sessions.json").read_text())
    assert [record["name"] for record in records] == ["demo"]
    assert records[0]["run_dir"] == session.run_dir


def test_a_session_survives_a_reload_unchanged(
    which: dict[str, str], client: FakeClient
) -> None:
    """Sessions outlive the popup that made them, and herdr restarts (SPEC §3.4)."""
    session = sessions.create_session(Profile(tools=[]), name="demo")
    sessions.attach(session)

    assert sessions.list_sessions() == [session]


def test_no_registry_yet_is_not_an_error(state_dir: Path) -> None:
    assert sessions.list_sessions() == []


def test_a_registry_that_will_not_parse_starts_empty_and_says_so(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_registry(state_dir, [])
    path.write_text("{not json")

    assert sessions.list_sessions() == []
    assert str(path) in capsys.readouterr().err


def test_a_record_of_the_wrong_shape_is_dropped(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_registry(state_dir, [record(name=7), record(name="demo"), "not a record"])

    assert [session.name for session in sessions.list_sessions()] == ["demo"]
    assert "dropped" in capsys.readouterr().err


def test_the_registry_is_never_written_in_place(
    which: dict[str, str], client: FakeClient, state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-write must not leave half a registry, so it is replaced whole."""
    sessions.create_session(Profile(tools=[]), name="demo")
    before = (state_dir / "sessions.json").read_text()
    monkeypatch.setattr(os, "replace", _boom)

    with pytest.raises(OSError):
        sessions.create_session(Profile(tools=[]), name="other")

    assert (state_dir / "sessions.json").read_text() == before
    assert list(state_dir.glob("*.tmp")) == []


def test_writers_take_a_lock_on_the_state_dir(
    which: dict[str, str], client: FakeClient, state_dir: Path
) -> None:
    """Two popups are two processes, so the read-modify-write is serialized by a file lock."""
    sessions.create_session(Profile(tools=[]), name="demo")

    assert (state_dir / "sessions.lock").exists()


def _boom(*args: object, **kwargs: object) -> None:
    raise OSError("disk full")


# --- the local branch ------------------------------------------------------


def test_launch_local_makes_a_plain_unlabelled_tab(client: FakeClient, tmp_path: Path) -> None:
    """No session, no sandbox, no label: an unlabelled tab is how you tell (SPEC §3.5)."""
    pane_id = sessions.launch_local(tmp_path)

    assert client.tabs == [(tmp_path, "", {})]
    assert client.commands == []
    assert sessions.list_sessions() == []
    assert pane_id == "wA:p2"


# --- a shell tab on a session that is already running -----------------------


def test_a_shell_tab_asks_the_backend_for_a_shell_and_is_labelled_as_one(
    state_dir: Path, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tab bar has to tell the two apart: they are the same sandbox, not the same thing."""
    fake = FakeBackend()
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)
    write_registry(state_dir, [record(backend="fake")])

    sessions.attach(sessions.get_session("demo"), shell=True)

    assert fake.opened == [(fake.run, "sbx:demo (shell)", None, True)]


def test_a_shell_tab_is_a_pane_of_the_session_like_any_other(
    state_dir: Path, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It holds the session open, and closing it is what ends the session when it is last."""
    fake = FakeBackend()
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)
    write_registry(state_dir, [record(backend="fake")])
    session = sessions.get_session("demo")

    agent_pane = sessions.attach(session)
    shell_pane = sessions.attach(sessions.get_session("demo"), shell=True)
    client.live.update({agent_pane, shell_pane})  # the fake backend opens its own panes

    assert sessions.get_session("demo").pane_ids == [agent_pane, shell_pane]

    client.close_pane(agent_pane)
    assert [collected.name for collected in sessions.reconcile()] == []
    assert sessions.get_session("demo").pane_ids == [shell_pane]

    client.close_pane(shell_pane)
    assert [collected.name for collected in sessions.reconcile()] == ["demo"]
    assert sessions.get_session("demo") is None


def test_the_label_says_shell_only_when_it_is_one() -> None:
    assert sessions.pane_label("review") == "sbx:review"
    assert sessions.pane_label("review", shell=True) == "sbx:review (shell)"


# --- ctrl-c between the boot and the first tab ------------------------------


def test_ctrl_c_before_the_first_tab_tears_the_session_down_and_is_not_wrapped(
    state_dir: Path, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cli.py turns KeyboardInterrupt into exit 130, so wrapping it would lose that."""
    fake = FakeBackend(fails_with=KeyboardInterrupt())
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)

    with pytest.raises(KeyboardInterrupt):
        sessions.launch(Profile(name="demo"), "demo", backend="fake")

    assert sessions.list_sessions() == []
    assert fake.collected  # the boot was torn down, not left running


def test_a_launch_that_fails_for_any_other_reason_still_says_which_session(
    state_dir: Path, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackend(fails_with=RuntimeError("no tab"))
    monkeypatch.setitem(sessions.BACKENDS, "fake", fake)

    with pytest.raises(RuntimeError, match="could not open its first tab"):
        sessions.launch(Profile(name="demo"), "demo", backend="fake")


# --- sweeping what a killed launch left running -----------------------------


class SweepingBackend(FakeBackend):
    """A backend with something running that no session claims."""

    def __init__(self, running: list[str]) -> None:
        super().__init__()
        self.running = running
        self.swept: list[set[str]] = []

    def sweep(self, known: set[str]) -> list[str]:
        self.swept.append(set(known))
        return [name for name in self.running if name not in known]


def test_gc_sweeps_what_no_session_claims(
    state_dir: Path, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)  # no msb on this host to ask
    backend = SweepingBackend(["paddock-orphan", "paddock-kept"])
    monkeypatch.setitem(sessions.BACKENDS, "fake", backend)
    write_registry(state_dir, [record(backend="fake", vm_handle="paddock-kept")])

    assert sessions.collect_orphans() == ["paddock-orphan"]
    assert backend.swept == [{"paddock-kept"}]


def test_a_backend_that_will_not_answer_a_sweep_is_a_message_not_a_failure(
    state_dir: Path, client: FakeClient, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """gc has already collected what it collected, and the rest is next time's job."""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    class Sulking(FakeBackend):
        def sweep(self, known: set[str]) -> list[str]:
            raise RuntimeError("msb ls did not answer")

    monkeypatch.setitem(sessions.BACKENDS, "fake", Sulking())

    assert sessions.collect_orphans() == []
    assert "could not sweep" in capsys.readouterr().err
