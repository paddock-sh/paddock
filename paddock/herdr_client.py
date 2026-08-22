"""Subprocess wrapper over the herdr CLI: the one module that shells out to `herdr`."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from paddock import log

logger = log.get_logger(__name__)


class HerdrError(RuntimeError):
    """herdr is missing, refused the command, or answered with something unusable."""


class HerdrMissing(HerdrError):
    """No herdr on PATH at all, told apart from a refusal, which says something is wrong."""


def create_tab(cwd: Path, label: str = "", env: dict[str, str] | None = None) -> str:
    """Create a focused tab and return its pane id. `env` sets variables in the new pane."""
    args = ["tab", "create"]
    workspace = os.environ.get("HERDR_ACTIVE_WORKSPACE_ID")
    # Outside herdr there is no workspace to name, and herdr picks the active one anyway.
    if workspace:
        args += ["--workspace", workspace]
    args += ["--cwd", str(cwd)]
    if label:
        args += ["--label", label]
    for name, value in (env or {}).items():
        args += ["--env", f"{name}={value}"]
    args.append("--focus")

    output = _run(*args)
    try:
        return json.loads(output)["result"]["root_pane"]["pane_id"]
    except json.JSONDecodeError as error:
        raise HerdrError(f"herdr tab create returned no JSON: {output!r}") from error
    except (KeyError, TypeError) as error:
        raise HerdrError(f"herdr tab create returned no pane id: {output!r}") from error


def list_pane_ids() -> set[str]:
    """Every pane herdr has open right now. No workspace filter: a session's tabs can be anywhere.

    herdr sends no event paddock is around to hear, so this is how a closed tab is noticed
    (SPEC §3.4). An empty set is a real answer; herdr not answering raises.
    """
    output = _run("pane", "list")
    try:
        return {pane["pane_id"] for pane in json.loads(output)["result"]["panes"]}
    except json.JSONDecodeError as error:
        raise HerdrError(f"herdr pane list returned no JSON: {output!r}") from error
    except (KeyError, TypeError) as error:
        raise HerdrError(f"herdr pane list returned no pane ids: {output!r}") from error


def run_in_pane(pane_id: str, command: str) -> None:
    """Run a command in an existing pane. The command is one argument, quoted by the caller."""
    _run("pane", "run", pane_id, command)


def reload_config() -> None:
    """Ask a running herdr to re-read its config. Raises when there is no server to ask."""
    _run("server", "reload-config")


def check_config() -> str:
    """Ask herdr whether the config on disk is one it can use. Raises when it is not."""
    return _run("config", "check")


def _run(*args: str) -> str:
    # The values of `--env` are left out: one of them can be a token.
    called = log.redact_env(args)
    logger.debug("herdr %s", called)
    try:
        completed = subprocess.run(["herdr", *args], capture_output=True, text=True, check=True)
    except FileNotFoundError as error:
        logger.debug("herdr is not on PATH")
        raise HerdrMissing("herdr not found on PATH: paddock needs herdr 0.8.0") from error
    except subprocess.CalledProcessError as error:
        # The whole of what herdr said goes to the log; only the readable part of it is
        # raised. Both are scrubbed: herdr quotes back what it was given, proxy URL and all.
        logger.debug(
            "herdr failed %s",
            log.context(
                exit=error.returncode,
                stdout=log.scrub((error.stdout or "").strip()),
                stderr=log.scrub((error.stderr or "").strip()),
            ),
        )
        raise HerdrError(f"herdr {called} failed: {reason(error.stdout, error.stderr)}") from error
    logger.debug("herdr done %s", log.context(exit=0, output=f"{len(completed.stdout)} bytes"))
    return completed.stdout


def reason(stdout: str | None, stderr: str | None) -> str:
    """Why herdr refused, in something a user can read.

    Both streams are read, because herdr uses both: a refused command is a JSON error blob
    on stdout, and `herdr config check` prints every diagnostic there too and says nothing
    at all on stderr. Reading stderr alone is what made a failed check show up as an empty
    pair of parentheses.
    """
    said = [_message(text) for text in (stderr, stdout)]
    return log.scrub("; ".join(part for part in said if part)) or "herdr said nothing about why"


def _message(text: str | None) -> str:
    """One stream as a line: the message out of herdr's JSON error, or the text as it came.

    The blob herdr answers a refused command with is no use to anyone reading a screen. The
    whole of it is in the debug log, and the sentence inside it is what is shown.
    """
    text = (text or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text
    error = data.get("error") if isinstance(data, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) and message else text
