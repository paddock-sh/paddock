"""Layer 3: a fresh agent config dir per session, holding only what the user picked.

It gets the agent's own credentials and the ticked skills, and a generated MCP whitelist.
Nothing else is in it, so unselected skills and MCP servers are not there to be found
(SPEC §4.3). The agent is pointed at it with an environment variable.

The same directory serves both backends. srt reads it where it was built, so it can be
symlinks; msb mounts it into a guest, where a link to a host path leads nowhere, so a
caller that names a `guest_dir` gets copies (SPEC §2.2).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from paddock import log
from paddock.agents import AgentSpec
from paddock.profiles import EVERYTHING, Profile

logger = log.get_logger(__name__)

# The file an agent reads its token from inside the config dir, and what a collected session
# loses (SPEC §8).
CREDENTIALS_FILE = ".credentials.json"

# The agent's own login inside a Keychain entry. The rest of that entry stays in the Keychain.
LOGIN_KEY = "claudeAiOauth"


@dataclass(frozen=True)
class Redirection:
    """How one agent's config dir is replaced."""

    # The variable that points the agent at the synthesized dir.
    env_var: str
    # Credential files the agent writes back to. Copied, so the sandbox works on its own
    # and the host's file is never touched. Everything else is a read-only symlink, unless
    # the whole directory is going into a guest, where a link out of the mount dangles.
    copied: tuple[str, ...] = ()
    # macOS Keychain service holding the token when the agent kept none in a file. A
    # fallback source for CREDENTIALS_FILE, never a replacement for one (SPEC §4.3).
    keychain: str = ""
    # The file the agent keeps its first-run answers in, inside the config dir. paddock
    # seeds it so a sandboxed session opens on the agent and not on an interstitial.
    first_run: str = ""
    # Variables the sandbox gets besides the config dir one, for what the config file
    # cannot say. Never a secret: these are settings, and the log prints them.
    env: tuple[tuple[str, str], ...] = ()


# Only Claude Code can be redirected today, so every other agent keeps its real config
# dir (SPEC §4.3).
REDIRECTIONS = {
    "claude": Redirection(
        "CLAUDE_CONFIG_DIR",
        copied=(".claude.json",),
        keychain="Claude Code-credentials",
        first_run=".claude.json",
        # `autoUpdates: false` in the config file is not enough for a native install,
        # which keeps updating anyway (`autoUpdatesProtectedForNative`) and then fails,
        # because the install directory is outside the sandbox. Measured live on
        # 2.1.240: with this set, the red banner is gone (SPEC §4.3).
        env=(("DISABLE_AUTOUPDATER", "1"),),
    )
}

# What Claude Code writes in `.claude.json` once its first-run questions are answered, as
# read off a real one on this machine. A sandboxed session is a fresh workdir every time,
# so without these the user meets the trust dialog before the prompt, and the auto-updater
# behind it: the sandbox cannot write to the install, so it fails and leaves a red banner
# over the session. Neither question is one the user can answer usefully here, and neither
# is a permission: the trust dialog asks about a directory paddock generated, and the
# updater is asking to write outside the sandbox (SPEC §4.3).
FIRST_RUN = {
    "hasCompletedOnboarding": True,
    "autoUpdates": False,
    # An upsell counter, not a permission: a session gets a config dir of its own, so
    # without this every session is the first one this offer has ever been made to, and
    # it stands between the user and the prompt every single time. Measured live on
    # 2.1.240: the offer is gone once the count is past its limit.
    "fullscreenUpsellSeenCount": 99,
}

# The same, for the directory the agent is started in.
FIRST_RUN_PROJECT = {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}


@dataclass
class SynthConfig:
    """What the launch needs: where the config dir is, and how to point the agent at it."""

    # None when the agent has no redirection, and then env and args are empty too.
    dir: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)
    # Real paths the config dir points at. srt checks the path an access resolves to, so
    # the launch has to allow reading these (SPEC §4.3).
    linked: list[Path] = field(default_factory=list)
    # Real paths the config dir copied. The sandbox has its own, so the host's can be hidden.
    copied: list[Path] = field(default_factory=list)
    # Skills and MCP servers the profile asked for that the host does not have.
    missing: list[str] = field(default_factory=list)


def build(
    profile: Profile,
    agent: AgentSpec,
    run_dir: Path,
    guest_dir: str = "",
    defer_credentials: bool = False,
    workdir: str = "",
) -> SynthConfig:
    """Build `run_dir/config` for the agent, or nothing when the agent cannot be redirected.

    `guest_dir` is the path this directory gets mounted at inside a microVM. Naming one
    changes two things (SPEC §4.3): everything is copied, because a symlink to a host path
    is outside the mount and dangles in the guest, and the agent is pointed at the mount
    point rather than at the run dir, which the guest cannot see.

    `defer_credentials` leaves the token out, for a caller that has more that can fail
    before the agent could use one. `place_credentials` puts it in afterwards.

    `workdir` is where the agent will be started, which is the directory it asks about
    trusting. A backend that starts it somewhere else (a mount point inside a guest) names
    that path; the rest get the workdir this profile and run dir imply.
    """
    redirect = REDIRECTIONS.get(profile.agent)
    if redirect is None or not agent.config_write_paths:
        return SynthConfig()

    config = run_dir / "config"
    config.mkdir(parents=True, exist_ok=True)
    in_guest = bool(guest_dir)

    linked, copied = [], []
    for path in agent.auth_read_paths:
        source = Path(path).expanduser()
        if defer_credentials and source.name == CREDENTIALS_FILE:
            continue
        by_copy = in_guest or source.name in redirect.copied
        if _take(source, config / source.name, copy=by_copy):
            (copied if by_copy else linked).append(source)
    if redirect.keychain and not defer_credentials and not (config / CREDENTIALS_FILE).exists():
        # The host keeps no credential file, so the redirected agent has nothing to read.
        _export_token(redirect.keychain, config / CREDENTIALS_FILE)
    if redirect.first_run:
        _seed_first_run(config / redirect.first_run, _workdirs(profile, run_dir, workdir))
    sources, missing = _take_skills(
        skill_dirs(agent), config / "skills", profile.skills, copy=in_guest
    )

    servers, unknown = _whitelisted_servers(agent, profile.mcp)
    # Written even when empty: with --strict-mcp-config it is what stops every other server.
    (config / ".mcp.json").write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n")

    # Where the agent will read all this from, which is not where it was built when the
    # directory is on its way into a guest.
    seen_as = PurePosixPath(guest_dir) if in_guest else config
    # Paths only. What these files hold is the agent's login (SPEC §4.3). The two lists are
    # reported the way they were built: a dir on its way into a guest is copies, not links.
    logger.debug(
        "config dir built %s",
        log.context(
            dir=config,
            seen_as=seen_as,
            linked=_paths([] if in_guest else linked + sources),
            copied=_paths(copied + sources if in_guest else copied),
            servers=len(servers),
            missing=", ".join(missing + [f"MCP server {name!r}" for name in unknown]),
        ),
    )
    return SynthConfig(
        dir=config,
        env={redirect.env_var: str(seen_as), **dict(redirect.env)},
        # --strict-mcp-config is the important one: without it the agent merges MCP config
        # from its other scopes and the whitelist leaks (SPEC §4.2).
        args=["--mcp-config", str(seen_as / ".mcp.json"), "--strict-mcp-config"],
        linked=[] if in_guest else linked + sources,
        copied=copied + sources if in_guest else copied,
        missing=missing + [f"MCP server {name!r}" for name in unknown],
    )


def place_credentials(profile: Profile, agent: AgentSpec, run_dir: Path) -> None:
    """Put the agent's token in a config dir that was built without one.

    The copy, not the symlink: deferring is the msb path, where a link out of the mount
    leads nowhere. The point of the split is that a launch which fails before this runs
    never wrote a token to disk at all (SPEC §2.2).
    """
    redirect = REDIRECTIONS.get(profile.agent)
    if redirect is None:
        return
    dest = run_dir / "config" / CREDENTIALS_FILE
    for path in agent.auth_read_paths:
        source = Path(path).expanduser()
        if source.name == CREDENTIALS_FILE:
            _take(source, dest, copy=True)
    if redirect.keychain and not dest.exists():
        _export_token(redirect.keychain, dest)


def skill_dirs(agent: AgentSpec) -> list[Path]:
    """Where an agent keeps its skills: `skills/` under each of its config dirs."""
    return [Path(path).expanduser() / "skills" for path in agent.config_write_paths]


def discard_credentials(run_dir: Path) -> None:
    """Delete the credential file of a run nobody is using any more (SPEC §8).

    The rest of the run dir stays, because deleting a workdir would lose work, but an exported
    token must not outlive the session. A symlinked one loses only the link.
    """
    (run_dir / "config" / CREDENTIALS_FILE).unlink(missing_ok=True)


def _workdirs(profile: Profile, run_dir: Path, named: str) -> list[str]:
    """The directories the agent may be started in, for the trust entries it reads.

    Both spellings of the path, because macOS has two for everything under /var and the
    agent writes down the one it was given. A backend that starts the agent somewhere its
    own (a mount point in a guest) names that instead.
    """
    if named:
        return [named]
    workdir = Path(profile.shared_dir).expanduser() if profile.shared_dir else run_dir / "work"
    return list(dict.fromkeys([str(workdir), os.path.realpath(workdir)]))


def _seed_first_run(path: Path, workdirs: list[str]) -> None:
    """Answer the agent's first-run questions in the synthesized config, and only there.

    The host's own config is never touched: this writes the copy in the run dir, and a
    session that has no copy to write in gets one with these answers and nothing else.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    projects = data.get("projects")
    projects = dict(projects) if isinstance(projects, dict) else {}
    for workdir in workdirs:
        entry = projects.get(workdir)
        projects[workdir] = {**(entry if isinstance(entry, dict) else {}), **FIRST_RUN_PROJECT}
    path.write_text(json.dumps({**data, **FIRST_RUN, "projects": projects}, indent=2) + "\n")
    logger.debug("first-run answers seeded %s", log.context(path=path, dirs=", ".join(workdirs)))


def _paths(paths: list[Path]) -> str:
    """Paths for one log line. Never what is in them."""
    return ", ".join(str(path) for path in paths)


def _take(source: Path, dest: Path, copy: bool) -> bool:
    """Put one credential file in the config dir, by copy or by symlink. Was there one?

    A missing source is skipped: a dangling symlink reads as an error, not as absence.
    """
    if not source.exists():
        return False
    if dest.exists():
        return True
    if copy:
        shutil.copy2(source, dest)
        _drop_mcp_servers(dest)
    else:
        dest.symlink_to(source)
    return True


def _drop_mcp_servers(path: Path) -> None:
    """Take the servers out of a copied config, leaving everything else as it was.

    The copy is a whole file, so its definitions would travel into the sandbox. The
    generated whitelist is the only source of servers (SPEC §4.3).
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return
    stripped = _without_mcp_servers(data)
    if stripped != data:
        path.write_text(json.dumps(stripped, indent=2) + "\n")


def _without_mcp_servers(data: object) -> object:
    """The same data with every `mcpServers` key gone, however deep it sits.

    A project scope keeps servers of its own, one level inside the file.
    """
    if isinstance(data, dict):
        kept = ((key, value) for key, value in data.items() if key != "mcpServers")
        return {key: _without_mcp_servers(value) for key, value in kept}
    if isinstance(data, list):
        return [_without_mcp_servers(item) for item in data]
    return data


def _export_token(service: str, dest: Path) -> None:
    """Write the login macOS keeps in the Keychain into the config dir, if it has one.

    A token on disk, so it goes nowhere but the run dir, readable by its owner alone, and
    holds the agent's own login and nothing else: the same entry keeps a token per MCP
    server the user has authorised, and no whitelist loads those. An entry of an
    unrecognised shape is left where it is: "Not logged in" is better than an unknown
    blob of secrets on disk. Anywhere without `security`, or without that entry, gets no
    file (SPEC §4.3).
    """
    try:
        found = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("no keychain login %s", log.context(service=service))
        return
    # Nothing below logs the entry, or any part of it: the whole thing is secret.
    try:
        entry = json.loads(found.stdout)
    except json.JSONDecodeError:
        logger.debug("the keychain entry is not JSON %s", log.context(service=service))
        return
    if not isinstance(entry, dict) or LOGIN_KEY not in entry:
        logger.debug("the keychain entry has no login in it %s", log.context(service=service))
        return
    dest.touch(mode=0o600)
    dest.write_text(json.dumps({LOGIN_KEY: entry[LOGIN_KEY]}, indent=2) + "\n")
    logger.debug(
        "exported the keychain login %s",
        log.context(service=service, path=dest, size=f"{dest.stat().st_size} bytes"),
    )


def _take_skills(
    sources: list[Path], dest: Path, names: list[str], copy: bool
) -> tuple[list[Path], list[str]]:
    """Put the ticked skills in the dir. Returns what was taken, and what the host lacks.

    `*` means every skill the agent has, which is the chooser's allow-all row. It names no
    skill of its own, so nothing about it can go missing.
    """
    dest.mkdir(parents=True, exist_ok=True)
    if EVERYTHING in names:
        names = _every_skill(sources)
    taken, missing = [], []
    for name in names:
        # A skill is a bare directory name: `../..` would link the whole config dir in.
        plain = bool(name) and "/" not in name and not name.startswith(".")
        found = [source / name for source in sources if plain and (source / name).is_dir()]
        if not found:
            missing.append(f"skill {name!r}")
            continue
        skill = dest / name
        if not skill.exists():
            if copy:
                # A copy follows the skill's own symlinks on the way in, so what lands is files.
                shutil.copytree(found[0], skill)
            else:
                skill.symlink_to(found[0])
        taken.append(found[0])
    return taken, missing


def _every_skill(sources: list[Path]) -> list[str]:
    """Every skill directory the agent has, in name order and with no repeats."""
    found: list[str] = []
    for source in sources:
        if source.is_dir():
            found += sorted(entry.name for entry in source.iterdir() if entry.is_dir())
    return list(dict.fromkeys(found))


def _whitelisted_servers(agent: AgentSpec, wanted: list[str]) -> tuple[dict, list[str]]:
    """Only the ticked servers, taken from wherever the agent's own config defines them."""
    defined: dict = {}
    for path in agent.auth_read_paths:
        try:
            data = json.loads(Path(path).expanduser().read_text())
        except (OSError, json.JSONDecodeError):
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if isinstance(servers, dict):
            defined.update(servers)
    return (
        {name: defined[name] for name in wanted if name in defined},
        [name for name in wanted if name not in defined],
    )
