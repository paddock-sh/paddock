"""Layer 3: a config dir holding only the agent's credentials and the skills that were ticked."""

import json
from pathlib import Path

import pytest

from paddock import synth_config
from paddock.agents import AgentSpec, builtin_agents
from paddock.profiles import Profile

CLAUDE = builtin_agents()["claude"]
CODEX = builtin_agents()["codex"]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A host home with the agent's real config dir in it: credentials, skills, MCP servers."""
    home = tmp_path / "home"
    (home / ".claude" / "skills" / "writing").mkdir(parents=True)
    (home / ".claude" / "skills" / "deploy").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}")
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}, "db": {"command": "db-mcp"}}})
    )
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    path = tmp_path / "run"
    path.mkdir()
    return path


def mcp_config(synth: synth_config.SynthConfig) -> dict:
    return json.loads((synth.dir / ".mcp.json").read_text())


# --- credentials -----------------------------------------------------------


def test_the_agents_credentials_are_symlinked_in(home: Path, run_dir: Path) -> None:
    """The agent needs its own key to work at all; nothing else follows it in."""
    synth = synth_config.build(Profile(), CLAUDE, run_dir)

    assert synth.dir == run_dir / "config"
    assert (synth.dir / ".credentials.json").readlink() == home / ".claude/.credentials.json"
    assert (synth.dir / ".claude.json").readlink() == home / ".claude.json"


def test_a_credential_file_the_host_does_not_have_is_left_out(home: Path, run_dir: Path) -> None:
    """A dangling symlink is worse than a missing one: the agent reads an error, not a file."""
    (home / ".claude.json").unlink()

    synth = synth_config.build(Profile(), CLAUDE, run_dir)

    assert not (synth.dir / ".claude.json").exists()
    assert not (synth.dir / ".claude.json").is_symlink()


# --- skills ----------------------------------------------------------------


def test_only_the_ticked_skills_are_there(home: Path, run_dir: Path) -> None:
    """This is the point of layer 3: an unticked skill is not there to be found."""
    synth = synth_config.build(Profile(skills=["writing"]), CLAUDE, run_dir)

    skills = synth.dir / "skills"
    assert [path.name for path in skills.iterdir()] == ["writing"]
    assert (skills / "writing").readlink() == home / ".claude/skills/writing"


def test_no_skills_means_an_empty_skills_dir(home: Path, run_dir: Path) -> None:
    synth = synth_config.build(Profile(skills=[]), CLAUDE, run_dir)

    assert list((synth.dir / "skills").iterdir()) == []


def test_a_skill_the_host_does_not_have_is_reported(home: Path, run_dir: Path) -> None:
    synth = synth_config.build(Profile(skills=["writing", "nope"]), CLAUDE, run_dir)

    assert [path.name for path in (synth.dir / "skills").iterdir()] == ["writing"]
    assert synth.missing == ["skill 'nope'"]


def test_skills_are_looked_for_under_every_config_dir_the_agent_has(
    home: Path, run_dir: Path
) -> None:
    """An agent can keep config in more than one place; the chooser lists them all."""
    (home / "share" / "skills" / "release").mkdir(parents=True)
    agent = AgentSpec(
        command="claude",
        auth_read_paths=["~/.claude/.credentials.json"],
        config_write_paths=["~/.claude", "~/share"],
    )

    synth = synth_config.build(Profile(skills=["writing", "release"]), agent, run_dir)

    assert (synth.dir / "skills" / "writing").readlink() == home / ".claude/skills/writing"
    assert (synth.dir / "skills" / "release").readlink() == home / "share/skills/release"
    assert synth.missing == []


def test_a_skill_name_that_is_a_path_is_refused(home: Path, run_dir: Path) -> None:
    """`../..` would link the whole home directory into the sandbox config dir."""
    synth = synth_config.build(Profile(skills=["../..", "/etc", ""]), CLAUDE, run_dir)

    assert list((synth.dir / "skills").iterdir()) == []
    assert len(synth.missing) == 3


# --- MCP whitelist ---------------------------------------------------------


def test_only_the_ticked_mcp_servers_reach_the_generated_config(home: Path, run_dir: Path) -> None:
    synth = synth_config.build(Profile(mcp=["github"]), CLAUDE, run_dir)

    assert mcp_config(synth) == {"mcpServers": {"github": {"command": "gh-mcp"}}}


def test_no_mcp_servers_means_an_empty_whitelist(home: Path, run_dir: Path) -> None:
    """Written even when empty: with --strict-mcp-config it is what stops every server."""
    synth = synth_config.build(Profile(mcp=[]), CLAUDE, run_dir)

    assert mcp_config(synth) == {"mcpServers": {}}


def test_an_mcp_server_the_host_does_not_define_is_reported(home: Path, run_dir: Path) -> None:
    synth = synth_config.build(Profile(mcp=["github", "nope"]), CLAUDE, run_dir)

    assert list(mcp_config(synth)["mcpServers"]) == ["github"]
    assert synth.missing == ["MCP server 'nope'"]


def test_an_unreadable_host_config_leaves_the_whitelist_empty(home: Path, run_dir: Path) -> None:
    (home / ".claude.json").write_text("not json")

    synth = synth_config.build(Profile(mcp=["github"]), CLAUDE, run_dir)

    assert mcp_config(synth) == {"mcpServers": {}}


# --- what the launch gets back ---------------------------------------------


def test_claude_is_pointed_at_the_synthesized_dir(home: Path, run_dir: Path) -> None:
    synth = synth_config.build(Profile(), CLAUDE, run_dir)

    assert synth.env == {"CLAUDE_CONFIG_DIR": str(run_dir / "config")}
    assert synth.args == [
        "--mcp-config",
        str(run_dir / "config" / ".mcp.json"),
        "--strict-mcp-config",
    ]


def test_an_agent_with_no_redirection_gets_no_config_dir(home: Path, run_dir: Path) -> None:
    """Only Claude Code has one today. The rest keep using their real config dir (SPEC §4.3)."""
    synth = synth_config.build(Profile(agent="codex"), CODEX, run_dir)

    assert synth.dir is None
    assert synth.env == {}
    assert synth.args == []
    assert not (run_dir / "config").exists()
