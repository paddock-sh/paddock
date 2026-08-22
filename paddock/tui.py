"""The chooser: the questions the popup asks, and the plan it hands back.

The questions are a thin shell. Everything that decides something (which
sessions to offer, which tools the host has, the answers as a `Profile`) is a
plain function here, so it is tested without a terminal. The new-session
questions are a list of steps over one answers dict, so a step can be asked
again: that is what "back" and the summary screen edit. Nothing in this module
launches anything: `choose()` returns a plan and `cli.py` carries it out, which
is why backing out of a question costs nothing.
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

# The two screens after the questions, and the three answers the summary takes.
SUMMARY, EDIT = "summary", "edit"
LAUNCH, CANCEL = "launch", "cancel"

# Steps that decide what comes after them. Editing one from the summary asks its dependents
# again. Tools are not under the agent: they come off the host PATH, not out of the agent.
DEPENDENTS = {
    "agent": ("command", "remember_as", "skills"),
    "share": ("directory",),
}

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
        what = _ask(questionary.select("New window:", choices=_options(first_choices(bool(live)))))
        if what == "local":
            return Local(cwd=str(cwd))
        if what == "attach":
            ref = _ask(questionary.select("Attach to:", choices=_options(session_choices(live))))
            return Attach(ref=str(ref))
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
            plan = build_session(base, answers)
            lines = summary_lines(str(answers.get("profile", CUSTOM)), plan)
            message = "Ready to launch:\n  " + "\n  ".join(lines) + "\n"
            entries = [("Launch", LAUNCH), ("Edit a step", EDIT), ("Cancel", CANCEL)]
            return _ask(questionary.select(message, choices=_options(entries)))
        if step == EDIT:
            return _pick("Edit which step:", edit_choices(answers))
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
    """Ask one step again from the summary, and anything its answer decides."""
    value = ask(step, answers)
    if value in (BACK, SKIP) or not _answer(answers, step, value):
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
    """Keep one answer. True when it changed a step that others depend on."""
    changed = step in DEPENDENTS and answers.get(step, value) != value
    answers[step] = value
    if changed and step == "agent":
        # Skills come out of the agent's own config dir, so the old agent's do not carry over.
        answers.pop("skills", None)
    return changed


def edit_choices(answers: dict) -> list[tuple[str, str]]:
    """The steps that were asked, in the order they were asked. A skipped one is not editable."""
    return [(STEP_LABELS[step], step) for step in STEPS if step in answers]


def summary_lines(start_from: str, plan: NewSession) -> list[str]:
    """Every answer on one line, for the last screen before anything happens."""
    profile = plan.profile
    network = ", ".join(profile.network_presets) or "none"
    if profile.extra_domains:
        network += " + " + ", ".join(profile.extra_domains)
    lines = [
        "Window: new sandbox session",
        f"Start from: {'Custom' if start_from == CUSTOM else start_from}",
        f"Agent: {profile.agent}",
    ]
    if plan.agent_command:
        lines.append(f"Command: {plan.agent_command}")
    return lines + [
        f"Tools: {', '.join(profile.tools) or 'none'}",
        f"Network: {network}",
        f"Skills: {', '.join(profile.skills) or 'none'}",
        f"Shared directory: {profile.shared_dir or 'none, an isolated workdir'}",
        f"Session name: {plan.name or '(generated)'}",
        f"Save as profile: {plan.save_as or 'not saved'}",
    ]


# --- what each question offers ---------------------------------------------


def first_choices(has_sessions: bool) -> list[tuple[str, str]]:
    """The first question. Attach is offered only when there is something to attach to."""
    choices = [("Local namespace (no sandbox)", "local"), ("New sandbox session", "new")]
    if has_sessions:
        choices.append(("Attach to an existing session", "attach"))
    return choices


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
    """Saved profiles, plus a blank start."""
    entries = [(f"{name} ({profile.agent})", name) for name, profile in sorted(saved.items())]
    return entries + [("Custom", CUSTOM)]


def agent_choices(registry: dict[str, AgentSpec]) -> list[tuple[str, str]]:
    """Registered agents, plus a command typed in by hand."""
    entries = [(f"{spec.name} ({spec.command})", key) for key, spec in sorted(registry.items())]
    return entries + [("Custom command", CUSTOM)]


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
        str(answers.get("directory", "")),
    )
    return NewSession(
        profile=profile,
        name=str(answers.get("name", "")),
        save_as=str(answers.get("save_as", "")),
        agent_command=str(answers.get("command", "")),
    )


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
