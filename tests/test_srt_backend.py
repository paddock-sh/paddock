"""The srt backend: settings JSON, the PATH shim dir, the composed pane command."""

import json
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from paddock.agents import AgentSpec, builtin_agents
from paddock.backends import srt
from paddock.profiles import Profile

HOME = Path.home()
CLAUDE = builtin_agents()["claude"]

# What `env -i` keeps, in the order the backend writes it.
KEEP_ENV = {
    "HOME": "/home/x",
    "USER": "x",
    "LOGNAME": "x",
    "SHELL": "/bin/zsh",
    "TERM": "xterm",
    "LANG": "en_US.UTF-8",
    "LC_ALL": "en_US.UTF-8",
    "TMPDIR": "/tmp/x",
}


class FakeClient:
    """Stands in for herdr_client: records what would have been asked of herdr."""

    def __init__(self) -> None:
        self.tabs: list[tuple[Path, str]] = []
        self.commands: list[tuple[str, str]] = []

    def create_tab(self, cwd: Path, label: str = "") -> str:
        self.tabs.append((cwd, label))
        return "wA:p2"

    def run_in_pane(self, pane_id: str, command: str) -> None:
        self.commands.append((pane_id, command))


@pytest.fixture
def which(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Control what the backend finds on the host PATH."""
    found = {"srt": "/opt/bin/srt", "npx": "/opt/bin/npx", "git": "/usr/bin/git"}
    monkeypatch.setattr(shutil, "which", found.get)
    return found


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    fake = FakeClient()
    monkeypatch.setattr(srt.herdr_client, "create_tab", fake.create_tab)
    monkeypatch.setattr(srt.herdr_client, "run_in_pane", fake.run_in_pane)
    return fake


def inner_command(command: str) -> list[str]:
    """The command srt runs, split back into words."""
    return shlex.split(shlex.split(command)[4])


# --- settings JSON ---------------------------------------------------------


def test_the_settings_hold_every_key_srt_requires() -> None:
    """srt validates the file against a schema: a missing key is a hard startup failure."""
    settings = srt.build_settings(Profile(), CLAUDE, Path("/work"))

    assert set(settings) == {"network", "filesystem"}
    assert set(settings["network"]) == {"allowedDomains", "deniedDomains"}
    assert set(settings["filesystem"]) == {"denyRead", "allowRead", "allowWrite", "denyWrite"}


def test_writes_are_allowed_for_the_workdir_and_temp(tmp_path: Path) -> None:
    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work")

    allow_write = settings["filesystem"]["allowWrite"]
    assert str(tmp_path / "work") in allow_write
    assert "/tmp" in allow_write
    assert "/private/tmp" in allow_write
    assert "/dev/null" in allow_write


def test_the_temp_dir_from_the_environment_is_writable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TMPDIR is kept in the sandbox environment, so what it points at has to be writable."""
    real = tmp_path / "real-tmp"
    real.mkdir()
    link = tmp_path / "link-tmp"
    link.symlink_to(real)
    monkeypatch.setenv("TMPDIR", f"{link}/")

    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work")

    assert str(real.resolve()) in settings["filesystem"]["allowWrite"]
    assert str(link) not in settings["filesystem"]["allowWrite"]


@pytest.mark.parametrize("value", ["", None])
def test_without_a_temp_dir_nothing_extra_is_writable(
    value: str | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if value is None:
        monkeypatch.delenv("TMPDIR", raising=False)
    else:
        monkeypatch.setenv("TMPDIR", value)

    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work")

    assert settings["filesystem"]["allowWrite"] == [
        str(tmp_path / "work"),
        "/tmp",
        "/private/tmp",
        "/dev/null",
        str(HOME / ".claude"),
    ]


def test_the_run_dir_is_not_writable(tmp_path: Path) -> None:
    """It holds the settings file and the shim dir. The sandbox only reads those."""
    run_dir = tmp_path / "run"

    settings = srt.build_settings(Profile(), CLAUDE, run_dir / "work")

    assert str(run_dir) not in settings["filesystem"]["allowWrite"]


def test_every_configured_path_is_expanded(tmp_path: Path) -> None:
    """A literal `~/.ssh` reaching srt would deny nothing at all."""
    profile = Profile(
        deny_read=["~/.ssh"],
        shared_dir="~/shared",
        extra_allow_write=["~/scratch"],
    )
    agent = AgentSpec(
        command="claude",
        auth_read_paths=["~/.claude/.credentials.json"],
        config_write_paths=["~/.claude"],
    )

    settings = srt.build_settings(profile, agent, tmp_path / "work")

    assert "~" not in json.dumps(settings)
    assert settings["filesystem"]["denyRead"] == [str(HOME / ".ssh")]
    assert settings["filesystem"]["allowRead"] == [str(HOME / ".claude/.credentials.json")]
    assert str(HOME / "shared") in settings["filesystem"]["allowWrite"]
    assert str(HOME / "scratch") in settings["filesystem"]["allowWrite"]
    assert str(HOME / ".claude") in settings["filesystem"]["allowWrite"]


def test_the_domain_allowlist_comes_from_the_profile(tmp_path: Path) -> None:
    profile = Profile(network_presets=["github"], extra_domains=["example.com"])

    settings = srt.build_settings(profile, CLAUDE, tmp_path / "work")

    assert settings["network"]["allowedDomains"] == profile.allowed_domains()
    assert "example.com" in settings["network"]["allowedDomains"]


def test_the_agents_own_credentials_stay_readable(tmp_path: Path) -> None:
    """Otherwise a profile that denies a whole config dir locks the agent out of itself."""
    profile = Profile(deny_read=["~/.claude"])
    agent = AgentSpec(command="claude", auth_read_paths=["~/.claude/.credentials.json"] * 2)

    settings = srt.build_settings(profile, agent, tmp_path / "work")

    assert settings["filesystem"]["allowRead"] == [str(HOME / ".claude/.credentials.json")]


def test_a_denied_read_is_a_denied_write_too(tmp_path: Path) -> None:
    """Sharing the home directory must not make ~/.ssh writable."""
    profile = Profile(shared_dir="~")

    settings = srt.build_settings(profile, CLAUDE, HOME)

    assert str(HOME / ".ssh") in settings["filesystem"]["denyWrite"]
    assert settings["filesystem"]["denyWrite"] == settings["filesystem"]["denyRead"]


def test_a_path_is_not_listed_twice(tmp_path: Path) -> None:
    """A shared dir is also the workdir, so the naive list repeats it."""
    shared = tmp_path / "repo"
    profile = Profile(shared_dir=str(shared), extra_allow_write=[str(shared)])

    settings = srt.build_settings(profile, CLAUDE, shared)

    assert settings["filesystem"]["allowWrite"].count(str(shared)) == 1


def test_no_domain_is_denied_by_name(tmp_path: Path) -> None:
    """The allowlist refuses everything else already; the key is written because srt wants it."""
    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work")

    assert settings["network"]["deniedDomains"] == []


# --- PATH shim dir ---------------------------------------------------------


def test_the_shim_dir_holds_one_symlink_per_selected_tool(
    which: dict[str, str], tmp_path: Path
) -> None:
    shim, skipped = srt.build_shim_dir(tmp_path, ["git"])

    assert shim == tmp_path / "bin"
    assert sorted(path.name for path in shim.iterdir()) == ["git"]
    assert (shim / "git").readlink() == Path("/usr/bin/git")
    assert skipped == []


def test_a_tool_missing_from_the_host_is_reported_and_skipped(
    which: dict[str, str], tmp_path: Path
) -> None:
    shim, skipped = srt.build_shim_dir(tmp_path, ["git", "kubectl"])

    assert sorted(path.name for path in shim.iterdir()) == ["git"]
    assert skipped == ["kubectl"]


def test_the_same_tool_twice_makes_one_symlink(which: dict[str, str], tmp_path: Path) -> None:
    shim, skipped = srt.build_shim_dir(tmp_path, ["git", "git"])

    assert sorted(path.name for path in shim.iterdir()) == ["git"]
    assert skipped == []


def test_a_tool_name_that_is_a_path_is_refused(tmp_path: Path) -> None:
    """`../escape` would put a symlink outside the shim dir, or blow up with an OSError."""
    run_dir = tmp_path / "run"

    shim, skipped = srt.build_shim_dir(run_dir, ["../escape", "/bin/sh", "..", ""])

    assert list(shim.iterdir()) == []
    assert skipped == ["../escape", "/bin/sh", "..", ""]
    assert not (run_dir / "escape").exists()
    assert not (tmp_path / "escape").exists()


# --- the pane command ------------------------------------------------------


def test_the_pane_command_uses_srts_string_mode(which: dict[str, str]) -> None:
    """srt parses bare arguments as its own flags, so the command goes through -c."""
    command = srt.pane_command(Profile(), CLAUDE, Path("/run/s.json"), Path("/run/bin"))

    assert shlex.split(command)[:4] == ["srt", "--settings", "/run/s.json", "-c"]


def test_the_shim_dir_comes_first_on_the_sandbox_path(which: dict[str, str]) -> None:
    command = srt.pane_command(Profile(), CLAUDE, Path("/run/s.json"), Path("/run/bin"))

    assert "PATH=/run/bin:/usr/bin:/bin" in inner_command(command)
    assert inner_command(command)[-1] == "claude"


def test_without_the_system_path_only_the_shim_dir_is_on_path(which: dict[str, str]) -> None:
    profile = Profile(include_system_path=False)

    command = srt.pane_command(profile, CLAUDE, Path("/run/s.json"), Path("/run/bin"))

    assert "PATH=/run/bin" in inner_command(command)


def test_the_sandbox_starts_from_an_empty_environment(
    which: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever tokens the popup inherited stay outside the sandbox."""
    for name, value in KEEP_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-token")

    command = srt.pane_command(Profile(), CLAUDE, Path("/run/s.json"), Path("/run/bin"))

    assert inner_command(command) == [
        "env", "-i",
        *(f"{name}={value}" for name, value in KEEP_ENV.items()),
        "PATH=/run/bin:/usr/bin:/bin",
        "claude",
    ]
    assert "secret-token" not in command


def test_an_unset_variable_is_left_out(
    which: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in KEEP_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("TMPDIR")

    command = srt.pane_command(Profile(), CLAUDE, Path("/run/s.json"), Path("/run/bin"))

    assert not any(word.startswith("TMPDIR=") for word in inner_command(command))


def test_a_path_with_a_space_survives_both_layers_of_quoting(which: dict[str, str]) -> None:
    """The command is a string herdr hands to a shell, and the inner command is one too."""
    command = srt.pane_command(Profile(), CLAUDE, Path("/run dir/s.json"), Path("/run dir/bin"))

    assert shlex.split(command)[2] == "/run dir/s.json"
    assert "PATH=/run dir/bin:/usr/bin:/bin" in inner_command(command)


def test_the_composed_command_survives_a_real_shell(
    real_subprocess: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """srt is handed the command by a shell, so run it past one — a stub srt, never the real one."""
    stub_dir = tmp_path / "stub bin"
    stub_dir.mkdir()
    stub = stub_dir / "srt"
    stub.write_text('#!/bin/sh\nfor arg in "$@"; do echo "$arg"; done\n')
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(stub_dir))

    command = srt.pane_command(Profile(), CLAUDE, tmp_path / "s.json", tmp_path / "shim dir")
    result = subprocess.run(command, shell=True, capture_output=True, text=True)

    assert result.returncode == 0
    argv = result.stdout.splitlines()
    assert argv[:3] == ["--settings", str(tmp_path / "s.json"), "-c"]
    assert shlex.split(argv[3])[:2] == ["env", "-i"]
    assert f"PATH={tmp_path / 'shim dir'}:/usr/bin:/bin" in shlex.split(argv[3])


# --- finding srt -----------------------------------------------------------


def test_srt_on_the_path_is_used(which: dict[str, str]) -> None:
    assert srt.find_srt() == ["srt"]


def test_npx_is_the_fallback(which: dict[str, str]) -> None:
    del which["srt"]

    assert srt.find_srt() == ["npx", "-y", "@anthropic-ai/sandbox-runtime"]


def test_no_srt_and_no_npx_names_the_install_command(which: dict[str, str]) -> None:
    which.clear()

    with pytest.raises(srt.SrtNotFound, match="npm install -g @anthropic-ai/sandbox-runtime"):
        srt.find_srt()


# --- run dir and workdir ---------------------------------------------------


def test_each_launch_gets_its_own_timestamped_run_dir(state_dir: Path) -> None:
    first, second = srt.new_run_dir(), srt.new_run_dir()

    assert first != second
    assert first.parent == state_dir / "runs"
    assert first.name.startswith(time.strftime("%Y%m%d"))


def test_a_shared_dir_is_the_workdir_and_is_created(tmp_path: Path) -> None:
    shared = tmp_path / "repo"

    workdir = srt.workdir_for(Profile(shared_dir=str(shared)), tmp_path / "run")

    assert workdir == shared
    assert shared.is_dir()


def test_without_a_shared_dir_the_workdir_is_isolated_scratch(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"

    workdir = srt.workdir_for(Profile(shared_dir=""), run_dir)

    assert workdir == run_dir / "work"
    assert list(workdir.iterdir()) == []


# --- launch ----------------------------------------------------------------


def test_launch_creates_a_labelled_tab_in_the_workdir(
    which: dict[str, str], client: FakeClient, tmp_path: Path
) -> None:
    shared = tmp_path / "repo"

    pane_id = srt.launch(Profile(name="review", shared_dir=str(shared), tools=["git"]))

    assert pane_id == "wA:p2"
    assert client.tabs == [(shared, "sbx:review")]


def test_launch_runs_the_sandboxed_command_in_the_new_pane(
    which: dict[str, str], client: FakeClient, state_dir: Path
) -> None:
    srt.launch(Profile(tools=["git"]))

    pane_id, command = client.commands[0]
    settings_path = Path(shlex.split(command)[2])
    assert pane_id == "wA:p2"
    assert settings_path.parent.parent == state_dir / "runs"
    assert json.loads(settings_path.read_text())["network"]["allowedDomains"]


def test_launch_puts_the_run_dir_shim_on_the_sandbox_path(
    which: dict[str, str], client: FakeClient
) -> None:
    srt.launch(Profile(tools=["git"]))

    _, command = client.commands[0]
    shim = Path(shlex.split(command)[2]).parent / "bin"
    assert (shim / "git").is_symlink()
    assert f"PATH={shim}:/usr/bin:/bin" in inner_command(command)


def test_launch_shims_the_agent_binary_too(which: dict[str, str], client: FakeClient) -> None:
    """`env PATH=<shim> claude` only works if claude is on that PATH."""
    which["claude"] = "/opt/bin/claude"

    srt.launch(Profile(tools=["git"]))

    _, command = client.commands[0]
    shim = Path(shlex.split(command)[2]).parent / "bin"
    assert (shim / "claude").readlink() == Path("/opt/bin/claude")


def test_an_agent_named_by_absolute_path_is_not_shimmed(
    which: dict[str, str], client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shim named `/bin/zsh` would resolve outside the shim dir; the path works as it is."""
    monkeypatch.setenv("SHELL", "/bin/zsh")

    srt.launch(Profile(agent="shell", tools=["git"]))

    _, command = client.commands[0]
    shim = Path(shlex.split(command)[2]).parent / "bin"
    assert sorted(path.name for path in shim.iterdir()) == ["git"]
    assert inner_command(command)[-1] == "/bin/zsh"


def test_a_multi_word_agent_command_is_shimmed_by_its_first_word(
    which: dict[str, str], client: FakeClient, config_dir: Path
) -> None:
    (config_dir / "agents").mkdir(parents=True)
    (config_dir / "agents" / "wrapped.json").write_text(json.dumps({"command": "npx claude-code"}))

    srt.launch(Profile(agent="wrapped", tools=[]))

    _, command = client.commands[0]
    shim = Path(shlex.split(command)[2]).parent / "bin"
    assert (shim / "npx").is_symlink()
    assert inner_command(command)[-2:] == ["npx", "claude-code"]


def test_launch_reports_the_tools_it_could_not_shim(
    which: dict[str, str], client: FakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    srt.launch(Profile(tools=["kubectl"]))

    assert "kubectl" in capsys.readouterr().err


def test_launch_does_not_create_a_tab_when_srt_is_missing(
    which: dict[str, str], client: FakeClient
) -> None:
    which.clear()

    with pytest.raises(srt.SrtNotFound):
        srt.launch(Profile(tools=[]))

    assert client.tabs == []


def test_launch_rejects_a_profile_naming_an_unknown_agent(
    which: dict[str, str], client: FakeClient
) -> None:
    with pytest.raises(ValueError, match="unknown agent"):
        srt.launch(Profile(agent="nope"))

    assert client.tabs == []


def test_launch_local_makes_a_plain_unlabelled_tab(client: FakeClient, tmp_path: Path) -> None:
    assert srt.launch_local(tmp_path) == "wA:p2"
    assert client.tabs == [(tmp_path, "")]
    assert client.commands == []
