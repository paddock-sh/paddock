"""Sandbox sessions: the registry, names, attaching tabs, and collecting what nobody uses."""

import json
import os
import shlex
from dataclasses import asdict
from pathlib import Path

import pytest

from paddock import sessions
from paddock.profiles import Profile
from tests.conftest import FakeClient


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
    return (Path(session.run_dir) / "launch.sh").read_text().split("\n", 1)[1]


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


def test_a_pane_nobody_registered_changes_nothing(
    which: dict[str, str], client: FakeClient
) -> None:
    session = sessions.create_session(Profile(tools=[]), name="demo")
    sessions.attach(session)

    sessions.remove_pane("wA:p99")

    assert sessions.get_session("demo").pane_ids == session.pane_ids


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
