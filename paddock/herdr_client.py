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
        # herdr quotes back what it was given, proxy URL and all, so the reason is scrubbed
        # before it goes anywhere: this message is logged and shown to the user both.
        reason = log.scrub((error.stderr or "").strip())
        logger.debug("herdr failed %s", log.context(exit=error.returncode, stderr=reason))
        raise HerdrError(f"herdr {called} failed: {reason}") from error
    logger.debug("herdr done %s", log.context(exit=0, output=f"{len(completed.stdout)} bytes"))
    return completed.stdout
