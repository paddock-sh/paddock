"""The chooser's decisions, tested without a terminal: the questions are a thin shell."""

import json
import shutil
from pathlib import Path

import pytest
import questionary

from paddock import tui
from paddock.agents import AgentSpec, builtin_agents, load_agents
from paddock.profiles import (
    NETWORK_PRESETS,
    Profile,
    builtin_profiles,
    load_profiles,
    save_profile,
)
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
        # The choices each question offered, one list per question asked.
        self.offered: list[list[questionary.Choice]] = []

    def _next(self, message: str, **kwargs: object) -> FakeQuestion:
        self.asked.append(message)
        self.offered.append(list(kwargs.get("choices") or []))
        return FakeQuestion(self.answers.pop(0))

    select = checkbox = text = confirm = _next


def titles(rows: list[tuple[str, str, bool]]) -> list[str]:
    return [title for title, _, _ in rows]


def values(rows: list[tuple[str, str, bool]]) -> list[str]:
    return [value for _, value, _ in rows]


def ticks(rows: list[tuple[str, str, bool]]) -> dict[str, bool]:
    return {value: ticked for _, value, ticked in rows}


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
    """The point of the list is picking on agent, permissions and size (SPEC 3.1)."""
    session = Session(
        name="review", agent="claude", profile_name="hardened", pane_ids=["wA:p1", "wA:p2"]
    )

    assert tui.session_label(session) == "review [srt]: claude / hardened, 2 tabs"


def test_the_label_says_which_backend_the_session_runs_on() -> None:
    """Attaching means a different thing per backend, so the list says which (SPEC §3.2)."""
    label = tui.session_label(Session(name="build", backend="microsandbox"))

    assert label.startswith("build [microsandbox]: ")


def test_one_tab_is_not_two() -> None:
    assert tui.session_label(Session(name="solo", agent="codex", pane_ids=["wA:p1"])).endswith(
        ", 1 tab"
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


def test_tools_the_host_has_are_offered_and_ticked_from_the_profile(
    which: dict[str, str],
) -> None:
    rows = tui.tool_choices(Profile(tools=["git", "curl"]))

    assert values(rows) == ["git", "jq", "curl"]
    assert ticks(rows) == {"git": True, "jq": False, "curl": True}


def test_a_profiles_own_tools_are_offered_too(which: dict[str, str]) -> None:
    """Otherwise editing a profile that names something off the standard list drops it."""
    rows = tui.tool_choices(Profile(tools=["kubectl"]))

    assert ticks(rows)["kubectl"] is True


def test_a_tool_this_host_lacks_stays_ticked_and_says_so(which: dict[str, str]) -> None:
    """Editing a profile on a second machine must not quietly drop what it cannot see."""
    rows = tui.tool_choices(Profile(tools=["git", "curl"]))

    assert ticks(rows)["curl"] is True
    assert titles(rows)[-1] == "curl (not installed)"


def test_a_candidate_the_host_lacks_is_left_out(which: dict[str, str]) -> None:
    """Nobody needs a checklist of every tool they have never installed."""
    assert "docker" not in values(tui.tool_choices(Profile()))


def test_network_presets_are_pre_ticked_from_the_base_profile() -> None:
    rows = tui.network_choices(Profile(network_presets=["github"]))

    assert values(rows) == list(NETWORK_PRESETS)
    assert ticks(rows) == {name: name == "github" for name in NETWORK_PRESETS}


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

    rows = tui.skill_choices(agent, ["reviewer"])

    assert ticks(rows) == {"release": False, "reviewer": True}


def test_a_skill_already_chosen_stays_on_the_list(tmp_path: Path) -> None:
    """A skill the agent's directory no longer holds is still the user's answer."""
    (tmp_path / "agent-config" / "skills" / "release").mkdir(parents=True)
    agent = AgentSpec(command="claude", config_write_paths=[str(tmp_path / "agent-config")])

    rows = tui.skill_choices(agent, ["reviewer"])

    assert ticks(rows) == {"release": False, "reviewer": True}


def test_an_agent_with_no_skills_dir_is_fine(tmp_path: Path) -> None:
    """Most agents have no skills at all, and the question is skipped."""
    agent = AgentSpec(command="codex", config_write_paths=[str(tmp_path / "nothing-here")])

    assert tui.skill_choices(agent, []) == []


def test_an_agent_with_no_config_dirs_is_fine() -> None:
    assert tui.skill_choices(AgentSpec(command="sh"), []) == []


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


def test_answers_kept_as_they_were_keep_the_profiles_name() -> None:
    base = builtin_profiles()["claude-default"]

    profile = tui.build_profile(
        base,
        base.agent,
        base.tools,
        base.network_presets,
        base.extra_domains,
        base.skills,
        base.shared_dir,
    )

    assert profile == base


def test_changed_answers_do_not_keep_the_profiles_name() -> None:
    """A session claiming `claude-default` must be the permissions that profile describes."""
    base = builtin_profiles()["claude-default"]

    profile = tui.build_profile(base, "codex", base.tools, [], [], [], "")

    assert profile.name == "claude-default+custom"


def test_the_blank_start_is_already_custom() -> None:
    """No point calling it custom+custom."""
    profile = tui.build_profile(Profile(), "codex", ["git"], [], [], [], "")

    assert profile.name == "custom"


def test_a_typed_directory_is_resolved_against_the_popups_cwd(tmp_path: Path) -> None:
    """A relative answer means "here", and the sandbox settings need an absolute path."""
    cwd = (tmp_path / "here").resolve()
    cwd.mkdir()

    assert tui.resolve_shared_dir("repo", cwd) == str(cwd / "repo")
    assert tui.resolve_shared_dir("./repo", cwd) == str(cwd / "repo")
    assert tui.resolve_shared_dir("../sibling", cwd) == str(cwd.parent / "sibling")


def test_a_blank_directory_means_no_sharing(tmp_path: Path) -> None:
    """An isolated scratch workdir is the safe default, so blank must not mean the cwd."""
    assert tui.resolve_shared_dir("", tmp_path) == ""
    assert tui.resolve_shared_dir("   ", tmp_path) == ""


def test_an_absolute_or_home_directory_is_taken_as_written(tmp_path: Path) -> None:
    assert tui.resolve_shared_dir("/work/repo", tmp_path) == "/work/repo"
    assert tui.resolve_shared_dir("~/repo", tmp_path) == str((Path.home() / "repo").resolve())


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


def test_a_key_that_already_runs_something_else_is_refused(config_dir: Path) -> None:
    """Overwriting `claude` would strip its domains and credentials for every later launch."""
    with pytest.raises(ValueError, match="already runs"):
        tui.remember_agent("claude", "claude --model opus")

    assert not (config_dir / "agents").exists()
    assert load_agents()["claude"].api_domains == builtin_agents()["claude"].api_domains


def test_remembering_the_same_command_again_writes_nothing(config_dir: Path) -> None:
    """Re-running a saved profile's own agent is not a collision."""
    tui.remember_agent("wrapped", "npx claude-code")

    assert tui.remember_agent("wrapped", "npx claude-code") is None
    assert load_agents()["wrapped"].command == "npx claude-code"


def test_the_suggested_key_does_not_stand_on_a_registered_agent() -> None:
    registry = load_agents()

    assert tui.suggested_key("npx claude-code", registry) == "npx"
    assert tui.suggested_key("claude --model opus", registry) == "claude-custom"
    assert tui.suggested_key("/opt/bin/wrapper --flag", registry) == "wrapper"
    assert tui.suggested_key("", registry) == ""


# --- back navigation and the summary ---------------------------------------


class Steps:
    """Answers the questionnaire with no terminal: one answer per step, a tuple per visit.

    A step with nothing scripted answers SKIP, the way the chooser skips a question it has
    nothing to ask about. The last scripted answer stands when a step comes round again.
    """

    def __init__(self, **script: object) -> None:
        self.script = {
            step: list(answers) if isinstance(answers, tuple) else [answers]
            for step, answers in script.items()
        }
        self.asked: list[str] = []
        self.notices: list[str] = []

    def __call__(self, step: str, answers: dict[str, object]) -> object:
        self.asked.append(step)
        queue = self.script.get(step)
        if not queue:
            return tui.SKIP
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def notify(self, message: str) -> None:
        self.notices.append(message)


def test_back_returns_to_the_question_before_it() -> None:
    steps = Steps(
        profile=(tui.CUSTOM, "claude-default"), agent=(tui.BACK, "codex"), summary=tui.LAUNCH
    )

    answers = tui.collect(steps, steps.notify)

    assert steps.asked[:4] == ["profile", "agent", "profile", "agent"]
    assert answers["profile"] == "claude-default"
    assert answers["agent"] == "codex"


def test_going_back_steps_over_the_questions_that_were_not_asked() -> None:
    """Back from the tools list lands on the agent: the typed-command questions were skipped."""
    steps = Steps(
        profile=tui.CUSTOM, agent=("codex", "claude"), tools=(tui.BACK, ["git"]), summary=tui.LAUNCH
    )

    answers = tui.collect(steps, steps.notify)

    assert steps.asked[:8] == [
        "profile",
        "agent",
        "command",
        "remember_as",
        "tools",
        "remember_as",
        "command",
        "agent",
    ]
    assert answers["profile"] == tui.CUSTOM  # the earlier answer is still there
    assert answers["agent"] == "claude"


def test_answers_survive_a_back_then_forward_round_trip() -> None:
    common = {
        "profile": tui.CUSTOM,
        "agent": "codex",
        "tools": ["git"],
        "domains": "example.com",
        "name": "review",
        "save_as": "review-profile",
        "summary": tui.LAUNCH,
    }
    straight = Steps(network=["npm"], **common)
    detour = Steps(network=(tui.BACK, ["npm"]), **common)

    assert tui.collect(detour, detour.notify) == tui.collect(straight, straight.notify)
    assert detour.asked.count("tools") == 2


def test_the_summary_edits_one_answer_and_leaves_the_rest() -> None:
    steps = Steps(
        profile=tui.CUSTOM,
        agent="codex",
        tools=["git"],
        name=("review", "renamed"),
        summary=(tui.EDIT, tui.LAUNCH),
        edit="name",
    )

    answers = tui.collect(steps, steps.notify)

    assert answers["name"] == "renamed"
    assert (answers["agent"], answers["tools"]) == ("codex", ["git"])
    assert steps.asked.count("agent") == 1  # nothing else was asked again
    assert steps.notices == []


def test_editing_the_agent_asks_for_its_skills_again() -> None:
    """Skills belong to the agent whose config dir they came from, not to the answers."""
    steps = Steps(
        profile=tui.CUSTOM,
        agent=("claude", "codex"),
        skills=(["reviewer"], tui.SKIP),
        summary=(tui.EDIT, tui.LAUNCH),
        edit="agent",
    )

    answers = tui.collect(steps, steps.notify)

    assert "skills" not in answers
    assert steps.asked.count("skills") == 2
    assert len(steps.notices) == 1  # one line saying why


def test_an_edit_that_keeps_the_agent_asks_nothing_else() -> None:
    steps = Steps(
        profile=tui.CUSTOM,
        agent="codex",
        skills=["reviewer"],
        summary=(tui.EDIT, tui.LAUNCH),
        edit="agent",
    )

    answers = tui.collect(steps, steps.notify)

    assert answers["skills"] == ["reviewer"]
    assert steps.notices == []


def test_editing_the_profile_starts_the_answers_over_from_it(config_dir: Path) -> None:
    """Switching the base profile means that profile's values, not the ticks against the old one."""
    save_profile(
        Profile(
            name="hardened",
            agent="claude",
            tools=["git"],
            network_presets=["github"],
            extra_domains=["example.com"],
            shared_dir="/work/repo",
        )
    )
    steps = Steps(
        profile=(tui.CUSTOM, "hardened"),
        agent="claude",
        tools=["jq"],
        network=["npm"],
        domains="other.com",
        seed={"share": True, "directory": "/work/repo"},
        summary=(tui.EDIT, tui.LAUNCH),
        edit="profile",
    )

    answers = tui.collect(steps, steps.notify)
    plan = tui.build_session(tui.base_profile(load_profiles(), answers), answers)

    assert plan.profile == load_profiles()["hardened"]
    assert len(steps.notices) == 1  # one line saying the answers start from it now


def test_editing_the_profile_asks_none_of_its_answers_again(config_dir: Path) -> None:
    """The new profile answers them, so the summary comes straight back."""
    save_profile(Profile(name="hardened", agent="claude", tools=["git"]))
    steps = Steps(
        profile=(tui.CUSTOM, "hardened"),
        agent="claude",
        tools=["jq"],
        seed={"share": False},
        summary=(tui.EDIT, tui.LAUNCH),
        edit="profile",
    )

    answers = tui.collect(steps, steps.notify)

    assert steps.asked.count("tools") == 1
    assert "tools" not in answers  # forgotten, so the profile's own tools stand


def test_editing_the_share_answer_asks_about_the_directory_again() -> None:
    """Answering no must not leave the directory the earlier yes asked for behind."""
    steps = Steps(
        profile=tui.CUSTOM,
        agent="codex",
        share=(True, False),
        directory=("/work/repo", tui.SKIP),
        summary=(tui.EDIT, tui.LAUNCH),
        edit="share",
    )

    answers = tui.collect(steps, steps.notify)

    assert answers["share"] is False
    assert "directory" not in answers
    assert steps.notices == []


def test_cancelling_the_summary_ends_with_no_answers() -> None:
    steps = Steps(profile=tui.CUSTOM, agent="codex", summary=tui.CANCEL)

    assert tui.collect(steps, steps.notify) is None


def test_backing_out_of_the_first_question_leaves_the_questionnaire() -> None:
    """The first question of the popup is what is before the first question here."""
    steps = Steps(profile=tui.BACK)

    assert tui.collect(steps, steps.notify) is tui.BACK
    assert steps.asked == ["profile"]


def test_launching_from_the_summary_builds_the_plan_the_questions_described(
    tmp_path: Path,
) -> None:
    """The pin: the same answers as the linear flow, and the same plan out of them."""
    steps = Steps(
        profile=tui.CUSTOM,
        agent="codex",
        tools=["git"],
        network=["npm"],
        domains="example.com",
        share=True,
        directory=str(tmp_path.resolve()),
        name="review",
        save_as="review-profile",
        summary=tui.LAUNCH,
    )

    answers = tui.collect(steps, steps.notify)
    plan = tui.build_session(tui.base_profile(load_profiles(), answers), answers)

    assert plan == tui.NewSession(
        profile=Profile(
            agent="codex",
            tools=["git"],
            network_presets=["npm"],
            extra_domains=["example.com"],
            shared_dir=str(tmp_path.resolve()),
        ),
        name="review",
        save_as="review-profile",
    )


def test_the_summary_puts_every_choice_on_one_line() -> None:
    plan = tui.NewSession(
        profile=Profile(
            agent="wrapped",
            tools=["git"],
            network_presets=["npm"],
            extra_domains=["example.com"],
            skills=["reviewer"],
            shared_dir="/work/repo",
        ),
        name="review",
        save_as="review-profile",
        agent_command="npx claude-code",
    )

    assert tui.summary_lines(tui.CUSTOM, plan) == [
        "Window: new sandbox session",
        "Start from: Custom",
        "Agent: wrapped",
        "Command: npx claude-code",
        "Tools: git",
        "Network: npm + example.com",
        "Skills: reviewer",
        "Shared directory: /work/repo",
        "Session name: review",
        "Save as profile: review-profile",
    ]


def test_the_summary_says_what_was_left_out() -> None:
    plan = tui.NewSession(profile=Profile(agent="claude", tools=[], network_presets=[]))

    lines = tui.summary_lines("claude-default", plan)

    assert "Start from: claude-default" in lines
    assert "Tools: none" in lines
    assert "Network: none" in lines
    assert "Shared directory: none, an isolated workdir" in lines
    assert "Session name: (generated)" in lines
    assert "Save as profile: not saved" in lines
    assert not any(line.startswith("Command:") for line in lines)


def test_the_edit_list_offers_the_questions_that_were_asked() -> None:
    """A question the chooser skipped is not one to go back and change."""
    answers = {"name": "review", "agent": "codex", "profile": tui.CUSTOM}

    choices = tui.edit_choices(answers)

    assert [value for _, value in choices] == ["profile", "agent", "name"]
    assert [title for title, _ in choices] == ["Start from", "Agent", "Session name"]


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
    """No cwd: an attached tab belongs in the session's own workdir."""
    fake_sessions.registry.append(Session(session_id="s1", name="review"))
    monkeypatch.setattr(tui, "questionary", Scripted("attach", "s1"))

    assert tui.choose(tmp_path) == tui.Attach(ref="s1")


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
        tui.LAUNCH,  # the summary: launch it
    )
    monkeypatch.setattr(tui, "questionary", script)

    plan = tui.choose(tmp_path)

    assert plan == tui.NewSession(
        profile=Profile(
            agent="codex",
            tools=["git"],
            network_presets=["npm"],
            extra_domains=["example.com"],
            shared_dir=str(tmp_path.resolve()),
        ),
        name="review",
        save_as="review-profile",
    )
    assert script.asked[:-1] == [
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
    assert script.asked[-1].startswith("Ready to launch:")


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
            tui.LAUNCH,  # the summary
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
        "new", tui.CUSTOM, "claude", [], [], "", ["reviewer"], False, "", "", tui.LAUNCH
    )
    monkeypatch.setattr(tui, "questionary", script)

    plan = tui.choose(tmp_path)

    assert "Skills:" in script.asked
    assert plan.profile.skills == ["reviewer"]


def test_changing_the_agent_drops_the_profiles_skills(
    monkeypatch: pytest.MonkeyPatch, fake_sessions, which: dict[str, str], tmp_path: Path
) -> None:
    """Skills belong to the agent whose config dir they came from, not to the profile."""
    monkeypatch.setenv("HOME", str(tmp_path))
    save_profile(Profile(name="reviewing", agent="claude", skills=["reviewer"]))
    script = Scripted("new", "reviewing", "codex", [], [], "", False, "", "", tui.LAUNCH)
    monkeypatch.setattr(tui, "questionary", script)

    plan = tui.choose(tmp_path)

    assert "Skills:" not in script.asked
    assert plan.profile.skills == []


def test_the_tools_question_is_skipped_when_the_host_has_nothing_to_offer(
    monkeypatch: pytest.MonkeyPatch, fake_sessions, tmp_path: Path
) -> None:
    """An empty checklist is not a question, and the profile's tools stand."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    save_profile(Profile(name="bare", agent="codex", tools=[]))
    script = Scripted("new", "bare", "codex", [], "", False, "", "", tui.LAUNCH)
    monkeypatch.setattr(tui, "questionary", script)

    plan = tui.choose(tmp_path)

    assert "Tools on the sandbox PATH:" not in script.asked
    assert plan.profile.tools == []


def test_every_list_question_offers_a_way_back(
    monkeypatch: pytest.MonkeyPatch, fake_sessions, which: dict[str, str], tmp_path: Path
) -> None:
    """Back on the first questionnaire question is the popup's first question again."""
    monkeypatch.setenv("HOME", str(tmp_path))
    script = Scripted("new", tui.BACK, "local")
    monkeypatch.setattr(tui, "questionary", script)

    assert tui.choose(tmp_path) == tui.Local(cwd=str(tmp_path))
    assert script.asked == ["New window:", "Start from:", "New window:"]
    assert tui.BACK in [choice.value for choice in script.offered[1]]


def test_ticking_back_in_a_checklist_returns_to_the_question_before_it(
    monkeypatch: pytest.MonkeyPatch, fake_sessions, which: dict[str, str], tmp_path: Path
) -> None:
    """A checklist has no key to press for back, so the way back is an entry on the list."""
    monkeypatch.setenv("HOME", str(tmp_path))
    script = Scripted(
        "new",
        tui.CUSTOM,
        "codex",  # agent
        ["git", tui.BACK],  # tools: ticked, and back anyway
        "claude",  # the agent again
        ["git"],  # tools
        [],  # network presets
        "",  # extra domains
        False,  # share a directory?
        "",  # session name
        "",  # save the answers as
        tui.LAUNCH,
    )
    monkeypatch.setattr(tui, "questionary", script)

    plan = tui.choose(tmp_path)

    assert script.asked.count("Agent:") == 2
    assert plan.profile.agent == "claude"
    assert plan.profile.tools == ["git"]


def test_the_summary_sends_you_back_to_one_question(
    monkeypatch: pytest.MonkeyPatch, fake_sessions, which: dict[str, str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    answers = ["new", tui.CUSTOM, "codex", [], [], "", False, "", ""]
    script = Scripted(*answers, tui.EDIT, "name", "review", tui.LAUNCH)
    monkeypatch.setattr(tui, "questionary", script)

    plan = tui.choose(tmp_path)

    assert plan.name == "review"
    assert script.asked.count("Session name (blank to generate one):") == 2


def test_the_summary_swaps_the_base_profile_for_all_of_its_answers(
    monkeypatch: pytest.MonkeyPatch,
    fake_sessions,
    which: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Picking another profile at the summary hands over its tools, network and directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    save_profile(
        Profile(
            name="hardened",
            agent="claude",
            tools=["git"],
            network_presets=["github"],
            shared_dir=str(tmp_path),
        )
    )
    answers = ["new", tui.CUSTOM, "claude", ["jq"], [], "", False, "", ""]
    script = Scripted(*answers, tui.EDIT, "profile", "hardened", tui.LAUNCH)
    monkeypatch.setattr(tui, "questionary", script)

    plan = tui.choose(tmp_path)

    assert plan.profile == load_profiles()["hardened"]
    assert "hardened" in capsys.readouterr().err  # the one line saying why


def test_cancelling_the_summary_launches_nothing(
    monkeypatch: pytest.MonkeyPatch, fake_sessions, which: dict[str, str], tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    script = Scripted("new", tui.CUSTOM, "codex", [], [], "", False, "", "", tui.CANCEL)
    monkeypatch.setattr(tui, "questionary", script)

    assert tui.choose(tmp_path) is None
    assert fake_sessions.calls == [("list_sessions",)]
    assert any("Agent: codex" in message for message in script.asked)


def test_ctrl_c_at_the_summary_ends_the_chooser(
    monkeypatch: pytest.MonkeyPatch, fake_sessions, which: dict[str, str], tmp_path: Path
) -> None:
    """The summary is a question like any other: None means nothing was chosen."""
    monkeypatch.setenv("HOME", str(tmp_path))
    script = Scripted("new", tui.CUSTOM, "codex", [], [], "", False, "", "", None)
    monkeypatch.setattr(tui, "questionary", script)

    assert tui.choose(tmp_path) is None
    assert fake_sessions.calls == [("list_sessions",)]
