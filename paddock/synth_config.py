"""Layer 3: a fresh agent config dir per session, holding only what the user picked.

It gets the agent's own credentials and the ticked skills, and a generated MCP whitelist.
Nothing else is in it, so unselected skills and MCP servers are not there to be found
(SPEC §4.3). The agent is pointed at it with an environment variable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from paddock.agents import AgentSpec
from paddock.profiles import Profile

# The file an agent reads its token from inside the config dir, and what a collected session
# loses (SPEC §8).
CREDENTIALS_FILE = ".credentials.json"


@dataclass(frozen=True)
class Redirection:
    """How one agent's config dir is replaced."""

    # The variable that points the agent at the synthesized dir.
    env_var: str
    # Credential files the agent writes back to. Copied, so the sandbox works on its own
    # and the host's file is never touched. Everything else is a read-only symlink.
    copied: tuple[str, ...] = ()
    # macOS Keychain service holding the token when the agent kept none in a file. A
    # fallback source for CREDENTIALS_FILE, never a replacement for one (SPEC §4.3).
    keychain: str = ""


# Only Claude Code can be redirected today, so every other agent keeps its real config
# dir (SPEC §4.3).
REDIRECTIONS = {
    "claude": Redirection(
        "CLAUDE_CONFIG_DIR",
        copied=(".claude.json",),
        keychain="Claude Code-credentials",
    )
}


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


def build(profile: Profile, agent: AgentSpec, run_dir: Path) -> SynthConfig:
    """Build `run_dir/config` for the agent, or nothing when the agent cannot be redirected."""
    redirect = REDIRECTIONS.get(profile.agent)
    if redirect is None or not agent.config_write_paths:
        return SynthConfig()

    config = run_dir / "config"
    config.mkdir(parents=True, exist_ok=True)

    linked, copied = [], []
    for path in agent.auth_read_paths:
        source = Path(path).expanduser()
        by_copy = source.name in redirect.copied
        if _take(source, config / source.name, copy=by_copy):
            (copied if by_copy else linked).append(source)
    if redirect.keychain and not (config / CREDENTIALS_FILE).exists():
        # The host keeps no credential file, so the redirected agent has nothing to read.
        _export_token(redirect.keychain, config / CREDENTIALS_FILE)
    sources, missing = _link_skills(skill_dirs(agent), config / "skills", profile.skills)

    servers, unknown = _whitelisted_servers(agent, profile.mcp)
    mcp_file = config / ".mcp.json"
    # Written even when empty: with --strict-mcp-config it is what stops every other server.
    mcp_file.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n")

    return SynthConfig(
        dir=config,
        env={redirect.env_var: str(config)},
        # --strict-mcp-config is the important one: without it the agent merges MCP config
        # from its other scopes and the whitelist leaks (SPEC §4.2).
        args=["--mcp-config", str(mcp_file), "--strict-mcp-config"],
        linked=linked + sources,
        copied=copied,
        missing=missing + [f"MCP server {name!r}" for name in unknown],
    )


def skill_dirs(agent: AgentSpec) -> list[Path]:
    """Where an agent keeps its skills: `skills/` under each of its config dirs."""
    return [Path(path).expanduser() / "skills" for path in agent.config_write_paths]


def discard_credentials(run_dir: Path) -> None:
    """Delete the credential file of a run nobody is using any more (SPEC §8).

    The rest of the run dir stays — deleting a workdir would lose work — but an exported
    token must not outlive the session. A symlinked one loses only the link.
    """
    (run_dir / "config" / CREDENTIALS_FILE).unlink(missing_ok=True)


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
    if isinstance(data, dict) and "mcpServers" in data:
        del data["mcpServers"]
        path.write_text(json.dumps(data, indent=2) + "\n")


def _export_token(service: str, dest: Path) -> None:
    """Write the token macOS keeps in the login Keychain into the config dir, if it has one.

    A token on disk, so it goes nowhere but the run dir, readable by its owner alone.
    Anywhere without `security`, or without that entry, gets no file (SPEC §4.3).
    """
    try:
        found = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return
    token = found.stdout.strip()
    if not token:
        return
    dest.touch(mode=0o600)
    dest.write_text(token + "\n")


def _link_skills(sources: list[Path], dest: Path, names: list[str]) -> tuple[list[Path], list[str]]:
    """Symlink the ticked skills. Returns what was linked, and what the host does not have."""
    dest.mkdir(parents=True, exist_ok=True)
    linked, missing = [], []
    for name in names:
        # A skill is a bare directory name: `../..` would link the whole config dir in.
        plain = bool(name) and "/" not in name and not name.startswith(".")
        found = [source / name for source in sources if plain and (source / name).is_dir()]
        if not found:
            missing.append(f"skill {name!r}")
            continue
        link = dest / name
        if not link.exists():
            link.symlink_to(found[0])
        linked.append(found[0])
    return linked, missing


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
