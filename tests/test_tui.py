"""The chooser's decisions, tested without a terminal: the questions are a thin shell."""

import json
import shutil
from pathlib import Path

import pytest
import questionary

from paddock import tui
from paddock.agents import AgentSpec, builtin_agents, load_agents
from paddock.profiles import NETWORK_PRESETS, Profile, builtin_profiles, load_profiles
from tests.fake_sessions import Session


class FakeQuestion:
    """What questionary.select() and friends return: something with .ask()."""

    def __init__(self, answer: object) -> None:
        self.answer = answer

    def ask(self) -> object:
        return self.answer


class Scripted:
    """questionary with the answers decided in advance, so no terminal is needed."""

    Choice = questionary.Choice

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def _next(self, message: str, **kwargs: object) -> FakeQuestion:
        self.asked.append(message)
        return FakeQuestion(self.answers.pop(0))

    select = checkbox = text = confirm = _next


@pytest.fixture
def which(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Control what the chooser finds on the host PATH."""
    found = {"git": "/usr/bin/git", "jq": "/usr/bin/jq", "kubectl": "/usr/bin/kubectl"}
    monkeypatch.setattr(shutil, "which", found.get)
    return found


# --- the first question ----------------------------------------------------


def test_attach_is_offered_only_when_a_session_exists() -> None:
    without = [value for _, value in tui.first_choices(has_sessions=False)]
    with_one = [value for _, value in tui.first_choices(has_sessions=True)]

    assert without == ["local", "new"]
    assert with_one == ["local", "new", "attach"]


def test_a_session_is_shown_by_what_it_is() -> None:
    """The point of the list is picking on agent and size, not remembering names."""
    session = Session(name="review", agent="claude", pane_ids=["wA:p1", "wA:p2"])

    assert tui.session_label(session) == "review — claude, 2 tabs"


def test_one_tab_is_not_two() -> None:
    assert tui.session_label(Session(name="solo", agent="codex", pane_ids=["wA:p1"])).endswith(
        "codex, 1 tab"
    )


def test_a_session_is_picked_by_its_id() -> None:
    live = [Session(session_id="s1", name="review"), Session(session_id="s2", name="build")]

    assert [value for _, value in tui.session_choices(live)] == ["s1", "s2"]


# --- the new-session questions ---------------------------------------------


def test_saved_profiles_are_offered_with_a_blank_start() -> None:
    choices = tui.profile_choices(load_profiles())

    values = [value for _, value in choices]
    assert set(builtin_profiles()) <= set(values)
    assert values[-1] == tui.CUSTOM


def test_every_registered_agent_is_offered_plus_a_typed_command() -> None:
    choices = tui.agent_choices(load_agents())

    values = [value for _, value in choices]
    assert set(builtin_agents()) <= set(values)
    assert values[-1] == tui.CUSTOM
    assert any("claude" in title for title, _ in choices)


def test_only_tools_the_host_has_are_offered(which: dict[str, str]) -> None:
    """A tool that is not installed cannot be symlinked into the shim dir anyway."""
    choices = tui.tool_choices(Profile(tools=["git", "curl"]))

    assert [name for name, _ in choices] == ["git", "jq"]
    assert dict(choices) == {"git": True, "jq": False}


def test_a_profiles_own_tools_are_offered_too(which: dict[str, str]) -> None:
    """Otherwise editing a profile that names something off the standard list drops it."""
    choices = tui.tool_choices(Profile(tools=["kubectl"]))

    assert dict(choices)["kubectl"] is True


def test_network_presets_are_pre_ticked_from_the_base_profile() -> None:
    choices = tui.network_choices(Profile(network_presets=["github"]))

    assert [name for name, _ in choices] == list(NETWORK_PRESETS)
    assert dict(choices) == {name: name == "github" for name in NETWORK_PRESETS}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", []),
        ("example.com", ["example.com"]),
        ("a.com, b.com", ["a.com", "b.com"]),
        ("  a.com   b.com,,c.com ", ["a.com", "b.com", "c.com"]),
        ("a.com a.com", ["a.com"]),
    ],
)
def test_typed_domains_are_split_on_commas_and_spaces(text: str, expected: list[str]) -> None:
    assert tui.parse_domains(text) == expected


def test_skills_come_from_the_agents_own_config_dir(tmp_path: Path) -> None:
    skills = tmp_path / "agent-config" / "skills"
    (skills / "reviewer").mkdir(parents=True)
    (skills / "release").mkdir()
    (skills / "notes.md").write_text("not a skill")
    agent = AgentSpec(command="claude", config_write_paths=[str(tmp_path / "agent-config")])

    choices = tui.skill_choices(agent, Profile(skills=["reviewer"]))

    assert dict(choices) == {"release": False, "reviewer": True}


def test_an_agent_with_no_skills_dir_is_fine(tmp_path: Path) -> None:
    """Most agents have no skills at all, and the question is skipped."""
    agent = AgentSpec(command="codex", config_write_paths=[str(tmp_path / "nothing-here")])

    assert tui.skill_choices(agent, Profile()) == []


def test_an_agent_with_no_config_dirs_is_fine() -> None:
    assert tui.skill_choices(AgentSpec(command="sh"), Profile()) == []


# --- answers to a Profile --------------------------------------------------


def test_the_answers_become_a_profile() -> None:
    profile = tui.build_profile(
        Profile(),
        agent="codex",
        tools=["git"],
        presets=["npm"],
        extra_domains=["example.com"],
        skills=["reviewer"],
        shared_dir="/work/repo",
    )

    assert profile.agent == "codex"
    assert profile.tools == ["git"]
    assert profile.network_presets == ["npm"]
    assert profile.extra_domains == ["example.com"]
    assert profile.skills == ["reviewer"]
    assert profile.shared_dir == "/work/repo"


def test_fields_the_chooser_does_not_ask_about_come_from_the_base_profile() -> None:
    """MCP servers, denied reads and the rest are not re-asked, and must not be dropped."""
    base = Profile(
        name="hardened",
        mcp=["playwright"],
        deny_read=["~/.ssh", "~/.kube"],
        include_system_path=False,
        extra_allow_write=["/var/tmp"],
    )

    profile = tui.build_profile(base, "claude", [], [], [], [], "")

    assert profile.mcp == ["playwright"]
    assert profile.deny_read == ["~/.ssh", "~/.kube"]
    assert profile.include_system_path is False
    assert profile.extra_allow_write == ["/var/tmp"]
    assert profile.name == "hardened"


# --- saving the answers ----------------------------------------------------


def test_saving_the_answers_writes_a_profile_and_renames_it(config_dir: Path) -> None:
    profile, message = tui.save_answers(Profile(agent="codex"), "review")

    assert profile.name == "review"
    assert (config_dir / "profiles" / "review.json").is_file()
    assert load_profiles()["review"].agent == "codex"
    assert str(config_dir / "profiles" / "review.json") in message


@pytest.mark.parametrize("name", ["sub/dir", "../escape", ".hidden"])
def test_a_name_the_profile_rules_refuse_is_reported_not_raised(
    name: str, config_dir: Path
) -> None:
    """A typo in the name must not cost the user the sandbox they just described."""
    original = Profile(agent="codex")

    profile, message = tui.save_answers(original, name)

    assert profile == original
    assert "not saved" in message
    assert name in message
    assert not (config_dir / "profiles").exists()


# --- a typed-in agent command ----------------------------------------------


def test_a_typed_command_is_remembered_as_an_agent(config_dir: Path) -> None:
    """A profile names a registry key, so a one-off command has to become one."""
    path = tui.remember_agent("wrapped", "npx claude-code")

    assert path == config_dir / "agents" / "wrapped.json"
    assert json.loads(path.read_text())["command"] == "npx claude-code"
    assert load_agents()["wrapped"].command == "npx claude-code"


@pytest.mark.parametrize("key", ["", "sub/dir", "../escape", ".hidden"])
def test_a_remembered_agent_needs_a_plain_name(key: str) -> None:
    with pytest.raises(ValueError, match="plain filename"):
        tui.remember_agent(key, "claude")


# --- the questionary shell -------------------------------------------------


def test_the_first_answer_is_enough_for_a_local_tab(
    monkeypatch: pytest.MonkeyPatch, fake_sessions, tmp_path: Path
) -> None:
    monkeypatch.setattr(tui.questionary, "select", lambda *a, **k: FakeQuestion("local"))

    assert tui.choose(tmp_path) == tui.Local(cwd=str(tmp_path))
    assert fake_sessions.calls == [("list_sessions",)]


def test_backing_out_of_a_question_ends_the_chooser(
    monkeypatch: pytest.MonkeyPatch, fake_sessions, tmp_path: Path
) -> None:
    """Ctrl-C and escape answer None. Nothing is launched and nothing is written."""
    monkeypatch.setattr(tui.questionary, "select", lambda *a, **k: FakeQuestion(None))

    assert tui.choose(tmp_path) is None
    assert fake_sessions.calls == [("list_sessions",)]


def test_an_existing_session_is_picked_from_the_list(
    monkeypatch: pytest.MonkeyPatch, fake_sessions, tmp_path: Path
) -> None:
    fake_sessions.registry.append(Session(session_id="s1", name="review"))
    monkeypatch.setattr(tui, "questionary", Scripted("attach", "s1"))

    assert tui.choose(tmp_path) == tui.Attach(ref="s1", cwd=str(tmp_path))


def test_the_whole_questionnaire_becomes_one_plan(
    monkeypatch: pytest.MonkeyPatch, fake_sessions, which: dict[str, str], tmp_path: Path
) -> None:
    """The order the popup asks in, and the answers it turns into."""
    monkeypatch.setenv("HOME", str(tmp_path))  # no skills, so that question is skipped
    script = Scripted(
        "new",  # what kind of window
        tui.CUSTOM,  # start from
        "codex",  # agent
        ["git"],  # tools
        ["npm"],  # network presets
        "example.com",  # extra domains
        True,  # share a directory?
        str(tmp_path),  # which one
        "review",  # session name
        "review-profile",  # save the answers as
    )
    monkeypatch.setattr(tui, "questionary", script)

    plan = tui.choose(tmp_path)

    assert plan == tui.NewSession(
        profile=Profile(
            agent="codex",
            tools=["git"],
            network_presets=["npm"],
            extra_domains=["example.com"],
            shared_dir=str(tmp_path),
        ),
        name="review",
        save_as="review-profile",
    )
    assert script.asked == [
        "New window:",
        "Start from:",
        "Agent:",
        "Tools on the sandbox PATH:",
        "Network:",
        "Extra domains (space separated):",
        "Share a host directory?",
        "Directory:",
        "Session name (blank to generate one):",
        "Save these answers as a profile (blank to skip):",
    ]


def test_a_typed_command_is_carried_in_the_plan(
    monkeypatch: pytest.MonkeyPatch, fake_sessions, which: dict[str, str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        tui,
        "questionary",
        Scripted(
            "new",
            tui.CUSTOM,  # start from
            tui.CUSTOM,  # agent: a command instead
            "npx claude-code",
            "wrapped",  # remember it as
            [],  # tools
            [],  # network presets
            "",  # extra domains
            False,  # share a directory?
            "",  # session name
            "",  # save the answers as
        ),
    )

    plan = tui.choose(tmp_path)

    assert plan.agent_command == "npx claude-code"
    assert plan.profile.agent == "wrapped"
    assert plan.profile.shared_dir == ""


def test_skills_are_asked_about_when_the_agent_has_some(
    monkeypatch: pytest.MonkeyPatch, fake_sessions, which: dict[str, str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "skills" / "reviewer").mkdir(parents=True)
    script = Scripted(
        "new", tui.CUSTOM, "claude", [], [], "", ["reviewer"], False, "", ""
    )
    monkeypatch.setattr(tui, "questionary", script)

    plan = tui.choose(tmp_path)

    assert "Skills:" in script.asked
    assert plan.profile.skills == ["reviewer"]
