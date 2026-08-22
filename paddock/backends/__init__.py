"""Sandbox backends, and the run directory every one of them uses (SPEC §2).

Each session gets its own directory under `<state>/runs/`. What a backend puts in it
is its own business, but two things are the same for all of them: a `launch.json`
holding what a later tab needs to attach, and a `launch.sh` holding the command, so
the pane can be sent a short line instead of the command itself (SPEC §1.3).
"""

from __future__ import annotations

import shlex
import tempfile
import time
from pathlib import Path

from paddock import state_dir

# What a later tab attaches with. The keys are the backend's own.
LAUNCH_FILE = "launch.json"

# The command as a script, because the pane is sent a line, not a file (see launch_line).
LAUNCH_SCRIPT = "launch.sh"


class RunNotFound(RuntimeError):
    """The run dir holds no usable launch record, so nothing can attach to it."""


def new_run_dir() -> Path:
    """A fresh directory for this launch, named for when it was made."""
    runs = state_dir() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=runs))


def write_launch_script(run_dir: Path, command: str) -> Path:
    """Put the composed command in the run dir, where the pane can run it by name.

    The run dir is not writable from inside the sandbox, so the agent cannot rewrite
    the script that launched it.
    """
    script = run_dir / LAUNCH_SCRIPT
    script.write_text(f"#!/bin/sh\n{command}\n")
    script.chmod(0o700)
    return script


def launch_line(run_dir: Path) -> str:
    """What `herdr pane run` is sent: short on purpose.

    herdr types the line into the pane's shell, and a tty in canonical mode drops
    everything past 1024 bytes, which a composed command passes easily. `exec`
    replaces that shell, so closing the sandbox closes the pane.
    """
    return f"exec /bin/sh {shlex.quote(str(run_dir / LAUNCH_SCRIPT))}"
