"""Layer 3: a config dir holding only the agent's credentials and the skills that were ticked."""

import json
import stat
import subprocess
from pathlib import Path

import pytest

from paddock import synth_config
from paddock.agents import AgentSpec, builtin_agents
from paddock.profiles import Profile

CLAUDE = builtin_agents()["claude"]
CODEX = builtin_agents()["codex"]

# What macOS holds under "Claude Code-credentials" when the login is a Keychain one. The
# agent's own login sits beside a token per MCP server the user has authorised.
LOGIN = {"accessToken": "sk-ant-oat-test", "scopes": ["user:inference"]}
TOKEN = json.dumps({"claudeAiOauth": LOGIN, "mcpOAuth": {"supabase|x": {"access": "mcp-secret"}}})


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A host home with the agent's real config dir in it: credentials, skills, MCP servers."""
    home = tmp_path / "home"
    (home / ".claude" / "skills" / "writing").mkdir(parents=True)
    (home / ".claude" / "skills" / "deploy").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}")
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "numStartups": 7,
                # A project scope keeps servers of its own, one level down.
                "projects": {
                    "/repo": {"history": ["hello"], "mcpServers": {"local": {"command": "x-mcp"}}}
                },
                "mcpServers": {"github": {"command": "gh-mcp"}, "db": {"command": "db-mcp"}},
            }
        )
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


def test_the_agents_key_is_a_symlink(home: Path, run_dir: Path) -> None:
    """The agent needs its own key to work at all; nothing else follows it in."""
    synth = synth_config.build(Profile(), CLAUDE, run_dir)

    assert synth.dir == run_dir / "config"
    assert (synth.dir / ".credentials.json").readlink() == home / ".claude/.credentials.json"


def test_the_config_the_agent_writes_back_to_is_a_copy(home: Path, run_dir: Path) -> None:
    """The agent keeps working because it can write; the host's file is never touched."""
    original = (home / ".claude.json").read_text()

    synth = synth_config.build(Profile(), CLAUDE, run_dir)

    copy = synth.dir / ".claude.json"
    assert not copy.is_symlink()
    assert json.loads(copy.read_text())["projects"]["/repo"]["history"] == ["hello"]

    copy.write_text('{"changed": true}')
    assert (home / ".claude.json").read_text() == original


def test_the_copy_leaves_every_mcp_server_behind(home: Path, run_dir: Path) -> None:
    """A whole-file copy would carry them all in, and the whitelist is the only source.

    Project scopes keep servers of their own, so the key goes at whatever depth it is found.
    """
    synth = synth_config.build(Profile(mcp=["github"]), CLAUDE, run_dir)

    copy = json.loads((synth.dir / ".claude.json").read_text())
    assert "mcpServers" not in json.dumps(copy)
    assert copy == {"numStartups": 7, "projects": {"/repo": {"history": ["hello"]}}}
    assert mcp_config(synth) == {"mcpServers": {"github": {"command": "gh-mcp"}}}


def test_a_copy_with_no_mcp_servers_is_left_exactly_as_it_was(home: Path, run_dir: Path) -> None:
    """Nothing to take out, so nothing is rewritten."""
    (home / ".claude.json").write_text('{"numStartups":   7}\n')

    synth = synth_config.build(Profile(), CLAUDE, run_dir)

    assert (synth.dir / ".claude.json").read_text() == '{"numStartups":   7}\n'


@pytest.mark.parametrize("name", [".claude.json", ".claude/.credentials.json"])
def test_a_credential_file_the_host_does_not_have_is_left_out(
    name: str, home: Path, run_dir: Path
) -> None:
    """A dangling symlink is worse than a missing one: the agent reads an error, not a file."""
    (home / name).unlink()

    synth = synth_config.build(Profile(), CLAUDE, run_dir)

    left_out = synth.dir / Path(name).name
    assert not left_out.exists()
    assert not left_out.is_symlink()


# --- the keychain fallback -------------------------------------------------


def test_the_token_is_read_from_the_login_keychain(
    home: Path, run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code keeps its token there, not in a file, when it logs in on macOS."""
    (home / ".claude" / ".credentials.json").unlink()
    calls: list[list[str]] = []

    def security(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, TOKEN + "\n", "")

    monkeypatch.setattr(subprocess, "run", security)

    synth = synth_config.build(Profile(), CLAUDE, run_dir)

    assert calls == [["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]]
    assert json.loads((synth.dir / ".credentials.json").read_text()) == {"claudeAiOauth": LOGIN}


def test_the_mcp_tokens_beside_it_are_left_in_the_keychain(
    home: Path, run_dir: Path, keychain: dict[str, str]
) -> None:
    """The entry holds a token per authorised MCP server, and none of them are loaded."""
    (home / ".claude" / ".credentials.json").unlink()
    keychain["Claude Code-credentials"] = TOKEN

    synth = synth_config.build(Profile(), CLAUDE, run_dir)

    exported = (synth.dir / ".credentials.json").read_text()
    assert list(json.loads(exported)) == ["claudeAiOauth"]
    assert "mcp-secret" not in exported


@pytest.mark.parametrize("blob", ["not json", '["a list"]', '{"somethingElse": {}}'])
def test_a_keychain_entry_it_does_not_recognise_is_not_written_out(
    blob: str, home: Path, run_dir: Path, keychain: dict[str, str]
) -> None:
    """Better a visible "Not logged in" than an unknown blob of secrets on disk."""
    (home / ".claude" / ".credentials.json").unlink()
    keychain["Claude Code-credentials"] = blob

    synth = synth_config.build(Profile(), CLAUDE, run_dir)

    assert not (synth.dir / ".credentials.json").exists()


def test_the_exported_token_is_a_file_only_its_owner_can_read(
    home: Path, run_dir: Path, keychain: dict[str, str]
) -> None:
    """It is a token on disk, so the run dir is the only place it goes, at 0600."""
    (home / ".claude" / ".credentials.json").unlink()
    keychain["Claude Code-credentials"] = TOKEN

    synth = synth_config.build(Profile(), CLAUDE, run_dir)

    exported = synth.dir / ".credentials.json"
    assert not exported.is_symlink()
    assert stat.S_IMODE(exported.stat().st_mode) == 0o600


def test_the_real_file_wins_over_the_keychain(
    home: Path, run_dir: Path, keychain: dict[str, str]
) -> None:
    """The keychain is a fallback source: with a file, nothing is copied onto disk."""
    keychain["Claude Code-credentials"] = TOKEN

    synth = synth_config.build(Profile(), CLAUDE, run_dir)

    assert (synth.dir / ".credentials.json").readlink() == home / ".claude/.credentials.json"


def test_an_empty_keychain_leaves_the_config_dir_without_a_token(
    home: Path, run_dir: Path, keychain: dict[str, str]
) -> None:
    """Nothing to export on Linux, or where the user never logged in that way."""
    (home / ".claude" / ".credentials.json").unlink()

    synth = synth_config.build(Profile(), CLAUDE, run_dir)

    assert not (synth.dir / ".credentials.json").exists()
    assert keychain == {}


def test_an_agent_with_no_keychain_entry_of_its_own_asks_for_nothing(
    home: Path, run_dir: Path, keychain: dict[str, str]
) -> None:
    """Only Claude Code names a Keychain service; the export is not a general mechanism."""
    keychain["Claude Code-credentials"] = TOKEN

    synth = synth_config.build(Profile(agent="codex"), CODEX, run_dir)

    assert synth.dir is None


def test_a_collected_session_loses_its_exported_token(
    home: Path, run_dir: Path, keychain: dict[str, str]
) -> None:
    """The run dir outlives the session, and a token must not (SPEC §8)."""
    (home / ".claude" / ".credentials.json").unlink()
    keychain["Claude Code-credentials"] = TOKEN
    synth = synth_config.build(Profile(), CLAUDE, run_dir)

    synth_config.discard_credentials(run_dir)

    assert not (synth.dir / ".credentials.json").exists()
    assert (synth.dir / ".mcp.json").is_file()


def test_discarding_credentials_from_a_run_dir_without_any_is_not_an_error(
    run_dir: Path,
) -> None:
    """An agent with no redirection has no config dir under the run dir at all."""
    synth_config.discard_credentials(run_dir)


# --- skills ----------------------------------------------------------------


def test_only_the_ticked_skills_are_there(home: Path, run_dir: Path) -> None:
    """This is the point of layer 3: an unticked skill is not there to be found."""
    synth = synth_config.build(Profile(skills=["writing"]), CLAUDE, run_dir)

    skills = synth.dir / "skills"
    assert [path.name for path in skills.iterdir()] == ["writing"]
    assert (skills / "writing").readlink() == home / ".claude/skills/writing"


def test_what_it_linked_and_what_it_copied_come_back_for_the_settings(
    home: Path, run_dir: Path
) -> None:
    """srt checks the path an access resolves to, so the settings need both lists."""
    synth = synth_config.build(Profile(skills=["writing", "nope"]), CLAUDE, run_dir)

    assert synth.linked == [
        home / ".claude/.credentials.json",
        home / ".claude/skills/writing",
    ]
    assert synth.copied == [home / ".claude.json"]


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
