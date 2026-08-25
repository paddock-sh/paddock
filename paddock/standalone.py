"""`paddock run`: the same sandboxed session, in the terminal you are already in (SPEC §11).

herdr is not on this path and no tab is opened. paddock prepares the run exactly as a
launch does, then execs the run's own launch script in place, so the sandbox takes over
this process and the terminal it was started from. The failure hold, the pane log and the
policy are the script's, so they are the same here as they are in a pane.

What this mode costs is the tabs: a session here has one terminal, and no second tab to
attach, which is what herdr is for.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from paddock import log, sessions
from paddock.backends import LAUNCH_SCRIPT, SHELL_SCRIPT, write_run_script
from paddock.profiles import Profile

logger = log.get_logger(__name__)

# What the run is exec'd into: the same `/bin/sh <script>` a pane is sent (SPEC §1.3).
SHELL = "/bin/sh"


def start(
    profile: Profile, backend: str = sessions.DEFAULT_BACKEND, keep_alive: bool = False
) -> int:
    """Prepare a run and become it. Comes back only if the exec could not happen."""
    here = sessions.prepare_standalone(profile, backend, keep_alive)
    script = here.run_dir / LAUNCH_SCRIPT
    if here.session is None:
        if keep_alive:
            print(
                "paddock: nothing to keep alive here: this run ends with the terminal",
                file=sys.stderr,
            )
    else:
        print(f"paddock: session {here.session.name}", file=sys.stderr)
        if not keep_alive:
            # Nothing else would. This session has no pane for a reconcile to miss, and a
            # gc leaves a registered sandbox alone, so the script that runs it ends it.
            script = write_run_script(here.run_dir, collect_argv(here.session))
    try:
        return become(script, here.workdir)
    except OSError:
        # The exec is the last thing that can fail, and after it there is nothing left to
        # collect the sandbox the prepare booted.
        if here.session is not None:
            sessions.forget(here.session)
        raise


def attach(session: sessions.Session, shell: bool = False) -> int:
    """Put this terminal inside a session that is already running. No tab, no pane id."""
    here = sessions.attach_standalone(session, shell)
    return become(here.run_dir / (SHELL_SCRIPT if shell else LAUNCH_SCRIPT), here.workdir)


def become(script: Path, workdir: Path) -> int:
    """Replace this process with the run, in the directory a pane would have opened in."""
    if not script.exists():
        raise RuntimeError(
            f"the run in {script.parent} has no {script.name}, so there is nothing to enter "
            "here: start a new session"
        )
    logger.info("standalone exec %s", log.context(script=script, workdir=workdir))
    os.chdir(workdir)
    os.execv(SHELL, [SHELL, str(script)])
    return 0  # unreachable: execv either replaces this process or raises


def collect_argv(session: sessions.Session) -> list[str]:
    """How the run's own script ends the session when the agent it started exits.

    Through this interpreter, not through a `paddock` on PATH: the script runs in whatever
    environment the sandbox leaves behind, and the paddock that wrote it is the one that
    knows where the registry it wrote to is.
    """
    return [sys.executable, "-m", "paddock", "collect", session.session_id]
