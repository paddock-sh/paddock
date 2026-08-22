"""The srt backend: a settings file, a PATH shim dir, and the pane command that runs the agent.

srt is Anthropic's sandbox-runtime: Seatbelt on macOS, bubblewrap on Linux. It
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
from dataclasses import dataclass, replace
from pathlib import Path

from paddock import herdr_client, log, synth_config
from paddock.agents import AgentSpec, load_agents
from paddock.backends import (
    LAUNCH_FILE,
    LAUNCH_SCRIPT,
    SHELL_SCRIPT,
    RunNotFound,
    ensure_launch_script,
    launch_line,
    new_run_dir,
    write_launch_script,
)
from paddock.profiles import Profile
from paddock.synth_config import SynthConfig

logger = log.get_logger(__name__)

INSTALL_COMMAND = "npm install -g @anthropic-ai/sandbox-runtime"

# All the sandbox inherits from the popup. Anything else, API tokens above all, stays out.
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


class SrtNotFound(RuntimeError):
    """No `srt` on PATH and no `npx` to fetch it."""


@dataclass
class Run:
    """One prepared sandbox: the settings and workdir every attached tab shares."""

    run_dir: Path
    workdir: Path
    command: str
    env: dict[str, str]
    # The same sandbox entered as a plain shell (SPEC §3.2). Blank on a run prepared
    # before paddock could do that, which is a message rather than a broken tab.
    shell_command: str = ""


def find_srt() -> list[str]:
    """How to invoke srt: installed if it is there, through npx if it is not."""
    if shutil.which("srt"):
        return ["srt"]
    if shutil.which("npx"):
        return ["npx", "-y", "@anthropic-ai/sandbox-runtime"]
    raise SrtNotFound(f"srt not found, and no npx to run it. Install it with: {INSTALL_COMMAND}")


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
        # is denied both ways, so the skills and MCP servers nobody ticked are not there
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
        # A TUI agent puts its terminal in raw mode. Without this the sandbox is denied the
        # ioctl on /dev/ttys*, so claude draws gibberish and codex exits at once. srt grants
        # every terminal the user owns, not just this pane: the trade is in SPEC §2.1.
        "allowPty": True,
    }


def pane_command(
    profile: Profile, agent: AgentSpec, settings: Path, shim: Path, synth: SynthConfig
) -> str:
    """The command the launch script holds: srt wrapping the agent on a shimmed PATH."""
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


def host_shell() -> str:
    """The shell a shell tab runs. The user's, as the `shell` agent uses it."""
    return os.environ.get("SHELL") or "/bin/sh"


def shell_command(profile: Profile, settings: Path, shim: Path, synth: SynthConfig) -> str:
    """The same sandbox entered as a plain shell: one settings file, one workdir, no agent.

    Composed the way the agent's own command is, so the policy, the PATH and the kept
    environment cannot drift apart between the two. The config dir variable stays set, so
    a shell that runs the agent by hand gets the same synthesized config. The agent's
    command-line flags do not come with it: they are the agent's, not the shell's.
    """
    shell = AgentSpec(name="shell", command=host_shell())
    return pane_command(profile, shell, settings, shim, replace(synth, args=[]))


def prepare(profile: Profile) -> Run:
    """Get a run ready on disk: settings, shim dir, synthesized config, launch record.

    Opens no pane. Sessions decide when a tab appears and how many.
    """
    agent = load_agents().get(profile.agent)
    if agent is None:
        raise ValueError(f"profile {profile.name!r} names an unknown agent: {profile.agent!r}")

    run_dir = new_run_dir()
    workdir = workdir_for(profile, run_dir)
    logger.debug(
        "prepare %s",
        log.context(
            profile=profile.name, agent=profile.agent, run_dir=run_dir, workdir=workdir
        ),
    )
    # The agent runs on the shimmed PATH like everything else, so it needs a shim of its own,
    # and one for whatever it cannot start without: `codex` is a script that runs `node`.
    # An agent named by absolute path (the shell, say) is found without one. Choosing the
    # agent is what put these here, and every screen that says what can run names them.
    tools = list(
        dict.fromkeys(list(profile.tools) + shlex.split(agent.command)[:1] + agent.required_tools)
    )
    shim, skipped = build_shim_dir(run_dir, tools)
    logger.debug(
        "shim dir %s",
        log.context(dir=shim, shimmed=len(tools) - len(skipped), skipped=", ".join(skipped)),
    )
    # A name with a slash is the agent's own command. It runs without a shim, so it is
    # reported as how it runs, not as a tool that went missing.
    missing = [tool for tool in skipped if "/" not in tool]
    if missing:
        print(f"paddock: left off the sandbox PATH: {', '.join(missing)}", file=sys.stderr)
    for tool in (tool for tool in skipped if "/" in tool):
        print(
            f"paddock: {tool} runs by its absolute path; "
            "only bare tool names go on the sandbox PATH",
            file=sys.stderr,
        )
    synth = synth_config.build(profile, agent, run_dir)
    if synth.missing:
        left_out = ", ".join(synth.missing)
        print(f"paddock: not in the sandbox config dir: {left_out}", file=sys.stderr)
    settings = run_dir / "srt-settings.json"
    text = json.dumps(build_settings(profile, agent, workdir, synth), indent=2) + "\n"
    settings.write_text(text)
    logger.debug("settings written %s", log.context(path=settings, size=f"{len(text)} bytes"))
    # Composed before any tab exists, so a missing srt fails with no pane left behind.
    command = pane_command(profile, agent, settings, shim, synth)
    shell = shell_command(profile, settings, shim, synth)
    script = write_launch_script(run_dir, command)
    write_launch_script(run_dir, shell, SHELL_SCRIPT)
    # The length, never the command: it carries every environment value the sandbox keeps.
    logger.debug("launch script %s", log.context(path=script, command=f"{len(command)} chars"))

    run = Run(
        run_dir=run_dir, workdir=workdir, command=command, env=synth.env, shell_command=shell
    )
    (run_dir / LAUNCH_FILE).write_text(
        json.dumps(
            {
                "workdir": str(workdir),
                "command": command,
                "env": synth.env,
                "shell_command": shell,
            },
            indent=2,
        )
        + "\n"
    )
    return run


def load_run(run_dir: Path) -> Run:
    """Read a prepared run back, so a later tab attaches to the same settings and workdir.

    The script is rewritten when it is not the one this paddock would write, which covers
    a run dir prepared before paddock wrote scripts at all (it has none, and the pane runs
    it) as well as one whose script an upgrade has moved on from. The record holds the
    exact command either way, so a session gets today's launch behaviour on its next tab.
    """
    try:
        data = json.loads((run_dir / LAUNCH_FILE).read_text())
        run = Run(
            run_dir,
            Path(data["workdir"]),
            str(data["command"]),
            dict(data["env"]),
            # Absent on a run prepared before shell tabs existed, which open_pane reports.
            str(data.get("shell_command", "")),
        )
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise RunNotFound(f"no usable launch record in {run_dir}") from error
    ensure_launch_script(run_dir, run.command)
    if run.shell_command:
        ensure_launch_script(run_dir, run.shell_command, SHELL_SCRIPT)
    return run


def open_pane(run: Run, label: str = "", cwd: Path | None = None, shell: bool = False) -> str:
    """Open a tab on a prepared run and start the sandboxed agent in it. Returns the pane id.

    `shell` runs the user's shell under the same settings file and workdir instead, so the
    tab is inside the sandbox the agent is in without being a second agent (SPEC §3.2).
    """
    if shell and not run.shell_command:
        raise ValueError(
            f"the session in {run.run_dir} was prepared before paddock could open a shell "
            "in it, so start a new session to get one"
        )
    pane_id = herdr_client.create_tab(cwd or run.workdir, label=label, env=run.env)
    line = launch_line(run.run_dir, SHELL_SCRIPT if shell else LAUNCH_SCRIPT)
    herdr_client.run_in_pane(pane_id, line)
    logger.debug("pane opened %s", log.context(pane=pane_id, label=label, shell=shell, line=line))
    return pane_id


def sweep(known: set[str]) -> list[str]:
    """Nothing outlives the process here, so a sweep finds nothing and removes nothing."""
    return []


def collect(run_dir: Path, vm_handle: str = "") -> None:
    """Nothing is left running: an srt session is a settings file and a workdir (SPEC §3.2)."""


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
