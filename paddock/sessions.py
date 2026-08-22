"""Sandbox sessions: one running sandbox with a name, and the tabs attached to it (SPEC §3).

A session is registered in `<state>/sessions.json` and outlives the popup that made it,
and herdr restarts. What attaching means belongs to the backend: srt has no guest for a
second process to join, so its tabs share files and never a process tree, while msb execs
every tab into the same microVM.
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
from types import ModuleType

from paddock import herdr_client, log, state_dir, synth_config
from paddock.backends import SandboxGone, microsandbox, srt
from paddock.profiles import Profile

REGISTRY_FILE = "sessions.json"
LOCK_FILE = "sessions.lock"

logger = log.get_logger(__name__)

# Which module runs a session, by the name its record carries (SPEC §2.2).
BACKENDS: dict[str, ModuleType] = {"srt": srt, "msb": microsandbox}

DEFAULT_BACKEND = "srt"


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
    # A record written before there was a second backend is an srt session (SPEC §3.4).
    backend: str = DEFAULT_BACKEND
    # The sandbox msb runs this session in. Blank when the backend has no VM to name.
    vm_handle: str = ""

    def __post_init__(self) -> None:
        # Keys from the record that this paddock has no field for. A newer one may have
        # written them, so they ride along and are written back (SPEC §3.4). Not a field:
        # they are carried, never read, and the record shape stays what the SPEC says.
        self._unknown: dict[str, object] = {}


def backend_for(name: str) -> ModuleType:
    """The module that runs sessions on this backend. An unknown name is a message, not a crash."""
    module = BACKENDS.get(name)
    if module is None:
        raise ValueError(
            f"session backend {name!r} is not in this paddock: it has {', '.join(sorted(BACKENDS))}"
        )
    return module


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


def create_session(
    profile: Profile, name: str | None = None, backend: str = DEFAULT_BACKEND
) -> Session:
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
        run = backend_for(backend).prepare(profile)
        session = Session(
            session_id=_generate_id(taken),
            name=name,
            profile_name=profile.name,
            agent=profile.agent,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            run_dir=str(run.run_dir),
            keep_alive=False,
            pane_ids=[],
            backend=backend,
            # Only a backend with a VM names one, so this is blank for the rest.
            vm_handle=getattr(run, "vm_handle", ""),
        )
        _save(live + [session])
        logger.info(
            "session created %s",
            log.context(
                session=session.session_id,
                name=session.name,
                backend=backend,
                profile=profile.name,
                agent=profile.agent,
                run_dir=session.run_dir,
                vm=session.vm_handle,
            ),
        )
        return session


def attach(session: Session, cwd: Path | None = None) -> str:
    """Open a tab on the session and start the agent in it. Returns the pane id.

    The tab starts in the session's workdir unless another directory is named, which
    not every backend can honour: an msb tab always opens in the guest (SPEC §2.2).
    """
    backend = backend_for(session.backend)
    run = backend.load_run(Path(session.run_dir))
    try:
        pane_id = backend.open_pane(run, label=f"sbx:{session.name}", cwd=cwd)
    except SandboxGone as error:
        # There is nothing left to attach to, so the session ends here rather than
        # sitting in the registry offering tabs that cannot open (SPEC §3.4).
        _forget(session)
        raise SandboxGone(f"session {session.name!r} is over: {error}") from error
    session.pane_ids.append(pane_id)
    _record(session)
    logger.info(
        "session attached %s",
        log.context(
            session=session.session_id,
            name=session.name,
            backend=session.backend,
            pane=pane_id,
            cwd=cwd,
        ),
    )
    return pane_id


def launch(
    profile: Profile, name: str | None = None, backend: str = DEFAULT_BACKEND
) -> tuple[Session, str]:
    """A new session with its first tab: what the chooser does for "New sandbox session"."""
    session = create_session(profile, name, backend)
    try:
        return session, attach(session)
    except Exception as error:
        # create_session has already booted whatever the backend runs. A session that
        # never got its first tab must not keep that running, or sit in the registry.
        _forget(session)
        raise RuntimeError(f"{_describe(session)} could not open its first tab: {error}") from error


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
        logger.info("pane removed %s", log.context(pane=pane_id, sessions_left=len(kept)))
        for session in collected:
            _collect(session)


def reconcile() -> list[Session]:
    """Drop panes herdr no longer has, and collect the sessions that ran out of them.

    Nothing is watching herdr, so this runs at every paddock invocation instead (SPEC §3.4).
    Returns the sessions it collected, which is what `paddock gc` prints.
    """
    with _locked():
        try:
            alive = herdr_client.list_pane_ids()
        except herdr_client.HerdrError as error:
            # No answer is not "no panes": treating it as one would collect every session.
            logger.debug("not reconciling, herdr did not answer %s", log.scrub(str(error)))
            return []
        # Both reads happen under the lock, so a tab another paddock opened and registered
        # before this one got the lock is in the pane list too, and never looks closed.
        live = list_sessions()
        kept, collected, changed = [], [], False
        for session in live:
            panes = [pane_id for pane_id in session.pane_ids if pane_id in alive]
            if panes == session.pane_ids:
                kept.append(session)
                continue
            changed = True
            session.pane_ids = panes
            # A session that already had no panes is one mid-launch, or a kept one. Only
            # the session this just took the last pane from is over.
            if panes or session.keep_alive:
                kept.append(session)
            else:
                collected.append(session)
        if changed:
            _save(kept)
    for session in collected:
        _collect(session)
    # DEBUG, not INFO: this runs at every paddock invocation, and the sessions it did
    # collect say so themselves at INFO. A quiet reconcile is not news.
    logger.debug(
        "reconciled %s",
        log.context(panes=len(alive), sessions_left=len(kept), collected=len(collected)),
    )
    return collected


def launch_local(cwd: Path | None = None) -> str:
    """The chooser's other branch: an ordinary tab. No session, no sandbox, no label."""
    where = cwd or Path.cwd()
    pane_id = herdr_client.create_tab(where)
    logger.info("local tab %s", log.context(pane=pane_id, cwd=where))
    return pane_id


def _collect(session: Session) -> None:
    """End a session nobody is attached to any more (SPEC §3.4).

    The run dir stays on disk, because deleting a workdir would lose work, but the token
    in it does not outlive the session (SPEC §8), and neither does its sandbox. A backend
    this paddock does not have cannot be asked, and a pane closing is no place to raise:
    say what was left running and carry on.
    """
    synth_config.discard_credentials(Path(session.run_dir))
    logger.info(
        "session collected %s",
        log.context(
            session=session.session_id,
            name=session.name,
            backend=session.backend,
            run_dir=session.run_dir,
            vm=session.vm_handle,
        ),
    )
    try:
        backend = backend_for(session.backend)
    except ValueError as error:
        print(f"paddock: {error}", file=sys.stderr)
        return
    backend.collect(Path(session.run_dir), session.vm_handle)


def _forget(session: Session) -> None:
    """Take a session out of the registry and end it. Doing it twice is not an error."""
    with _locked():
        live = list_sessions()
        kept = [other for other in live if other.session_id != session.session_id]
        if len(kept) == len(live):
            return  # another path collected it already
        _save(kept)
    _collect(session)


def _describe(session: Session) -> str:
    """A session in an error message: its name, and the VM it runs in when it has one."""
    if session.vm_handle:
        return f"session {session.name!r} (microVM {session.vm_handle})"
    return f"session {session.name!r}"


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
    """
    with _locked():
        live = list_sessions()
        for index, other in enumerate(live):
            if other.session_id == session.session_id:
                session.pane_ids = list(dict.fromkeys(other.pane_ids + session.pane_ids))
                # Same reason: a key the other writer added is not this one's to drop.
                session._unknown = {**other._unknown, **session._unknown}
                live[index] = session
                break
        else:
            live.append(session)
        _save(live)


def _save(sessions: list[Session]) -> None:
    """Write the registry whole, then swap it in: a crash mid-write leaves the old one."""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # A temp file of this writer's own: a shared name would let two of them collide.
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".sessions-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as file:
            file.write(json.dumps([_as_record(session) for session in sessions], indent=2) + "\n")
        os.replace(temp, path)
    except OSError:
        Path(temp).unlink(missing_ok=True)
        raise


def _as_record(session: Session) -> dict:
    """One record: the session's own fields, and any a newer paddock wrote around them."""
    return {**session._unknown, **asdict(session)}


def _session(record: object) -> Session | None:
    """One session from one record, or None if it cannot be used as written."""
    if not isinstance(record, dict):
        return None
    # Field names and their types. asdict, not vars: a session carries more than its fields.
    shape = asdict(Session("", "", "", "", "", "", False, []))
    values = {key: value for key, value in record.items() if key in shape}
    # The fields with a default: a record written before they existed is still a session.
    for key in ("backend", "vm_handle"):
        values.setdefault(key, shape[key])
    if set(values) != set(shape):
        return None
    # A record of the wrong shape is dropped whole. Half of a session is worse than none.
    for key, value in values.items():
        if not isinstance(value, type(shape[key])):
            return None
        if isinstance(value, list) and not all(isinstance(item, str) for item in value):
            return None
    session = Session(**values)
    session._unknown = {key: value for key, value in record.items() if key not in shape}
    return session


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
