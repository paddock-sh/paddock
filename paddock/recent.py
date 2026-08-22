"""What the chooser opens on: the profile each workspace launched last.

One small file in the state dir, written after a launch and read when the popup opens. It is
a convenience and nothing else, so a missing, unreadable or wrong-shaped one costs the
convenience silently and paddock's own defaults stand instead.

Herdr tells the popup which workspace it is in (SPEC 1.2), and a workspace with no memory of
its own falls back to whatever was launched last anywhere: a second workspace should start
where you left off, not from nothing.
"""

from __future__ import annotations

import json
import os

from paddock import state_dir

MEMORY_FILE = "last-profile.json"

# The key a launch outside any herdr workspace is remembered under, and the fallback for a
# workspace that has launched nothing yet.
ANYWHERE = ""


def workspace() -> str:
    """The herdr workspace the popup was opened in, or blank when there is none."""
    return os.environ.get("HERDR_ACTIVE_WORKSPACE_ID", ANYWHERE)


def last_profile() -> str:
    """The profile this workspace launched last, else the one anything launched last."""
    remembered = _read()
    return remembered.get(workspace(), "") or remembered.get(ANYWHERE, "")


def remember(name: str) -> None:
    """Keep `name` as this workspace's last launch, and as the fallback for every other one."""
    if not name:
        return
    remembered = _read()
    remembered[workspace()] = name
    remembered[ANYWHERE] = name
    path = state_dir() / MEMORY_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(remembered, indent=2) + "\n")
    except OSError:
        pass  # a launch is worth more than the memory of it


def _read() -> dict[str, str]:
    """The memory as it stands, or an empty one if it cannot be used as written."""
    try:
        remembered = json.loads((state_dir() / MEMORY_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(remembered, dict):
        return {}
    return {key: value for key, value in remembered.items() if isinstance(value, str)}
