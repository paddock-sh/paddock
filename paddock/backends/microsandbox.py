"""The microsandbox backend: one persistent microVM per session, attached to by `msb exec`.

`msb` boots an OCI image in a libkrun microVM. The guest has its own kernel and its own
filesystem, so the host is not there to be denied: only what is mounted exists (SPEC §2.2).
There is no daemon. Each running VM is one `msb` process on the host.

`prepare()` boots the VM and `open_pane()` execs a shell into it, which is why tabs on one
msb session share a process namespace and tabs on an srt session do not (SPEC §3.2).
`collect()` destroys the VM, because nothing else will.

v1 of this backend runs the `shell` agent only. `_run` is the one place it shells out to
msb, and the seam every test stands in for.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from paddock import herdr_client
from paddock.agents import load_agents
from paddock.backends import (
    LAUNCH_FILE,
    RunNotFound,
    SandboxGone,
    launch_line,
    new_run_dir,
    write_launch_script,
)
from paddock.profiles import Profile

INSTALL_COMMAND = "curl -fsSL https://install.microsandbox.dev | sh"

# Small, and enough for a shell. An agent entry with an `image` overrides it (SPEC §5).
DEFAULT_IMAGE = "alpine"

# Where the session's workdir is mounted, and where a tab starts.
GUEST_WORKDIR = "/work"

# The only agent this backend runs. Provisioning another one inside the guest is next.
SHELL_AGENT = "shell"

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


def create_argv(handle: str, image: str, workdir: Path, domains: list[str]) -> list[str]:
    """The command that boots the session's VM. The image is positional, so it comes last."""
    return [
        find_msb(),
        "create",
        "--name",
        handle,
        "--mount-dir",
        f"{workdir}:{GUEST_WORKDIR}",
        "--workdir",
        GUEST_WORKDIR,
        *net_rules(domains),
        image,
    ]


def attach_command(handle: str) -> str:
    """What the launch script holds: a terminal shell in the VM the session already has."""
    return shlex.join([find_msb(), "exec", "--tty", handle])


def stop_argv(handle: str) -> list[str]:
    """`msb rm` refuses a running sandbox without -f (`still running`, exit 1), so -f stays."""
    return [find_msb(), "rm", "-f", handle]


def list_argv() -> list[str]:
    """msb's only machine-readable view of what it is running."""
    return [find_msb(), "ls", "--format", "json"]


def vm_status(handle: str) -> str | None:
    """What msb says about this sandbox: its status, or None when msb has no such name."""
    output = _run(*list_argv())
    try:
        listed = json.loads(output or "[]")
    except ValueError as error:
        raise MsbError(f"msb ls did not answer with JSON: {output!r}") from error
    if not isinstance(listed, list):
        raise MsbError(f"msb ls did not answer with a list of sandboxes: {output!r}")
    for entry in listed:
        if isinstance(entry, dict) and entry.get("name") == handle:
            return str(entry.get("status", "")).lower()
    return None


def vm_is_running(handle: str) -> bool:
    """Can a tab exec into this VM? A stopped sandbox still has a record, and cannot.

    Asked before a tab is opened, because `msb exec` into a VM that is gone fails after
    the pane exists, which leaves a dead tab and a pane id nothing can use.
    """
    return vm_status(handle) == "running"


def stop_vm(handle: str) -> None:
    """Destroy the VM, running or stopped. One msb has never heard of is not an error.

    A sandbox that is only stopped still holds its disk, so it is removed like any other.
    """
    try:
        if vm_status(handle) is None:
            return
        _run(*stop_argv(handle))
    except (MsbError, MsbNotFound) as error:
        # It went away between the two calls, or msb will not answer. Either way the
        # session is over, and a pane closing is no place to raise.
        print(f"paddock: {error}", file=sys.stderr)


def prepare(profile: Profile) -> Run:
    """Boot the session's VM and write what a tab needs to attach to it. Opens no pane."""
    agent = load_agents().get(profile.agent)
    if agent is None:
        raise ValueError(f"profile {profile.name!r} names an unknown agent: {profile.agent!r}")
    if profile.agent != SHELL_AGENT:
        raise ValueError(
            f"the msb backend runs the {SHELL_AGENT!r} agent only, not {profile.agent!r}: "
            "agent provisioning inside the guest lands with the next feature"
        )
    # Before the run dir exists, so a missing msb leaves nothing behind.
    find_msb()

    run_dir = new_run_dir()
    workdir = workdir_for(profile, run_dir)
    handle = vm_handle(run_dir)
    _run(*create_argv(handle, agent.image or DEFAULT_IMAGE, workdir, profile.allowed_domains()))

    command = attach_command(handle)
    write_launch_script(run_dir, command)
    (run_dir / LAUNCH_FILE).write_text(
        json.dumps({"vm_handle": handle, "workdir": str(workdir), "command": command}, indent=2)
        + "\n"
    )
    return Run(run_dir=run_dir, workdir=workdir, vm_handle=handle, command=command)


def load_run(run_dir: Path) -> Run:
    """Read a prepared run back, so a later tab execs into the VM the first one booted."""
    try:
        data = json.loads((run_dir / LAUNCH_FILE).read_text())
        return Run(run_dir, Path(data["workdir"]), str(data["vm_handle"]), str(data["command"]))
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise RunNotFound(f"no usable launch record in {run_dir}") from error


def open_pane(run: Run, label: str = "", cwd: Path | None = None) -> str:
    """Open a tab and exec a shell into the session's VM. Returns the pane id.

    The tab's own directory is the host side of the mount, so the pane and the guest
    shell are looking at the same files.
    """
    if cwd is not None:
        raise ValueError(
            f"an msb session always opens in the guest workdir {GUEST_WORKDIR}: "
            f"{cwd} would only set the host tab's own directory, which the guest shell replaces"
        )
    if not vm_is_running(run.vm_handle):
        raise SandboxGone(f"the microVM {run.vm_handle} is not running any more")
    pane_id = herdr_client.create_tab(run.workdir, label=label)
    herdr_client.run_in_pane(pane_id, launch_line(run.run_dir))
    return pane_id


def collect(run_dir: Path, vm_handle: str = "") -> None:
    """Destroy the session's VM. The run dir names it, and the registry's handle is the fallback."""
    try:
        vm_handle = load_run(run_dir).vm_handle
    except RunNotFound:
        pass  # the run dir lost its record, so the caller's handle is all there is
    if vm_handle:
        stop_vm(vm_handle)


def _run(*args: str) -> str:
    try:
        completed = subprocess.run(
            args, capture_output=True, text=True, check=True, timeout=COMMAND_TIMEOUT
        )
    except FileNotFoundError as error:
        raise MsbNotFound(f"msb not found on PATH. Install it with: {INSTALL_COMMAND}") from error
    except subprocess.TimeoutExpired as error:
        raise MsbError(f"msb {args[1]} gave up after {COMMAND_TIMEOUT}s") from error
    except subprocess.CalledProcessError as error:
        reason = (error.stderr or "").strip()
        raise MsbError(f"msb {' '.join(args[1:])} failed: {reason}") from error
    return completed.stdout
