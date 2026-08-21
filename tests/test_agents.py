"""Agent registry: built-ins, user files that extend or override, tolerant loading."""

import json
from pathlib import Path

import pytest

from paddock.agents import AgentSpec, agent_dir, builtin_agents, load_agents


def write_agent(config_dir: Path, stem: str, data: object) -> Path:
    path = config_dir / "agents" / f"{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


def test_builtins_cover_the_spec_keys() -> None:
    assert set(builtin_agents()) == {"claude", "codex", "opencode", "aider", "gemini", "shell"}


def test_claude_entry_has_command_domains_and_paths() -> None:
    claude = builtin_agents()["claude"]

    assert claude.name == "Claude Code"
    assert claude.command == "claude"
    assert "api.anthropic.com" in claude.api_domains
    assert claude.auth_read_paths
    assert claude.config_write_paths
    assert claude.image == ""


def test_only_the_selected_agents_credentials_are_listed() -> None:
    """Auth policy: no entry hands out another agent's or a general credential dir."""
    forbidden = {"~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh"}
    for key, agent in builtin_agents().items():
        assert not forbidden & set(agent.auth_read_paths), key
        assert not any(path.startswith("~/.ssh") for path in agent.auth_read_paths), key


def test_shell_agent_uses_the_users_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert builtin_agents()["shell"].command == "/bin/zsh"

    monkeypatch.delenv("SHELL")
    assert builtin_agents()["shell"].command == "/bin/sh"


def test_load_returns_builtins_when_no_user_files_exist() -> None:
    assert set(load_agents()) == set(builtin_agents())


def test_user_file_adds_a_new_agent(config_dir: Path) -> None:
    write_agent(
        config_dir,
        "mycoder",
        {
            "name": "My Coder",
            "command": "mycoder",
            "api_domains": ["api.mycoder.dev"],
            "auth_read_paths": ["~/.mycoder/auth.json"],
            "config_write_paths": ["~/.mycoder"],
        },
    )

    agent = load_agents()["mycoder"]

    assert agent == AgentSpec(
        name="My Coder",
        command="mycoder",
        api_domains=["api.mycoder.dev"],
        auth_read_paths=["~/.mycoder/auth.json"],
        config_write_paths=["~/.mycoder"],
    )


def test_user_file_overrides_a_builtin(config_dir: Path) -> None:
    write_agent(config_dir, "claude", {"command": "claude-canary", "api_domains": ["example.com"]})

    agent = load_agents()["claude"]

    assert agent.command == "claude-canary"
    assert agent.api_domains == ["example.com"]


def test_malformed_and_unusable_files_are_skipped(config_dir: Path) -> None:
    write_agent(config_dir, "broken", "{not json")
    write_agent(config_dir, "a-list", ["nope"])
    write_agent(config_dir, "wrong-types", {"command": ["mycoder"]})
    write_agent(config_dir, "good", {"command": "good"})

    loaded = load_agents()

    assert "broken" not in loaded
    assert "a-list" not in loaded
    assert "wrong-types" not in loaded
    assert loaded["good"].command == "good"


def test_unknown_fields_are_ignored(config_dir: Path) -> None:
    write_agent(config_dir, "future", {"command": "future", "warp_drive": True})

    assert load_agents()["future"].command == "future"


def test_agent_dir_follows_the_config_dir_override(config_dir: Path) -> None:
    assert agent_dir() == config_dir / "agents"
