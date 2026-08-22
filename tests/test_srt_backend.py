"""The srt backend: settings JSON, the PATH shim dir, the composed pane command."""

import json
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from paddock.agents import AgentSpec, builtin_agents
from paddock.backends import srt
from paddock.profiles import Profile
from paddock.synth_config import SynthConfig
from tests.conftest import FakeClient

HOME = Path.home()
CLAUDE = builtin_agents()["claude"]

# The agent has no synthesized config dir, so it keeps writing to its real one.
NO_REDIRECT = SynthConfig()
# The agent is redirected: it writes in the run dir instead (SPEC §4.3).
REDIRECTED = SynthConfig(
    dir=Path("/run/config"),
    env={"CLAUDE_CONFIG_DIR": "/run/config"},
    args=["--mcp-config", "/run/config/.mcp.json", "--strict-mcp-config"],
    linked=[HOME / ".claude/.credentials.json", HOME / ".claude/skills/writing"],
    copied=[HOME / ".claude.json"],
)

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


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway home for the tests that build a whole run, config dir and all."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def inner_command(command: str) -> list[str]:
    """The command srt runs, split back into words."""
    return shlex.split(shlex.split(command)[4])


# --- settings JSON ---------------------------------------------------------


def test_the_settings_hold_every_key_srt_requires() -> None:
    """srt validates the file against a schema: a missing key is a hard startup failure."""
    settings = srt.build_settings(Profile(), CLAUDE, Path("/work"), NO_REDIRECT)

    assert set(settings) == {"network", "filesystem"}
    assert set(settings["network"]) == {"allowedDomains", "deniedDomains"}
    assert set(settings["filesystem"]) == {"denyRead", "allowRead", "allowWrite", "denyWrite"}


def test_writes_are_allowed_for_the_workdir_and_temp(tmp_path: Path) -> None:
    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work", NO_REDIRECT)

    allow_write = settings["filesystem"]["allowWrite"]
    assert str(tmp_path / "work") in allow_write
    assert "/tmp" in allow_write
    assert "/private/tmp" in allow_write
    assert "/dev/null" in allow_write


def test_the_temp_dir_from_the_environment_is_writable_under_both_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TMPDIR is kept in the sandbox environment, so what it points at has to be writable.

    Both names are listed, the way /tmp and /private/tmp are: srt matches the path as
    written, and resolves the one the agent actually opens.
    """
    real = tmp_path / "real-tmp"
    real.mkdir()
    link = tmp_path / "link-tmp"
    link.symlink_to(real)
    monkeypatch.setenv("TMPDIR", f"{link}/")

    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work", NO_REDIRECT)

    assert str(real.resolve()) in settings["filesystem"]["allowWrite"]
    assert str(link) in settings["filesystem"]["allowWrite"]


@pytest.mark.parametrize("value", ["", None])
def test_without_a_temp_dir_nothing_extra_is_writable(
    value: str | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if value is None:
        monkeypatch.delenv("TMPDIR", raising=False)
    else:
        monkeypatch.setenv("TMPDIR", value)

    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work", NO_REDIRECT)

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

    settings = srt.build_settings(Profile(), CLAUDE, run_dir / "work", NO_REDIRECT)

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

    settings = srt.build_settings(profile, agent, tmp_path / "work", NO_REDIRECT)

    assert "~" not in json.dumps(settings)
    assert settings["filesystem"]["denyRead"] == [str(HOME / ".ssh")]
    assert settings["filesystem"]["allowRead"] == [str(HOME / ".claude/.credentials.json")]
    assert str(HOME / "shared") in settings["filesystem"]["allowWrite"]
    assert str(HOME / "scratch") in settings["filesystem"]["allowWrite"]
    assert str(HOME / ".claude") in settings["filesystem"]["allowWrite"]


def test_the_domain_allowlist_comes_from_the_profile(tmp_path: Path) -> None:
    profile = Profile(network_presets=["github"], extra_domains=["example.com"])

    settings = srt.build_settings(profile, CLAUDE, tmp_path / "work", NO_REDIRECT)

    assert settings["network"]["allowedDomains"] == profile.allowed_domains()
    assert "example.com" in settings["network"]["allowedDomains"]


def test_the_agents_own_credentials_stay_readable(tmp_path: Path) -> None:
    """Otherwise a profile that denies a whole config dir locks the agent out of itself."""
    profile = Profile(deny_read=["~/.claude"])
    agent = AgentSpec(command="claude", auth_read_paths=["~/.claude/.credentials.json"] * 2)

    settings = srt.build_settings(profile, agent, tmp_path / "work", NO_REDIRECT)

    assert settings["filesystem"]["allowRead"] == [str(HOME / ".claude/.credentials.json")]


def test_a_denied_read_is_a_denied_write_too(tmp_path: Path) -> None:
    """Sharing the home directory must not make ~/.ssh writable."""
    profile = Profile(shared_dir="~")

    settings = srt.build_settings(profile, CLAUDE, HOME, NO_REDIRECT)

    assert str(HOME / ".ssh") in settings["filesystem"]["denyWrite"]
    assert settings["filesystem"]["denyWrite"] == settings["filesystem"]["denyRead"]


def test_a_path_is_not_listed_twice(tmp_path: Path) -> None:
    """A shared dir is also the workdir, so the naive list repeats it."""
    shared = tmp_path / "repo"
    profile = Profile(shared_dir=str(shared), extra_allow_write=[str(shared)])

    settings = srt.build_settings(profile, CLAUDE, shared, NO_REDIRECT)

    assert settings["filesystem"]["allowWrite"].count(str(shared)) == 1


def test_no_domain_is_denied_by_name(tmp_path: Path) -> None:
    """The allowlist refuses everything else already; the key is written because srt wants it."""
    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work", NO_REDIRECT)

    assert settings["network"]["deniedDomains"] == []


# --- the synthesized config dir in the settings ----------------------------


def test_a_redirected_agent_writes_in_the_synth_dir_not_its_own(tmp_path: Path) -> None:
    """The known gap from the previous PR: the real config dir was writable. It is not now."""
    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work", REDIRECTED)

    allow_write = settings["filesystem"]["allowWrite"]
    assert "/run/config" in allow_write
    assert str(HOME / ".claude") not in allow_write


def test_a_redirected_agents_real_config_dir_is_denied_both_ways(tmp_path: Path) -> None:
    """Otherwise the skills and MCP servers nobody ticked are still there to read."""
    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work", REDIRECTED)

    assert str(HOME / ".claude") in settings["filesystem"]["denyRead"]
    assert str(HOME / ".claude") in settings["filesystem"]["denyWrite"]


def test_a_redirected_agent_can_still_read_its_credentials(tmp_path: Path) -> None:
    """They are symlinked into the synth dir, so the real paths have to stay readable."""
    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work", REDIRECTED)

    assert str(HOME / ".claude/.credentials.json") in settings["filesystem"]["allowRead"]


def test_a_redirected_agents_credentials_are_read_only(tmp_path: Path) -> None:
    """A sandboxed agent reading its own key is the point; rewriting the host's copy is not."""
    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work", REDIRECTED)

    assert str(HOME / ".claude/.credentials.json") in settings["filesystem"]["denyWrite"]
    assert str(HOME / ".claude.json") in settings["filesystem"]["denyWrite"]


def test_what_the_config_dir_copied_is_hidden_on_the_host(tmp_path: Path) -> None:
    """The sandbox has its own copy, so it never needs — and never gets — the host's."""
    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work", REDIRECTED)

    assert str(HOME / ".claude.json") in settings["filesystem"]["denyRead"]
    assert str(HOME / ".claude.json") not in settings["filesystem"]["allowRead"]


def test_the_skills_the_config_dir_linked_stay_readable(tmp_path: Path) -> None:
    """The links point back into the denied config dir, and srt checks the resolved path."""
    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work", REDIRECTED)

    assert str(HOME / ".claude/skills/writing") in settings["filesystem"]["allowRead"]
    assert str(HOME / ".claude") in settings["filesystem"]["denyRead"]


def test_an_agent_with_no_redirection_keeps_writing_to_its_own_config(tmp_path: Path) -> None:
    """The gap stays open where the SPEC defines no redirection: blocking it breaks the agent."""
    settings = srt.build_settings(Profile(), CLAUDE, tmp_path / "work", NO_REDIRECT)

    assert str(HOME / ".claude") in settings["filesystem"]["allowWrite"]
    assert str(HOME / ".claude") not in settings["filesystem"]["denyRead"]


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
    command = srt.pane_command(
        Profile(), CLAUDE, Path("/run/s.json"), Path("/run/bin"), NO_REDIRECT
    )

    assert shlex.split(command)[:4] == ["srt", "--settings", "/run/s.json", "-c"]


def test_the_shim_dir_comes_first_on_the_sandbox_path(which: dict[str, str]) -> None:
    command = srt.pane_command(
        Profile(), CLAUDE, Path("/run/s.json"), Path("/run/bin"), NO_REDIRECT
    )

    assert "PATH=/run/bin:/usr/bin:/bin" in inner_command(command)
    assert inner_command(command)[-1] == "claude"


def test_without_the_system_path_only_the_shim_dir_is_on_path(which: dict[str, str]) -> None:
    profile = Profile(include_system_path=False)

    command = srt.pane_command(profile, CLAUDE, Path("/run/s.json"), Path("/run/bin"), NO_REDIRECT)

    assert "PATH=/run/bin" in inner_command(command)


def test_the_sandbox_starts_from_an_empty_environment(
    which: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever tokens the popup inherited stay outside the sandbox."""
    for name, value in KEEP_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-token")

    command = srt.pane_command(
        Profile(), CLAUDE, Path("/run/s.json"), Path("/run/bin"), NO_REDIRECT
    )

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

    command = srt.pane_command(
        Profile(), CLAUDE, Path("/run/s.json"), Path("/run/bin"), NO_REDIRECT
    )

    assert not any(word.startswith("TMPDIR=") for word in inner_command(command))


def test_the_config_dir_variable_is_written_into_the_command(which: dict[str, str]) -> None:
    """`herdr tab create --env` sets it for the pane, but `env -i` wipes that for the sandbox."""
    command = srt.pane_command(
        Profile(), CLAUDE, Path("/run/s.json"), Path("/run/bin"), REDIRECTED
    )

    assert "CLAUDE_CONFIG_DIR=/run/config" in inner_command(command)


def test_the_agents_extra_arguments_follow_its_command(which: dict[str, str]) -> None:
    command = srt.pane_command(
        Profile(), CLAUDE, Path("/run/s.json"), Path("/run/bin"), REDIRECTED
    )

    assert inner_command(command)[-4:] == [
        "claude",
        "--mcp-config",
        "/run/config/.mcp.json",
        "--strict-mcp-config",
    ]


def test_a_path_with_a_space_survives_both_layers_of_quoting(which: dict[str, str]) -> None:
    """The command is a string herdr hands to a shell, and the inner command is one too."""
    command = srt.pane_command(
        Profile(), CLAUDE, Path("/run dir/s.json"), Path("/run dir/bin"), NO_REDIRECT
    )

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

    command = srt.pane_command(
        Profile(), CLAUDE, tmp_path / "s.json", tmp_path / "shim dir", NO_REDIRECT
    )
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


# --- preparing a run -------------------------------------------------------


def test_prepare_writes_everything_the_run_needs(
    which: dict[str, str], fake_home: Path, state_dir: Path
) -> None:
    run = srt.prepare(Profile(tools=["git"]))

    assert run.run_dir.parent == state_dir / "runs"
    assert run.workdir == run.run_dir / "work"
    assert json.loads((run.run_dir / "srt-settings.json").read_text())["filesystem"]
    assert (run.run_dir / "bin" / "git").is_symlink()
    assert (run.run_dir / "config" / ".mcp.json").is_file()


def test_a_ticked_skill_is_readable_end_to_end(which: dict[str, str], fake_home: Path) -> None:
    """The whole point of layer 3, and the one thing a symlink alone does not buy."""
    skill = fake_home / ".claude" / "skills" / "writing"
    skill.mkdir(parents=True)

    run = srt.prepare(Profile(agent="claude", tools=[], skills=["writing"]))

    settings = json.loads((run.run_dir / "srt-settings.json").read_text())["filesystem"]
    assert (run.run_dir / "config" / "skills" / "writing").readlink() == skill
    assert str(skill) in settings["allowRead"]
    assert str(fake_home / ".claude") in settings["denyRead"]


def test_the_agent_writes_its_config_in_the_run_dir_end_to_end(
    which: dict[str, str], fake_home: Path
) -> None:
    """The copy is in the writable config dir; the host's file is denied both ways."""
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude.json").write_text("{}")

    run = srt.prepare(Profile(agent="claude", tools=[]))

    settings = json.loads((run.run_dir / "srt-settings.json").read_text())["filesystem"]
    assert (run.run_dir / "config" / ".claude.json").is_file()
    assert str(run.run_dir / "config") in settings["allowWrite"]
    assert str(fake_home / ".claude.json") in settings["denyRead"]
    assert str(fake_home / ".claude.json") in settings["denyWrite"]


def test_prepare_opens_no_tab(which: dict[str, str], fake_home: Path, client: FakeClient) -> None:
    """Sessions decide when a pane appears; the backend only gets the run ready."""
    srt.prepare(Profile(tools=["git"]))

    assert client.tabs == []


def test_prepare_points_the_command_at_the_settings_it_wrote(
    which: dict[str, str], fake_home: Path
) -> None:
    run = srt.prepare(Profile(tools=["git"]))

    assert shlex.split(run.command)[2] == str(run.run_dir / "srt-settings.json")
    assert f"PATH={run.run_dir / 'bin'}:/usr/bin:/bin" in inner_command(run.command)


def test_prepare_shims_the_agent_binary_too(which: dict[str, str], fake_home: Path) -> None:
    """`env PATH=<shim> claude` only works if claude is on that PATH."""
    which["claude"] = "/opt/bin/claude"

    run = srt.prepare(Profile(tools=["git"]))

    assert (run.run_dir / "bin" / "claude").readlink() == Path("/opt/bin/claude")


def test_an_agent_named_by_absolute_path_is_not_shimmed(
    which: dict[str, str], fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shim named `/bin/zsh` would resolve outside the shim dir; the path works as it is."""
    monkeypatch.setenv("SHELL", "/bin/zsh")

    run = srt.prepare(Profile(agent="shell", tools=["git"]))

    assert sorted(path.name for path in (run.run_dir / "bin").iterdir()) == ["git"]
    assert inner_command(run.command)[-1] == "/bin/zsh"


def test_a_multi_word_agent_command_is_shimmed_by_its_first_word(
    which: dict[str, str], fake_home: Path, config_dir: Path
) -> None:
    (config_dir / "agents").mkdir(parents=True)
    (config_dir / "agents" / "wrapped.json").write_text(json.dumps({"command": "npx claude-code"}))

    run = srt.prepare(Profile(agent="wrapped", tools=[]))

    assert (run.run_dir / "bin" / "npx").is_symlink()
    assert inner_command(run.command)[-2:] == ["npx", "claude-code"]


def test_prepare_reports_the_tools_it_could_not_shim(
    which: dict[str, str], fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    srt.prepare(Profile(tools=["kubectl"]))

    assert "kubectl" in capsys.readouterr().err


def test_prepare_reports_what_the_config_dir_left_out(
    which: dict[str, str], fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    srt.prepare(Profile(tools=[], skills=["writing"]))

    assert "writing" in capsys.readouterr().err


def test_prepare_fails_before_anything_when_srt_is_missing(
    which: dict[str, str], fake_home: Path, client: FakeClient
) -> None:
    which.clear()

    with pytest.raises(srt.SrtNotFound):
        srt.prepare(Profile(tools=[]))

    assert client.tabs == []


def test_prepare_rejects_a_profile_naming_an_unknown_agent(which: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="unknown agent"):
        srt.prepare(Profile(agent="nope"))


# --- attaching a pane to a prepared run ------------------------------------


def test_a_prepared_run_reads_back_the_same(which: dict[str, str], fake_home: Path) -> None:
    """A second tab attaches to the same settings file and workdir, hours later (SPEC §3.2)."""
    run = srt.prepare(Profile(tools=[]))

    assert srt.load_run(run.run_dir) == run


def test_a_run_dir_with_no_launch_record_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(srt.RunNotFound, match=str(tmp_path)):
        srt.load_run(tmp_path)


def test_open_pane_creates_the_tab_then_runs_the_command(
    which: dict[str, str], fake_home: Path, client: FakeClient
) -> None:
    run = srt.prepare(Profile(tools=[]))

    pane_id = srt.open_pane(run, label="sbx:demo")

    assert client.tabs == [(run.workdir, "sbx:demo", run.env)]
    assert client.commands == [(pane_id, run.command)]


def test_open_pane_passes_the_config_dir_to_the_tab(
    which: dict[str, str], fake_home: Path, client: FakeClient
) -> None:
    """SPEC §1.3: layer-3 variables are set at tab creation, not smuggled into the command."""
    run = srt.prepare(Profile(tools=[]))

    srt.open_pane(run, label="sbx:demo")

    assert client.tabs[0][2] == {"CLAUDE_CONFIG_DIR": str(run.run_dir / "config")}


def test_open_pane_can_start_the_tab_elsewhere(
    which: dict[str, str], fake_home: Path, client: FakeClient, tmp_path: Path
) -> None:
    run = srt.prepare(Profile(tools=[]))

    srt.open_pane(run, label="sbx:demo", cwd=tmp_path)

    assert client.tabs[0][0] == tmp_path
