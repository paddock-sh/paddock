"""Permission profiles: the chooser's answers, saved as editable JSON."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from paddock import config_dir
from paddock.agents import load_agents

# Offered in the tool checklist. The chooser shows the ones present on PATH.
TOOL_CANDIDATES = [
    "git", "rg", "fd", "jq", "curl", "node", "npm", "npx", "uv", "python3",
    "go", "cargo", "make", "cmake", "gh", "docker", "psql", "sqlite3",
]

# The entry in `tools` or `skills` that is not one of them: everything the host has, rather
# than a list of names. A sentinel like NETWORK_ALL, because no list means "all of them",
# and a backend reads it rather than expanding it (SPEC §4.1).
EVERYTHING = "*"

# The one checklist entry that is not a domain group: it names this machine (SPEC §2.1).
LOCAL_SERVICES = "local services (localhost)"

# What ticking it opens, which the domain names alone understate. Seatbelt's loopback rule
# takes no port, so the grant is every listening port, not the ones anyone had in mind.
LOCAL_SERVICES_CONSEQUENCE = "every service listening on this machine's loopback, whatever port"

# How loopback can be written. Naming any of these is what turns the grant on (SPEC §2.1).
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

# The other entry that is not a domain group: no allowlist at all. It names no domains
# because there is no pattern that means "every domain" — srt has none, and msb wants a
# default rather than a rule (SPEC §2.1). A backend reads the sentinel, not a list.
NETWORK_ALL = "everything"

# Named domain groups for the network checklist.
NETWORK_PRESETS: dict[str, list[str]] = {
    "anthropic": ["api.anthropic.com", "*.anthropic.com"],
    # What Codex CLI signs in and talks to. The agent registry opens these for codex anyway;
    # the preset is here so a profile for any other agent can grant them by ticking a box.
    "openai": ["api.openai.com", "chatgpt.com", "auth.openai.com"],
    "github": ["github.com", "*.github.com", "*.githubusercontent.com"],
    "npm": ["registry.npmjs.org", "*.npmjs.org", "*.npmjs.com"],
    "pypi/uv": ["pypi.org", "files.pythonhosted.org", "*.pythonhosted.org", "astral.sh"],
    # The module proxy serves the zips too, so no general storage host is needed.
    "go": ["proxy.golang.org", "sum.golang.org"],
    "crates.io": ["crates.io", "*.crates.io", "static.crates.io"],
    # ghcr.io redirects bottle downloads to pkg-containers.
    "homebrew": [
        "formulae.brew.sh",
        "*.brew.sh",
        "ghcr.io",
        "pkg-containers.githubusercontent.com",
    ],
    # A local inference server, a dev server, a database on this machine. Never ticked by
    # default: its entries name no port, so what it opens is every port on this machine's
    # loopback. Naming one, `localhost:8080` in the extra-domains box or in an agent's
    # own `api_domains`, is the narrower way in, and on msb it is a rule for that one
    # port (SPEC §2.1, §2.2).
    LOCAL_SERVICES: ["localhost", "127.0.0.1"],
    # No allowlist at all. Empty on purpose: the backend reads the key, not the value.
    NETWORK_ALL: [],
}

# Credential directories no agent gets unless the profile says so.
DEFAULT_DENY_READ = ["~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh"]


@dataclass
class Profile:
    name: str = "custom"
    # Key into the agent registry.
    agent: str = "claude"
    # Binaries symlinked into the sandbox PATH shim dir.
    tools: list[str] = field(default_factory=lambda: ["git", "rg", "curl"])
    # Append /usr/bin:/bin so a shell and coreutils work.
    include_system_path: bool = True
    network_presets: list[str] = field(default_factory=lambda: ["anthropic", "github"])
    extra_domains: list[str] = field(default_factory=list)
    # Host directory, read-write. "" means an isolated scratch workdir instead.
    shared_dir: str = ""
    skills: list[str] = field(default_factory=list)
    mcp: list[str] = field(default_factory=list)
    deny_read: list[str] = field(default_factory=lambda: list(DEFAULT_DENY_READ))
    # Writable paths beyond the workdir, the run dir and /tmp.
    extra_allow_write: list[str] = field(default_factory=list)

    def allowed_domains(self) -> list[str]:
        """Presets expanded, plus extra domains, plus the chosen agent's, deduped and sorted."""
        domains: list[str] = []
        for preset in self.network_presets:
            domains += NETWORK_PRESETS.get(preset, [])
        domains += self.extra_domains
        agent = load_agents().get(self.agent)
        if agent is not None:
            domains += agent.api_domains
        return sorted(set(domains))

    def opens_local_services(self) -> bool:
        """Whether the resolved domains name loopback, which is what grants it (SPEC §2.1).

        The preset is the usual way in, but a typed-in domain and an agent's own
        `api_domains` are the same declaration, so all three are read the same. A `:port`
        suffix does not change the answer here: whether a backend can act on the port is
        the backend's question, not this one (SPEC §2.1, §2.2).
        """
        return any(names_loopback(domain) for domain in self.allowed_domains())

    def opens_every_domain(self) -> bool:
        """Whether the profile asks for no allowlist at all (SPEC §2.1).

        A sentinel rather than a value in `allowed_domains()`, because no string means
        "every domain" to every backend: srt rejects the ones that try, and msb wants a
        default instead of a rule. What the backends share is the question, not the answer.
        """
        return NETWORK_ALL in self.network_presets

    def opens_every_tool(self) -> bool:
        """Whether the sandbox runs on the host's own PATH rather than a shim dir (§4.1).

        The shim dir was always the soft layer: an absolute path reaches any binary on the
        machine whatever is in it. So this drops the dir rather than filling it with every
        name on the host, and what the sandbox may write and reach is untouched.
        """
        return EVERYTHING in self.tools

    def opens_every_skill(self) -> bool:
        """Whether the config dir gets every skill the agent has, rather than a list (§4.3)."""
        return EVERYTHING in self.skills


def names_loopback(domain: str) -> bool:
    """Whether this entry names this machine, under any spelling and with or without a port."""
    return _host(domain) in LOOPBACK_HOSTS


def loopback_port(domain: str) -> int | None:
    """The one port a loopback entry scopes itself to, or None when it scopes itself to none.

    `localhost:8080` is one port on this machine and nothing else. A bare `localhost` is
    the machine and every port on it: there is no port to default to, because paddock has
    no idea what is listening, and inventing one would be a rule for a service nobody
    named. So None here means "every port", not "no port": a caller that cannot scope
    (srt, §2.1) ignores this entirely, and one that can (msb, §2.2) writes what it gets.

    None for a domain that is not loopback at all, which has no local port by definition.
    """
    host = _host(domain)
    if host not in LOOPBACK_HOSTS or host == domain:
        return None
    return int(domain.rpartition(":")[2])


def profile_dir() -> Path:
    return config_dir() / "profiles"


def builtin_profiles() -> dict[str, Profile]:
    return {
        "claude-default": Profile(
            name="claude-default",
            tools=["git", "rg", "fd", "jq", "curl", "node", "npm", "npx", "uv", "python3"],
            network_presets=["anthropic", "github", "npm", "pypi/uv"],
        ),
        "offline-shell": Profile(
            name="offline-shell",
            agent="shell",
            tools=["git", "rg", "fd", "jq"],
            network_presets=[],
        ),
    }


def load_profiles() -> dict[str, Profile]:
    """Built-ins, plus user files from `<config>/profiles/*.json`. A user file wins by name."""
    profiles = builtin_profiles()
    for path in sorted(profile_dir().glob("*.json")):
        profile = _read(path)
        if profile is not None:
            profiles[path.stem] = profile
    return profiles


def save_profile(profile: Profile) -> Path:
    """Write the profile to `<config>/profiles/<name>.json` and return the path."""
    name = profile.name
    if not name or "/" in name or name.startswith("."):
        raise ValueError(f"profile name must be a plain filename, got {name!r}")
    profile_dir().mkdir(parents=True, exist_ok=True)
    path = profile_dir() / f"{name}.json"
    path.write_text(json.dumps(asdict(profile), indent=2) + "\n")
    return path


def _host(domain: str) -> str:
    """A domain entry without srt's optional `:port` suffix.

    A bare IPv6 literal is all colons, so only a bracketed host or one with no colon at
    all can lose a trailing number: `::1` keeps its `1`, `[::1]:443` does not.

    A suffix is a port only when it is one: decimal digits, in range. `isdecimal` rather
    than `isdigit` because `isdigit` accepts characters `int` will not parse (`localhost:²`
    said yes and then raised), and the range because 0 and 65536 are not ports anything
    listens on. Anything else leaves the entry whole, which makes it an odd domain rather
    than this machine, and an odd domain is the safe reading: it gets a domain rule that
    resolves to nothing, where a loopback reading would get a rule aimed here (SPEC §2.2).
    """
    host, separator, port = domain.rpartition(":")
    if not separator or not port.isdecimal() or not 0 < int(port) < 65536:
        return domain
    bracketed = host.startswith("[") and host.endswith("]")
    return host if bracketed or ":" not in host else domain


def _read(path: Path) -> Profile | None:
    """One profile from one file, or None if the file cannot be used as written."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data["name"] = path.stem  # the filename is the name, so saving writes the same file back
    defaults = vars(Profile())  # field names, and the type each field must have
    values = {key: value for key, value in data.items() if key in defaults}
    # A wrong-shaped field rejects the whole file. Half-applying it would give the
    # sandbox a policy nobody wrote.
    for key, value in values.items():
        if not isinstance(value, type(defaults[key])):
            return None
        if isinstance(value, list) and not all(isinstance(item, str) for item in value):
            return None
    return Profile(**values)
