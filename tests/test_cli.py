"""The `paddock` command: argv to an action, and exactly one thing done about it."""

import inspect
import json
from dataclasses import fields
from pathlib import Path

import pytest

from paddock import cli, log, sessions, tui
from paddock.backends.srt import SrtNotFound
from paddock.herdr_client import HerdrError
from paddock.profiles import Profile, load_profiles
from tests import fake_sessions as fake_sessions_module
from tests.fake_sessions import Session


@pytest.fixture
def chooser(monkeypatch: pytest.MonkeyPatch):
    """Answer the popup with a fixed plan: the TUI itself is tested in test_tui.py."""

    def answer(plan: object | None):
        monkeypatch.setattr(tui, "choose", lambda cwd: plan)

    return answer


def names(calls: list[tuple]) -> list[str]:
    return [call[0] for call in calls]


# --- argv ------------------------------------------------------------------


def test_no_arguments_means_the_chooser() -> None:
    """That is how the herdr popup runs it."""
    assert cli.parse_args([]).name == "choose"


def test_flags_with_no_subcommand_still_mean_the_chooser() -> None:
    command = cli.parse_args(["--dry-run"])

    assert (command.name, command.dry_run) == ("choose", True)


def test_help_is_still_help() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--help"])


def test_each_subcommand_is_recognised() -> None:
    assert cli.parse_args(["choose"]).name == "choose"
    assert cli.parse_args(["profiles"]).name == "profiles"
    assert cli.parse_args(["init"]).name == "init"
    assert cli.parse_args(["launch", "claude-default"]).profile == "claude-default"
    assert cli.parse_args(["attach", "review"]).ref == "review"


def test_init_takes_a_dry_run_and_an_undo() -> None:
    assert cli.parse_args(["init", "--dry-run"]).dry_run is True
    assert cli.parse_args(["init", "--undo"]).undo is True
    assert cli.parse_args(["init"]).undo is False


def test_cwd_and_dry_run_are_flags_on_the_subcommand() -> None:
    command = cli.parse_args(["attach", "review", "--cwd", "/work", "--dry-run"])

    assert (command.cwd, command.dry_run) == ("/work", True)


def test_the_defaults_are_here_and_do_it_for_real() -> None:
    command = cli.parse_args(["choose"])

    assert (command.cwd, command.dry_run) == ("", False)


@pytest.mark.parametrize("argv", [["frobnicate"], ["launch"], ["attach"]])
def test_argv_that_makes_no_sense_is_refused(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(argv)


# --- what would happen -----------------------------------------------------


def test_a_dry_run_says_where_a_local_tab_would_open(
    fake_sessions, chooser, capsys: pytest.CaptureFixture[str]
) -> None:
    chooser(tui.Local(cwd="/work"))

    assert cli.main(["choose", "--dry-run"]) == 0
    assert "/work" in capsys.readouterr().out
    assert fake_sessions.calls == []


def test_a_dry_run_attach_touches_no_session(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["attach", "review", "--cwd", "/work", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "review" in out and "/work" in out
    assert fake_sessions.calls == []


def test_a_dry_run_launch_names_the_profile_and_agent(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["launch", "offline-shell", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "offline-shell" in out and "agent shell" in out
    assert fake_sessions.calls == []


def test_a_dry_run_saves_no_profile(
    fake_sessions, chooser, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chooser(tui.NewSession(profile=Profile(), name="review", save_as="review"))

    assert cli.main(["choose", "--dry-run"]) == 0
    assert "review" in capsys.readouterr().out
    assert not (config_dir / "profiles").exists()
    assert fake_sessions.calls == []


# --- doing it --------------------------------------------------------------


def test_local_opens_a_plain_tab(
    fake_sessions, chooser, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chooser(tui.Local(cwd=str(tmp_path)))

    assert cli.main(["choose"]) == 0
    assert fake_sessions.calls == [("launch_local", tmp_path)]
    assert capsys.readouterr().out.strip() == "wA:p1"


def test_backing_out_of_the_chooser_does_nothing(
    fake_sessions, chooser, capsys: pytest.CaptureFixture[str]
) -> None:
    chooser(None)

    assert cli.main(["choose"]) == 0
    assert fake_sessions.calls == []
    assert capsys.readouterr().out == ""


def test_the_chooser_opens_in_the_current_directory_by_default(
    fake_sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []

    def record(cwd: Path) -> tui.Local:
        seen.append(cwd)
        return tui.Local(cwd=str(cwd))

    monkeypatch.setattr(tui, "choose", record)

    cli.main(["choose"])

    assert seen == [Path.cwd()]


def test_attach_finds_the_session_then_puts_a_tab_on_it(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    session = Session(session_id="s1", name="review")
    fake_sessions.registry.append(session)

    assert cli.main(["attach", "review", "--cwd", "/work"]) == 0
    assert fake_sessions.calls == [("get_session", "review"), ("attach", session, Path("/work"))]
    assert capsys.readouterr().out.strip() == "wA:p9"


def test_attach_without_a_cwd_leaves_the_session_its_own_workdir(fake_sessions) -> None:
    """The tab belongs where the session works, not where the popup happened to open."""
    session = Session(session_id="s1", name="review")
    fake_sessions.registry.append(session)

    assert cli.main(["attach", "review"]) == 0
    assert fake_sessions.calls[1] == ("attach", session, None)


def test_attaching_to_a_session_that_is_gone_says_so(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["attach", "review"]) == 1
    assert names(fake_sessions.calls) == ["get_session"]
    assert "review" in capsys.readouterr().err


def test_launch_starts_a_session_from_a_saved_profile(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["launch", "claude-default"]) == 0

    call = fake_sessions.calls[0]
    assert call[0] == "launch"
    assert call[1] == load_profiles()["claude-default"]
    assert call[2] is None  # no name given, so sessions picks one
    assert capsys.readouterr().out.strip() == "wA:p3"


def test_launch_can_share_the_directory_it_is_run_from(fake_sessions) -> None:
    assert cli.main(["launch", "claude-default", "--cwd", "/work/repo"]) == 0

    assert fake_sessions.calls[0][1].shared_dir == "/work/repo"


def test_launch_with_no_such_profile_says_so(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["launch", "nope"]) == 1
    assert fake_sessions.calls == []
    assert "nope" in capsys.readouterr().err


def test_a_new_session_is_launched_with_the_name_that_was_typed(fake_sessions, chooser) -> None:
    chooser(tui.NewSession(profile=Profile(name="custom"), name="review"))

    assert cli.main(["choose"]) == 0
    assert fake_sessions.calls[0][2] == "review"


def test_answers_are_saved_as_a_profile_before_the_launch(
    fake_sessions, chooser, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chooser(tui.NewSession(profile=Profile(agent="codex"), save_as="review"))

    assert cli.main(["choose"]) == 0
    assert (config_dir / "profiles" / "review.json").is_file()
    assert fake_sessions.calls[0][1].name == "review"  # launched under the name it was saved as
    assert "review.json" in capsys.readouterr().err


def test_a_profile_name_that_will_not_save_is_reported_and_the_launch_goes_on(
    fake_sessions, chooser, capsys: pytest.CaptureFixture[str]
) -> None:
    chooser(tui.NewSession(profile=Profile(agent="codex"), save_as="../escape"))

    assert cli.main(["choose"]) == 0
    assert "not saved" in capsys.readouterr().err
    assert names(fake_sessions.calls) == ["launch"]


def test_a_typed_command_is_remembered_before_the_launch(
    fake_sessions, chooser, config_dir: Path
) -> None:
    profile = Profile(agent="wrapped")
    chooser(tui.NewSession(profile=profile, agent_command="npx claude-code"))

    assert cli.main(["choose"]) == 0

    entry = json.loads((config_dir / "agents" / "wrapped.json").read_text())
    assert entry["command"] == "npx claude-code"
    assert names(fake_sessions.calls) == ["launch"]


def test_a_typed_command_with_an_unusable_key_launches_nothing(
    fake_sessions, chooser, capsys: pytest.CaptureFixture[str]
) -> None:
    chooser(tui.NewSession(profile=Profile(agent="../escape"), agent_command="claude"))

    assert cli.main(["choose"]) == 1
    assert fake_sessions.calls == []
    assert "plain filename" in capsys.readouterr().err


def test_a_typed_command_that_would_overwrite_an_agent_launches_nothing(
    fake_sessions, chooser, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Better to ask for another name than to change what `claude` means from now on."""
    chooser(tui.NewSession(profile=Profile(agent="claude"), agent_command="claude --model opus"))

    assert cli.main(["choose"]) == 1
    assert fake_sessions.calls == []
    assert "already runs" in capsys.readouterr().err
    assert not (config_dir / "agents").exists()


def test_a_command_the_registry_already_runs_is_not_written_again(
    fake_sessions, chooser, capsys: pytest.CaptureFixture[str]
) -> None:
    chooser(tui.NewSession(profile=Profile(agent="claude"), agent_command="claude"))

    assert cli.main(["choose"]) == 0
    assert "remembered" not in capsys.readouterr().err
    assert names(fake_sessions.calls) == ["launch"]


def test_ctrl_c_leaves_no_traceback(fake_sessions, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(cwd: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(tui, "choose", interrupt)

    assert cli.main(["choose"]) == 130
    assert fake_sessions.calls == []


@pytest.mark.parametrize(
    "error",
    [
        HerdrError("herdr not found on PATH"),
        SrtNotFound("srt not found, and no npx to run it"),
        RuntimeError("the sandbox would not start"),
        ValueError("profile names an unknown agent"),
    ],
)
def test_a_launch_that_fails_says_why_instead_of_tracing_back(
    error: Exception, fake_sessions, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The popup closes with the process, so a traceback is never seen by anyone."""

    def fail(cwd: Path) -> None:
        raise error

    monkeypatch.setattr(tui, "choose", fail)

    assert cli.main(["choose"]) == 1
    assert capsys.readouterr().err.strip() == f"paddock: {error}"


# --- wiring paddock into herdr ---------------------------------------------


def test_init_hands_its_flags_to_the_init_module(
    fake_sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[bool, bool]] = []

    def record(dry_run: bool, undo: bool) -> int:
        asked.append((dry_run, undo))
        return 0

    monkeypatch.setattr(cli.init, "run", record)

    assert cli.main(["init", "--undo"]) == 0
    assert asked == [(False, True)]
    assert fake_sessions.calls == []


def test_init_that_will_not_touch_a_config_says_why(
    fake_sessions, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An init that refuses reaches the user as a message, not a traceback."""
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".config" / "herdr" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('keys.new_tab = "prefix+c"\n')

    assert cli.main(["init"]) == 1
    assert "[keys] table" in capsys.readouterr().err


# --- listing profiles ------------------------------------------------------


def test_profiles_prints_one_line_each(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["profiles"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == len(load_profiles())
    assert any(line.startswith("claude-default") and "claude" in line for line in lines)
    assert fake_sessions.calls == []


def test_a_profile_line_says_where_it_works_and_what_it_can_reach() -> None:
    lines = cli.profile_lines({"scratch": Profile(name="scratch", network_presets=[])})

    assert "isolated workdir" in lines[0]
    assert "no network" in lines[0]


# --- reading the log back --------------------------------------------------


def test_logs_takes_an_optional_session() -> None:
    assert cli.parse_args(["logs"]).ref == ""
    assert cli.parse_args(["logs", "review"]).ref == "review"


def test_logs_prints_the_path_and_the_end_of_the_file(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log.setup()
    log.get_logger("paddock.demo").info("a thing that happened")

    assert cli.main(["logs"]) == 0

    printed = capsys.readouterr().out
    assert str(log.log_path()) in printed
    assert "a thing that happened" in printed


def test_logs_shows_the_last_lines_only(capsys: pytest.CaptureFixture[str]) -> None:
    log.setup()
    for number in range(cli.TAIL_LINES + 10):
        log.get_logger("paddock.demo").info("line %s", number)

    cli.main(["logs"])

    printed = capsys.readouterr().out
    assert "line 9\n" not in printed
    assert "line 49" in printed


def test_logs_with_no_log_yet_says_where_it_will_be(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["logs"]) == 0

    assert str(log.log_path()) in capsys.readouterr().out


def test_logs_for_a_session_shows_that_run_and_its_pane_log(
    fake_sessions, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "pane.log").write_text("srt: sandbox setup failed\n")
    fake_sessions.registry.append(
        fake_sessions.Session(session_id="abc123", name="review", run_dir=str(run_dir))
    )

    assert cli.main(["logs", "review"]) == 0

    printed = capsys.readouterr().out
    assert "abc123" in printed and str(run_dir) in printed
    assert "srt: sandbox setup failed" in printed


def test_a_pane_log_is_shown_with_a_warning_that_it_is_not_paddocks(
    fake_sessions, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """paddock keeps secrets out of its own lines. It cannot promise that of the agent's."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "pane.log").write_text("agent: token abc\n")
    fake_sessions.registry.append(fake_sessions.Session(name="review", run_dir=str(run_dir)))

    cli.main(["logs", "review"])

    assert "the agent's own output" in capsys.readouterr().out


def test_paddocks_own_log_carries_no_such_warning(capsys: pytest.CaptureFixture[str]) -> None:
    log.setup()
    log.get_logger("paddock.demo").info("a thing that happened")

    cli.main(["logs"])

    assert "the agent's own output" not in capsys.readouterr().out


def test_logs_for_a_session_that_is_gone_says_so(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["logs", "nope"]) == 1

    assert "no session named 'nope'" in capsys.readouterr().err


# --- the stand-in still stands in ------------------------------------------


def calling_shape(function: object) -> list[tuple[str, object]]:
    return [(p.name, p.default) for p in inspect.signature(function).parameters.values()]


def test_the_fake_sessions_module_matches_the_real_one() -> None:
    """These tests only prove the CLI right while the stand-in has the real shape."""
    for name in [
        "list_sessions",
        "get_session",
        "create_session",
        "attach",
        "launch",
        "remove_pane",
        "launch_local",
    ]:
        real = getattr(sessions, name)
        fake = getattr(fake_sessions_module, name)
        assert calling_shape(fake) == calling_shape(real), name

    assert [field.name for field in fields(fake_sessions_module.Session)] == [
        field.name for field in fields(sessions.Session)
    ]
