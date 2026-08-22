"""The `paddock` command: argv to an action, and exactly one thing done about it."""

import inspect
import json
from dataclasses import fields
from pathlib import Path

import pytest

from paddock import cli, log, recent, sessions, tui
from paddock.backends.srt import SrtNotFound
from paddock.herdr_client import HerdrError
from paddock.profiles import Profile, load_profiles, save_profile
from tests import fake_sessions as fake_sessions_module
from tests.fake_sessions import Session


@pytest.fixture
def terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal to draw the chooser in. Without one it refuses, which is its own test."""
    monkeypatch.setattr(cli, "has_terminal", lambda: True)


@pytest.fixture
def chooser(terminal, monkeypatch: pytest.MonkeyPatch):
    """Answer the popup with a fixed plan: the TUI itself is tested in test_tui.py."""

    def answer(plan: object | None):
        monkeypatch.setattr(tui, "choose", lambda cwd, answers=None, attach=False: plan)

    return answer


@pytest.fixture
def quiet_start(monkeypatch: pytest.MonkeyPatch) -> list[tui.NewSession]:
    """Swallow the progress screen, and record the plans it was drawn for."""
    drawn: list[tui.NewSession] = []
    monkeypatch.setattr(tui, "starting", drawn.append)
    return drawn


@pytest.fixture
def failure_screen(monkeypatch: pytest.MonkeyPatch):
    """Stand in for the screen a failed launch ends on, and say which button it comes back on."""
    shown: list[tuple[str, str]] = []

    def answer(*back: bool):
        replies = list(back)

        def show(message: str, log_path: str = "") -> bool:
            shown.append((message, log_path))
            return replies.pop(0) if replies else False

        monkeypatch.setattr(tui, "launch_failed", show)
        return shown

    return answer


def names(calls: list[tuple]) -> list[str]:
    return [call[0] for call in calls]


def rest(calls: list[tuple]) -> list[tuple]:
    """The calls besides the reconcile every session command starts with, tested on its own."""
    return [call for call in calls if call[0] != "reconcile"]


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


def test_version_prints_the_version_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    """`paddock --version` answers on the top-level parser, not rerouted into the chooser."""
    with pytest.raises(SystemExit) as exit:
        cli.main(["--version"])

    assert exit.value.code == 0
    assert capsys.readouterr().out.strip() == "paddock 0.2.0"


def test_each_subcommand_is_recognised() -> None:
    assert cli.parse_args(["choose"]).name == "choose"
    assert cli.parse_args(["profiles"]).name == "profiles"
    assert cli.parse_args(["init"]).name == "init"
    assert cli.parse_args(["launch", "claude-default"]).profile == "claude-default"
    assert cli.parse_args(["attach", "review"]).ref == "review"
    assert cli.parse_args(["gc"]).name == "gc"


def test_choose_takes_an_attach_flag() -> None:
    """What prefix+shift+s runs: the chooser, opened on the list of live sessions."""
    assert cli.parse_args(["choose", "--attach"]).attach is True
    assert cli.parse_args(["choose"]).attach is False


def test_the_attach_flag_reaches_the_chooser(
    fake_sessions, terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[bool] = []

    def record(cwd: Path, answers: dict | None = None, attach: bool = False) -> None:
        asked.append(attach)
        return None

    monkeypatch.setattr(tui, "choose", record)

    cli.main(["choose", "--attach"])

    assert asked == [True]


def test_a_failed_launch_comes_back_to_the_form_not_to_the_attach_list(
    fake_sessions, terminal, failure_screen, quiet_start, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The list was a way in. The second time round the answers are what matters."""
    asked: list[bool] = []

    def record(cwd: Path, answers: dict | None = None, attach: bool = False):
        asked.append(attach)
        return tui.NewSession(profile=Profile())

    monkeypatch.setattr(tui, "choose", record)
    monkeypatch.setattr(fake_sessions_module, "launch", raising(RuntimeError("no srt")))
    failure_screen(True, False)

    cli.main(["choose", "--attach"])

    assert asked == [True, False]


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
    assert rest(fake_sessions.calls) == [("launch_local", tmp_path)]
    assert capsys.readouterr().out.strip() == "wA:p1"


def test_backing_out_of_the_chooser_does_nothing(
    fake_sessions, chooser, capsys: pytest.CaptureFixture[str]
) -> None:
    chooser(None)

    assert cli.main(["choose"]) == 0
    assert rest(fake_sessions.calls) == []
    assert capsys.readouterr().out == ""


def test_the_chooser_opens_in_the_current_directory_by_default(
    fake_sessions, terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []

    def record(cwd: Path, answers: dict | None = None, attach: bool = False) -> tui.Local:
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
    assert rest(fake_sessions.calls) == [
        ("get_session", "review"),
        ("attach", session, Path("/work"), False),
    ]
    assert capsys.readouterr().out.strip() == "wA:p9"


def test_attach_without_a_cwd_leaves_the_session_its_own_workdir(fake_sessions) -> None:
    """The tab belongs where the session works, not where the popup happened to open."""
    session = Session(session_id="s1", name="review")
    fake_sessions.registry.append(session)

    assert cli.main(["attach", "review"]) == 0
    assert rest(fake_sessions.calls)[1] == ("attach", session, None, False)


def test_attaching_to_a_session_that_is_gone_says_so(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["attach", "review"]) == 1
    assert names(rest(fake_sessions.calls)) == ["get_session"]
    assert "review" in capsys.readouterr().err


def test_launch_starts_a_session_from_a_saved_profile(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["launch", "claude-default"]) == 0

    call = rest(fake_sessions.calls)[0]
    assert call[0] == "launch"
    assert call[1] == load_profiles()["claude-default"]
    assert call[2] is None  # no name given, so sessions picks one
    assert capsys.readouterr().out.strip() == "wA:p3"


def test_launch_can_ask_for_the_other_backend(fake_sessions) -> None:
    """Manual testing of msb until the chooser offers it."""
    assert cli.main(["launch", "offline-shell", "--backend", "msb"]) == 0

    assert rest(fake_sessions.calls)[0][3] == "msb"


def test_launch_is_an_srt_session_unless_another_backend_is_named(fake_sessions) -> None:
    assert cli.main(["launch", "offline-shell"]) == 0

    assert rest(fake_sessions.calls)[0][3] == "srt"


def test_a_dry_run_launch_names_a_backend_that_is_not_the_default(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["launch", "offline-shell", "--backend", "msb", "--dry-run"]) == 0

    assert "msb" in capsys.readouterr().out
    assert rest(fake_sessions.calls) == []


def test_launch_can_share_the_directory_it_is_run_from(fake_sessions) -> None:
    assert cli.main(["launch", "claude-default", "--cwd", "/work/repo"]) == 0

    assert rest(fake_sessions.calls)[0][1].shared_dir == "/work/repo"


def test_launch_with_no_such_profile_says_so(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["launch", "nope"]) == 1
    assert rest(fake_sessions.calls) == []
    assert "nope" in capsys.readouterr().err


def test_a_new_session_is_launched_with_the_name_that_was_typed(fake_sessions, chooser) -> None:
    chooser(tui.NewSession(profile=Profile(name="custom"), name="review"))

    assert cli.main(["choose"]) == 0
    assert rest(fake_sessions.calls)[0][2] == "review"


def test_answers_are_saved_as_a_profile_before_the_launch(
    fake_sessions, chooser, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chooser(tui.NewSession(profile=Profile(agent="codex"), save_as="review"))

    assert cli.main(["choose"]) == 0
    assert (config_dir / "profiles" / "review.json").is_file()
    # launched under the name it was saved as
    assert rest(fake_sessions.calls)[0][1].name == "review"
    assert "review.json" in capsys.readouterr().err


def test_a_profile_name_that_will_not_save_is_reported_and_the_launch_goes_on(
    fake_sessions, chooser, capsys: pytest.CaptureFixture[str]
) -> None:
    chooser(tui.NewSession(profile=Profile(agent="codex"), save_as="../escape"))

    assert cli.main(["choose"]) == 0
    assert "not saved" in capsys.readouterr().err
    assert names(rest(fake_sessions.calls)) == ["launch"]


def test_a_typed_command_is_remembered_before_the_launch(
    fake_sessions, chooser, config_dir: Path
) -> None:
    profile = Profile(agent="wrapped")
    chooser(tui.NewSession(profile=profile, agent_command="npx claude-code"))

    assert cli.main(["choose"]) == 0

    entry = json.loads((config_dir / "agents" / "wrapped.json").read_text())
    assert entry["command"] == "npx claude-code"
    assert names(rest(fake_sessions.calls)) == ["launch"]


def test_a_typed_command_with_an_unusable_key_launches_nothing(
    fake_sessions, chooser, capsys: pytest.CaptureFixture[str]
) -> None:
    chooser(tui.NewSession(profile=Profile(agent="../escape"), agent_command="claude"))

    assert cli.main(["choose"]) == 1
    assert rest(fake_sessions.calls) == []
    assert "plain filename" in capsys.readouterr().err


def test_a_typed_command_that_would_overwrite_an_agent_launches_nothing(
    fake_sessions, chooser, config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Better to ask for another name than to change what `claude` means from now on."""
    chooser(tui.NewSession(profile=Profile(agent="claude"), agent_command="claude --model opus"))

    assert cli.main(["choose"]) == 1
    assert rest(fake_sessions.calls) == []
    assert "already runs" in capsys.readouterr().err
    assert not (config_dir / "agents").exists()


def test_a_command_the_registry_already_runs_is_not_written_again(
    fake_sessions, chooser, capsys: pytest.CaptureFixture[str]
) -> None:
    chooser(tui.NewSession(profile=Profile(agent="claude"), agent_command="claude"))

    assert cli.main(["choose"]) == 0
    assert "remembered" not in capsys.readouterr().err
    assert names(rest(fake_sessions.calls)) == ["launch"]


def test_ctrl_c_leaves_no_traceback(
    fake_sessions, terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(cwd: Path, answers: dict | None = None, attach: bool = False) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(tui, "choose", interrupt)

    assert cli.main(["choose"]) == 130
    assert rest(fake_sessions.calls) == []


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
    error: Exception, fake_sessions, terminal, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The popup closes with the process, so a traceback is never seen by anyone."""

    def fail(cwd: Path, answers: dict | None = None, attach: bool = False) -> None:
        raise error

    monkeypatch.setattr(tui, "choose", fail)

    assert cli.main(["choose"]) == 1
    assert capsys.readouterr().err.strip() == f"paddock: {error}"


# --- a launch that fails in front of the popup ------------------------------


def raising(error: Exception):
    """A sessions.launch that will not launch."""

    def launch(*args: object, **kwargs: object) -> tuple:
        raise error

    return launch


def test_a_launch_that_fails_before_the_pane_gets_a_screen(
    fake_sessions, chooser, quiet_start, failure_screen, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The popup closes with the process, so stderr is a terminal nobody is looking at."""
    chooser(tui.NewSession(profile=Profile(), backend="msb"))
    monkeypatch.setattr(fake_sessions_module, "launch", raising(RuntimeError("no npm in there")))
    shown = failure_screen(False)  # Cancel

    assert cli.main(["choose"]) == 1
    assert [message for message, _ in shown] == ["no npm in there"]
    assert "no npm in there" not in capsys.readouterr().err


def test_the_screen_after_a_failure_names_the_log(
    fake_sessions, chooser, quiet_start, failure_screen, monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
) -> None:
    chooser(tui.NewSession(profile=Profile()))
    monkeypatch.setattr(fake_sessions_module, "launch", raising(RuntimeError("nope")))
    shown = failure_screen(False)

    cli.main(["choose"])

    assert shown[0][1] == str(state_dir / "logs" / "paddock.log")


def test_back_to_the_form_asks_again_with_every_answer_still_there(
    fake_sessions, terminal, quiet_start, failure_screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A minute of waiting must not cost the answers that were waited on."""
    plan = tui.NewSession(profile=Profile(agent="codex", tools=["git"]), name="review")
    asked: list[dict] = []

    def ask(cwd: Path, answers: dict | None = None, attach: bool = False) -> object:
        asked.append(dict(answers or {}))
        return plan if len(asked) == 1 else None

    monkeypatch.setattr(tui, "choose", ask)
    monkeypatch.setattr(fake_sessions_module, "launch", raising(RuntimeError("nope")))
    failure_screen(True)  # ← Back to the form

    assert cli.main(["choose"]) == 0
    assert asked[0] == {}
    assert asked[1]["agent"] == "codex"
    assert asked[1]["tools"] == ["git"]
    assert asked[1]["name"] == "review"


def test_a_failure_that_is_not_a_runtime_error_gets_the_screen_too(
    fake_sessions, chooser, quiet_start, failure_screen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A traceback into a popup that is closing is no more use than a message into it."""
    chooser(tui.NewSession(profile=Profile()))
    monkeypatch.setattr(fake_sessions_module, "launch", raising(KeyError("backend")))
    shown = failure_screen(False)

    assert cli.main(["choose"]) == 1
    assert len(shown) == 1


def test_the_no_terminal_paths_still_say_it_on_stderr(
    fake_sessions, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`paddock launch` has no popup to draw in, so it keeps the message it always had."""
    monkeypatch.setattr(fake_sessions_module, "launch", raising(RuntimeError("no npm in there")))

    assert cli.main(["launch", "claude-default"]) == 1
    assert capsys.readouterr().err.strip() == "paddock: no npm in there"


def test_what_the_launch_is_doing_is_on_the_screen_before_it_blocks(
    fake_sessions, chooser, capsys: pytest.CaptureFixture[str]
) -> None:
    """The minute an msb guest takes to install an agent used to be a blank popup."""
    chooser(tui.NewSession(profile=Profile(agent="claude"), name="review", backend="msb"))

    assert cli.main(["choose"]) == 0

    drawn = capsys.readouterr().err
    assert "Starting review" in drawn
    assert "installing claude in the guest" in drawn


def test_the_launch_command_draws_no_progress_screen(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    """It is a script's entry point, not a popup: its stdout is the pane id and nothing else."""
    assert cli.main(["launch", "claude-default"]) == 0
    assert "Starting" not in capsys.readouterr().err


def test_a_slow_launch_says_what_it_is_doing_before_it_blocks(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    """The chooser has a screen for this; the command line printed nothing for 22 seconds."""
    assert cli.main(["launch", "claude-default", "--backend", "msb"]) == 0

    said = capsys.readouterr().err
    assert "pulling the node:22-slim image" in said
    assert "installing claude in the guest" in said


def test_an_srt_launch_stays_quiet_because_it_starts_at_once(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["launch", "claude-default"]) == 0
    assert capsys.readouterr().err == ""


def test_a_dry_run_says_what_would_happen_and_announces_nothing(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["launch", "claude-default", "--backend", "msb", "--dry-run"]) == 0
    assert capsys.readouterr().err == ""


class Redirected:
    """A stream that is not a terminal, which is what a pipe or a file looks like."""

    def isatty(self) -> bool:
        return False


def test_a_redirected_screen_is_no_more_a_terminal_than_a_redirected_keyboard(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form would be written into the redirect, and the terminal left blank."""
    monkeypatch.setattr(cli.sys, "stdout", Redirected())

    assert cli.has_terminal() is False


def test_without_a_terminal_the_chooser_says_which_flag_does_the_job(
    fake_sessions, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The chooser draws a screen. With nowhere to draw it, say what to run instead."""
    monkeypatch.setattr(cli, "has_terminal", lambda: False)

    assert cli.main(["choose"]) == 1
    assert "paddock launch" in capsys.readouterr().err
    assert rest(fake_sessions.calls) == []


def test_the_backend_the_chooser_named_is_what_the_session_runs_on(
    fake_sessions, chooser, capsys: pytest.CaptureFixture[str]
) -> None:
    """SPEC 3.2: which sandbox runs it is a launch decision, not a profile field."""
    chooser(tui.NewSession(profile=Profile(), backend="msb"))

    assert cli.main(["choose"]) == 0
    assert rest(fake_sessions.calls) == [("launch", Profile(), None, "msb")]


# --- collecting sessions whose tabs are gone -------------------------------


def test_the_chooser_reconciles_before_it_lists_anything(
    fake_sessions, chooser, tmp_path: Path
) -> None:
    """A session whose last tab closed must not still be on offer to attach to."""
    chooser(tui.Local(cwd=str(tmp_path)))

    assert cli.main(["choose"]) == 0
    assert names(fake_sessions.calls) == ["reconcile", "launch_local"]


def test_launch_reconciles_first(fake_sessions) -> None:
    assert cli.main(["launch", "claude-default"]) == 0
    assert names(fake_sessions.calls) == ["reconcile", "launch"]


def test_attach_reconciles_first(fake_sessions) -> None:
    fake_sessions.registry.append(Session(session_id="s1", name="review"))

    assert cli.main(["attach", "review"]) == 0
    assert names(fake_sessions.calls) == ["reconcile", "get_session", "attach"]


def test_a_dry_run_collects_nothing(fake_sessions) -> None:
    """A dry run says what would happen. Destroying a microVM is not saying."""
    assert cli.main(["launch", "claude-default", "--dry-run"]) == 0
    assert fake_sessions.calls == []


def test_gc_reconciles_and_names_what_it_collected(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    """The explicit run, for a shell outside herdr or a session you think is stuck."""
    fake_sessions.collects.append(Session(name="review"))

    assert cli.main(["gc"]) == 0
    assert names(fake_sessions.calls) == ["reconcile", "collect_orphans", "collect_run_dirs"]
    assert "review" in capsys.readouterr().out


def test_gc_with_nothing_to_collect_says_so(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    """Saying nothing is what a command that did not run looks like, and gc is asked on a hunch."""
    assert cli.main(["gc"]) == 0
    assert capsys.readouterr().out == "paddock: nothing to collect\n"


def test_gc_that_collected_something_does_not_say_there_was_nothing(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_sessions.collects.append(Session(name="review"))

    assert cli.main(["gc"]) == 0
    assert "nothing to collect" not in capsys.readouterr().out


def test_gc_names_the_run_dirs_it_swept(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    """A launch that failed leaves a directory nothing else would ever remove."""
    fake_sessions.stale_runs.append(Path("/state/runs/20260822-000000-abcd"))

    assert cli.main(["gc"]) == 0
    said = capsys.readouterr().out
    assert "removed the orphaned run dir /state/runs/20260822-000000-abcd" in said


def test_gc_takes_no_arguments() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["gc", "review"])


def test_a_launch_is_what_the_chooser_opens_on_next_time(
    fake_sessions, chooser, config_dir: Path, state_dir: Path
) -> None:
    """The profile the answers stood on, so a changed launch still opens on the one it began as."""
    save_profile(Profile(name="hardened"))
    changed = Profile(name="hardened+custom", tools=["git"])
    chooser(tui.NewSession(profile=changed, started_from="hardened"))

    assert cli.main(["choose"]) == 0
    assert recent.last_profile() == "hardened"


def test_answers_that_stand_on_nothing_saved_are_not_opened_on(
    fake_sessions, chooser, state_dir: Path
) -> None:
    """Custom is not a profile anyone can come back to, so it is not remembered as one."""
    chooser(tui.NewSession(profile=Profile(), started_from=tui.CUSTOM))

    assert cli.main(["choose"]) == 0
    assert recent.last_profile() == ""


def test_saving_the_answers_is_what_the_next_popup_opens_on(
    fake_sessions, chooser, config_dir: Path, state_dir: Path
) -> None:
    chooser(tui.NewSession(profile=Profile(), save_as="review", started_from=tui.CUSTOM))

    assert cli.main(["choose"]) == 0
    assert recent.last_profile() == "review"


def test_a_session_told_to_keep_running_is_written_down_as_keeping_running(
    fake_sessions, chooser
) -> None:
    """SPEC 3.4's one field a caller sets on purpose, asked for under Advanced."""
    chooser(tui.NewSession(profile=Profile(), keep_alive=True))

    assert cli.main(["choose"]) == 0
    assert names(rest(fake_sessions.calls)) == ["launch", "set_keep_alive"]
    assert fake_sessions.calls[-1][2] is True


def test_a_session_that_ends_with_its_last_tab_is_left_alone(fake_sessions, chooser) -> None:
    chooser(tui.NewSession(profile=Profile()))

    assert cli.main(["choose"]) == 0
    assert names(rest(fake_sessions.calls)) == ["launch"]


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
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    log.setup()
    log.get_logger("paddock.demo").info("a thing that happened")

    assert cli.main(["logs"]) == 0

    printed = capsys.readouterr().out
    assert str(log.log_path()) in printed
    assert "a thing that happened" in printed


def test_logs_collects_dead_sessions_first_like_every_other_lookup(fake_sessions) -> None:
    """`logs` finds a session by ref, so it reconciles before it looks (SPEC §3.4)."""
    cli.main(["logs"])

    assert ("reconcile",) in fake_sessions.calls


def test_logs_shows_the_last_lines_only(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    log.setup()
    for number in range(cli.TAIL_LINES + 10):
        log.get_logger("paddock.demo").info("line %s", number)

    cli.main(["logs"])

    printed = capsys.readouterr().out
    assert "line 9\n" not in printed
    assert "line 49" in printed


def test_logs_with_no_log_yet_says_where_it_will_be(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_paddocks_own_log_carries_no_such_warning(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
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
        "set_keep_alive",
        "remove_pane",
        "reconcile",
        "launch_local",
    ]:
        real = getattr(sessions, name)
        fake = getattr(fake_sessions_module, name)
        assert calling_shape(fake) == calling_shape(real), name

    assert [field.name for field in fields(fake_sessions_module.Session)] == [
        field.name for field in fields(sessions.Session)
    ]


def test_a_dry_run_names_what_the_agent_will_add_to_the_path() -> None:
    """No launch adds a tool to the sandbox PATH without saying which and for whom."""
    plan = tui.NewSession(profile=Profile(agent="codex", tools=["git"]))

    assert "plus node, needed by codex" in cli.describe(plan)


def test_a_dry_run_says_nothing_extra_for_an_agent_that_needs_nothing() -> None:
    assert "needed by" not in cli.describe(tui.NewSession(profile=Profile(agent="claude")))


# --- a shell in a sandbox that is already running ---------------------------


def test_attach_shell_asks_sessions_for_a_shell(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    session = Session(session_id="s1", name="review")
    fake_sessions.registry.append(session)

    assert cli.main(["attach", "review", "--shell"]) == 0
    assert rest(fake_sessions.calls)[1] == ("attach", session, None, True)


def test_attach_without_the_flag_is_still_the_agent(fake_sessions) -> None:
    fake_sessions.registry.append(Session(session_id="s1", name="review"))

    assert cli.main(["attach", "review"]) == 0
    assert rest(fake_sessions.calls)[1][3] is False


def test_a_dry_run_says_which_of_the_two_it_would_open() -> None:
    shell = cli.describe(tui.Attach(ref="review", shell=True))
    agent = cli.describe(tui.Attach(ref="review"))

    assert "would attach a shell to session 'review'" in shell
    assert "would attach a tab to session 'review'" in agent


def test_the_chooser_can_ask_for_a_shell_too(fake_sessions, chooser) -> None:
    """The Open field's second question, carried through the same plan as everything else."""
    session = Session(session_id="s1", name="review")
    fake_sessions.registry.append(session)
    chooser(tui.Attach(ref="s1", shell=True))

    assert cli.main(["choose"]) == 0
    assert rest(fake_sessions.calls)[1] == ("attach", session, None, True)


def test_gc_names_the_orphans_it_swept(
    fake_sessions, capsys: pytest.CaptureFixture[str]
) -> None:
    """A launch nobody could roll back leaves a sandbox running, and gc is where that is said."""
    fake_sessions.orphans.append("paddock-20260822-000000-abcd")

    assert cli.main(["gc"]) == 0
    assert "removed the orphaned sandbox paddock-20260822-000000-abcd" in capsys.readouterr().out


def test_logs_names_both_of_a_sessions_logs_when_both_exist(
    fake_sessions, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A session with a shell tab has two stories, and the launch is not always in the agent's."""
    (tmp_path / "pane.log").write_text("the agent said this\n")
    (tmp_path / "shell.log").write_text("the shell said that\n")
    fake_sessions.registry.append(Session(name="review", run_dir=str(tmp_path)))

    assert cli.main(["logs", "review"]) == 0

    said = capsys.readouterr().out
    assert "the agent said this" in said
    assert "the shell said that" in said


def test_logs_names_only_the_log_a_session_kept(
    fake_sessions, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "shell.log").write_text("the shell said that\n")
    fake_sessions.registry.append(Session(name="review", run_dir=str(tmp_path)))

    assert cli.main(["logs", "review"]) == 0

    said = capsys.readouterr().out
    assert "shell.log" in said
    assert "pane.log" not in said


def test_a_session_with_no_log_yet_still_says_where_it_will_be(
    fake_sessions, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_sessions.registry.append(Session(name="review", run_dir=str(tmp_path)))

    assert cli.main(["logs", "review"]) == 0
    assert "pane.log" in capsys.readouterr().out


def test_an_empty_pane_log_says_what_empty_means(
    fake_sessions, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The script keeps stderr, and a healthy agent draws on stdout, so empty is the good case.

    Printing the path and then nothing at all is what a broken command looks like.
    """
    (tmp_path / "pane.log").write_text("")
    fake_sessions.registry.append(Session(name="review", run_dir=str(tmp_path)))

    assert cli.main(["logs", "review"]) == 0

    said = capsys.readouterr().out
    assert "nothing logged yet" in said
    assert cli.EMPTY_PANE_LOG in said


def test_a_pane_log_with_something_in_it_explains_nothing(
    fake_sessions, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "pane.log").write_text("the agent said this\n")
    fake_sessions.registry.append(Session(name="review", run_dir=str(tmp_path)))

    assert cli.main(["logs", "review"]) == 0
    assert cli.EMPTY_PANE_LOG not in capsys.readouterr().out


def test_the_help_describes_the_keys_init_actually_binds(
    capsys: pytest.CaptureFixture[str]
) -> None:
    """It named prefix+c, which is now the one key paddock gives back to herdr."""
    with pytest.raises(SystemExit):
        cli.parse_args(["--help"])

    said = capsys.readouterr().out
    assert "prefix+s" in said
    assert "prefix+c" not in said
