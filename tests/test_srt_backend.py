"""The srt backend: settings JSON, the PATH shim dir, the composed pane command."""

import json
import shlex
import shutil
import time
from pathlib import Path

import pytest

from paddock.agents import AgentSpec, builtin_agents
from paddock.backends import srt
from paddock.profiles import Profile

HOME = Path.home()
CLAUDE = builtin_agents()["claude"]


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


# --- settings JSON ---------------------------------------------------------


def test_writes_are_allowed_for_the_workdir_the_run_dir_and_temp(tmp_path: Path) -> None:
    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "run", tmp_path / "work")

    allow_write = settings["filesystem"]["allowWrite"]
    assert str(tmp_path / "work") in allow_write
    assert str(tmp_path / "run") in allow_write
    assert "/tmp" in allow_write
    assert "/private/tmp" in allow_write
    assert "/dev/null" in allow_write


def test_every_configured_path_is_expanded(tmp_path: Path) -> None:
    """A literal `~/.ssh` reaching srt would deny nothing at all."""
    profile = Profile(
        deny_read=["~/.ssh"],
        shared_dir="~/shared",
        extra_allow_write=["~/scratch"],
    )
    agent = AgentSpec(command="claude", config_write_paths=["~/.claude"])

    settings = srt.build_settings(profile, agent, tmp_path / "run", tmp_path / "work")

    assert "~" not in json.dumps(settings)
    assert settings["filesystem"]["denyRead"] == [str(HOME / ".ssh")]
    assert str(HOME / "shared") in settings["filesystem"]["allowWrite"]
    assert str(HOME / "scratch") in settings["filesystem"]["allowWrite"]
    assert str(HOME / ".claude") in settings["filesystem"]["allowWrite"]


def test_the_domain_allowlist_comes_from_the_profile(tmp_path: Path) -> None:
    profile = Profile(network_presets=["github"], extra_domains=["example.com"])

    settings = srt.build_settings(profile, CLAUDE, tmp_path / "run", tmp_path / "work")

    assert settings["network"]["allowedDomains"] == profile.allowed_domains()
    assert "example.com" in settings["network"]["allowedDomains"]


def test_a_path_is_not_listed_twice(tmp_path: Path) -> None:
    """A shared dir is also the workdir, so the naive list repeats it."""
    shared = tmp_path / "repo"
    profile = Profile(shared_dir=str(shared), extra_allow_write=[str(shared)])

    settings = srt.build_settings(profile, CLAUDE, tmp_path / "run", shared)

    assert settings["filesystem"]["allowWrite"].count(str(shared)) == 1


def test_reads_are_allowed_and_writes_denied_by_default(tmp_path: Path) -> None:
    """srt's own defaults do the work: nothing to list on either side."""
    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "run", tmp_path / "work")

    assert settings["filesystem"]["allowRead"] == []
    assert settings["filesystem"]["denyWrite"] == []


# --- PATH shim dir ---------------------------------------------------------


def test_the_shim_dir_holds_one_symlink_per_selected_tool(
    which: dict[str, str], tmp_path: Path
) -> None:
    shim = srt.build_shim_dir(tmp_path, ["git"])

    assert shim == tmp_path / "bin"
    assert sorted(path.name for path in shim.iterdir()) == ["git"]
    assert (shim / "git").readlink() == Path("/usr/bin/git")


def test_a_tool_missing_from_the_host_is_skipped_with_a_warning(
    which: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    shim = srt.build_shim_dir(tmp_path, ["git", "kubectl"])

    assert sorted(path.name for path in shim.iterdir()) == ["git"]
    assert "kubectl" in capsys.readouterr().err


# --- the pane command ------------------------------------------------------


def test_the_shim_dir_comes_first_on_the_sandbox_path(which: dict[str, str]) -> None:
    command = srt.pane_command(Profile(), CLAUDE, Path("/run/s.json"), Path("/run/bin"))

    outer = shlex.split(command)
    assert outer[:3] == ["srt", "--settings", "/run/s.json"]
    assert shlex.split(outer[3]) == ["env", "PATH=/run/bin:/usr/bin:/bin", "claude"]


def test_without_the_system_path_only_the_shim_dir_is_on_path(which: dict[str, str]) -> None:
    profile = Profile(include_system_path=False)

    command = srt.pane_command(profile, CLAUDE, Path("/run/s.json"), Path("/run/bin"))

    assert shlex.split(shlex.split(command)[3]) == ["env", "PATH=/run/bin", "claude"]


def test_a_path_with_a_space_survives_both_layers_of_quoting(which: dict[str, str]) -> None:
    """The command is a string herdr hands to a shell, and the inner command is one too."""
    command = srt.pane_command(Profile(), CLAUDE, Path("/run dir/s.json"), Path("/run dir/bin"))

    outer = shlex.split(command)
    assert outer[:3] == ["srt", "--settings", "/run dir/s.json"]
    assert shlex.split(outer[3]) == ["env", "PATH=/run dir/bin:/usr/bin:/bin", "claude"]


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
    settings_path = Path(shlex.split(command)[2])
    shim = settings_path.parent / "bin"
    assert (shim / "git").is_symlink()
    assert f"PATH={shim}:/usr/bin:/bin" in shlex.split(shlex.split(command)[3])


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
