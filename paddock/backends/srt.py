"""The srt backend: a settings file, a PATH shim dir, and the pane command that runs the agent.

srt is Anthropic's sandbox-runtime — Seatbelt on macOS, bubblewrap on Linux. It
enforces write paths, read denials and the domain allowlist. Tool selection is a
PATH shim dir, which is a soft allowlist: an absolute path still reaches any
binary on the host (SPEC §4.1).

`prepare()` gets a run ready on disk and `open_pane()` puts a tab on it. They are
separate because a session is prepared once and attached to many times (SPEC §3.2).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from paddock import herdr_client, state_dir, synth_config
from paddock.agents import AgentSpec, load_agents
from paddock.profiles import Profile
from paddock.synth_config import SynthConfig

INSTALL_COMMAND = "npm install -g @anthropic-ai/sandbox-runtime"

# All the sandbox inherits from the popup. Anything else — API tokens above all — stays out.
KEEP_ENV = ("HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "TMPDIR")

# srt sets these in the shell it spawns, per invocation: they point the sandbox at its own
# proxy, which is the only way out to the network. `env -i` would wipe them, so each is named
# in the command and expanded by that shell. No value is ever read from the popup (SPEC §2.1).
PROXY_ENV = (
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
    "ftp_proxy",
    "FTP_PROXY",
    "grpc_proxy",
    "GRPC_PROXY",
    "RSYNC_PROXY",
    "DOCKER_HTTP_PROXY",
    "DOCKER_HTTPS_PROXY",
    "npm_config_noproxy",
    "SANDBOX_RUNTIME",
    "GIT_CONFIG_PARAMETERS",
    "GIT_SSH_COMMAND",
)

# The prepared run, written into the run dir so a later tab can attach to the same policy.
LAUNCH_FILE = "launch.json"


class SrtNotFound(RuntimeError):
    """No `srt` on PATH and no `npx` to fetch it."""


class RunNotFound(RuntimeError):
    """The run dir holds no usable launch record, so nothing can attach to it."""


@dataclass
class Run:
    """One prepared sandbox: the settings and workdir every attached tab shares."""

    run_dir: Path
    workdir: Path
    command: str
    env: dict[str, str]


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


def build_settings(
    profile: Profile, agent: AgentSpec, workdir: Path, synth: SynthConfig
) -> dict:
    """The srt policy for one run. Every configured path is expanded here (SPEC §2.1).

    srt validates this against a schema and refuses to start if a key is missing, so
    every key is written even when its list is empty.
    """
    # /tmp and /private/tmp are one directory under two names on macOS; srt matches the
    # path as written. /dev/null is here so discarded output works.
    allow_write = [workdir, Path("/tmp"), Path("/private/tmp"), Path("/dev/null")]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        # The sandbox keeps TMPDIR (see KEEP_ENV), so what it points at has to be writable.
        allow_write += _both_names(Path(tmpdir))
    if profile.shared_dir:
        allow_write.append(_expand(profile.shared_dir))
    deny_read = [_expand(path) for path in profile.deny_read]
    # What the agent may not read, it may not write either.
    deny_write = list(deny_read)
    auth = [_expand(path) for path in agent.auth_read_paths]
    # The selected agent's own credentials, so a broad deny_read cannot lock it out.
    allow_read = list(auth)
    config_dirs = [_expand(path) for path in agent.config_write_paths]
    if synth.dir is None:
        # The agent has no synthesized config dir, so it writes to its real one. Blocking
        # it would break the agent (SPEC §2.1).
        allow_write += config_dirs
    else:
        # Layer 3: the agent writes in the synthesized dir instead, and its real config dir
        # is denied both ways — so the skills and MCP servers nobody ticked are not there
        # to read (SPEC §4.3).
        allow_write.append(synth.dir)
        # What that dir copied, the sandbox has its own of, so the host's is hidden too.
        deny_read += config_dirs + synth.copied
        deny_write += config_dirs + auth
        # What it symlinked is reached through the denied directory, and srt checks the
        # path an access resolves to. Allowing those by name re-opens exactly them.
        allow_read = [path for source in synth.linked for path in _both_names(source)]
    allow_write += [_expand(path) for path in profile.extra_allow_write]
    return {
        "network": {
            "allowedDomains": profile.allowed_domains(),
            "deniedDomains": [],
        },
        "filesystem": {
            "denyRead": _as_strings(deny_read),
            "allowRead": _as_strings(allow_read),
            "allowWrite": _as_strings(allow_write),
            "denyWrite": _as_strings(deny_write),
        },
    }


def pane_command(
    profile: Profile, agent: AgentSpec, settings: Path, shim: Path, synth: SynthConfig
) -> str:
    """The command `herdr pane run` executes: srt wrapping the agent on a shimmed PATH."""
    entries = [str(shim)]
    if profile.include_system_path:
        entries += ["/usr/bin", "/bin"]
    keep = [f"{name}={os.environ[name]}" for name in KEEP_ENV if os.environ.get(name)]
    # `env -i` wipes what the tab was given, so the config dir variable is set here too.
    keep += [f"{name}={value}" for name, value in synth.env.items()]
    path = "PATH=" + ":".join(entries)
    # Unquoted on purpose: srt's own shell is where these have values, and where they expand.
    proxied = " ".join(f'{name}="${name}"' for name in PROXY_ENV)
    rest = shlex.join([path, *shlex.split(agent.command), *synth.args])
    inner = f"{shlex.join(['env', '-i', *keep])} {proxied} {rest}"
    # -c takes the whole command as one string. Passed as bare words, srt's own parser
    # reads the agent's flags as its own.
    return shlex.join([*find_srt(), "--settings", str(settings), "-c", inner])


def prepare(profile: Profile) -> Run:
    """Get a run ready on disk: settings, shim dir, synthesized config, launch record.

    Opens no pane. Sessions decide when a tab appears and how many.
    """
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
    synth = synth_config.build(profile, agent, run_dir)
    if synth.missing:
        left_out = ", ".join(synth.missing)
        print(f"paddock: not in the sandbox config dir: {left_out}", file=sys.stderr)
    settings = run_dir / "srt-settings.json"
    settings.write_text(json.dumps(build_settings(profile, agent, workdir, synth), indent=2) + "\n")
    # Composed before any tab exists, so a missing srt fails with no pane left behind.
    command = pane_command(profile, agent, settings, shim, synth)

    run = Run(run_dir=run_dir, workdir=workdir, command=command, env=synth.env)
    (run_dir / LAUNCH_FILE).write_text(
        json.dumps({"workdir": str(workdir), "command": command, "env": synth.env}, indent=2) + "\n"
    )
    return run


def load_run(run_dir: Path) -> Run:
    """Read a prepared run back, so a later tab attaches to the same settings and workdir."""
    try:
        data = json.loads((run_dir / LAUNCH_FILE).read_text())
        return Run(run_dir, Path(data["workdir"]), str(data["command"]), dict(data["env"]))
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise RunNotFound(f"no usable launch record in {run_dir}") from error


def open_pane(run: Run, label: str = "", cwd: Path | None = None) -> str:
    """Open a tab on a prepared run and start the sandboxed agent in it. Returns the pane id."""
    pane_id = herdr_client.create_tab(cwd or run.workdir, label=label, env=run.env)
    herdr_client.run_in_pane(pane_id, run.command)
    return pane_id


def _expand(path: str) -> Path:
    return Path(path).expanduser()


def _both_names(path: Path) -> list[Path]:
    """A path as written and as resolved, the way /tmp and /private/tmp are both listed.

    srt matches the path as written, and checks the one an access resolves to.
    """
    return [path, Path(os.path.realpath(path))]


def _as_strings(paths) -> list[str]:
    """Paths as strings, in order, without repeats."""
    return list(dict.fromkeys(str(path) for path in paths))


def _is_plain_name(tool: str) -> bool:
    """A tool is a bare filename: `../escape` would put a symlink outside the shim dir."""
    return bool(tool) and "/" not in tool and not tool.startswith(".")
