"""The `paddock` command: argv to an action, and exactly one thing done about it."""

import json
from pathlib import Path

import pytest

from paddock import cli, tui
from paddock.profiles import Profile, load_profiles
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


def test_each_subcommand_is_recognised() -> None:
    assert cli.parse_args(["choose"]).name == "choose"
    assert cli.parse_args(["profiles"]).name == "profiles"
    assert cli.parse_args(["launch", "claude-default"]).profile == "claude-default"
    assert cli.parse_args(["attach", "review"]).ref == "review"


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


def test_ctrl_c_leaves_no_traceback(fake_sessions, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupt(cwd: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(tui, "choose", interrupt)

    assert cli.main(["choose"]) == 130
    assert fake_sessions.calls == []


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
