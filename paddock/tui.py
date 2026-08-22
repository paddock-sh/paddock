"""The chooser: what the popup asks, and the plan it hands back.

The screen is a thin shell. Everything that decides something (which sessions to
offer, which tools the host has, what a value means, the answers as a `Profile`)
is a plain function here, so it is tested without a terminal. One answers dict
holds the lot, `settle()` keeps it consistent when a field changes, and
`form_rows()` and `confirm_lines()` say what those answers mean. Nothing in this
module launches anything: `choose()` returns a plan and `cli.py` carries it out,
which is why backing out costs nothing.

The redesign in `docs/design/chooser-redesign.md` is landing in steps: the
fields, words and rules are here, while the shell still asks them one question
at a time.
"""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import questionary

from paddock import sessions
from paddock.agents import AgentSpec, agent_dir, load_agents
from paddock.profiles import (
    NETWORK_PRESETS,
    TOOL_CANDIDATES,
    Profile,
    load_profiles,
    save_profile,
)

# The "none of the saved ones" entry in the profile and agent lists. It is not a
# name anyone would give a file, so it cannot collide with a real key.
CUSTOM = "+custom"
# The "back" entry on every list question, and what a step answers when it is picked.
BACK = "+back"
# What a step answers when it has nothing to ask about, such as skills for an agent with none.
SKIP = "+skip"

# The new-session questions, in the order they are asked. One answer each, in one dict.
STEPS = (
    "profile",
    "agent",
    "command",
    "remember_as",
    "tools",
    "network",
    "domains",
    "skills",
    "share",
    "directory",
    "name",
    "save_as",
)

# What the summary screen and the edit list call each step.
STEP_LABELS = {
    "profile": "Start from",
    "agent": "Agent",
    "command": "Command",
    "remember_as": "Remember the command as",
    "tools": "Tools",
    "network": "Network presets",
    "domains": "Extra domains",
    "skills": "Skills",
    "share": "Share a host directory",
    "directory": "Shared directory",
    "name": "Session name",
    "save_as": "Save as profile",
}

# The two screens after the questions, the three answers the summary takes, and what a
# newly chosen base profile seeds the answers under it with.
SUMMARY, EDIT, SEED = "summary", "edit", "seed"
LAUNCH, CANCEL = "launch", "cancel"

# The steps whose answer settles others. Editing one from the summary asks these again; a new
# base profile hands its own values over instead, so it asks nothing. Tools are under neither,
# because they come off the host PATH, not out of the agent.
DEPENDENTS = {
    "profile": (),
    "agent": ("command", "remember_as", "skills"),
    "share": ("directory",),
}

# The two answers to the Open field that are not a session to attach to.
NEW, LOCAL = "new", "local"

# The fields of the form, in the order they are shown. Fixed, because the digits 1 to 8 jump
# to them and a list that reorders would make a digit a lie.
FIELDS = ("open", "profile", "agent", "tools", "network", "files", "skills", "advanced")

# What each field is called on the form.
FIELD_LABELS = {
    "open": "Open",
    "profile": "Profile",
    "agent": "Agent",
    "tools": "Tools",
    "network": "Network",
    "files": "Files",
    "skills": "Skills",
    "advanced": "Advanced",
}

# The heading of the editor that opens on a field.
FIELD_TITLES = {
    "open": "Open",
    "profile": "Profile",
    "agent": "Agent",
    "tools": "Tools it can run",
    "network": "Network access",
    "files": "Files",
    "skills": "Skills it can see",
    "advanced": "Advanced",
}

# One line under the field the cursor is on. Each says what the value showing means, not what
# the question is: "everything else is refused" tells you what happens, "Network:" does not.
FIELD_HINTS = {
    "open": "New sandbox: an agent under the OS sandbox. Local tab: an ordinary herdr tab "
    "with full access to this machine.",
    "profile": "Fills in everything below. Change anything and this session runs as the "
    "profile plus changes, not as the profile.",
    "agent": "The command that runs inside the sandbox. It gets its own login and no other "
    "agent's.",
    "tools": "Ticked binaries are on the sandbox PATH. Unticked ones are not reachable by "
    "name. An absolute path still runs them, so this is convenience, not a boundary.",
    "network": "Only these domains are reachable. Everything else is refused by the OS. Tick "
    "nothing for an offline sandbox.",
    "files": "Isolated: the sandbox gets a fresh scratch directory and no host path of yours "
    "is writable. Shared: that one directory is the only thing on your machine it can change.",
    "skills": "Unticked skills are not in the sandbox's config dir at all, so the agent "
    "cannot find them.",
    "advanced": "The session name, saving these answers as a profile, MCP servers, extra "
    "writable paths, denied reads and the system PATH.",
}

# The line under a field a local or attached tab greys out.
NO_SANDBOX = "No sandbox, so there is nothing to permit."

# One line per entry on the Open list.
OPEN_HINTS = {
    NEW: "An agent under the OS sandbox, with the permissions below. Seatbelt on macOS, "
    "bubblewrap on Linux.",
    LOCAL: "No sandbox. The agent can read and write anything you can.",
    "attach": "A second tab on a sandbox already running. Same files and same policy, "
    "separate process tree.",
}

# What a base profile carries, so picking another one hands over all of it.
PROFILE_CARRIES = (
    "agent",
    "command",
    "remember_as",
    "tools",
    "network",
    "domains",
    "skills",
    "share",
    "directory",
)

# Answers one step: a value, BACK, or SKIP when that step has nothing to ask about.
Asker = Callable[[str, dict], object]
# Tells the user something between two questions.
Notify = Callable[[str], None]


@dataclass
class Local:
    """Open an ordinary tab. No session, no sandbox."""

    cwd: str


@dataclass
class Attach:
    """Put a new tab on a session that is already running."""

    ref: str
    # Blank leaves the session its own workdir, which is what attaching usually means.
    cwd: str = ""


@dataclass
class NewSession:
    """Start a session from these answers."""

    profile: Profile
    # Session name. Blank lets sessions pick one.
    name: str = ""
    # Save the answers as a profile under this name. Blank saves nothing.
    save_as: str = ""
    # A command the user typed instead of picking an agent, remembered as `profile.agent`.
    agent_command: str = ""


Plan = Local | Attach | NewSession


class _Cancelled(Exception):
    """Ctrl-C or escape: questionary answers None, and the chooser stops there."""


def choose(cwd: Path) -> Plan | None:
    """Ask what to open. None means the user backed out, and nothing has happened."""
    try:
        return _choose(cwd)
    except _Cancelled:
        return None


# --- the questions ---------------------------------------------------------


def _choose(cwd: Path) -> Plan | None:
    live = sessions.list_sessions()
    while True:
        what = _ask(questionary.select("Open:", choices=_options(open_choices(live))))
        if what == LOCAL:
            return Local(cwd=str(cwd))
        if what != NEW:  # anything else on that list is a session to attach to
            return Attach(ref=str(what))
        plan = _new_session(cwd)
        if plan is not BACK:  # back out of the first question of the questionnaire: ask again
            return plan


def _new_session(cwd: Path) -> NewSession | str | None:
    """A plan, BACK to go back to the first question, or None if the summary was cancelled."""
    saved, registry = load_profiles(), load_agents()
    answers = collect(_asker(cwd, saved, registry), _notice)
    if not isinstance(answers, dict):
        return answers
    return build_session(base_profile(saved, answers), answers)


def _asker(cwd: Path, saved: dict[str, Profile], registry: dict[str, AgentSpec]) -> Asker:
    """One step to one question. The only part of the questionnaire that needs a terminal.

    An answer already given is what a question offers back, so going back and coming
    forward again changes nothing the user did not change.
    """

    def ask(step: str, answers: dict) -> object:
        base = base_profile(saved, answers)
        if step == "profile":
            return _pick("Start from:", profile_choices(saved), answers.get("profile"))
        if step == "agent":
            known = base.agent if base.agent in registry else None
            return _pick("Agent:", agent_choices(registry), answers.get("agent", known))
        if step == "command":
            if answers.get("agent") != CUSTOM:
                return SKIP
            return _type("Command to run in the sandbox:", str(answers.get("command", "")))
        if step == "remember_as":
            if answers.get("agent") != CUSTOM:
                return SKIP
            typed = str(answers.get("command", ""))
            suggestion = answers.get("remember_as") or suggested_key(typed, registry)
            return _type("Remember it as:", str(suggestion))
        if step == "tools":
            rows = tool_choices(base, answers.get("tools"))
            return _tick("Tools on the sandbox PATH:", rows) if rows else SKIP
        if step == "network":
            return _tick("Network:", network_choices(base, answers.get("network")))
        if step == "domains":
            typed = answers.get("domains", " ".join(base.extra_domains))
            return _type("Extra domains (space separated):", str(typed))
        if step == "skills":
            # Skills come out of the agent's own config dir, so another agent's do not carry over.
            agent = chosen_agent(answers, base)
            carried = base.skills if agent == base.agent else []
            rows = skill_choices(registry.get(agent, AgentSpec()), answers.get("skills", carried))
            return _tick("Skills:", rows) if rows else SKIP
        if step == "share":
            shares = bool(answers.get("share", bool(base.shared_dir)))
            return _ask(questionary.confirm("Share a host directory?", default=shares))
        if step == "directory":
            if not answers.get("share"):
                return SKIP
            typed = answers.get("directory") or base.shared_dir or str(cwd)
            return resolve_shared_dir(_type("Directory:", str(typed)), cwd)
        if step == "name":
            return _type("Session name (blank to generate one):", str(answers.get("name", "")))
        if step == "save_as":
            typed = str(answers.get("save_as", ""))
            return _type("Save these answers as a profile (blank to skip):", typed)
        if step == SUMMARY:
            lines = confirm_lines(answers, base, registry)
            rows = [f"{label:<10}{text}" for label, text in lines]
            message = "Launch this sandbox?\n  " + "\n  ".join(rows) + "\n"
            entries = [("Launch", LAUNCH), ("Edit a step", EDIT), ("Cancel", CANCEL)]
            return _ask(questionary.select(message, choices=_options(entries)))
        if step == EDIT:
            return _pick("Edit which step:", edit_choices(answers))
        if step == SEED:
            # Only what a forgotten answer cannot say for itself: with no directory answer,
            # nothing is shared, however the profile that now stands has it.
            seeded: dict = {"share": bool(base.shared_dir)}
            if base.shared_dir:
                seeded["directory"] = base.shared_dir
            return seeded
        raise ValueError(f"the chooser has no question for {step!r}")

    return ask


def _ask(question: questionary.Question) -> object:
    answer = question.ask()
    if answer is None:
        raise _Cancelled
    return answer


def _pick(message: str, pairs: list[tuple[str, str]], default: object = None) -> object:
    """A list question, with the way back on the end of the list."""
    choices = _options([*pairs, ("← Back", BACK)])
    return _ask(questionary.select(message, choices=choices, default=default))


def _tick(message: str, rows: list[tuple[str, str, bool]]) -> object:
    """A checklist. There is no key for back, so back is a tick, and it wins over the others."""
    answer = _ask(questionary.checkbox(message, choices=_ticks([*rows, ("← Back", BACK, False)])))
    return BACK if BACK in answer else answer


def _type(message: str, default: str = "") -> str:
    """A typed answer. Nothing to put a back entry on: the summary is how these are changed."""
    return str(_ask(questionary.text(message, default=default))).strip()


def _notice(message: str) -> None:
    """A line the user needs between two questions. stderr, because stdout carries the pane id."""
    print(f"paddock: {message}", file=sys.stderr)


def _options(pairs: list[tuple[str, str]]) -> list[questionary.Choice]:
    return [questionary.Choice(title, value=value) for title, value in pairs]


def _ticks(rows: list[tuple[str, str, bool]]) -> list[questionary.Choice]:
    return [questionary.Choice(title, value=value, checked=on) for title, value, on in rows]


# --- the questionnaire as a state machine ----------------------------------


def collect(ask: Asker, notify: Notify) -> dict | str | None:
    """The new-session questions over one answers dict, so any of them can be asked again.

    `ask` answers one step, or BACK to go back to the step before it, or SKIP when that step
    has nothing to ask about. Returns the answers to launch, BACK if the user backed out of
    the first step, or None if they cancelled at the summary.
    """
    answers: dict = {}
    if not _walk(ask, answers):
        return BACK
    while True:
        choice = ask(SUMMARY, answers)
        if choice == CANCEL:
            return None
        if choice == LAUNCH:
            return answers
        step = ask(EDIT, answers)
        if step != BACK:
            _edit(ask, notify, answers, str(step))


def _walk(ask: Asker, answers: dict) -> bool:
    """Ask every step in turn. False means the user backed out of the first one."""
    index, going_back = 0, False
    while 0 <= index < len(STEPS):
        step = STEPS[index]
        value = ask(step, answers)
        if value == SKIP:  # nothing to ask about, so carry on the way we were going
            answers.pop(step, None)
            index += -1 if going_back else 1
        elif value == BACK:
            going_back = True
            index -= 1
        else:
            _answer(answers, step, value)
            going_back = False
            index += 1
    return index >= 0


def _edit(ask: Asker, notify: Notify, answers: dict, step: str) -> None:
    """Ask one step again from the summary, and settle whatever its new answer decides."""
    value = ask(step, answers)
    if value in (BACK, SKIP) or not _answer(answers, step, value):
        return
    if step == "profile":
        # Another base profile means starting over from it, not keeping the old ticks against
        # it. Its own values are what stand, so none of them is asked for again.
        answers.update(ask(SEED, answers))
        notify(f"the answers now start from {profile_label(str(value))}, so its values stand")
        return
    if step == "agent":
        notify("the agent changed, so its own skills are asked for again")
    for dependent in DEPENDENTS[step]:
        answer = ask(dependent, answers)
        if answer == SKIP:
            answers.pop(dependent, None)
        elif answer != BACK:
            _answer(answers, dependent, answer)


def _answer(answers: dict, step: str, value: object) -> bool:
    """Keep one answer, settled. True when it changed a step that others depend on."""
    changed = step in DEPENDENTS and answers.get(step, value) != value
    answers[step] = value
    if changed:
        # The walk's `share` step is the form's Files field.
        kept = settle(answers, "files" if step == "share" else step)
        answers.clear()
        answers.update(kept)
    return changed


def settle(answers: dict, field: str) -> dict:
    """The answers after `field` changed, with whatever that field decides forgotten.

    The rules the questionnaire had, fired on a field change instead of during a walk.
    Another base profile means starting over from it, not keeping the old ticks against it.
    Skills and a typed command come out of the agent, so another agent's do not carry over.
    Sharing nothing leaves no directory behind. A forgotten answer is not lost work: the
    profile or the agent that now stands answers it.
    """
    settled = dict(answers)
    if field == "profile":
        for carried in PROFILE_CARRIES:
            settled.pop(carried, None)
    elif field == "agent":
        settled.pop("skills", None)
        if settled.get("agent") != CUSTOM:  # a registered agent brings its own command
            settled.pop("command", None)
            settled.pop("remember_as", None)
    elif field == "files" and not settled.get("share"):
        settled.pop("directory", None)
    return settled


def edit_choices(answers: dict) -> list[tuple[str, str]]:
    """The steps that were asked, in the order they were asked. A skipped one is not editable."""
    return [(STEP_LABELS[step], step) for step in STEPS if step in answers]


def profile_label(key: str) -> str:
    """What a profile answer is called on screen. The blank start is Custom, not a key."""
    return "Custom" if key == CUSTOM else key


# --- the form and the confirm ----------------------------------------------


def form_title(answers: dict, base: Profile) -> str:
    """What the form calls the answers: the profile they stand on, and whether they match it.

    A session that says it runs `claude-default` has to be the permissions that profile
    describes, so anything changed is said out loud in the title.
    """
    name = profile_label(str(answers.get("profile", CUSTOM)))
    built = build_session(base, answers).profile
    return name if replace(built, name=base.name) == base else f"{name} + changes"


def form_rows(
    answers: dict,
    base: Profile,
    registry: dict[str, AgentSpec],
    live: list[sessions.Session] | None = None,
    cwd: str = "",
) -> list[tuple[str, str, str]]:
    """The form: a label, the value showing and the hint for it, one row per field.

    A local or an attached tab greys out everything the sandbox fields decide, because none
    of it applies. Nothing is hidden and nothing moves, so the screen never rearranges.
    """
    opened = str(answers.get("open", NEW))
    values = _field_values(answers, base, registry, live or [], cwd)
    rows = []
    for field in FIELDS:
        if opened != NEW and field not in ("open", "files"):
            rows.append((FIELD_LABELS[field], "-", NO_SANDBOX))
        else:
            rows.append((FIELD_LABELS[field], values[field], FIELD_HINTS[field]))
    return rows


def confirm_lines(
    answers: dict, base: Profile, registry: dict[str, AgentSpec]
) -> list[tuple[str, str]]:
    """The resolved policy, for the last screen before anything happens.

    The form shows group names; this is the only screen that shows the domains they open,
    the agent's own domains folded in and the paths as they will be.
    """
    plan = build_session(base, answers)
    profile = plan.profile
    agent = agent_title(profile.agent, registry)
    if plan.agent_command:
        agent = f"{profile.agent}, running {plan.agent_command}"
    return [
        ("session", plan.name or "generated at launch"),
        ("agent", agent),
        ("profile", _profile_line(answers, base)),
        ("can write", _writable(profile)),
        ("can read", _readable(profile)),
        ("can reach", _reachable(profile)),
        ("can run", _runnable(profile)),
        ("can see", _visible(profile, registry)),
    ]


def _field_values(
    answers: dict,
    base: Profile,
    registry: dict[str, AgentSpec],
    live: list[sessions.Session],
    cwd: str,
) -> dict[str, str]:
    """What each field reads as. Every value says the answer, not the question."""
    plan = build_session(base, answers)
    profile = plan.profile
    opened = str(answers.get("open", NEW))
    tools = " ".join(profile.tools)
    return {
        "open": _open_value(opened, live),
        "profile": profile_label(str(answers.get("profile", CUSTOM))),
        "agent": agent_title(profile.agent, registry),
        "tools": f"{tools} ({len(profile.tools)})" if tools else "none",
        "network": _network_value(profile),
        "files": _files_value(opened, profile, cwd),
        "skills": " ".join(profile.skills) or "none",
        "advanced": _advanced_value(plan),
    }


def _open_value(opened: str, live: list[sessions.Session]) -> str:
    if opened == LOCAL:
        return "Local tab"
    for session in live:
        if session.session_id == opened:
            return f"Attach: {session.name}"
    return "New sandbox"


def _network_value(profile: Profile) -> str:
    """The groups ticked, and how many domains they open once the agent's own are folded in."""
    domains = profile.allowed_domains()
    if not domains:
        return "none, an offline sandbox"
    return f"{', '.join(profile.network_presets) or 'none'} ({_counted(domains)})"


def _counted(domains: list[str]) -> str:
    return f"{len(domains)} domain" + ("" if len(domains) == 1 else "s")


def _files_value(opened: str, profile: Profile, cwd: str) -> str:
    if opened == LOCAL:
        return cwd or "this directory"
    if opened != NEW:
        return "the session's own workdir"
    return profile.shared_dir or "an isolated scratch directory"


def _advanced_value(plan: NewSession) -> str:
    """What has been set under Advanced, or what lives there when nothing has."""
    set_here = [plan.name, f"saved as {plan.save_as}" if plan.save_as else ""]
    return ", ".join(part for part in set_here if part) or "name, save as profile, MCP"


def _profile_line(answers: dict, base: Profile) -> str:
    title = form_title(answers, base)
    return title if title.endswith("+ changes") else f"{title}, unchanged"


def _writable(profile: Profile) -> str:
    paths = ([profile.shared_dir] if profile.shared_dir else []) + profile.extra_allow_write
    if paths:
        return f"its own workdir, /tmp and /dev/null, plus {', '.join(paths)}"
    return "its own workdir, /tmp and /dev/null. No path of yours."


def _readable(profile: Profile) -> str:
    if not profile.deny_read:
        return "your disk, and nothing is denied"
    return f"your disk, except {' '.join(profile.deny_read)}"


def _reachable(profile: Profile) -> str:
    domains = profile.allowed_domains()
    if not domains:
        return "nothing, this sandbox is offline"
    return f"{_counted(domains)}: {', '.join(domains)}"


def _runnable(profile: Profile) -> str:
    tools = " ".join(profile.tools) or "nothing by name"
    return f"{tools}, plus /usr/bin:/bin" if profile.include_system_path else tools


def _visible(profile: Profile, registry: dict[str, AgentSpec]) -> str:
    spec = registry.get(profile.agent)
    name = spec.name if spec and spec.name else profile.agent
    parts = [f"its own {name} login", "No other agent's keys"]
    parts.append(f"Skills: {', '.join(profile.skills)}" if profile.skills else "No skills")
    if profile.mcp:
        parts.append(f"MCP servers: {', '.join(profile.mcp)}")
    return ". ".join(parts) + "."


# --- what each question offers ---------------------------------------------


def open_choices(live: list[sessions.Session]) -> list[tuple[str, str]]:
    """The Open field: a new sandbox, a plain local tab, and every live session on one list."""
    return [("New sandbox", NEW), ("Local tab", LOCAL), *session_choices(live)]


def open_hint(value: str) -> str:
    """The line under the Open list. Anything that is not new or local is a live session."""
    return OPEN_HINTS.get(value, OPEN_HINTS["attach"])


def session_label(session: sessions.Session) -> str:
    """A session by what it is (agent, permissions, size), not by its name alone (SPEC §3.1)."""
    panes = len(session.pane_ids)
    return (
        f"{session.name}: {session.agent} / {session.profile_name}, "
        f"{panes} tab{'' if panes == 1 else 's'}"
    )


def session_choices(live: list[sessions.Session]) -> list[tuple[str, str]]:
    return [(session_label(session), session.session_id) for session in live]


def profile_choices(saved: dict[str, Profile]) -> list[tuple[str, str]]:
    """Saved profiles, plus a start on paddock's own defaults, which is not a blank slate."""
    entries = [(f"{name} ({profile.agent})", name) for name, profile in sorted(saved.items())]
    return entries + [("Custom (built-in defaults)", CUSTOM)]


def agent_title(key: str, registry: dict[str, AgentSpec]) -> str:
    """What an agent is called on screen: its name and command, or the key it is saved under."""
    spec = registry.get(key)
    return f"{spec.name} ({spec.command})" if spec else key


def agent_choices(registry: dict[str, AgentSpec]) -> list[tuple[str, str]]:
    """Registered agents, plus a command typed in by hand."""
    entries = [(agent_title(key, registry), key) for key in sorted(registry)]
    return entries + [("Something else...", CUSTOM)]


def tool_choices(base: Profile, selected: list[str] | None = None) -> list[tuple[str, str, bool]]:
    """Tools to offer, as title, name and whether it is ticked.

    Candidates the host does not have are left out: nobody needs a checklist of tools
    they never installed. The base profile's own tools stay on the list either way,
    marked when they are missing, so editing a profile on another machine cannot
    quietly drop what that machine cannot see. `selected` is the answer already given,
    which is what a question asked a second time offers back.
    """
    ticked = base.tools if selected is None else selected
    rows = []
    for name in dict.fromkeys(TOOL_CANDIDATES + base.tools + list(ticked)):
        if shutil.which(name):
            rows.append((name, name, name in ticked))
        elif name in base.tools or name in ticked:
            rows.append((f"{name} (not installed)", name, name in ticked))
    return rows


def network_choices(
    base: Profile, selected: list[str] | None = None
) -> list[tuple[str, str, bool]]:
    ticked = base.network_presets if selected is None else selected
    return [(name, name, name in ticked) for name in NETWORK_PRESETS]


def skill_choices(agent: AgentSpec, selected: list[str]) -> list[tuple[str, str, bool]]:
    """Skills under the agent's own config dirs, plus any already chosen, which stay ticked.

    An agent with no skills directory offers none, and the question is skipped.
    """
    names: list[str] = []
    for path in agent.config_write_paths:
        directory = Path(path).expanduser() / "skills"
        if directory.is_dir():
            names += sorted(entry.name for entry in directory.iterdir() if entry.is_dir())
    return [(name, name, name in selected) for name in dict.fromkeys(names + list(selected))]


def parse_domains(text: str) -> list[str]:
    """Typed-in domains: commas or spaces, blanks dropped, no repeats."""
    return list(dict.fromkeys(text.replace(",", " ").split()))


def resolve_shared_dir(answer: str, cwd: Path) -> str:
    """A typed directory as an absolute path. Blank means share nothing, not share here."""
    answer = answer.strip()
    if not answer:
        return ""
    # An absolute or ~ answer wins the join, so relative answers mean "next to the popup".
    return str((cwd / Path(answer).expanduser()).resolve())


def suggested_key(command: str, registry: dict[str, AgentSpec]) -> str:
    """A registry key for a typed-in command that does not stand on a registered one."""
    words = command.split()
    first = Path(words[0]).name if words else ""
    return f"{first}-custom" if first in registry else first


# --- the answers -----------------------------------------------------------


def base_profile(saved: dict[str, Profile], answers: dict) -> Profile:
    """The profile the answers start from. The blank start is a default Profile."""
    key = answers.get("profile", CUSTOM)
    return Profile() if key == CUSTOM else saved[str(key)]


def chosen_agent(answers: dict, base: Profile) -> str:
    """The agent the answers name: a registry key, or the key a typed-in command is saved under."""
    agent = answers.get("agent", base.agent)
    if agent == CUSTOM:
        return str(answers.get("remember_as", ""))
    return str(agent)


def build_session(base: Profile, answers: dict) -> NewSession:
    """The answers as a plan. A step that was never asked keeps the base profile's value."""
    agent = chosen_agent(answers, base)
    carried = base.skills if agent == base.agent else []
    profile = build_profile(
        base,
        agent,
        list(answers.get("tools", base.tools)),
        list(answers.get("network", base.network_presets)),
        parse_domains(str(answers.get("domains", " ".join(base.extra_domains)))),
        list(answers.get("skills", carried)),
        _shared_dir(base, answers),
    )
    return NewSession(
        profile=profile,
        name=str(answers.get("name", "")),
        save_as=str(answers.get("save_as", "")),
        agent_command=str(answers.get("command", "")),
    )


def _shared_dir(base: Profile, answers: dict) -> str:
    """The Files answer: nothing when nothing is shared, else the typed or the profile's path."""
    if not answers.get("share", bool(base.shared_dir)):
        return ""
    return str(answers.get("directory", base.shared_dir))


def build_profile(
    base: Profile,
    agent: str,
    tools: list[str],
    presets: list[str],
    extra_domains: list[str],
    skills: list[str],
    shared_dir: str,
) -> Profile:
    """The answers as a Profile. Fields the chooser never asks about keep the base's values.

    Changed answers get a changed name: a session that says it runs `claude-default` has
    to be the permissions that profile describes. The blank start is already custom.
    """
    built = replace(
        base,
        agent=agent,
        tools=list(tools),
        network_presets=list(presets),
        extra_domains=list(extra_domains),
        skills=list(skills),
        shared_dir=shared_dir,
    )
    if built == base or base.name == Profile().name:
        return built
    return replace(built, name=f"{base.name}+custom")


def save_answers(profile: Profile, name: str) -> tuple[Profile, str]:
    """Save the answers under `name`. Returns the profile to launch and a line for the user.

    A name the profile rules refuse costs the save, never the sandbox just described.
    """
    renamed = replace(profile, name=name)
    try:
        path = save_profile(renamed)
    except ValueError as error:
        return profile, f"paddock: profile not saved: {error}"
    return renamed, f"paddock: saved {path}"


def remember_agent(key: str, command: str) -> Path | None:
    """Write a typed-in command to the agent registry: a profile names a key, not a command.

    None means the key already runs that command and nothing was written. A key that runs
    something else is refused: a user file replaces a registry entry whole, so overwriting
    one would drop its domains and credential paths for every profile that names it.
    """
    if not key or "/" in key or key.startswith("."):
        raise ValueError(f"agent key must be a plain filename, got {key!r}")
    known = load_agents().get(key)
    if known is not None:
        if known.command == command:
            return None
        raise ValueError(f"agent {key!r} already runs {known.command!r}, so choose another name")
    agent_dir().mkdir(parents=True, exist_ok=True)
    path = agent_dir() / f"{key}.json"
    path.write_text(json.dumps({"name": key, "command": command}, indent=2) + "\n")
    return path
