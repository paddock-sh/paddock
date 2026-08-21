"""The chooser: the questions the popup asks, and the plan it hands back.

The questions are a thin shell. Everything that decides something — which
sessions to offer, which tools the host has, the answers as a `Profile` — is a
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
    cwd: str


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


def _choose(cwd: Path) -> Plan:
    live = sessions.list_sessions()
    what = _ask(questionary.select("New window:", choices=_options(first_choices(bool(live)))))
    if what == "local":
        return Local(cwd=str(cwd))
    if what == "attach":
        ref = _ask(questionary.select("Attach to:", choices=_options(session_choices(live))))
        return Attach(ref=ref, cwd=str(cwd))
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
            questionary.text("Remember it as:", default=command.split()[0] if command else "")
        ).strip()

    tools = _ask(
        questionary.checkbox("Tools on the sandbox PATH:", choices=_ticks(tool_choices(base)))
    )
    presets = _ask(questionary.checkbox("Network:", choices=_ticks(network_choices(base))))
    domains = _ask(
        questionary.text("Extra domains (space separated):", default=" ".join(base.extra_domains))
    )

    skills = base.skills
    offered = skill_choices(registry.get(agent, AgentSpec()), base)
    if offered:
        skills = _ask(questionary.checkbox("Skills:", choices=_ticks(offered)))

    shared = ""
    if _ask(questionary.confirm("Share a host directory?", default=bool(base.shared_dir))):
        shared = _ask(
            questionary.text("Directory:", default=base.shared_dir or str(cwd))
        ).strip()

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


def _ticks(pairs: list[tuple[str, bool]]) -> list[questionary.Choice]:
    return [questionary.Choice(name, value=name, checked=ticked) for name, ticked in pairs]


# --- what each question offers ---------------------------------------------


def first_choices(has_sessions: bool) -> list[tuple[str, str]]:
    """The first question. Attach is offered only when there is something to attach to."""
    choices = [("Local namespace (no sandbox)", "local"), ("New sandbox session", "new")]
    if has_sessions:
        choices.append(("Attach to an existing session", "attach"))
    return choices


def session_label(session: sessions.Session) -> str:
    """A session by what it is, so the choice does not depend on remembering names."""
    panes = len(session.pane_ids)
    return f"{session.name} — {session.agent}, {panes} tab{'' if panes == 1 else 's'}"


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


def tool_choices(base: Profile) -> list[tuple[str, bool]]:
    """Tool candidates the host actually has, ticked as the base profile has them.

    The base profile's own tools are offered too, so a profile naming something off
    the standard list does not quietly lose it.
    """
    names = dict.fromkeys(TOOL_CANDIDATES + base.tools)
    return [(name, name in base.tools) for name in names if shutil.which(name)]


def network_choices(base: Profile) -> list[tuple[str, bool]]:
    return [(name, name in base.network_presets) for name in NETWORK_PRESETS]


def skill_choices(agent: AgentSpec, base: Profile) -> list[tuple[str, bool]]:
    """Skills under the agent's own config dirs. An agent with none is skipped, not an error."""
    names: list[str] = []
    for path in agent.config_write_paths:
        directory = Path(path).expanduser() / "skills"
        if directory.is_dir():
            names += sorted(entry.name for entry in directory.iterdir() if entry.is_dir())
    return [(name, name in base.skills) for name in dict.fromkeys(names)]


def parse_domains(text: str) -> list[str]:
    """Typed-in domains: commas or spaces, blanks dropped, no repeats."""
    return list(dict.fromkeys(text.replace(",", " ").split()))


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
    """The answers as a Profile. Fields the chooser never asks about keep the base's values."""
    return replace(
        base,
        agent=agent,
        tools=list(tools),
        network_presets=list(presets),
        extra_domains=list(extra_domains),
        skills=list(skills),
        shared_dir=shared_dir,
    )


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


def remember_agent(key: str, command: str) -> Path:
    """Write a typed-in command to the agent registry: a profile names a key, not a command."""
    if not key or "/" in key or key.startswith("."):
        raise ValueError(f"agent key must be a plain filename, got {key!r}")
    agent_dir().mkdir(parents=True, exist_ok=True)
    path = agent_dir() / f"{key}.json"
    path.write_text(json.dumps({"name": key, "command": command}, indent=2) + "\n")
    return path
