"""A stand-in for `paddock.sessions`: the same calls, recorded instead of carried out.

The chooser and the CLI are tested on which call they make with what, not on what a
session does about it, which `test_sessions.py` covers. `conftest.py`'s `fake_sessions`
fixture patches this over the real module, and a test in `test_cli.py` fails if the two
shapes drift apart. Only the `Session` defaults are a liberty, to keep the tests short.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from paddock.profiles import Profile


@dataclass
class Session:
    session_id: str = "s1"
    name: str = "one"
    profile_name: str = "claude-default"
    agent: str = "claude"
    created_at: str = "2026-01-01T00:00:00"
    run_dir: str = "/state/runs/s1"
    keep_alive: bool = False
    pane_ids: list[str] = field(default_factory=list)


# What the fake was asked to do, in order, as ("name", *arguments).
calls: list[tuple] = []
# Sessions a test wants to exist.
registry: list[Session] = []


def reset() -> None:
    calls.clear()
    registry.clear()


def list_sessions() -> list[Session]:
    calls.append(("list_sessions",))
    return list(registry)


def get_session(ref: str) -> Session | None:
    calls.append(("get_session", ref))
    for session in registry:
        if ref in (session.session_id, session.name):
            return session
    return None


def create_session(profile: Profile, name: str | None = None) -> Session:
    calls.append(("create_session", profile, name))
    return Session(name=name or "generated", profile_name=profile.name, agent=profile.agent)


def attach(session: Session, cwd: Path | None = None) -> str:
    calls.append(("attach", session, cwd))
    return "wA:p9"


def launch(profile: Profile, name: str | None = None) -> tuple[Session, str]:
    calls.append(("launch", profile, name))
    session = Session(name=name or "generated", profile_name=profile.name, agent=profile.agent)
    return session, "wA:p3"


def remove_pane(pane_id: str) -> None:
    calls.append(("remove_pane", pane_id))


def launch_local(cwd: Path | None = None) -> str:
    calls.append(("launch_local", cwd))
    return "wA:p1"
