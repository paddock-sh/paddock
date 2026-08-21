"""Agent registry: what each agent is called, needs on the network, and reads to authenticate."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from paddock import config_dir


@dataclass
class AgentSpec:
    name: str = ""
    # Executable run inside the sandbox. An entry without one is unusable and is skipped.
    command: str = ""
    api_domains: list[str] = field(default_factory=list)
    # Only this agent's own credentials. Never another agent's, never ~/.ssh and friends.
    auth_read_paths: list[str] = field(default_factory=list)
    config_write_paths: list[str] = field(default_factory=list)
    # OCI image for the microsandbox backend (SPEC §2.2). Unused by the srt backend.
    image: str = ""


def agent_dir() -> Path:
    return config_dir() / "agents"


def builtin_agents() -> dict[str, AgentSpec]:
    return {
        "claude": AgentSpec(
            name="Claude Code",
            command="claude",
            api_domains=["api.anthropic.com", "*.anthropic.com"],
            auth_read_paths=["~/.claude/.credentials.json", "~/.claude.json"],
            config_write_paths=["~/.claude"],
        ),
        "codex": AgentSpec(
            name="Codex CLI",
            command="codex",
            api_domains=["api.openai.com", "chatgpt.com", "auth.openai.com"],
            auth_read_paths=["~/.codex/auth.json"],
            config_write_paths=["~/.codex"],
        ),
        "opencode": AgentSpec(
            name="OpenCode",
            command="opencode",
            # models.dev serves the provider catalogue it fetches at startup.
            api_domains=[
                "opencode.ai",
                "*.opencode.ai",
                "models.dev",
                "api.anthropic.com",
                "api.openai.com",
            ],
            auth_read_paths=["~/.local/share/opencode/auth.json"],
            config_write_paths=["~/.config/opencode", "~/.local/share/opencode"],
        ),
        "aider": AgentSpec(
            name="Aider",
            command="aider",
            api_domains=["api.openai.com", "api.anthropic.com", "openrouter.ai"],
            auth_read_paths=["~/.aider.conf.yml"],
            config_write_paths=["~/.aider"],
        ),
        "gemini": AgentSpec(
            name="Gemini CLI",
            command="gemini",
            # Login with Google routes model calls through Code Assist, not the direct API.
            api_domains=[
                "generativelanguage.googleapis.com",
                "cloudcode-pa.googleapis.com",
                "oauth2.googleapis.com",
            ],
            auth_read_paths=["~/.gemini/oauth_creds.json"],
            config_write_paths=["~/.gemini"],
        ),
        "shell": AgentSpec(
            name="Shell",
            command=os.environ.get("SHELL", "/bin/sh"),
        ),
    }


def load_agents() -> dict[str, AgentSpec]:
    """Built-ins, plus user entries from `<config>/agents/*.json`. A user file wins by key."""
    agents = builtin_agents()
    for path in sorted(agent_dir().glob("*.json")):
        agent = _read(path)
        if agent is not None:
            agents[path.stem] = agent
    return agents


def _read(path: Path) -> AgentSpec | None:
    """One agent from one file, or None if the file cannot be used as written."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("name", path.stem)
    defaults = vars(AgentSpec())  # field names, and the type each field must have
    values = {key: value for key, value in data.items() if key in defaults}
    # A wrong-shaped field rejects the whole file. Half-applying it would give the
    # sandbox a policy nobody wrote.
    for key, value in values.items():
        if not isinstance(value, type(defaults[key])):
            return None
        if isinstance(value, list) and not all(isinstance(item, str) for item in value):
            return None
    agent = AgentSpec(**values)
    return agent if agent.command else None
