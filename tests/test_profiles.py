"""Profiles: defaults, JSON round-trip, tolerant loading, domain merging."""

import json
from pathlib import Path

import pytest

from paddock.agents import builtin_agents
from paddock.profiles import (
    DEFAULT_DENY_READ,
    LOCAL_SERVICES,
    NETWORK_ALL,
    NETWORK_PRESETS,
    Profile,
    builtin_profiles,
    load_profiles,
    loopback_port,
    names_loopback,
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
        "openai",
        "github",
        "npm",
        "pypi/uv",
        "go",
        "crates.io",
        "homebrew",
        LOCAL_SERVICES,
        NETWORK_ALL,
    }


def test_the_everything_preset_names_no_domains_of_its_own() -> None:
    """It is a sentinel, not a group: what it asks for is "no allowlist at all"."""
    assert NETWORK_PRESETS[NETWORK_ALL] == []


def test_no_profile_ships_with_everything_ticked() -> None:
    assert NETWORK_ALL not in Profile().network_presets
    for profile in builtin_profiles().values():
        assert NETWORK_ALL not in profile.network_presets


def test_the_everything_preset_asks_for_every_domain() -> None:
    assert Profile(network_presets=[NETWORK_ALL]).opens_every_domain() is True
    assert Profile().opens_every_domain() is False


def test_the_everything_preset_leaves_the_resolved_domains_alone() -> None:
    """The sentinel is read by the backend, not folded into the list, so it adds nothing."""
    profile = Profile(agent="claude", network_presets=["github", NETWORK_ALL])
    named = Profile(agent="claude", network_presets=["github"])

    assert profile.allowed_domains() == named.allowed_domains()


def test_the_openai_preset_opens_what_codex_signs_in_and_talks_to() -> None:
    """Codex reaches these whatever is ticked. The preset is how any other agent can."""
    assert NETWORK_PRESETS["openai"] == builtin_agents()["codex"].api_domains


def test_the_local_services_preset_names_loopback_and_nothing_else() -> None:
    """It is not a domain group: what it names is this machine, under both its names."""
    assert NETWORK_PRESETS[LOCAL_SERVICES] == ["localhost", "127.0.0.1"]


def test_no_profile_ships_with_local_services_ticked() -> None:
    """Reaching every local server is a grant, so it is never on unless someone ticks it."""
    assert LOCAL_SERVICES not in Profile().network_presets
    for profile in builtin_profiles().values():
        assert LOCAL_SERVICES not in profile.network_presets


def test_the_local_services_preset_opens_loopback() -> None:
    assert Profile(network_presets=[LOCAL_SERVICES]).opens_local_services() is True


def test_a_profile_without_it_opens_no_loopback() -> None:
    remote = Profile(network_presets=[], extra_domains=["example.com"])

    assert Profile().opens_local_services() is False
    assert remote.opens_local_services() is False


@pytest.mark.parametrize(
    "domain", ["localhost", "127.0.0.1", "::1", "[::1]", "localhost:11434", "[::1]:11434"]
)
def test_any_way_of_writing_loopback_opens_it(domain: str) -> None:
    """A typed-in domain is the same grant as the preset, port suffix or not."""
    profile = Profile(network_presets=[], extra_domains=[domain])

    assert profile.opens_local_services() is True


@pytest.mark.parametrize("domain", ["my.localhost.dev", "notlocalhost", "127.0.0.1.example.com"])
def test_a_domain_that_merely_looks_like_loopback_does_not_open_it(domain: str) -> None:
    assert Profile(network_presets=[], extra_domains=[domain]).opens_local_services() is False


@pytest.mark.parametrize("domain", ["localhost", "127.0.0.1", "::1", "[::1]"])
def test_loopback_with_no_port_of_its_own_scopes_to_no_port(domain: str) -> None:
    """The preset's entries carry no port, and paddock does not invent one for them.

    None is every port, not no port: a backend that can scope reads it as the whole
    machine, which is what the tick said (SPEC §2.2).
    """
    assert loopback_port(domain) is None
    assert names_loopback(domain) is True


@pytest.mark.parametrize(
    ("domain", "port"),
    [("localhost:8000", 8000), ("127.0.0.1:5432", 5432), ("[::1]:1234", 1234)],
)
def test_a_loopback_domain_with_a_port_is_scoped_to_that_port(domain: str, port: int) -> None:
    """Naming the port is the whole configuration: the server's port, wherever it is typed."""
    assert loopback_port(domain) == port


@pytest.mark.parametrize("domain", ["example.com", "api.anthropic.com:443", "my.localhost.dev"])
def test_a_domain_that_is_not_loopback_has_no_local_port(domain: str) -> None:
    assert loopback_port(domain) is None
    assert names_loopback(domain) is False


def test_an_agent_that_names_a_local_port_opens_it_for_its_profile(config_dir: Path) -> None:
    """A local-inference agent declares the server it calls. That declaration is the choice."""
    agents = config_dir / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "local.json").write_text(
        json.dumps({"command": "aider", "api_domains": ["localhost:8080"]})
    )
    profile = Profile(agent="local", network_presets=[])

    assert profile.opens_local_services() is True
    assert [loopback_port(domain) for domain in profile.allowed_domains()] == [8080]


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


def test_the_claude_profile_allows_npm_so_a_guest_can_install_the_agent() -> None:
    """On msb the boot script downloads claude, under the same allowlist as everything else."""
    assert "npm" in builtin_profiles()["claude-default"].network_presets


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
