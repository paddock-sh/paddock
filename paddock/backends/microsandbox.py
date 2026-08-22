"""The microsandbox backend: one persistent microVM per session, attached to by `msb exec`.

`msb` boots an OCI image in a libkrun microVM. The guest has its own kernel and its own
filesystem, so the host is not there to be denied: only what is mounted exists (SPEC §2.2).
There is no daemon. Each running VM is one `msb` process on the host.

`prepare()` boots the VM and `open_pane()` execs into it, which is why tabs on one msb
session share a process namespace and tabs on an srt session do not (SPEC §3.2).
`collect()` destroys the VM, because nothing else will.

An agent needs two more things than a shell: its image, and a boot script that installs it
when the image does not ship it. Layer 3 comes in as a mount plus one variable, so the
guest reads the same synthesized config dir srt does (SPEC §4.3).

`_run` is the one place this shells out to msb, and the seam every test stands in for.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from paddock import herdr_client, log, synth_config
from paddock.agents import AgentSpec, load_agents
from paddock.backends import (
    LAUNCH_FILE,
    LAUNCH_SCRIPT,
    SHELL_SCRIPT,
    RunNotFound,
    SandboxGone,
    ensure_launch_script,
    launch_line,
    new_run_dir,
    write_launch_script,
)
from paddock.profiles import Profile
from paddock.synth_config import SynthConfig

logger = log.get_logger(__name__)

INSTALL_COMMAND = "curl -fsSL https://install.microsandbox.dev | sh"

# Small, and enough for a shell. An agent entry with an `image` overrides it (SPEC §5).
DEFAULT_IMAGE = "alpine"

# Where the session's workdir is mounted, and where a tab starts.
GUEST_WORKDIR = "/work"

# The synthesized config dir arrives read-only at GUEST_CONFIG_SRC and is copied to
# GUEST_CONFIG on the guest's own overlay, which is what the agent's variable names. So the
# guest reads what the run dir holds and writes only to a copy that dies with the VM (§4.3).
GUEST_CONFIG_SRC = "/paddock-config-src"
GUEST_CONFIG = "/paddock-config"

# The agent that means "the guest's own shell": there is nothing to install and nothing to
# point at a config dir, and the image's default shell is what a tab attaches to.
SHELL_AGENT = "shell"

# What a shell tab execs in the guest. Named, not left to the image: `msb exec` with no argv
# runs the image's own command, which for node:22-slim is the Node REPL and not a shell.
# Every image this backend boots already has to have it: the boot script and the config copy
# both run through `/bin/sh -c`.
GUEST_SHELL = "/bin/sh"

# What msb allows the boot-time execs, the install and the config copy. The install is the
# one slow step: 21s for claude, on top of a first image pull. Shorter than COMMAND_TIMEOUT
# so msb usually reports a slow command itself, but not always: an exec that never starts
# in the guest is not on msb's clock, and one such hang was caught by COMMAND_TIMEOUT here.
BOOT_TIMEOUT = "110s"

# Sandbox names have to be unique on the host, not just in this registry.
HANDLE_PREFIX = "paddock-"

# msb has hung once with no timeout (see the spike). Nothing here takes a minute, except a
# first image pull, and a popup that never returns is worse than one that says why.
COMMAND_TIMEOUT = 120


class MsbNotFound(RuntimeError):
    """No `msb` on PATH."""


class MsbError(RuntimeError):
    """msb refused a command."""


@dataclass
class Run:
    """One prepared session: the VM every attached tab execs into, and its mounted workdir."""

    run_dir: Path
    workdir: Path
    vm_handle: str
    command: str
    # The same guest entered as its own shell, alongside whatever the agent tab is doing:
    # `msb exec` joins the running VM, so both are in one process namespace (SPEC §3.2).
    shell_command: str = ""


def find_msb() -> str:
    """How to invoke msb. There is no fallback: it is a hypervisor, not an npm package."""
    if shutil.which("msb"):
        return "msb"
    raise MsbNotFound(f"msb not found on PATH. Install it with: {INSTALL_COMMAND}")


def vm_handle(run_dir: Path) -> str:
    """The sandbox name, and the only handle msb needs. The run dir already has a unique one."""
    return f"{HANDLE_PREFIX}{run_dir.name}"


def workdir_for(profile: Profile, run_dir: Path) -> Path:
    """The host directory mounted into the guest: the shared one, or scratch in the run dir.

    Resolved, because msb mounts the source as written and `/tmp/x` is really
    `/private/tmp/x` on macOS, which fails as a mount source (see the spike).
    """
    workdir = Path(profile.shared_dir).expanduser() if profile.shared_dir else run_dir / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir.resolve()


def net_rules(domains: list[str]) -> list[str]:
    """Deny everything, then allow one host per domain the profile named.

    A rule names a host, a protocol and a port, so this is https to those hosts and
    nothing else. No domains at all means no network, and no DNS to resolve it with.
    """
    rules = ["--net-default", "deny"]
    if not domains:
        return rules
    rules += ["--net-rule", "allow@dns"]
    for domain in domains:
        rules += ["--net-rule", f"allow@{domain}:tcp:443"]
    return rules


def create_argv(
    handle: str, image: str, workdir: Path, domains: list[str], synth: SynthConfig
) -> list[str]:
    """The command that boots the session's VM. The image is positional, so it comes last.

    A synthesized config dir is mounted read-only: the guest gets its own copy of it
    (`copy_config_argv`), so nothing it writes there reaches the host. `-e` names that copy,
    and reaches every later exec, including the shell a tab attaches to, which is why the
    guest needs nothing from the host tab.
    """
    argv = [
        find_msb(),
        "create",
        "--name",
        handle,
        "--mount-dir",
        f"{workdir}:{GUEST_WORKDIR}",
        "--workdir",
        GUEST_WORKDIR,
    ]
    if synth.dir is not None:
        # Resolved for the same reason the workdir is: msb mounts the source as written.
        argv += ["--mount-dir", f"{synth.dir.resolve()}:{GUEST_CONFIG_SRC}:ro"]
        for name, value in synth.env.items():
            argv += ["-e", f"{name}={value}"]
    return argv + [*net_rules(domains), image]


def copy_config_argv(handle: str) -> list[str]:
    """Copy the mounted config dir onto the guest's own filesystem, once, before the first tab.

    Run after the credentials are placed, so the copy has them. What the agent then writes
    to its config, the generated MCP whitelist included, stays in the guest and goes when
    the VM does (SPEC §4.3).
    """
    return [
        find_msb(),
        "exec",
        "--timeout",
        BOOT_TIMEOUT,
        handle,
        "--",
        "/bin/sh",
        "-c",
        f"mkdir -p {GUEST_CONFIG} && cp -a {GUEST_CONFIG_SRC}/. {GUEST_CONFIG}/",
    ]


def boot_script(agent: AgentSpec) -> str:
    """Install the agent in the guest, unless the image already has it.

    Asked first, so a custom image with the agent baked in pays nothing. A stock image
    pays the install once per session: a new sandbox is a fresh clone of the image, so
    msb's layer cache saves the pull and not the install (SPEC §2.2).
    """
    tool = shlex.split(agent.command)[0]
    return f"command -v {shlex.quote(tool)} >/dev/null 2>&1 || {agent.install}"


def boot_argv(handle: str, agent: AgentSpec) -> list[str]:
    """The one exec between booting the VM and the first tab: put the agent in the guest."""
    return [
        find_msb(),
        "exec",
        "--timeout",
        BOOT_TIMEOUT,
        handle,
        "--",
        "/bin/sh",
        "-c",
        boot_script(agent),
    ]


def attach_command(handle: str, agent_argv: list[str]) -> str:
    """What the launch script holds: a terminal in the VM the session already has.

    Running the agent, or, with nothing to run, the image's own shell.
    """
    argv = [find_msb(), "exec", "--tty", handle]
    if agent_argv:
        argv += ["--", *agent_argv]
    return shlex.join(argv)


def stop_argv(handle: str) -> list[str]:
    """`msb rm` refuses a running sandbox without -f (`still running`, exit 1), so -f stays."""
    return [find_msb(), "rm", "-f", handle]


def list_argv() -> list[str]:
    """msb's only machine-readable view of what it is running."""
    return [find_msb(), "ls", "--format", "json"]


def _listed() -> list[dict]:
    """What msb is running, as records. The one place its `ls` output is read."""
    output = _run(*list_argv())
    try:
        listed = json.loads(output or "[]")
    except ValueError as error:
        raise MsbError(f"msb ls did not answer with JSON: {output!r}") from error
    if not isinstance(listed, list):
        raise MsbError(f"msb ls did not answer with a list of sandboxes: {output!r}")
    return [entry for entry in listed if isinstance(entry, dict)]


def vm_status(handle: str) -> str | None:
    """What msb says about this sandbox: its status, or None when msb has no such name."""
    for entry in _listed():
        if entry.get("name") == handle:
            return str(entry.get("status", "")).lower()
    return None


def vm_is_running(handle: str) -> bool:
    """Can a tab exec into this VM? A stopped sandbox still has a record, and cannot.

    Asked before a tab is opened, because `msb exec` into a VM that is gone fails after
    the pane exists, which leaves a dead tab and a pane id nothing can use.
    """
    return vm_status(handle) == "running"


def stop_vm(handle: str) -> None:
    """Try to destroy the VM, running or stopped. Best effort: a pane closing cannot raise.

    A sandbox that is only stopped still holds its disk, so it is removed like any other.
    One msb has never heard of is not an error. Anything else leaves the VM up, so the
    message has to say which one, and how to finish the job by hand.
    """
    try:
        if vm_status(handle) is None:
            return
        _run(*stop_argv(handle))
    except (MsbError, MsbNotFound) as error:
        print(
            f"paddock: could not remove the microVM {handle}: {error}. "
            f"It may still be running. Remove it with: msb rm -f {handle}",
            file=sys.stderr,
        )


def prepare(profile: Profile) -> Run:
    """Boot the session's VM, put the agent in it, and write what a tab needs to attach.

    Opens no pane. The guest holds what the image holds, so an agent without one is
    refused here, before a VM exists.
    """
    agent = load_agents().get(profile.agent)
    if agent is None:
        raise ValueError(f"profile {profile.name!r} names an unknown agent: {profile.agent!r}")
    if profile.agent != SHELL_AGENT and not agent.image:
        raise ValueError(
            f"agent {profile.agent!r} has no image, so the msb backend has nothing to run it "
            "in: give the agent an `image` in the registry, or launch it on the srt backend"
        )
    # Before the run dir exists, so a missing msb leaves nothing behind.
    find_msb()

    run_dir = new_run_dir()
    workdir = workdir_for(profile, run_dir)
    handle = vm_handle(run_dir)
    logger.debug(
        "prepare %s",
        log.context(
            profile=profile.name,
            agent=profile.agent,
            image=agent.image or DEFAULT_IMAGE,
            run_dir=run_dir,
            workdir=workdir,
            vm=handle,
            domains=len(profile.allowed_domains()),
        ),
    )
    # Without the token: nothing can use it until the agent is installed, and everything
    # between here and there can fail (SPEC §4.3).
    synth = synth_config.build(
        profile, agent, run_dir, guest_dir=GUEST_CONFIG, defer_credentials=True
    )
    if synth.missing:
        left_out = ", ".join(synth.missing)
        print(f"paddock: not in the guest config dir: {left_out}", file=sys.stderr)
    if synth.dir is None and profile.agent != SHELL_AGENT:
        print(
            f"paddock: no config dir redirection for {profile.agent!r}, so it starts "
            "unauthenticated in the guest: nothing carries its credentials in",
            file=sys.stderr,
        )
    image = agent.image or DEFAULT_IMAGE
    try:
        _run(*create_argv(handle, image, workdir, profile.allowed_domains(), synth))
        if agent.install:
            _provision(handle, agent, image)
        if synth.dir is not None:
            # Only now: a launch that got no further never wrote a token to disk.
            synth_config.place_credentials(profile, agent, run_dir)
            _run(*copy_config_argv(handle))
        command = attach_command(handle, _agent_argv(profile, agent, synth))
        # Named rather than left to the image, and the guest's own rather than the host's:
        # a host `$SHELL` need not exist in there (see `_agent_argv`).
        shell = attach_command(handle, [GUEST_SHELL])
        script = write_launch_script(run_dir, command)
        write_launch_script(run_dir, shell, SHELL_SCRIPT)
        # The length, never the command: the same rule the srt backend writes under.
        logger.debug("launch script %s", log.context(path=script, command=f"{len(command)} chars"))
        (run_dir / LAUNCH_FILE).write_text(
            json.dumps(
                {
                    "vm_handle": handle,
                    "workdir": str(workdir),
                    "command": command,
                    "shell_command": shell,
                },
                indent=2,
            )
            + "\n"
        )
    except BaseException:
        # BaseException, not Exception: ctrl-c is a KeyboardInterrupt and nearly all of the
        # 20 to 70 seconds this takes is after the VM exists, so catching only Exception
        # left a microVM running that nothing would ever collect. Nothing has registered
        # this session, so a VM or a token left here is orphaned. Both go, and the bare
        # `raise` reports the failure as it was: a KeyboardInterrupt is never wrapped.
        synth_config.discard_credentials(run_dir)
        stop_vm(handle)
        raise
    return Run(
        run_dir=run_dir,
        workdir=workdir,
        vm_handle=handle,
        command=command,
        shell_command=shell,
    )


def load_run(run_dir: Path) -> Run:
    """Read a prepared run back, so a later tab execs into the VM the first one booted.

    The script is rewritten when it is not the one this paddock would write, exactly as it
    is on the srt backend: the attach line is the backend's, but the pane around it, its
    log and its hold on a failed launch, is the same for both (SPEC §9).
    """
    try:
        data = json.loads((run_dir / LAUNCH_FILE).read_text())
        run = Run(
            run_dir,
            Path(data["workdir"]),
            str(data["vm_handle"]),
            str(data["command"]),
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
    """Open a tab and exec a shell into the session's VM. Returns the pane id.

    The tab's own directory is the host side of the mount, so the pane and the guest
    shell are looking at the same files.

    `shell` execs the image's own shell instead of the agent. `msb exec` joins the VM that
    is already running, so it lands in the same guest, the same /work and the same process
    namespace as the agent tab (SPEC §3.2).
    """
    if cwd is not None:
        raise ValueError(
            f"an msb session always opens in the guest workdir {GUEST_WORKDIR}: "
            f"{cwd} would only set the host tab's own directory, which the guest shell replaces"
        )
    if shell and not run.shell_command:
        raise ValueError(
            f"the session in {run.run_dir} was prepared before paddock could open a shell "
            "in it, so start a new session to get one"
        )
    if not vm_is_running(run.vm_handle):
        raise SandboxGone(f"the microVM {run.vm_handle} is not running any more")
    pane_id = herdr_client.create_tab(run.workdir, label=label)
    line = launch_line(run.run_dir, SHELL_SCRIPT if shell else LAUNCH_SCRIPT)
    herdr_client.run_in_pane(pane_id, line)
    logger.debug(
        "pane opened %s",
        log.context(pane=pane_id, label=label, vm=run.vm_handle, shell=shell, line=line),
    )
    return pane_id


def sweep(known: set[str]) -> list[str]:
    """Destroy paddock's own sandboxes that no session claims. Returns the handles removed.

    The rollback in `prepare` covers the interruptions it survives, ctrl-c included. A
    process killed outright cannot roll anything back, and what that leaves is a microVM
    nothing would ever collect, so `paddock gc` sweeps for them (SPEC §8). Only handles this
    paddock would have made are touched: another tool's sandboxes are none of its business.
    """
    if not shutil.which("msb"):
        return []  # no msb on this machine, so no microVM of its making is running
    removed = []
    for entry in _listed():
        name = str(entry.get("name", ""))
        if name.startswith(HANDLE_PREFIX) and name not in known:
            stop_vm(name)
            removed.append(name)
    return removed


def collect(run_dir: Path, vm_handle: str = "") -> None:
    """Destroy the session's VM. The run dir names it, and the registry's handle is the fallback."""
    try:
        vm_handle = load_run(run_dir).vm_handle
    except RunNotFound:
        pass  # the run dir lost its record, so the caller's handle is all there is
    logger.debug("collect %s", log.context(run_dir=run_dir, vm=vm_handle))
    if vm_handle:
        stop_vm(vm_handle)


def _agent_argv(profile: Profile, agent: AgentSpec, synth: SynthConfig) -> list[str]:
    """What a tab runs in the guest: the agent and its config flags, or the guest's own shell.

    The shell agent's command is a host path (`$SHELL`), which the image need not have, so
    a shell session attaches to whatever shell the image ships instead. An empty list is
    the only thing that means "the image's shell": a registry entry with no command is
    rejected when it is loaded, so no other agent can produce one.
    """
    if profile.agent == SHELL_AGENT:
        return []
    return [*shlex.split(agent.command), *synth.args]


def _provision(handle: str, agent: AgentSpec, image: str) -> None:
    """Run the boot script, saying what failed. prepare's rollback takes the VM down.

    The usual failure is a profile whose network does not reach the install's registry,
    which the message has to say plainly.
    """
    try:
        _run(*boot_argv(handle, agent))
    except MsbError as error:
        raise MsbError(
            f"could not install {agent.command!r} in the {image} guest. The profile's "
            f"network has to allow whatever `{agent.install}` downloads: {error}"
        ) from error


def _run(*args: str) -> str:
    """The one place this shells out to msb, and so the one place its argv is logged.

    Every msb command goes through here: create, exec, ls and rm. The argv is a summary
    of mounts and names, which is what makes a boot that failed explicable at all, but the
    values of `-e` are left out on principle, the way herdr's `--env` are. Nothing msb is
    given today is secret (a config dir is mounted, never a token passed), and this keeps
    that true of anything added later.
    """
    called = log.redact_env(args[1:])
    logger.debug("msb %s", called)
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, check=True, timeout=COMMAND_TIMEOUT
        )
    except FileNotFoundError as error:
        logger.debug("msb is not on PATH")
        raise MsbNotFound(f"msb not found on PATH. Install it with: {INSTALL_COMMAND}") from error
    except subprocess.TimeoutExpired as error:
        logger.debug("msb timed out %s", log.context(command=args[1], seconds=COMMAND_TIMEOUT))
        raise MsbError(f"msb {args[1]} gave up after {COMMAND_TIMEOUT}s") from error
    except subprocess.CalledProcessError as error:
        # msb quotes back what it was given, so the reason is scrubbed like herdr's is.
        reason = log.scrub((error.stderr or "").strip())
        logger.debug("msb failed %s", log.context(exit=error.returncode, stderr=reason))
        raise MsbError(f"msb {called} failed: {reason}") from error
    logger.debug("msb done %s", log.context(output=f"{len(completed.stdout)} bytes"))
    return completed.stdout
