"""Profiles: defaults, JSON round-trip, tolerant loading, domain merging."""

import json
from pathlib import Path

import pytest

from paddock.profiles import (
    DEFAULT_DENY_READ,
    NETWORK_PRESETS,
    Profile,
    builtin_profiles,
    load_profiles,
    profile_dir,
    save_profile,
)


def write_profile(config_dir: Path, stem: str, data: object) -> Path:
    path = config_dir / "profiles" / f"{stem}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


def test_defaults_match_the_spec() -> None:
    profile = Profile()
    assert profile.name == "custom"
    assert profile.agent == "claude"
    assert profile.tools == ["git", "rg", "curl"]
    assert profile.include_system_path is True
    assert profile.network_presets == ["anthropic", "github"]
    assert profile.extra_domains == []
    assert profile.shared_dir == ""
    assert profile.skills == []
    assert profile.mcp == []
    assert profile.deny_read == ["~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh"]
    assert profile.extra_allow_write == []


def test_defaults_are_not_shared_between_instances() -> None:
    Profile().deny_read.append("~/.kube")
    assert "~/.kube" not in Profile().deny_read
    assert "~/.kube" not in DEFAULT_DENY_READ


def test_network_presets_cover_the_spec_keys() -> None:
    assert set(NETWORK_PRESETS) == {
        "anthropic",
        "github",
        "npm",
        "pypi/uv",
        "go",
        "crates.io",
        "homebrew",
    }


def test_save_then_load_round_trips_every_field(config_dir: Path) -> None:
    profile = Profile(
        name="round-trip",
        agent="codex",
        tools=["git", "jq"],
        include_system_path=False,
        network_presets=["npm"],
        extra_domains=["example.com"],
        shared_dir="/work/repo",
        skills=["reviewer"],
        mcp=["playwright"],
        deny_read=["~/.ssh"],
        extra_allow_write=["/var/tmp"],
    )
    path = save_profile(profile)

    assert path == config_dir / "profiles" / "round-trip.json"
    assert load_profiles()["round-trip"] == profile


def test_load_returns_builtins_when_no_user_files_exist() -> None:
    loaded = load_profiles()
    assert set(builtin_profiles()) <= set(loaded)
    assert loaded["claude-default"].agent == "claude"


def test_user_file_replaces_a_builtin_wholesale(config_dir: Path) -> None:
    """A user file is the whole profile: unset fields fall back to dataclass defaults."""
    write_profile(config_dir, "claude-default", {"agent": "aider", "tools": ["git"]})

    profile = load_profiles()["claude-default"]

    assert profile.agent == "aider"
    assert profile.tools == ["git"]
    assert profile.name == "claude-default"
    assert profile.network_presets == Profile().network_presets
    assert profile.network_presets != builtin_profiles()["claude-default"].network_presets


def test_the_filename_is_the_profile_name(config_dir: Path) -> None:
    write_profile(config_dir, "on-disk", {"name": "ignored"})

    loaded = load_profiles()

    assert "ignored" not in loaded
    assert loaded["on-disk"].name == "on-disk"


def test_user_file_adds_a_new_profile(config_dir: Path) -> None:
    write_profile(config_dir, "scratch", {"agent": "shell", "network_presets": []})

    assert load_profiles()["scratch"].agent == "shell"


def test_malformed_and_unusable_files_are_skipped(config_dir: Path) -> None:
    write_profile(config_dir, "broken", "{not json")
    write_profile(config_dir, "a-list", ["nope"])
    write_profile(config_dir, "wrong-types", {"tools": "git"})
    write_profile(config_dir, "good", {"agent": "gemini"})

    loaded = load_profiles()

    assert "broken" not in loaded
    assert "a-list" not in loaded
    assert "wrong-types" not in loaded
    assert loaded["good"].agent == "gemini"


def test_a_list_of_non_strings_is_skipped(config_dir: Path) -> None:
    """Numbers in extra_domains would blow up allowed_domains(); numbers in deny_read
    would reach the backend as a policy nobody wrote."""
    write_profile(config_dir, "numeric-domains", {"extra_domains": [1, 2, 3]})
    write_profile(config_dir, "numeric-denies", {"deny_read": [123]})

    loaded = load_profiles()

    assert "numeric-domains" not in loaded
    assert "numeric-denies" not in loaded


def test_unknown_fields_are_ignored(config_dir: Path) -> None:
    write_profile(config_dir, "future", {"agent": "shell", "teleport": True})

    profile = load_profiles()["future"]

    assert profile.agent == "shell"
    assert not hasattr(profile, "teleport")


def test_allowed_domains_merges_presets_extras_and_agent_domains() -> None:
    profile = Profile(agent="claude", network_presets=["github"], extra_domains=["example.com"])

    domains = profile.allowed_domains()

    assert "example.com" in domains
    assert set(NETWORK_PRESETS["github"]) <= set(domains)
    assert "api.anthropic.com" in domains  # from the claude agent entry


def test_allowed_domains_dedupes_preset_and_agent_overlap() -> None:
    profile = Profile(agent="claude", network_presets=["anthropic"])

    assert profile.allowed_domains().count("api.anthropic.com") == 1


def test_allowed_domains_ignores_unknown_presets_and_agents() -> None:
    profile = Profile(agent="nope", network_presets=["nope"], extra_domains=["example.com"])

    assert profile.allowed_domains() == ["example.com"]


def test_profile_dir_follows_the_config_dir_override(config_dir: Path) -> None:
    assert profile_dir() == config_dir / "profiles"


@pytest.mark.parametrize("name", ["", "sub/dir", "../escape", ".hidden"])
def test_save_rejects_a_name_that_is_not_a_plain_filename(name: str) -> None:
    with pytest.raises(ValueError):
        save_profile(Profile(name=name))
