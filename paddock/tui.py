"""The chooser: the questions the popup asks, and the plan it hands back.

The questions are a thin shell. Everything that decides something (which
sessions to offer, which tools the host has, the answers as a `Profile`) is a
plain function here, so it is tested without a terminal. Nothing in this module
launches anything: `choose()` returns a plan and `cli.py` carries it out, which
is why backing out of a question costs nothing.
"""

from __future__ import annotations

import json
import shutil
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
from paddock.sessions import DEFAULT_BACKEND

# The "none of the saved ones" entry in the profile and agent lists. It is not a
# name anyone would give a file, so it cannot collide with a real key.
CUSTOM = "+custom"


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
    # Which backend runs it. The chooser does not ask yet, so only `paddock launch` sets this.
    backend: str = DEFAULT_BACKEND


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


def _choose(cwd: Path) -> Plan:
    live = sessions.list_sessions()
    what = _ask(questionary.select("New window:", choices=_options(first_choices(bool(live)))))
    if what == "local":
        return Local(cwd=str(cwd))
    if what == "attach":
        ref = _ask(questionary.select("Attach to:", choices=_options(session_choices(live))))
        return Attach(ref=ref)
    return _new_session(cwd)


def _new_session(cwd: Path) -> NewSession:
    saved = load_profiles()
    key = _ask(questionary.select("Start from:", choices=_options(profile_choices(saved))))
    base = Profile() if key == CUSTOM else saved[key]

    registry = load_agents()
    agent = _ask(
        questionary.select(
            "Agent:",
            choices=_options(agent_choices(registry)),
            default=base.agent if base.agent in registry else None,
        )
    )
    command = ""
    if agent == CUSTOM:
        command = _ask(questionary.text("Command to run in the sandbox:")).strip()
        agent = _ask(
            questionary.text("Remember it as:", default=suggested_key(command, registry))
        ).strip()

    tools = list(base.tools)
    tool_rows = tool_choices(base)
    if tool_rows:  # an empty checklist is not a question
        tools = _ask(questionary.checkbox("Tools on the sandbox PATH:", choices=_ticks(tool_rows)))
    presets = _ask(questionary.checkbox("Network:", choices=_ticks(network_choices(base))))
    domains = _ask(
        questionary.text("Extra domains (space separated):", default=" ".join(base.extra_domains))
    )

    # Skills come out of the agent's own config dir, so another agent's do not carry over.
    skills: list[str] = []
    carried = base.skills if agent == base.agent else []
    skill_rows = skill_choices(registry.get(agent, AgentSpec()), carried)
    if skill_rows:
        skills = _ask(questionary.checkbox("Skills:", choices=_ticks(skill_rows)))

    shared = ""
    if _ask(questionary.confirm("Share a host directory?", default=bool(base.shared_dir))):
        answer = _ask(questionary.text("Directory:", default=base.shared_dir or str(cwd)))
        shared = resolve_shared_dir(answer, cwd)

    name = _ask(questionary.text("Session name (blank to generate one):")).strip()
    save_as = _ask(questionary.text("Save these answers as a profile (blank to skip):")).strip()
    profile = build_profile(base, agent, tools, presets, parse_domains(domains), skills, shared)
    return NewSession(profile=profile, name=name, save_as=save_as, agent_command=command)


def _ask(question: questionary.Question) -> object:
    answer = question.ask()
    if answer is None:
        raise _Cancelled
    return answer


def _options(pairs: list[tuple[str, str]]) -> list[questionary.Choice]:
    return [questionary.Choice(title, value=value) for title, value in pairs]


def _ticks(rows: list[tuple[str, str, bool]]) -> list[questionary.Choice]:
    return [questionary.Choice(title, value=value, checked=on) for title, value, on in rows]


# --- what each question offers ---------------------------------------------


def first_choices(has_sessions: bool) -> list[tuple[str, str]]:
    """The first question. Attach is offered only when there is something to attach to."""
    choices = [("Local namespace (no sandbox)", "local"), ("New sandbox session", "new")]
    if has_sessions:
        choices.append(("Attach to an existing session", "attach"))
    return choices


def session_label(session: sessions.Session) -> str:
    """A session by what it is (backend, agent, permissions, size), not by its name (SPEC §3.1).

    The backend is there because attaching means a different thing on each one (SPEC §3.2).
    """
    panes = len(session.pane_ids)
    return (
        f"{session.name} [{session.backend}]: {session.agent} / {session.profile_name}, "
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


def tool_choices(base: Profile) -> list[tuple[str, str, bool]]:
    """Tools to offer, as title, name and whether it is ticked.

    Candidates the host does not have are left out: nobody needs a checklist of tools
    they never installed. The base profile's own tools stay on the list either way,
    marked when they are missing, so editing a profile on another machine cannot
    quietly drop what that machine cannot see.
    """
    rows = []
    for name in dict.fromkeys(TOOL_CANDIDATES + base.tools):
        if shutil.which(name):
            rows.append((name, name, name in base.tools))
        elif name in base.tools:
            rows.append((f"{name} (not installed)", name, True))
    return rows


def network_choices(base: Profile) -> list[tuple[str, str, bool]]:
    return [(name, name, name in base.network_presets) for name in NETWORK_PRESETS]


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
