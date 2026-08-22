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


# --- the Open field --------------------------------------------------------


def test_local_new_and_every_live_session_are_on_one_list() -> None:
    """One field, so attaching is a pick and not a second screen."""
    without = [value for _, value in tui.open_choices([])]
    with_one = [value for _, value in tui.open_choices([Session(session_id="s1")])]

    assert without == ["new", "local"]
    assert with_one == ["new", "local", "s1"]


def test_a_session_on_the_open_list_is_shown_by_what_it_is() -> None:
    live = [Session(session_id="s1", name="review", agent="claude", pane_ids=["wA:p1"])]

    assert tui.open_choices(live)[-1][0] == "review: claude / claude-default, 1 tab"


def test_attaching_says_the_tabs_do_not_share_a_process_tree() -> None:
    """SPEC 3.2: with srt, attached tabs share files and policy, never a runtime."""
    assert "separate process tree" in tui.open_hint("s1")
    assert "No sandbox" in tui.open_hint("local")
    assert "sandbox" in tui.open_hint("new")


def test_a_session_is_shown_by_what_it_is() -> None:
    """The point of the list is picking on agent, permissions and size (SPEC 3.1)."""
    session = Session(
        name="review", agent="claude", profile_name="hardened", pane_ids=["wA:p1", "wA:p2"]
    )

    assert tui.session_label(session) == "review: claude / hardened, 2 tabs"


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


def test_the_edit_list_offers_the_questions_that_were_asked() -> None:
    """A question the chooser skipped is not one to go back and change."""
    answers = {"name": "review", "agent": "codex", "profile": tui.CUSTOM}

    choices = tui.edit_choices(answers, Profile())

    assert [value for _, value in choices] == [
        "profile",
        "agent",
        "tools",
        "network",
        "domains",
        "skills",
        "share",
        "name",
        "save_as",
    ]
    assert choices[0] == ("Start from", "profile")


def test_a_typed_command_is_editable_and_a_picked_agent_is_not() -> None:
    """The command and the key it is saved under only exist when one was typed."""
    typed = [value for _, value in tui.edit_choices({"agent": tui.CUSTOM}, Profile())]
    picked = [value for _, value in tui.edit_choices({"agent": "codex"}, Profile())]

    assert "command" in typed and "remember_as" in typed
    assert "command" not in picked and "remember_as" not in picked


def test_the_directory_is_editable_only_when_something_is_shared() -> None:
    shared = [value for _, value in tui.edit_choices({"share": True}, Profile())]
    isolated = [value for _, value in tui.edit_choices({}, Profile())]
    from_profile = [value for _, value in tui.edit_choices({}, Profile(shared_dir="/work/repo"))]

    assert "directory" in shared
    assert "directory" not in isolated
    assert "directory" in from_profile


def test_the_agent_is_still_editable_after_a_profile_swap() -> None:
    """The swap forgets the old answer, so the list has to offer what the profile now says."""
    settled = tui.settle({"profile": "hardened", "agent": "claude"}, "profile")

    assert "agent" in [value for _, value in tui.edit_choices(settled, Profile())]


def test_swapping_the_base_profile_takes_its_agent(config_dir: Path) -> None:
    """Another profile means starting over from it, and its agent is one of its answers."""
    save_profile(Profile(name="hardened", agent="codex"))
    settled = tui.settle({"profile": "hardened", "agent": "claude"}, "profile")

    plan = tui.build_session(tui.base_profile(load_profiles(), settled), settled)

    assert plan.profile.agent == "codex"


# --- the form's fields ------------------------------------------------------


def test_the_form_has_eight_fields_in_a_fixed_order() -> None:
    """Fixed, because the digits 1 to 8 jump to them and a reordering list would lie."""
    assert tui.FIELDS == (
        "open",
        "profile",
        "agent",
        "tools",
        "network",
        "files",
        "skills",
        "advanced",
    )


def test_every_field_has_a_label_a_title_and_a_hint() -> None:
    for field in tui.FIELDS:
        assert tui.FIELD_LABELS[field]
        assert tui.FIELD_TITLES[field]
        assert tui.FIELD_HINTS[field]


def test_a_hint_says_what_the_value_means_not_what_the_question_is() -> None:
    assert "refused by the OS" in tui.FIELD_HINTS["network"]
    assert "not reachable by name" in tui.FIELD_HINTS["tools"]
    assert "not a boundary" in tui.FIELD_HINTS["tools"]  # the honest trust model, in the UI


def test_the_profile_hint_quotes_the_title_the_form_will_show() -> None:
    base = builtin_profiles()["claude-default"]

    assert "+ changes" in tui.FIELD_HINTS["profile"]
    assert tui.form_title({"profile": "claude-default", "tools": []}, base).endswith("+ changes")


def test_every_box_inside_an_editor_carries_its_own_line() -> None:
    """The boxes the merges swallowed: they lost their screen, not their explanation."""
    assert "the sandbox can reach" in tui.EDITOR_HINTS["command"]
    assert "example.com" in tui.EDITOR_HINTS["also_allow"]
    assert "sbx:" in tui.EDITOR_HINTS["name"]
    assert "one pick next time" in tui.EDITOR_HINTS["save_as"]


def test_a_key_clash_is_worded_as_a_clash() -> None:
    """The key is never asked for, so the one time it is, it says why."""
    assert tui.key_clash("codex") == "codex already runs something else. Call this one:"


def test_a_profile_on_the_list_says_what_it_is_not_what_it_is_called() -> None:
    saved = {
        "review": Profile(
            name="review",
            agent="claude",
            tools=["git", "rg", "fd"],
            network_presets=["github"],
            shared_dir="~/dev",
        ),
        "offline": Profile(name="offline", agent="shell", tools=["git"], network_presets=[]),
    }
    registry = load_agents()

    assert tui.profile_hint("review", saved, registry) == (
        "Claude Code, 3 tools, github only, shares ~/dev"
    )
    assert tui.profile_hint("offline", saved, registry) == "Shell, 1 tool, no network"
    assert tui.profile_hint(tui.CUSTOM, saved, registry) == (
        "Starts from paddock's own defaults, not from nothing."
    )


def test_an_agent_on_the_list_says_what_it_reaches_whatever_you_tick() -> None:
    registry = load_agents()

    assert tui.agent_hint("claude", registry) == (
        "Runs claude in the sandbox. Reaches api.anthropic.com, *.anthropic.com "
        "whatever you tick."
    )
    assert "remembers it" in tui.agent_hint(tui.CUSTOM, registry)


def test_the_form_shows_one_row_per_field_with_the_profiles_own_answers() -> None:
    base = builtin_profiles()["claude-default"]

    rows = tui.form_rows({"profile": "claude-default"}, base, load_agents(), [])

    assert [label for label, _, _ in rows] == [tui.FIELD_LABELS[field] for field in tui.FIELDS]
    assert dict((label, value) for label, value, _ in rows) == {
        "Open": "New sandbox",
        "Profile": "claude-default",
        "Agent": "Claude Code (claude)",
        "Tools": "git rg fd jq curl node npm npx uv python3 (10)",
        "Network": "anthropic, github, npm, pypi/uv (12 domains)",
        "Files": "an isolated scratch directory",
        "Skills": "none",
        "Advanced": "name, save as profile, MCP",
    }


def test_a_shared_directory_and_a_session_name_show_on_the_form() -> None:
    answers = {"share": True, "directory": "/work/repo", "name": "review"}

    rows = dict((label, value) for label, value, _ in tui.form_rows(answers, Profile(), {}, []))

    assert rows["Files"] == "/work/repo"
    assert rows["Advanced"] == "review"


def test_a_local_tab_greys_out_everything_the_sandbox_decides() -> None:
    """Nothing is hidden and nothing moves, so the screen never rearranges under you."""
    rows = tui.form_rows({"open": "local"}, Profile(), load_agents(), [], cwd="/dev/paddock")
    values = dict((label, value) for label, value, _ in rows)
    hints = dict((label, hint) for label, _, hint in rows)

    greyed = {label for label, value in values.items() if value == "-"}

    assert values["Open"] == "Local tab"
    assert values["Files"] == "/dev/paddock"  # the tab still opens somewhere
    assert greyed == set(values) - {"Open", "Files"}
    assert hints["Network"] == tui.NO_SANDBOX


def test_attaching_names_the_session_and_keeps_its_workdir() -> None:
    live = [Session(session_id="s1", name="review")]

    shown = tui.form_rows({"open": "s1"}, Profile(), {}, live)
    rows = dict((label, value) for label, value, _ in shown)

    assert rows["Open"] == "Attach: review"
    assert rows["Files"] == "the session's own workdir"


def test_a_session_that_is_gone_says_so() -> None:
    """The registry can lose a session between listing it and drawing the form."""
    shown = tui.form_rows({"open": "s9"}, Profile(), {}, [])

    assert dict((label, value) for label, value, _ in shown)["Open"] == "session is gone: s9"


def test_the_title_names_the_profile_the_answers_stand_on() -> None:
    base = builtin_profiles()["claude-default"]

    assert tui.form_title({"profile": "claude-default"}, base) == "claude-default"
    assert tui.form_title({}, Profile()) == "Custom"


def test_the_title_says_when_the_answers_no_longer_match_the_profile() -> None:
    """A session that says it runs claude-default has to be what that profile describes."""
    base = builtin_profiles()["claude-default"]

    answers = {"profile": "claude-default", "tools": ["git"]}

    assert tui.form_title(answers, base) == "claude-default + changes"


# --- the rules, on a field change -------------------------------------------


def test_another_profile_hands_over_everything_it_carries() -> None:
    """Another profile means starting over from it, not keeping the old ticks against it."""
    answers = {
        "profile": "hardened",
        "agent": "codex",
        "tools": ["jq"],
        "network": ["npm"],
        "domains": "example.com",
        "skills": ["reviewer"],
        "share": True,
        "directory": "/work/repo",
        "name": "review",
        "save_as": "keep",
    }

    settled = tui.settle(answers, "profile")

    assert settled == {"profile": "hardened", "name": "review", "save_as": "keep"}


def test_settling_leaves_the_answers_it_was_given_alone() -> None:
    """The form keeps the answers; settle says what the new ones are."""
    answers = {"profile": "hardened", "tools": ["jq"]}

    tui.settle(answers, "profile")

    assert answers == {"profile": "hardened", "tools": ["jq"]}


def test_another_agent_drops_the_skills() -> None:
    """Skills come out of the agent's own config dir, so another agent's do not carry over."""
    answers = {"agent": "codex", "skills": ["reviewer"], "tools": ["git"]}

    assert tui.settle(answers, "agent") == {"agent": "codex", "tools": ["git"]}


def test_a_registered_agent_drops_the_command_typed_for_the_last_one() -> None:
    answers = {"agent": "codex", "command": "npx claude-code", "remember_as": "wrapped"}

    assert tui.settle(answers, "agent") == {"agent": "codex"}


def test_a_typed_command_keeps_the_command_it_was_given() -> None:
    answers = {"agent": tui.CUSTOM, "command": "npx claude-code", "remember_as": "wrapped"}

    assert tui.settle(answers, "agent") == answers


def test_sharing_nothing_drops_the_directory() -> None:
    """Answering "isolated" must not leave the directory an earlier "shared" asked for behind."""
    assert tui.settle({"share": False, "directory": "/work/repo"}, "files") == {"share": False}

    shared = {"share": True, "directory": "/work/repo"}
    assert tui.settle(shared, "files") == shared


def test_a_typed_directory_is_a_shared_one_even_with_nothing_else_answered() -> None:
    """The Files field is one answer: a directory is what "shared" means."""
    typed = {"directory": "/work/repo"}

    assert tui.settle(typed, "files", Profile()) == typed
    assert tui.build_session(Profile(), typed).profile.shared_dir == "/work/repo"


def test_an_unanswered_files_field_leaves_the_profiles_directory_alone() -> None:
    """Two questions became one field, so with neither answered the profile still stands."""
    base = Profile(shared_dir="/work/repo")

    assert tui.build_session(base, {}).profile.shared_dir == "/work/repo"
    assert tui.build_session(base, {"share": False}).profile.shared_dir == ""


def test_a_field_that_decides_nothing_else_settles_nothing() -> None:
    answers = {"tools": ["git"], "skills": ["reviewer"]}

    assert tui.settle(answers, "tools") == answers


# --- the confirm screen -----------------------------------------------------


def test_the_confirm_says_every_permission_out_loud() -> None:
    labels = [label for label, _ in tui.confirm_lines({}, Profile(), load_agents())]

    assert labels == [
        "session",
        "agent",
        "profile",
        "can write",
        "can read",
        "can reach",
        "can run",
        "can see",
    ]


def test_the_confirm_expands_the_presets_into_domains() -> None:
    """The form shows group names. This is the only screen that shows what they open."""
    base = builtin_profiles()["claude-default"]

    lines = dict(tui.confirm_lines({"profile": "claude-default"}, base, load_agents()))

    assert lines["can reach"].startswith("12 domains: ")
    assert "api.anthropic.com" in lines["can reach"]
    assert "pypi/uv" not in lines["can reach"]


def test_a_long_domain_list_is_cut_short_with_a_count() -> None:
    """Nine and a count fit the line. The whole list does not, and wrapping moves the layout."""
    base = builtin_profiles()["claude-default"]

    reach = dict(tui.confirm_lines({"profile": "claude-default"}, base, load_agents()))["can reach"]
    shown = reach.split(": ", 1)[1].split(", ")

    assert len(shown) == 10
    assert shown[-1] == "+3"


def test_the_confirm_folds_in_the_agents_own_domains() -> None:
    """The agent reaches its own API whatever is ticked, so the screen has to say so."""
    lines = dict(tui.confirm_lines({}, Profile(agent="codex", network_presets=[]), load_agents()))

    assert "api.openai.com" in lines["can reach"]


def test_the_confirm_says_an_offline_sandbox_is_offline() -> None:
    profile = Profile(agent="shell", network_presets=[])

    lines = dict(tui.confirm_lines({}, profile, load_agents()))

    assert lines["can reach"] == "nothing, this sandbox is offline"


def test_the_confirm_names_the_only_writable_path_of_yours() -> None:
    isolated = dict(tui.confirm_lines({}, Profile(), load_agents()))
    shared = dict(tui.confirm_lines({}, Profile(shared_dir="/work/repo"), load_agents()))

    assert isolated["can write"] == "its own workdir, /tmp and /dev/null. No path of yours."
    assert "/work/repo" in shared["can write"]


def test_the_confirm_says_which_reads_are_denied() -> None:
    lines = dict(tui.confirm_lines({}, Profile(), load_agents()))

    assert lines["can read"] == "your disk, except ~/.ssh ~/.aws ~/.gnupg ~/.config/gh"


def test_the_confirm_says_the_system_path_is_appended() -> None:
    with_path = dict(tui.confirm_lines({"tools": ["git"]}, Profile(), load_agents()))
    without = Profile(include_system_path=False)

    assert with_path["can run"] == "git, plus /usr/bin:/bin"
    assert dict(tui.confirm_lines({"tools": ["git"]}, without, load_agents()))["can run"] == "git"


def test_the_confirm_says_which_login_the_agent_gets() -> None:
    """The trust model in one line: its own credentials, never another agent's."""
    lines = dict(tui.confirm_lines({}, Profile(agent="claude"), load_agents()))

    assert lines["can see"] == "its own Claude Code login. No other agent's keys. No skills."


def test_the_confirm_says_the_session_name_and_the_typed_command() -> None:
    answers = {"agent": tui.CUSTOM, "command": "npx claude-code", "remember_as": "wrapped"}

    generated = dict(tui.confirm_lines({}, Profile(), load_agents()))
    typed = dict(tui.confirm_lines({**answers, "name": "review"}, Profile(), load_agents()))

    assert generated["session"] == "generated at launch"
    assert typed["session"] == "review"
    assert typed["agent"] == "wrapped, running npx claude-code"


def test_a_command_with_no_key_yet_still_reads_as_a_sentence() -> None:
    """The key is derived, so the confirm can be drawn before there is one."""
    answers = {"agent": tui.CUSTOM, "command": "npx claude-code"}

    lines = dict(tui.confirm_lines(answers, Profile(), load_agents()))

    assert lines["agent"] == "running npx claude-code"


def test_the_confirm_says_whether_the_answers_still_match_the_profile() -> None:
    base = builtin_profiles()["claude-default"]
    kept = {"profile": "claude-default"}

    unchanged = dict(tui.confirm_lines(kept, base, load_agents()))
    changed = dict(tui.confirm_lines({**kept, "tools": ["git"]}, base, load_agents()))

    assert unchanged["profile"] == "claude-default, unchanged"
    assert changed["profile"] == "claude-default + changes"


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
    monkeypatch.setattr(tui, "questionary", Scripted("s1"))

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
        "Open:",
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
    assert script.asked[-1].startswith("Launch this sandbox?")


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
    assert script.asked == ["Open:", "Start from:", "Open:"]
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
    assert any("Codex CLI (codex)" in message for message in script.asked)


def test_ctrl_c_at_the_summary_ends_the_chooser(
    monkeypatch: pytest.MonkeyPatch, fake_sessions, which: dict[str, str], tmp_path: Path
) -> None:
    """The summary is a question like any other: None means nothing was chosen."""
    monkeypatch.setenv("HOME", str(tmp_path))
    script = Scripted("new", tui.CUSTOM, "codex", [], [], "", False, "", "", None)
    monkeypatch.setattr(tui, "questionary", script)

    assert tui.choose(tmp_path) is None
    assert fake_sessions.calls == [("list_sessions",)]
