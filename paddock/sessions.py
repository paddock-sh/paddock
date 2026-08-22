"""Sandbox sessions: one settings file and workdir, with the tabs attached to it (SPEC §3).

A session is registered in `<state>/sessions.json` and outlives the popup that made it,
and herdr restarts. Attaching starts a new process under the same policy. With srt there
is no guest for a second process to join, so tabs share files, never a process tree.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from paddock import herdr_client, state_dir, synth_config
from paddock.backends import srt
from paddock.profiles import Profile

REGISTRY_FILE = "sessions.json"
LOCK_FILE = "sessions.lock"


@dataclass
class Session:
    session_id: str
    name: str
    profile_name: str
    agent: str
    created_at: str
    run_dir: str
    keep_alive: bool
    pane_ids: list[str]


def registry_path() -> Path:
    return state_dir() / REGISTRY_FILE


def list_sessions() -> list[Session]:
    """Every live session. An unreadable registry is reported and treated as empty."""
    path = registry_path()
    try:
        records = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        print(f"paddock: cannot read the session registry, starting empty: {path}", file=sys.stderr)
        return []
    if not isinstance(records, list):
        print(f"paddock: session registry is not a list, starting empty: {path}", file=sys.stderr)
        return []
    sessions = [session for session in map(_session, records) if session is not None]
    dropped = len(records) - len(sessions)
    if dropped:
        print(f"paddock: dropped {dropped} unusable session records from {path}", file=sys.stderr)
    return sessions


def get_session(ref: str) -> Session | None:
    """Find a session by id, else by name: the chooser shows names, scripts use ids."""
    live = list_sessions()
    for session in live:
        if session.session_id == ref:
            return session
    for session in live:
        if session.name == ref:
            return session
    return None


def create_session(profile: Profile, name: str | None = None) -> Session:
    """Register a session and get its run ready on disk. Opens no pane."""
    with _locked():
        live = list_sessions()
        # Both are references a caller can pass to get_session, so neither may be reused.
        taken = {session.name for session in live} | {session.session_id for session in live}
        name = name.strip() if name else _generate_name(profile.name, taken)
        if not name:
            raise ValueError("a session name cannot be empty")
        if name in taken:
            raise ValueError(f"a live session already answers to {name!r}")

        # Prepared before the session is registered, so a failed setup leaves no dead entry.
        run = srt.prepare(profile)
        session = Session(
            session_id=_generate_id(taken),
            name=name,
            profile_name=profile.name,
            agent=profile.agent,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            run_dir=str(run.run_dir),
            keep_alive=False,
            pane_ids=[],
        )
        _save(live + [session])
        return session


def attach(session: Session, cwd: Path | None = None) -> str:
    """Open a tab on the session and start the agent in it. Returns the pane id.

    The tab starts in the session's workdir unless another directory is named.
    """
    run = srt.load_run(Path(session.run_dir))
    pane_id = srt.open_pane(run, label=f"sbx:{session.name}", cwd=cwd)
    session.pane_ids.append(pane_id)
    _record(session)
    return pane_id


def launch(profile: Profile, name: str | None = None) -> tuple[Session, str]:
    """A new session with its first tab: what the chooser does for "New sandbox session"."""
    session = create_session(profile, name)
    return session, attach(session)


def set_keep_alive(session: Session, keep_alive: bool) -> None:
    """Whether the session survives its last pane (SPEC 3.4), written down where it is read."""
    session.keep_alive = keep_alive
    _record(session)


def remove_pane(pane_id: str) -> None:
    """Detach a closed pane, and collect the session when it was the last one."""
    with _locked():
        live = list_sessions()
        if not any(pane_id in session.pane_ids for session in live):
            return
        kept, collected = [], []
        for session in live:
            if pane_id in session.pane_ids:
                session.pane_ids.remove(pane_id)
                if not session.pane_ids and not session.keep_alive:
                    collected.append(session)
                    continue
            kept.append(session)
        _save(kept)
        for session in collected:
            # The run dir stays on disk, because deleting a workdir would lose work, but the
            # token in it does not outlive the session (SPEC §8).
            synth_config.discard_credentials(Path(session.run_dir))


def launch_local(cwd: Path | None = None) -> str:
    """The chooser's other branch: an ordinary tab. No session, no sandbox, no label."""
    return herdr_client.create_tab(cwd or Path.cwd())


@contextmanager
def _locked() -> Iterator[None]:
    """Hold the registry while reading and writing it, so two popups cannot interleave."""
    path = state_dir() / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:  # the lock is released when the file closes
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _record(session: Session) -> None:
    """Store the session, keeping the panes another tab registered since it was loaded.

    `keep_alive` comes from the caller: it is the one field a caller sets on purpose.

    A session that is no longer registered was collected while this caller held it, so it is
    left collected. Writing it back would bring a dead session round again, with a run dir
    whose credentials have already been discarded.
    """
    with _locked():
        live = list_sessions()
        for index, other in enumerate(live):
            if other.session_id == session.session_id:
                session.pane_ids = list(dict.fromkeys(other.pane_ids + session.pane_ids))
                live[index] = session
                _save(live)
                return
        print(
            f"paddock: session {session.name!r} was collected while it was being used, "
            "so it is not being registered again",
            file=sys.stderr,
        )


def _save(sessions: list[Session]) -> None:
    """Write the registry whole, then swap it in: a crash mid-write leaves the old one."""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # A temp file of this writer's own: a shared name would let two of them collide.
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".sessions-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as file:
            file.write(json.dumps([asdict(session) for session in sessions], indent=2) + "\n")
        os.replace(temp, path)
    except OSError:
        Path(temp).unlink(missing_ok=True)
        raise


def _session(record: object) -> Session | None:
    """One session from one record, or None if it cannot be used as written."""
    if not isinstance(record, dict):
        return None
    shape = vars(Session("", "", "", "", "", "", False, []))  # field names and their types
    values = {key: value for key, value in record.items() if key in shape}
    if set(values) != set(shape):
        return None
    # A record of the wrong shape is dropped whole. Half of a session is worse than none.
    for key, value in values.items():
        if not isinstance(value, type(shape[key])):
            return None
        if isinstance(value, list) and not all(isinstance(item, str) for item in value):
            return None
    return Session(**values)


def _generate_name(profile_name: str, taken: set[str]) -> str:
    """The profile name and a short suffix, so two sessions on one profile still differ."""
    stem = profile_name or "sandbox"
    while True:
        name = f"{stem}-{secrets.token_hex(2)}"
        if name not in taken:
            return name


def _generate_id(taken: set[str]) -> str:
    while True:
        session_id = secrets.token_hex(4)
        if session_id not in taken:
            return session_id
