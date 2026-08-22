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


def test_claude_names_the_image_and_the_install_that_puts_it_in_a_guest() -> None:
    """The msb backend boots this image and runs this command when the image lacks claude."""
    claude = builtin_agents()["claude"]

    assert claude.image == "node:22-slim"
    # Pinned: a session must not pick up a new agent release on its own (SPEC §2.2).
    assert claude.install == "npm install -g @anthropic-ai/claude-code@2.1.239"


def test_an_agent_with_no_image_is_srt_only() -> None:
    """No image means the msb backend refuses it, so the field stays blank until one exists."""
    assert builtin_agents()["codex"].image == ""
    assert builtin_agents()["codex"].install == ""


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


def test_a_list_of_non_strings_is_skipped(config_dir: Path) -> None:
    write_agent(config_dir, "numeric-domains", {"command": "x", "api_domains": [1, 2]})
    write_agent(config_dir, "numeric-auth", {"command": "x", "auth_read_paths": [123]})

    loaded = load_agents()

    assert "numeric-domains" not in loaded
    assert "numeric-auth" not in loaded


def test_an_entry_without_a_command_is_skipped(config_dir: Path) -> None:
    write_agent(config_dir, "no-command", {"name": "No Command", "api_domains": ["x.dev"]})
    write_agent(config_dir, "empty-command", {"name": "Empty", "command": ""})

    loaded = load_agents()

    assert "no-command" not in loaded
    assert "empty-command" not in loaded


def test_unknown_fields_are_ignored(config_dir: Path) -> None:
    write_agent(config_dir, "future", {"command": "future", "warp_drive": True})

    assert load_agents()["future"].command == "future"


def test_agent_dir_follows_the_config_dir_override(config_dir: Path) -> None:
    assert agent_dir() == config_dir / "agents"


def test_codex_names_the_tool_its_command_cannot_start_without() -> None:
    """`codex` is a `#!/usr/bin/env node` script, so the sandbox PATH needs node on it."""
    assert builtin_agents()["codex"].required_tools == ["node"]


def test_no_other_builtin_asks_for_a_tool_it_does_not_need() -> None:
    """A required tool goes on the sandbox PATH unasked, so only a real one may be listed."""
    asked = {key: agent.required_tools for key, agent in builtin_agents().items()}

    assert asked == {
        "claude": [],
        "codex": ["node"],
        "opencode": [],
        "aider": [],
        "gemini": [],
        "shell": [],
    }


def test_a_user_file_can_say_what_its_agent_needs(config_dir: Path) -> None:
    write_agent(config_dir, "mycoder", {"command": "mycoder", "required_tools": ["python3"]})

    assert load_agents()["mycoder"].required_tools == ["python3"]


def test_required_tools_that_are_not_strings_reject_the_file(config_dir: Path) -> None:
    """Half a policy is worse than none: the whole entry goes, as any wrong field does."""
    write_agent(config_dir, "numeric", {"command": "x", "required_tools": [7]})
    write_agent(config_dir, "not-a-list", {"command": "x", "required_tools": "node"})

    loaded = load_agents()

    assert "numeric" not in loaded
    assert "not-a-list" not in loaded
