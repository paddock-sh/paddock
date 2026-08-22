"""The chooser's decisions, tested without a terminal: the questions are a thin shell."""

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from paddock import sessions, tui
from paddock.agents import AgentSpec, builtin_agents, load_agents
from paddock.profiles import (
    NETWORK_PRESETS,
    Profile,
    builtin_profiles,
    load_profiles,
    save_profile,
)
from tests.fake_sessions import Session


def titles(rows: list[tuple[str, str, bool]]) -> list[str]:
    return [title for title, _, _ in rows]


def values(rows: list[tuple[str, str, bool]]) -> list[str]:
    return [value for _, value, _ in rows]


def ticks(rows: list[tuple[str, str, bool]]) -> dict[str, bool]:
    return {value: ticked for _, value, ticked in rows}


@pytest.fixture
def which(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Control what the chooser finds on the host PATH.

    The agents are in it because the agent list refuses one this machine has not got, and
    what the form does must not depend on which agents the developer happens to have.
    """
    found = {
        "git": "/usr/bin/git",
        "jq": "/usr/bin/jq",
        "kubectl": "/usr/bin/kubectl",
        "claude": "/usr/bin/claude",
        "codex": "/usr/bin/codex",
        "opencode": "/usr/bin/opencode",
        "aider": "/usr/bin/aider",
        "gemini": "/usr/bin/gemini",
    }
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

    assert tui.open_choices(live)[-1][0] == "review [srt]: claude / claude-default, 1 tab"


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


def test_every_registered_agent_is_offered_plus_a_typed_command(which: dict[str, str]) -> None:
    choices = tui.agent_choices(load_agents())

    values = [value for _, value, _ in choices]
    assert set(builtin_agents()) <= set(values)
    assert values[-1] == tui.CUSTOM
    assert any("claude" in title for title, _, _ in choices)


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


# --- the form's fields ------------------------------------------------------


def test_the_form_has_a_field_per_thing_it_decides_in_a_fixed_order() -> None:
    """Fixed, because a digit jumps to each of them and a reordering list would lie."""
    assert tui.FIELDS == (
        "open",
        "profile",
        "backend",
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


def test_the_form_shows_one_row_per_field_with_the_profiles_own_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # whether this machine has skills is its own test
    base = builtin_profiles()["claude-default"]

    rows = tui.form_rows({"profile": "claude-default"}, base, load_agents(), [])

    assert [label for label, *_ in rows] == [tui.FIELD_LABELS[field] for field in tui.FIELDS]
    assert dict((label, value) for label, value, _, _ in rows) == {
        "Open": "New sandbox",
        "Profile": "claude-default",
        "Backend": "srt (instant, a policy sandbox)",
        "Agent": "Claude Code (claude)",
        "Tools": "git rg fd jq curl node npm npx uv python3",
        "Network": "anthropic, github, npm, pypi/uv",
        "Files": "an isolated scratch directory",
        "Skills": "none for this agent",
        "Advanced": "name, save as profile, MCP",
    }
    assert dict((label, note) for label, _, _, note in rows)["Tools"] == "(10)"
    assert dict((label, note) for label, _, _, note in rows)["Network"] == "(12 domains)"


def test_a_shared_directory_and_a_session_name_show_on_the_form() -> None:
    answers = {"share": True, "directory": "/work/repo", "name": "review"}

    rows = dict((label, value) for label, value, _, _ in tui.form_rows(answers, Profile(), {}, []))

    assert rows["Files"] == "/work/repo"
    assert rows["Advanced"] == "review"


def test_a_local_tab_greys_out_everything_the_sandbox_decides() -> None:
    """Nothing is hidden and nothing moves, so the screen never rearranges under you."""
    rows = tui.form_rows({"open": "local"}, Profile(), load_agents(), [], cwd="/dev/paddock")
    values = dict((label, value) for label, value, _, _ in rows)
    hints = dict((label, hint) for label, _, hint, _ in rows)

    greyed = {label for label, value in values.items() if value == "-"}

    assert values["Open"] == "Local tab"
    assert values["Files"] == "/dev/paddock"  # the tab still opens somewhere
    assert greyed == set(values) - {"Open", "Files"}
    assert hints["Network"] == tui.NO_SANDBOX


def test_attaching_names_the_session_and_keeps_its_workdir() -> None:
    live = [Session(session_id="s1", name="review")]

    shown = tui.form_rows({"open": "s1"}, Profile(), {}, live)
    rows = dict((label, value) for label, value, _, _ in shown)

    assert rows["Open"] == "Attach: review"
    assert rows["Files"] == "the session's own workdir"


def test_a_session_that_is_gone_says_so() -> None:
    """The registry can lose a session between listing it and drawing the form."""
    shown = tui.form_rows({"open": "s9"}, Profile(), {}, [])

    assert dict((label, value) for label, value, *_ in shown)["Open"] == "session is gone: s9"


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


def test_swapping_the_base_profile_takes_its_agent(config_dir: Path) -> None:
    """Another profile means starting over from it, and its agent is one of its answers."""
    save_profile(Profile(name="hardened", agent="codex"))
    settled = tui.settle({"profile": "hardened", "agent": "claude"}, "profile")

    plan = tui.build_session(tui.base_profile(load_profiles(), settled), settled)

    assert plan.profile.agent == "codex"


# --- the confirm screen -----------------------------------------------------


def test_the_confirm_says_every_permission_out_loud() -> None:
    labels = [label for label, _ in tui.confirm_lines({}, Profile(), load_agents())]

    assert labels == [
        "session",
        "backend",
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




# --- the chooser, driven by real key presses --------------------------------

# The keys, as the parser sees them. A digit jumps to a field and enter opens it.
#
# Escape needs a byte after it before the parser can be sure it is not the start of an arrow
# key, and the screen that is closing swallows that byte. So a screen left with escape in the
# middle of a run is sent two, and `k` moves up where an arrow would be eaten.
ESC, CTRL_C = "\x1b", "\x03"
DOWN, UP, TAB = "\x1b[B", "\x1b[A", "\t"
OPEN_FIELD, PROFILE, BACKEND, AGENT = "1\r", "2\r", "3\r", "4\r"
TOOLS, NETWORK, FILES, SKILLS, ADVANCED = "5\r", "6\r", "7\r", "8\r", "9\r"


def test_launching_what_the_form_already_says_is_one_key_press(
    press, fake_sessions, tmp_path: Path
) -> None:
    """The common case: the answers are already there, so Launch is the whole interaction."""
    plan = press("L", lambda: tui.choose(tmp_path))

    assert plan == tui.NewSession(profile=Profile(), backend="srt")
    assert fake_sessions.calls == [("list_sessions",)]  # it read the sessions and did nothing


def test_escape_on_the_form_closes_the_popup_with_no_plan(
    press, fake_sessions, tmp_path: Path
) -> None:
    assert press(ESC * 2, lambda: tui.choose(tmp_path)) is None
    assert fake_sessions.calls == [("list_sessions",)]


def test_ctrl_c_cancels_the_popup_wherever_it_is_pressed(
    press, fake_sessions, tmp_path: Path
) -> None:
    """cli.py turns that into the exit code 130, which is the convention fzf set."""
    with pytest.raises(KeyboardInterrupt):
        press(CTRL_C, lambda: tui.choose(tmp_path))
    with pytest.raises(KeyboardInterrupt):
        press(f"{TOOLS}{CTRL_C}", lambda: tui.choose(tmp_path))


def test_a_local_tab_is_two_key_presses_and_no_permissions(
    press, fake_sessions, tmp_path: Path
) -> None:
    plan = press(f"{OPEN_FIELD}{DOWN}\rL", lambda: tui.choose(tmp_path))

    assert plan == tui.Local(cwd=str(tmp_path))


def test_a_live_session_is_on_the_same_field_as_the_new_one(
    press, fake_sessions, tmp_path: Path
) -> None:
    """No cwd: an attached tab belongs in the session's own workdir."""
    fake_sessions.registry.append(Session(session_id="s1", name="review"))

    plan = press(f"{OPEN_FIELD}{DOWN}{DOWN}\rL", lambda: tui.choose(tmp_path))

    assert plan == tui.Attach(ref="s1")


def test_no_sandbox_means_none_of_the_fields_under_it_open(
    press, fake_sessions, tmp_path: Path
) -> None:
    """Greying them out is the promise, so pressing enter on one has to do nothing."""
    plan = press(f"{OPEN_FIELD}{DOWN}\r{TOOLS}L", lambda: tui.choose(tmp_path))

    assert plan == tui.Local(cwd=str(tmp_path))


def test_another_profile_hands_over_all_of_its_answers(
    press, fake_sessions, config_dir: Path, tmp_path: Path
) -> None:
    save_profile(Profile(name="hardened", agent="codex", tools=["git"], network_presets=[]))

    plan = press(f"{PROFILE}{UP}{UP}\rL", lambda: tui.choose(tmp_path))

    assert plan.profile == load_profiles()["hardened"]


def test_the_backend_is_a_field_and_says_what_each_one_costs(
    press, fake_sessions, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shutil, "which", {"msb": "/opt/bin/msb"}.get)

    plan = press(f"{BACKEND}{DOWN}\rL", lambda: tui.choose(tmp_path))

    assert plan.backend == "msb"


def test_a_backend_this_machine_cannot_run_is_not_chosen(
    press, fake_sessions, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """It stays on the list and says why, the way a tool the host lacks does."""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    plan = press(f"{BACKEND}{DOWN}\r{ESC}L", lambda: tui.choose(tmp_path))

    assert plan.backend == "srt"
    assert tui.backend_choices()[1][2].startswith("msb is not installed")


def test_every_backend_the_field_offers_is_one_sessions_can_actually_run() -> None:
    """The field names a backend and `sessions` looks it up, so a typo here is a dead launch.

    `tests/fake_sessions` stands in for the real module everywhere else in this file, which
    is why the registry is read from the real one here. The order is the field's own: the
    cheapest first, not whatever order the registry happens to be written in.
    """
    assert {key for key, _, _ in tui.backend_choices()} == set(sessions.BACKENDS)


def test_choosing_another_agent_drops_the_skills_that_came_with_the_last_one(
    press, fake_sessions, which: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    config_dir: Path, tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    save_profile(Profile(name="reviewing", agent="claude", skills=["reviewer"]))

    plan = press(f"{PROFILE}{UP}\r{AGENT}{DOWN}\rL", lambda: tui.choose(tmp_path))

    assert plan.profile.agent == "codex"
    assert plan.profile.skills == []


def test_a_typed_command_is_asked_for_on_the_agent_field(
    press, fake_sessions, tmp_path: Path
) -> None:
    """Three questions became one field: the agent, the command, and the key it is saved under."""
    keys = f"{AGENT}{DOWN * 5}\rnpx claude-code\rL"

    plan = press(keys, lambda: tui.choose(tmp_path))

    assert plan.agent_command == "npx claude-code"
    assert plan.profile.agent == "npx"


def test_the_tools_are_ticked_off_a_checklist(
    press, fake_sessions, which: dict[str, str], tmp_path: Path
) -> None:
    plan = press(f"{TOOLS} \rL", lambda: tui.choose(tmp_path))

    assert plan.profile.tools == ["rg", "curl"]  # git was ticked, and the space unticked it


def test_escape_from_an_editor_keeps_what_was_done_in_it(
    press, fake_sessions, which: dict[str, str], tmp_path: Path
) -> None:
    """The promise that the questionnaire could not keep: escape loses no answer."""
    plan = press(f"{TOOLS} {ESC}L", lambda: tui.choose(tmp_path))

    assert plan.profile.tools == ["rg", "curl"]


def test_the_network_groups_and_the_extra_domains_are_one_screen(
    press, fake_sessions, tmp_path: Path
) -> None:
    """Section 5.5: the box under the checklist is what killed the extra-domains question."""
    keys = f"{NETWORK} {TAB}example.com\rL"

    plan = press(keys, lambda: tui.choose(tmp_path))

    assert plan.profile.network_presets == ["github"]
    assert plan.profile.extra_domains == ["example.com"]


def test_sharing_a_directory_is_one_field(press, fake_sessions, tmp_path: Path) -> None:
    plan = press(f"{FILES}{DOWN}\r\rL", lambda: tui.choose(tmp_path))

    assert plan.profile.shared_dir == str(tmp_path.resolve())


def test_the_isolated_scratch_directory_is_the_other_answer_to_the_same_field(
    press, fake_sessions, config_dir: Path, tmp_path: Path
) -> None:
    save_profile(Profile(name="shared", shared_dir=str(tmp_path)))

    plan = press(f"{PROFILE}{UP}\r{FILES}{UP}\rL", lambda: tui.choose(tmp_path))

    assert plan.profile.shared_dir == ""


def test_a_directory_that_is_not_there_is_reported(tmp_path: Path) -> None:
    """The footer's other job. The chooser says so rather than launching into nothing."""
    assert tui.missing_directory("", tmp_path) == ""
    assert tui.missing_directory(".", tmp_path) == ""
    assert "no directory there" in tui.missing_directory("nowhere", tmp_path)


def test_the_skills_are_ticked_off_the_agents_own_list(
    press, fake_sessions, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "skills" / "reviewer").mkdir(parents=True)

    plan = press(f"{SKILLS} \rL", lambda: tui.choose(tmp_path))

    assert plan.profile.skills == ["reviewer"]


def test_the_session_name_lives_under_advanced(press, fake_sessions, tmp_path: Path) -> None:
    plan = press(f"{ADVANCED}\rreview\rL", lambda: tui.choose(tmp_path))

    assert plan.name == "review"


def test_the_s_key_saves_the_answers_as_a_profile(press, fake_sessions, tmp_path: Path) -> None:
    """A question every launch used to ask is a key press on the form now."""
    plan = press("sreview-profile\rL", lambda: tui.choose(tmp_path))

    assert plan.save_as == "review-profile"


def test_the_whole_form_becomes_one_plan(
    press, fake_sessions, which: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pin: every field answered, and the one plan they describe."""
    monkeypatch.setenv("HOME", str(tmp_path))  # no skills, so that field opens nothing
    keys = (
        f"{AGENT}{DOWN}\r"  # codex
        f"{TOOLS} \r"  # untick git
        f"{NETWORK} {TAB}example.com\r"  # github only, plus a domain
        f"{FILES}{DOWN}\r\r"  # share this directory
        f"{ADVANCED}\rreview\r"  # name it
        "sreview-profile\r"  # save the answers
        "L"
    )

    plan = press(keys, lambda: tui.choose(tmp_path))

    assert plan == tui.NewSession(
        profile=Profile(
            agent="codex",
            tools=["rg", "curl"],
            network_presets=["github"],
            extra_domains=["example.com"],
            shared_dir=str(tmp_path.resolve()),
        ),
        name="review",
        save_as="review-profile",
        backend="srt",
    )


def test_every_list_opens_on_the_answer_it_already_has(
    press, fake_sessions, which: dict[str, str], config_dir: Path, tmp_path: Path
) -> None:
    """Enter on a list you only meant to look at must not quietly change the answer."""
    save_profile(Profile(name="hardened", agent="codex", tools=["git"], network_presets=[]))

    opened = press(f"{OPEN_FIELD}{DOWN}\r{OPEN_FIELD}\rL", lambda: tui.choose(tmp_path))
    profile = press(f"{PROFILE}{UP}{UP}\r{PROFILE}\rL", lambda: tui.choose(tmp_path))
    agent = press(f"{AGENT}{DOWN}\r{AGENT}\rL", lambda: tui.choose(tmp_path))

    assert opened == tui.Local(cwd=str(tmp_path))
    assert profile.profile == load_profiles()["hardened"]
    assert agent.profile.agent == "codex"


def test_the_backend_list_opens_on_the_backend_that_was_chosen(
    press, fake_sessions, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reopening it and pressing enter reverted the microVM to srt, which is a silent downgrade."""
    monkeypatch.setattr(shutil, "which", {"msb": "/opt/bin/msb"}.get)

    plan = press(f"{BACKEND}{DOWN}\r{BACKEND}\rL", lambda: tui.choose(tmp_path))

    assert plan.backend == "msb"


def test_a_key_clash_offers_a_name_that_is_free(
    press, fake_sessions, config_dir: Path, tmp_path: Path
) -> None:
    """Offering the taken name back would fail the launch after the whole form was filled in."""
    tui.remember_agent("claude-custom", "some other wrapper")
    keys = f"{AGENT}{DOWN * 6}\rclaude --model opus\r\rL"

    plan = press(keys, lambda: tui.choose(tmp_path))

    assert plan.profile.agent == "claude-custom-2"
    assert plan.agent_command == "claude --model opus"


def test_a_free_key_is_one_the_registry_does_not_answer_to(config_dir: Path) -> None:
    registry = load_agents()

    assert tui.free_key("wrapped", registry) == "wrapped"
    assert tui.free_key("claude", registry) == "claude-2"


def test_saving_answers_needs_answers_to_save(press, fake_sessions, tmp_path: Path) -> None:
    """A local tab permits nothing, so there is nothing for the s key to write down."""
    plan = press(f"{OPEN_FIELD}{DOWN}\rsL", lambda: tui.choose(tmp_path))

    assert plan == tui.Local(cwd=str(tmp_path))


def test_the_open_list_rules_off_the_sessions_only_when_there_are_some() -> None:
    assert tui.open_rule([]) == -1
    assert tui.open_rule([Session(session_id="s1")]) == 1


def test_escape_from_the_directory_box_goes_back_to_the_files_list(
    press, fake_sessions, tmp_path: Path
) -> None:
    """One level at a time: the box was opened from the list, so escape lands on the list."""
    plan = press(f"{FILES}{DOWN}\r{ESC}{ESC}k\rL", lambda: tui.choose(tmp_path))

    assert plan.profile.shared_dir == ""


def test_the_skills_field_says_when_the_agent_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Section 4.4: a field with nothing to offer says so, rather than opening on nothing."""
    monkeypatch.setenv("HOME", str(tmp_path))
    bare = tui.form_rows({}, Profile(), load_agents(), [])

    (tmp_path / ".claude" / "skills" / "reviewer").mkdir(parents=True)
    offered = tui.form_rows({}, Profile(), load_agents(), [])

    assert dict((label, value) for label, value, _, _ in bare)["Skills"] == "none for this agent"
    assert dict((label, value) for label, value, _, _ in offered)["Skills"] == "none"


def test_taking_the_back_row_leaves_a_field_as_it_was(
    press, fake_sessions, config_dir: Path, tmp_path: Path
) -> None:
    """The row and the key are one answer: one level back, with every answer kept."""
    save_profile(Profile(name="hardened", agent="codex", tools=["git"], network_presets=[]))

    plan = press(f"{PROFILE}{UP}{UP}\r{PROFILE}{UP}{UP}\rL", lambda: tui.choose(tmp_path))

    assert plan.profile == load_profiles()["hardened"]


def test_the_back_row_on_a_checklist_keeps_the_ticks(
    press, fake_sessions, which: dict[str, str], tmp_path: Path
) -> None:
    plan = press(f"{TOOLS} {UP * 4}\rL", lambda: tui.choose(tmp_path))

    assert plan.profile.tools == ["rg", "curl"]


# --- an agent this machine has not got --------------------------------------


def test_an_agent_that_is_not_installed_is_offered_and_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hiding it would leave the user wondering where it went, and choosing it opens a dead tab."""
    monkeypatch.setattr(shutil, "which", {"claude": "/usr/bin/claude"}.get)

    titles = {value: title for title, value, _ in tui.agent_choices(load_agents())}
    refusals = {value: why for _, value, why in tui.agent_choices(load_agents())}

    assert titles["opencode"] == "OpenCode (opencode) (not installed)"
    assert refusals["opencode"].startswith("opencode is not installed")
    assert refusals["claude"] == ""
    assert titles["claude"] == "Claude Code (claude)"


def test_an_agent_run_by_a_path_of_its_own_is_never_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shell agent is `$SHELL`, which is the user's own answer to where it lives."""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    refusals = {value: why for _, value, why in tui.agent_choices(load_agents())}

    assert tui.agent_refusal("shell", load_agents()) == ""
    assert refusals["shell"] == ""
    assert refusals[tui.CUSTOM] == ""


def test_a_refused_agent_cannot_be_chosen_off_the_list(
    press, fake_sessions, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Enter on it says why instead of quietly launching a tab that dies on `not found`."""
    monkeypatch.setattr(shutil, "which", {"claude": "/usr/bin/claude"}.get)

    plan = press(f"{AGENT}{DOWN * 3}\r{ESC}L", lambda: tui.choose(tmp_path))

    assert plan.profile.agent == "claude"  # opencode was on the cursor and was not taken


# --- the warning an msb install earns ---------------------------------------


def msb_plan(**profile: object) -> tui.NewSession:
    return tui.NewSession(profile=Profile(agent="claude", **profile), backend="msb")


def test_an_msb_install_without_the_npm_preset_is_warned_about() -> None:
    """The install runs in the guest, where the profile's domains are the whole network."""
    plan = msb_plan(network_presets=["anthropic", "github"])

    assert tui.install_warning(plan, load_agents()) == tui.INSTALL_WARNING


def test_the_npm_preset_is_what_takes_the_warning_away() -> None:
    plan = msb_plan(network_presets=["anthropic", "npm"])

    assert tui.install_warning(plan, load_agents()) == ""


def test_nothing_is_warned_about_on_a_backend_that_installs_nothing() -> None:
    """srt runs the agent this machine already has, so there is no install to feed."""
    srt = tui.NewSession(profile=Profile(agent="claude", network_presets=[]))

    assert tui.install_warning(srt, load_agents()) == ""


def test_an_agent_with_no_install_is_not_warned_about() -> None:
    plan = tui.NewSession(profile=Profile(agent="shell", network_presets=[]), backend="msb")

    assert tui.install_warning(plan, load_agents()) == ""


def test_the_confirm_carries_the_warning_and_drops_it_when_it_is_answered() -> None:
    answers = {"backend": "msb", "agent": "claude", "network": ["anthropic"]}
    without = dict(tui.confirm_lines(answers, Profile(), load_agents()))
    with_npm = dict(
        tui.confirm_lines({**answers, "network": ["anthropic", "npm"]}, Profile(), load_agents())
    )

    assert without["warning"] == tui.INSTALL_WARNING
    assert "warning" not in with_npm


def test_the_profile_is_never_changed_to_suit_the_install() -> None:
    """What a sandbox may reach is an answer the user gives, not one paddock fills in."""
    answers = {"backend": "msb", "agent": "claude", "network": ["anthropic"]}

    tui.confirm_lines(answers, Profile(), load_agents())

    assert answers["network"] == ["anthropic"]


# --- what a launch says it is doing -----------------------------------------


def test_the_slow_steps_of_an_msb_launch_are_named_before_it_blocks_on_them() -> None:
    steps = tui.starting_lines(msb_plan(network_presets=["npm"]), load_agents())

    assert steps[0] == "pulling the node:22-slim image"
    assert steps[1] == "installing claude in the guest"
    assert "40 seconds" in steps[2]


def test_a_launch_that_will_fail_on_its_install_says_so_while_it_runs() -> None:
    steps = tui.starting_lines(msb_plan(network_presets=[]), load_agents())

    assert steps[-1] == tui.INSTALL_WARNING


def test_an_srt_launch_has_one_step_because_it_has_nothing_slow_to_do() -> None:
    assert tui.starting_lines(tui.NewSession(profile=Profile()), load_agents()) == [
        "preparing the sandbox"
    ]


# --- a plan, back to the answers that made it -------------------------------


def test_a_plan_goes_back_to_the_form_it_was_made_on() -> None:
    """A minute of waiting on a launch that failed must not cost the answers behind it."""
    plan = tui.NewSession(
        profile=Profile(
            agent="codex",
            tools=["git"],
            network_presets=["github"],
            extra_domains=["example.com"],
            shared_dir="/work/repo",
        ),
        name="review",
        save_as="reviewing",
        backend="msb",
    )

    answers = tui.answers_from(plan, load_profiles())

    assert tui.build_session(tui.base_profile(load_profiles(), answers), answers) == plan


def test_the_profile_a_plan_stood_on_is_read_back_off_its_name(config_dir: Path) -> None:
    save_profile(Profile(name="hardened", deny_read=["~/.ssh", "~/.kube"]))
    saved = load_profiles()
    plan = tui.NewSession(profile=replace(saved["hardened"], tools=["git"], name="hardened+custom"))

    answers = tui.answers_from(plan, saved)

    assert answers["profile"] == "hardened"
    # the fields the form never asks about come back with it, not reset to the defaults
    assert tui.build_session(tui.base_profile(saved, answers), answers).profile.deny_read == [
        "~/.ssh",
        "~/.kube",
    ]


def test_a_local_or_attached_tab_gives_back_the_one_answer_it_has() -> None:
    assert tui.answers_from(tui.Local(cwd="/tmp"), {}) == {"open": tui.LOCAL}
    assert tui.answers_from(tui.Attach(ref="s1"), {}) == {"open": "s1"}
