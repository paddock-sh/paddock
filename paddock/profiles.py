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

# Named domain groups for the network checklist.
NETWORK_PRESETS: dict[str, list[str]] = {
    "anthropic": ["api.anthropic.com", "*.anthropic.com"],
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
