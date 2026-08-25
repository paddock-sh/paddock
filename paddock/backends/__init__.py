"""Sandbox backends, and the run directory every one of them uses (SPEC §2).

Each session gets its own directory under `<state>/runs/`. What a backend puts in it
is its own business, but three things are the same for all of them: a `launch.json`
holding what a later tab needs to attach, a `launch.sh` holding the command, so the
pane can be sent a short line instead of the command itself (SPEC §1.3), and a
`pane.log` that script keeps the launch's stderr in (SPEC §9).

The script is written here rather than in each backend, so a pane that dies takes the
same shape whichever sandbox was behind it: srt's `srt -c ...` and msb's `msb exec
--tty` are both just the command the script wraps.
"""

from __future__ import annotations

import os
import shlex
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from paddock import log, state_dir

# What a later tab attaches with. The keys are the backend's own.
LAUNCH_FILE = "launch.json"

# The command as a script, because the pane is sent a line, not a file (see launch_line).
LAUNCH_SCRIPT = "launch.sh"

# The same run entered as a plain shell instead of as the agent (SPEC §3.2). A second script
# rather than a second kind of pane: a shell tab is held, logged and replayed like any other.
SHELL_SCRIPT = "shell.sh"

# The wrapper `paddock run` becomes when the run it starts has to be collected on the way
# out (SPEC §11). It runs the launch script and ends the session after it.
RUN_SCRIPT = "run.sh"

# The pid of the `paddock run` that became this run. A run in somebody's terminal keeps no
# registry entry on every backend, so this is what tells a sweep it is not over (SPEC §11).
PID_FILE = "standalone.pid"

# A launch that fails does so at once. A non-zero exit later than this is the agent ending,
# ctrl-c included, and holding the pane on that would hold it hostage.
HOLD_WITHIN_SECONDS = 10

# How much of pane.log a failed launch replays into the pane before it waits.
TAIL_ON_FAILURE = 20

# One earlier generation of pane.log is kept, at this size.
PANE_LOG_MAX_BYTES = 1_000_000

logger = log.get_logger(__name__)


class RunNotFound(RuntimeError):
    """The run dir holds no usable launch record, so nothing can attach to it."""


class SandboxGone(RuntimeError):
    """The sandbox this run named is not there any more, so no tab can join it.

    Raised before a tab is opened, so a session that lost its VM says so instead of
    leaving a dead pane. Sessions treats it as the end of that session (SPEC §3.4).
    """


@dataclass
class Swept:
    """What one backend's sweep did: what it took away, and what it would not answer for.

    `unowned` is a sandbox named the way paddock names its own that no run of this state
    dir made. Another paddock context on this host owns it, or a test run does, and either
    way removing it would destroy a live session that is none of this gc's business
    (SPEC §3.4). It is named at the user instead, with the command to remove it by hand.
    """

    removed: list[str] = field(default_factory=list)
    unowned: list[str] = field(default_factory=list)


def new_run_dir() -> Path:
    """A fresh directory for this launch, named for when it was made."""
    runs = state_dir() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=runs))


def log_for(name: str) -> str:
    """Which log a script appends to. A shell tab keeps its own, away from the agent's."""
    return log.SHELL_LOG if name == SHELL_SCRIPT else log.PANE_LOG


def write_launch_script(run_dir: Path, command: str, name: str = LAUNCH_SCRIPT) -> Path:
    """Put the composed command in the run dir, where the pane can run it by name.

    The run dir is not writable from inside the sandbox, so the agent cannot rewrite
    the script that launched it. `name` is which of the run's scripts this is: the agent's,
    or the shell one that enters the same sandbox without it.
    """
    script = run_dir / name
    script.write_text(script_text(run_dir, command, name))
    script.chmod(0o700)
    return script


def ensure_launch_script(run_dir: Path, command: str, name: str = LAUNCH_SCRIPT) -> bool:
    """Rewrite the script when it is not the one this paddock would write. Did it rewrite?

    This covers a run dir prepared before paddock wrote scripts at all (it has none, and
    the pane runs it) as well as one whose script an upgrade has moved on from. The launch
    record holds the exact command either way, so a session gets today's launch behaviour
    on its next tab, on whichever backend it runs.
    """
    if written_script(run_dir, name) == script_text(run_dir, command, name):
        return False
    write_launch_script(run_dir, command, name)
    logger.debug("launch script rewritten %s", log.context(path=run_dir / name))
    return True


def written_script(run_dir: Path, name: str = LAUNCH_SCRIPT) -> str:
    """The script as it is on disk, or nothing when there is none."""
    try:
        return (run_dir / name).read_text()
    except OSError:
        return ""


def script_text(run_dir: Path, command: str, name: str = LAUNCH_SCRIPT) -> str:
    """The script the pane runs: the command, its stderr kept, and a pane that stays put.

    A launch that failed used to take the pane with it, error and all, so the one thing
    that would have said why was gone before anyone could read it. Now stderr is appended
    to the run's log, and a failure replays the end of it and waits.

    The launch runs in the foreground with a plain file redirection, not through a pipe.
    A pipe closes when its last writer does, and an agent that backgrounds anything leaves
    a process holding stderr for as long as it lives, so the pane would hang on a launch
    that went perfectly well. stdout is untouched either way: the agent draws its interface
    there, and it has to stay a terminal.

    A shell tab is held to a different test. `exit 1` in an interactive shell is the user
    leaving, not a launch that failed, and holding the pane on it would hold the user
    hostage for typing. So a shell pane is held only when the run wrote something to its
    log: a shell that could not start says why on stderr, and one the user ended says
    nothing at all.
    """
    quiet_exit_is_fine = name == SHELL_SCRIPT
    pane_log = shlex.quote(str(log.pane_log_path(run_dir, log_for(name))))
    return "\n".join(
        [
            "#!/bin/sh",
            "# Written by paddock when the run was prepared.",
            f"paddock_log={pane_log}",
            "",
            *(_prompt() if quiet_exit_is_fine else []),
            "# One earlier generation is kept, so a run nobody closes cannot fill the disk.",
            "# The braces matter: without them the shell, not wc, reports a log that is not",
            "# there yet, and the first launch of every run would print an error at the user.",
            "paddock_size=$( { wc -c < \"$paddock_log\"; } 2>/dev/null | tr -dc '0-9' )",
            '[ -n "$paddock_size" ] && [ "$paddock_size" -gt '
            f'{PANE_LOG_MAX_BYTES} ] && mv -f "$paddock_log" "$paddock_log.1"',
            "",
            "paddock_launch() {",
            command,
            "}",
            "",
            "paddock_start=$(date +%s 2>/dev/null || echo 0)",
            "paddock_said=$( { wc -c < \"$paddock_log\"; } 2>/dev/null | tr -dc '0-9' )",
            'paddock_launch 2>>"$paddock_log"',
            "paddock_exit=$?",
            '[ "$paddock_exit" = 0 ] && exit 0',
            "",
            "# Only a launch that failed holds the pane. An agent that ran for a while and",
            "# then exited non-zero (ctrl-c is 130) was watched by whoever was sitting there.",
            "paddock_end=$(date +%s 2>/dev/null || echo 0)",
            '[ "$((paddock_end - paddock_start))" -ge '
            f'{HOLD_WITHIN_SECONDS} ] && exit "$paddock_exit"',
            "",
            *(
                [
                    "# A shell that said nothing on its way out is a user leaving, not a",
                    "# launch that failed. One that could not start wrote the reason here.",
                    "paddock_now=$( { wc -c < \"$paddock_log\"; } 2>/dev/null | tr -dc '0-9' )",
                    '[ "${paddock_now:-0}" = "${paddock_said:-0}" ] && exit "$paddock_exit"',
                    "",
                ]
                if quiet_exit_is_fine
                else []
            ),
            "# An interface that died can leave the terminal raw, and then nothing echoes.",
            "stty sane 2>/dev/null",
            "printf 'paddock: launch failed (exit %s), log: %s\\n'"
            ' "$paddock_exit" "$paddock_log" >&2',
            f'tail -n {TAIL_ON_FAILURE} "$paddock_log" >&2',
            "printf 'paddock: press enter to close this pane. ' >&2",
            "read -r paddock_key",
            'exit "$paddock_exit"',
            "",
        ]
    )


def _prompt() -> list[str]:
    """Put "paddock:" in front of the prompt, so a sandboxed shell does not look like yours.

    zsh reads PROMPT and everything else reads PS1, so both are set. A shell that sources a
    startup file of its own writes its own prompt over this, and a backend that starts the
    command from an empty environment (srt's `env -i`) drops it before the shell ever sees
    it: what says so in every case is the tab label, `sbx:<session> (shell)`.
    """
    return [
        "# A sandboxed shell should not read as an ordinary one (SPEC §3.2).",
        'PS1="paddock:${PS1:-\\$ }"',
        'PROMPT="$PS1"',
        "export PS1 PROMPT",
        "",
    ]


def write_run_script(run_dir: Path, after: list[str]) -> Path:
    """Wrap one of the run's scripts so a run in the user's own terminal ends itself.

    `paddock run` execs into the sandbox (SPEC §11), so there is no paddock process left to
    notice the exit. This script is that process: it runs the launch script, then runs
    `after`, then leaves with the launch script's own status.
    """
    script = run_dir / RUN_SCRIPT
    script.write_text(run_script_text(run_dir, after))
    script.chmod(0o700)
    return script


def run_script_text(run_dir: Path, after: list[str]) -> str:
    """The wrapper: one script, one command after it, and the first one's exit status.

    The command runs on the way out however the run ended. Ctrl-c goes to the whole
    foreground group, so the shell running this gets it too, and a sandbox left running
    because the user pressed a key is exactly the leak this exists to stop. A trap covers
    that and the terminal closing with it. Nothing covers being killed outright, which is
    what `paddock collect <session>` is for.

    The trap is dropped before the command runs, so a second ctrl-c cannot interrupt the
    collection the first one started. A child inherits an ignored signal across exec, so
    that covers the command itself and not only the shell waiting for it.
    """
    return "\n".join(
        [
            "#!/bin/sh",
            "# Written by paddock for a run in the terminal it was started from.",
            "paddock_after() {",
            '  [ -n "$paddock_done" ] && return',
            "  paddock_done=1",
            "  trap '' INT HUP TERM",
            f"  {shlex.join(after)}",
            "}",
            "trap paddock_after EXIT HUP INT TERM",
            "",
            f"/bin/sh {shlex.quote(str(run_dir / LAUNCH_SCRIPT))}",
            "paddock_exit=$?",
            "paddock_after",
            'exit "$paddock_exit"',
            "",
        ]
    )


def write_pid_marker(run_dir: Path) -> Path:
    """Name the process that is about to become this run (SPEC §11).

    Written before the exec, and `execv` keeps the pid, so the number in here is the
    process sitting in the sandbox for as long as it is sitting there.
    """
    path = run_dir / PID_FILE
    path.write_text(f"{os.getpid()}\n")
    return path


def run_is_live(run_dir: Path) -> bool:
    """Whether a terminal is still in this run, by the pid it left behind (SPEC §11).

    A run that keeps no registry entry has nothing else to say so, and a sweep that
    removed its shim dir would take the tools out from under a live sandbox.

    A pid this user may not signal belongs to somebody else and counts as alive: refusing
    to remove a directory costs a directory, and removing a live run costs the session.
    A pid the system has since given to something else is covered by the grace period a
    sweep already leaves, which is longer than the gap between the two.
    """
    try:
        pid = int((run_dir / PID_FILE).read_text().strip())
    except (OSError, ValueError):
        return False  # no marker, or one nothing wrote a number in: not a live run
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def launch_line(run_dir: Path, name: str = LAUNCH_SCRIPT) -> str:
    """What `herdr pane run` is sent: short on purpose.

    herdr types the line into the pane's shell, and a tty in canonical mode drops
    everything past 1024 bytes, which a composed command passes easily. `exec`
    replaces that shell, so closing the sandbox closes the pane.
    """
    return f"exec /bin/sh {shlex.quote(str(run_dir / name))}"
