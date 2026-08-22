"""Subprocess wrapper over the herdr CLI — the one module that shells out to `herdr`."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


class HerdrError(RuntimeError):
    """herdr is missing, refused the command, or answered with something unusable."""


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


def run_in_pane(pane_id: str, command: str) -> None:
    """Run a command in an existing pane. The command is one argument, quoted by the caller."""
    _run("pane", "run", pane_id, command)


def reload_config() -> None:
    """Ask a running herdr to re-read its config. Raises when there is no server to ask."""
    _run("server", "reload-config")


def _run(*args: str) -> str:
    try:
        completed = subprocess.run(["herdr", *args], capture_output=True, text=True, check=True)
    except FileNotFoundError as error:
        raise HerdrError("herdr not found on PATH — paddock needs herdr 0.8.0") from error
    except subprocess.CalledProcessError as error:
        reason = (error.stderr or "").strip()
        raise HerdrError(f"herdr {' '.join(args)} failed: {reason}") from error
    return completed.stdout
