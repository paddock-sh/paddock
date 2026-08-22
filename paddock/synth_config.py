"""Layer 3: a fresh agent config dir per session, holding only what the user picked.

It gets the agent's own credentials and the ticked skills, and a generated MCP whitelist.
Nothing else is in it, so unselected skills and MCP servers are not there to be found
(SPEC §4.3). The agent is pointed at it with an environment variable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from paddock.agents import AgentSpec
from paddock.profiles import Profile

# The variable that repoints an agent's config dir. Only Claude Code has one, so every
# other agent keeps using its real config dir (SPEC §4.3).
CONFIG_DIR_ENV = {"claude": "CLAUDE_CONFIG_DIR"}


@dataclass
class SynthConfig:
    """What the launch needs: where the config dir is, and how to point the agent at it."""

    # None when the agent has no redirection, and then env and args are empty too.
    dir: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)
    # Skills and MCP servers the profile asked for that the host does not have.
    missing: list[str] = field(default_factory=list)


def build(profile: Profile, agent: AgentSpec, run_dir: Path) -> SynthConfig:
    """Build `run_dir/config` for the agent, or nothing when the agent cannot be redirected."""
    env_var = CONFIG_DIR_ENV.get(profile.agent)
    if env_var is None or not agent.config_write_paths:
        return SynthConfig()

    config = run_dir / "config"
    config.mkdir(parents=True, exist_ok=True)

    for path in agent.auth_read_paths:
        _link(Path(path).expanduser(), config / Path(path).name)
    missing = _link_skills(skill_dirs(agent), config / "skills", profile.skills)

    servers, unknown = _whitelisted_servers(agent, profile.mcp)
    mcp_file = config / ".mcp.json"
    # Written even when empty: with --strict-mcp-config it is what stops every other server.
    mcp_file.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n")

    return SynthConfig(
        dir=config,
        env={env_var: str(config)},
        # --strict-mcp-config is the important one: without it the agent merges MCP config
        # from its other scopes and the whitelist leaks (SPEC §4.2).
        args=["--mcp-config", str(mcp_file), "--strict-mcp-config"],
        missing=missing + [f"MCP server {name!r}" for name in unknown],
    )


def skill_dirs(agent: AgentSpec) -> list[Path]:
    """Where an agent keeps its skills: `skills/` under each of its config dirs."""
    return [Path(path).expanduser() / "skills" for path in agent.config_write_paths]


def _link(target: Path, link: Path) -> None:
    """Symlink one file in. A missing target is skipped: a dangling link reads as an error."""
    if target.exists() and not link.exists():
        link.symlink_to(target)


def _link_skills(sources: list[Path], dest: Path, names: list[str]) -> list[str]:
    """Symlink the ticked skills, and report the ones the host does not have."""
    dest.mkdir(parents=True, exist_ok=True)
    missing = []
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
    return missing


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
