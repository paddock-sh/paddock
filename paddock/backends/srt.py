"""The srt backend: a settings file, a PATH shim dir, and the pane command that runs the agent.

srt is Anthropic's sandbox-runtime — Seatbelt on macOS, bubblewrap on Linux. It
enforces write paths, read denials and the domain allowlist. Tool selection is a
PATH shim dir, which is a soft allowlist: an absolute path still reaches any
binary on the host (SPEC §4.1).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path

from paddock import herdr_client, state_dir
from paddock.agents import AgentSpec, load_agents
from paddock.profiles import Profile

INSTALL_COMMAND = "npm install -g @anthropic-ai/sandbox-runtime"

# All the sandbox inherits from the popup. Anything else — API tokens above all — stays out.
KEEP_ENV = ("HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "TMPDIR")


class SrtNotFound(RuntimeError):
    """No `srt` on PATH and no `npx` to fetch it."""


def find_srt() -> list[str]:
    """How to invoke srt: installed if it is there, through npx if it is not."""
    if shutil.which("srt"):
        return ["srt"]
    if shutil.which("npx"):
        return ["npx", "-y", "@anthropic-ai/sandbox-runtime"]
    raise SrtNotFound(f"srt not found, and no npx to run it. Install it with: {INSTALL_COMMAND}")


def new_run_dir() -> Path:
    """A fresh directory for this launch: its shim dir, settings file and scratch workdir."""
    runs = state_dir() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=runs))


def workdir_for(profile: Profile, run_dir: Path) -> Path:
    """The shared directory when the profile names one, otherwise isolated scratch."""
    workdir = _expand(profile.shared_dir) if profile.shared_dir else run_dir / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def build_shim_dir(run_dir: Path, tools: list[str]) -> tuple[Path, list[str]]:
    """Symlink the selected tools into a directory that becomes the sandbox PATH.

    Returns the directory and the tools it could not shim, for the caller to report.
    """
    shim = run_dir / "bin"
    shim.mkdir(parents=True, exist_ok=True)
    skipped = []
    for tool in tools:
        target = shutil.which(tool) if _is_plain_name(tool) else None
        if target is None:
            skipped.append(tool)
            continue
        link = shim / tool
        if not link.is_symlink():
            link.symlink_to(target)
    return shim, skipped


def build_settings(profile: Profile, agent: AgentSpec, workdir: Path) -> dict:
    """The srt policy for one run. Every configured path is expanded here (SPEC §2.1).

    srt validates this against a schema and refuses to start if a key is missing, so
    every key is written even when its list is empty.
    """
    # /tmp and /private/tmp are one directory under two names on macOS; srt matches the
    # path as written. /dev/null is here so discarded output works.
    allow_write = [workdir, Path("/tmp"), Path("/private/tmp"), Path("/dev/null")]
    if profile.shared_dir:
        allow_write.append(_expand(profile.shared_dir))
    # Known gap: this is the agent's real config dir. Blocking it breaks the agent; the
    # synthesized config dir (SPEC §4.3) closes it by pointing the agent somewhere else.
    allow_write += [_expand(path) for path in agent.config_write_paths]
    allow_write += [_expand(path) for path in profile.extra_allow_write]
    deny_read = [_expand(path) for path in profile.deny_read]
    return {
        "network": {
            "allowedDomains": profile.allowed_domains(),
            "deniedDomains": [],
        },
        "filesystem": {
            "denyRead": _as_strings(deny_read),
            # The selected agent's own credentials, so a broad deny_read cannot lock it out.
            "allowRead": _as_strings(_expand(path) for path in agent.auth_read_paths),
            "allowWrite": _as_strings(allow_write),
            # What the agent may not read, it may not write either.
            "denyWrite": _as_strings(deny_read),
        },
    }


def pane_command(profile: Profile, agent: AgentSpec, settings: Path, shim: Path) -> str:
    """The command `herdr pane run` executes: srt wrapping the agent on a shimmed PATH."""
    entries = [str(shim)]
    if profile.include_system_path:
        entries += ["/usr/bin", "/bin"]
    keep = [f"{name}={os.environ[name]}" for name in KEEP_ENV if os.environ.get(name)]
    path = "PATH=" + ":".join(entries)
    inner = shlex.join(["env", "-i", *keep, path, *shlex.split(agent.command)])
    # -c takes the whole command as one string. Passed as bare words, srt's own parser
    # reads the agent's flags as its own.
    return shlex.join([*find_srt(), "--settings", str(settings), "-c", inner])


def launch(profile: Profile) -> str:
    """Set up the run, open a sandboxed tab, start the agent in it. Returns the pane id."""
    agent = load_agents().get(profile.agent)
    if agent is None:
        raise ValueError(f"profile {profile.name!r} names an unknown agent: {profile.agent!r}")

    run_dir = new_run_dir()
    workdir = workdir_for(profile, run_dir)
    # The agent runs on the shimmed PATH like everything else, so it needs a shim of its own.
    # An agent named by absolute path — the shell, say — is found without one.
    tools = list(profile.tools) + shlex.split(agent.command)[:1]
    shim, skipped = build_shim_dir(run_dir, tools)
    if skipped:
        print(f"paddock: left off the sandbox PATH: {', '.join(skipped)}", file=sys.stderr)
    settings = run_dir / "srt-settings.json"
    settings.write_text(json.dumps(build_settings(profile, agent, workdir), indent=2) + "\n")
    # Composed before the tab exists, so a missing srt fails with no pane left behind.
    command = pane_command(profile, agent, settings, shim)

    # Sessions arrive in the next feature; until then the label names the profile (SPEC §3.5).
    pane_id = herdr_client.create_tab(workdir, label=f"sbx:{profile.name}")
    herdr_client.run_in_pane(pane_id, command)
    return pane_id


def launch_local(cwd: Path) -> str:
    """The chooser's other branch: an ordinary tab, no sandbox, no label."""
    return herdr_client.create_tab(cwd)


def _expand(path: str) -> Path:
    return Path(path).expanduser()


def _as_strings(paths) -> list[str]:
    """Paths as strings, in order, without repeats."""
    return list(dict.fromkeys(str(path) for path in paths))


def _is_plain_name(tool: str) -> bool:
    """A tool is a bare filename: `../escape` would put a symlink outside the shim dir."""
    return bool(tool) and "/" not in tool and not tool.startswith(".")
