"""Where paddock writes down what it did, so a pane that vanished can still be explained.

The file under the state dir takes everything at DEBUG. stderr takes warnings and worse,
because the chooser runs in a popup and every line there is in the user's face.
`PADDOCK_LOG=debug` lowers that bar; `PADDOCK_LOG_FILE` moves the file.

Nothing secret is ever written: no token, no credential file content, no Keychain output
and no proxy URL, because srt puts the proxy password in the URL. Paths, byte counts and
lengths say enough (SPEC §9).
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from paddock import state_dir

# Every module logger hangs off this one, so `paddock.backends.srt` says which layer spoke.
ROOT = "paddock"

LOG_FILE = "paddock.log"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3

# What a pane's launch script keeps its stderr in, next to the run it belongs to.
PANE_LOG = "pane.log"

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def log_path() -> Path:
    """The log every paddock process appends to. `PADDOCK_LOG_FILE` overrides it."""
    # `or`, not a get() default: an empty value must not resolve to the current directory.
    override = os.environ.get("PADDOCK_LOG_FILE") or ""
    return Path(override).expanduser() if override else state_dir() / "logs" / LOG_FILE


def pane_log_path(run_dir: Path) -> Path:
    """Where the launch script of one run keeps the pane's stderr."""
    return run_dir / PANE_LOG


def stderr_level() -> int:
    """What reaches the popup: warnings, unless `PADDOCK_LOG` asks for more."""
    return LEVELS.get((os.environ.get("PADDOCK_LOG") or "").strip().lower(), logging.WARNING)


# What setup() put on the logger. Other things attach handlers of their own (pytest does),
# so paddock keeps track of its own rather than counting what is there.
_HANDLERS: list[logging.Handler] = []


def setup() -> None:
    """Attach the two handlers. Calling it again does nothing, so any entry point may."""
    if _HANDLERS:
        return
    root = logging.getLogger(ROOT)
    root.setLevel(logging.DEBUG)
    root.propagate = False  # paddock's log is its own, not that of whatever imported it
    formatter = logging.Formatter(FORMAT, datefmt=DATE_FORMAT)

    stream = logging.StreamHandler()
    stream.setLevel(stderr_level())
    _attach(root, stream, formatter)

    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT)
    except OSError:
        return  # no log file is worth a launcher that will not start
    handler.setLevel(logging.DEBUG)
    _attach(root, handler, formatter)


def reset() -> None:
    """Take paddock's handlers off, so the next setup reads the environment again. For tests."""
    root = logging.getLogger(ROOT)
    for handler in _HANDLERS:
        root.removeHandler(handler)
        handler.close()
    _HANDLERS.clear()


def _attach(root: logging.Logger, handler: logging.Handler, formatter: logging.Formatter) -> None:
    handler.setFormatter(formatter)
    root.addHandler(handler)
    _HANDLERS.append(handler)


def get_logger(name: str) -> logging.Logger:
    """The logger for one module. Call it with `__name__`, so the line names the layer."""
    if name != ROOT and not name.startswith(ROOT + "."):
        name = f"{ROOT}.{name}"
    return logging.getLogger(name)


def context(**fields: object) -> str:
    """`name=value` pairs for the end of a message: ids, paths and counts, never a secret.

    Anything not set is left out, so a line stays short enough to read.
    """
    return " ".join(f"{name}={value}" for name, value in fields.items() if value not in (None, ""))


# The credentials in a URL: srt's proxy keeps its password there, and other tools quote
# the whole URL back at us when they cannot reach it.
CREDENTIALS_IN_A_URL = re.compile(r"([a-zA-Z][\w+.-]*://)[^\s/@]*@")


# How the tools paddock shells out to spell "set this variable": herdr takes `--env`, msb
# takes `-e`. Both are redacted, so neither backend can put a value in the log by accident.
ENV_FLAGS = ("--env", "-e")


def redact_env(args: tuple[str, ...] | list[str]) -> str:
    """A command line with every environment value taken out. The name is kept, the value never is.

    An environment value is a token as often as it is a path, so none of them is written.
    Every spelling counts: `--env NAME=VALUE`, `--env=NAME=VALUE`, and msb's `-e NAME=VALUE`.
    """
    parts, next_is_env = [], False
    for arg in args:
        joined = next((flag for flag in ENV_FLAGS if arg.startswith(flag + "=")), "")
        if next_is_env:
            parts.append(arg.split("=", 1)[0] + "=...")
        elif joined:
            parts.append(f"{joined}=" + arg[len(joined) + 1 :].split("=", 1)[0] + "=...")
        else:
            parts.append(arg)
        next_is_env = arg in ENV_FLAGS
    return " ".join(parts)


def scrub(text: str) -> str:
    """Text with the credentials taken out of any URL in it, and the rest left as it was.

    Error messages from other programs quote what they were given, and what paddock is
    given includes a proxy URL with a password in it.
    """
    return CREDENTIALS_IN_A_URL.sub(r"\1...@", text)


def tail(path: Path, count: int) -> str:
    """The last `count` lines of a file, or a line saying there is nothing there yet."""
    try:
        lines = path.read_text(errors="replace").splitlines(keepends=True)
    except OSError:
        return f"paddock: nothing logged yet at {path}\n"
    return "".join(lines[-count:])
