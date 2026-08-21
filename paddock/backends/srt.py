"""The srt backend: a settings file, a PATH shim dir, and the pane command that runs the agent.

srt is Anthropic's sandbox-runtime — Seatbelt on macOS, bubblewrap on Linux. It
enforces write paths, read denials and the domain allowlist. Tool selection is a
PATH shim dir, which is a soft allowlist: an absolute path still reaches any
binary on the host (SPEC §4.1).
"""

from __future__ import annotations

import json
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


def build_shim_dir(run_dir: Path, tools: list[str]) -> Path:
    """Symlink the selected tools into a directory that becomes the sandbox PATH."""
    shim = run_dir / "bin"
    shim.mkdir(parents=True, exist_ok=True)
    missing = []
    for tool in tools:
        target = shutil.which(tool)
        if target is None:
            missing.append(tool)
            continue
        link = shim / tool
        if not link.is_symlink():
            link.symlink_to(target)
    if missing:
        print(f"paddock: not on the host PATH, skipped: {', '.join(missing)}", file=sys.stderr)
    return shim


def build_settings(profile: Profile, agent: AgentSpec, run_dir: Path, workdir: Path) -> dict:
    """The srt policy for one run. Every configured path is expanded here (SPEC §2.1)."""
    allow_write = [workdir, run_dir, Path("/tmp"), Path("/private/tmp"), Path("/dev/null")]
    if profile.shared_dir:
        allow_write.append(_expand(profile.shared_dir))
    allow_write += [_expand(path) for path in agent.config_write_paths]
    allow_write += [_expand(path) for path in profile.extra_allow_write]
    return {
        "network": {"allowedDomains": profile.allowed_domains()},
        "filesystem": {
            "denyRead": [str(_expand(path)) for path in profile.deny_read],
            # srt allows reads and denies writes by default, so both lists stay empty.
            "allowRead": [],
            "allowWrite": list(dict.fromkeys(str(path) for path in allow_write)),
            "denyWrite": [],
        },
    }


def pane_command(profile: Profile, agent: AgentSpec, settings: Path, shim: Path) -> str:
    """The command `herdr pane run` executes: srt wrapping the agent on a shimmed PATH."""
    entries = [str(shim)]
    if profile.include_system_path:
        entries += ["/usr/bin", "/bin"]
    inner = f"env PATH={shlex.quote(':'.join(entries))} {agent.command}"
    return shlex.join([*find_srt(), "--settings", str(settings), inner])


def launch(profile: Profile) -> str:
    """Set up the run, open a sandboxed tab, start the agent in it. Returns the pane id."""
    agent = load_agents().get(profile.agent)
    if agent is None:
        raise ValueError(f"profile {profile.name!r} names an unknown agent: {profile.agent!r}")

    run_dir = new_run_dir()
    workdir = workdir_for(profile, run_dir)
    # The agent runs on the shimmed PATH like everything else, so it needs a shim of its own.
    # An agent named by absolute path — the shell, say — is found without one.
    tools = list(profile.tools)
    if "/" not in agent.command:
        tools.append(agent.command)
    shim = build_shim_dir(run_dir, tools)
    settings = run_dir / "srt-settings.json"
    body = json.dumps(build_settings(profile, agent, run_dir, workdir), indent=2)
    settings.write_text(body + "\n")
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
