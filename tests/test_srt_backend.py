"""The srt backend: settings JSON, the PATH shim dir, the composed pane command."""

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from paddock import backends
from paddock.agents import AgentSpec, builtin_agents
from paddock.backends import srt
from paddock.profiles import Profile
from paddock.synth_config import SynthConfig
from tests.conftest import FakeClient, launch_command

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

# What srt injects into the shell it spawns, per invocation, so the sandbox can reach the
# network through its proxy.
PROXY_ENV = (
    "http_proxy",
    "HTTP_PROXY",
    "https_proxy",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
    "ftp_proxy",
    "FTP_PROXY",
    "grpc_proxy",
    "GRPC_PROXY",
    "RSYNC_PROXY",
    "DOCKER_HTTP_PROXY",
    "DOCKER_HTTPS_PROXY",
    "npm_config_noproxy",
    "SANDBOX_RUNTIME",
    "GIT_CONFIG_PARAMETERS",
    "GIT_SSH_COMMAND",
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

    assert set(settings) == {"network", "filesystem", "allowPty"}
    assert set(settings["network"]) == {"allowedDomains", "deniedDomains"}
    assert set(settings["filesystem"]) == {"denyRead", "allowRead", "allowWrite", "denyWrite"}


def test_the_settings_allow_pty_operations() -> None:
    """A TUI agent needs a raw-mode terminal, and Seatbelt denies the ioctl without this.

    Without it claude draws gibberish and refuses typing, and codex exits at once (SPEC §2.1).
    """
    settings = srt.build_settings(Profile(), CLAUDE, Path("/work"), NO_REDIRECT)

    assert settings["allowPty"] is True


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
    """The sandbox has its own copy, so it never needs the host's, and never gets it."""
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
        *(f"{name}=${name}" for name in PROXY_ENV),
        "PATH=/run/bin:/usr/bin:/bin",
        "claude",
    ]
    assert "secret-token" not in command


def test_the_proxy_variables_srt_sets_are_passed_through_by_name(which: dict[str, str]) -> None:
    """srt gives these to the shell it spawns, and `env -i` wipes them: no network at all.

    Each is named and left for that shell to expand, so the live values are its own (SPEC §2.1).
    """
    command = srt.pane_command(
        Profile(), CLAUDE, Path("/run/s.json"), Path("/run/bin"), NO_REDIRECT
    )

    inner = shlex.split(command)[4]
    for name in PROXY_ENV:
        assert f'{name}="${name}"' in inner


def test_no_proxy_value_is_read_from_the_environment_here(
    which: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The values that matter are srt's own, and only its shell has them."""
    monkeypatch.setenv("HTTPS_PROXY", "http://the-popups-proxy:1234")

    command = srt.pane_command(
        Profile(), CLAUDE, Path("/run/s.json"), Path("/run/bin"), NO_REDIRECT
    )

    assert "the-popups-proxy" not in command


def test_a_planted_api_key_still_does_not_reach_the_sandbox(
    which: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing the proxy variables through must not become a hole for everything else."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-planted")

    command = srt.pane_command(
        Profile(), CLAUDE, Path("/run/s.json"), Path("/run/bin"), NO_REDIRECT
    )

    assert "OPENAI_API_KEY" not in command
    assert "sk-planted" not in command


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
    """srt is handed the command by a shell, so run it past one: a stub srt, never the real one."""
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
    first, second = backends.new_run_dir(), backends.new_run_dir()

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
    which: dict[str, str],
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A shim named `/bin/zsh` would resolve outside the shim dir; the path works as it is.

    It is by design, so it is reported as such, not as something left out.
    """
    monkeypatch.setenv("SHELL", "/bin/zsh")

    run = srt.prepare(Profile(agent="shell", tools=["git"]))

    assert sorted(path.name for path in (run.run_dir / "bin").iterdir()) == ["git"]
    assert inner_command(run.command)[-1] == "/bin/zsh"
    err = capsys.readouterr().err
    assert "/bin/zsh runs by its absolute path" in err
    assert "left off the sandbox PATH" not in err


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


# --- the launch script -----------------------------------------------------


def test_prepare_writes_the_command_to_a_launch_script(
    which: dict[str, str], fake_home: Path
) -> None:
    """The pane gets a short line instead, because a tty truncates a long one."""
    run = srt.prepare(Profile(tools=[]))

    script = run.run_dir / "launch.sh"
    assert script.read_text().startswith("#!/bin/sh\n")
    assert launch_command(run.run_dir) == run.command


def test_the_launch_script_is_executable_by_its_owner_only(
    which: dict[str, str], fake_home: Path
) -> None:
    run = srt.prepare(Profile(tools=[]))

    assert (run.run_dir / "launch.sh").stat().st_mode & 0o777 == 0o700


def test_the_proxy_variables_reach_the_script_unexpanded(
    which: dict[str, str], fake_home: Path
) -> None:
    """They are srt's own, so the script has to carry the names, not values (SPEC §2.1)."""
    run = srt.prepare(Profile(tools=[]))

    text = (run.run_dir / "launch.sh").read_text()
    for name in PROXY_ENV:
        assert f'{name}="${name}"' in text


def test_the_pane_line_is_a_short_exec_of_the_script(
    which: dict[str, str], fake_home: Path
) -> None:
    """`herdr pane run` types this into the pane's tty, which drops it past 1024 bytes."""
    run = srt.prepare(Profile(tools=[]))

    line = backends.launch_line(run.run_dir)

    assert line == f"exec /bin/sh {run.run_dir}/launch.sh"
    assert len(line) < 512


def test_a_run_dir_with_a_space_is_still_one_argument(tmp_path: Path) -> None:
    run_dir = tmp_path / "run dir"

    assert shlex.split(backends.launch_line(run_dir))[2] == str(run_dir / "launch.sh")


def stub_srt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Put a fake srt first on PATH. The rest of PATH stays: the script needs `tail` and `date`."""
    stub_dir = tmp_path / "stub bin"
    stub_dir.mkdir()
    stub = stub_dir / "srt"
    stub.write_text(f"#!/bin/sh\n{body}\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}:{os.environ['PATH']}")


def run_script(run_dir: Path) -> subprocess.CompletedProcess:
    """Run the launch script the way a pane does, with a keypress ready for a held pane.

    The timeout is the point of several of these: a script that waits on something it
    should not has to fail the build, not hang it.
    """
    return subprocess.run(
        backends.launch_line(run_dir),
        shell=True,
        capture_output=True,
        text=True,
        input="\n",
        timeout=20,
    )


def test_the_script_reaches_srt_with_everything_intact(
    real_subprocess: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One more shell sits between the pane and srt now, so run the whole path past one."""
    stub_srt(tmp_path, monkeypatch, 'for arg in "$@"; do echo "$arg"; done')
    monkeypatch.setenv("HTTPS_PROXY", "http://the-popups-proxy:1234")

    command = srt.pane_command(
        Profile(), CLAUDE, tmp_path / "s.json", tmp_path / "shim dir", NO_REDIRECT
    )
    backends.write_launch_script(tmp_path, command)
    result = run_script(tmp_path)

    assert result.returncode == 0
    argv = result.stdout.splitlines()
    assert argv[:3] == ["--settings", str(tmp_path / "s.json"), "-c"]
    assert f"PATH={tmp_path / 'shim dir'}:/usr/bin:/bin" in shlex.split(argv[3])
    # srt's own shell expands these, and it is the only one that has the right values.
    assert 'HTTPS_PROXY="$HTTPS_PROXY"' in argv[3]
    assert "the-popups-proxy" not in result.stdout


# --- a launch that fails, and the pane that has to show it -------------------


def test_the_launch_keeps_its_stderr_and_replays_it_when_it_fails(
    real_subprocess: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pane that closed on failure took the one line that said why with it."""
    stub_srt(tmp_path, monkeypatch, 'echo "srt: sandbox setup failed" >&2\nexit 7')
    backends.write_launch_script(tmp_path, "srt --settings s.json")

    result = run_script(tmp_path)

    assert result.returncode == 7
    assert "srt: sandbox setup failed" in (tmp_path / "pane.log").read_text()
    # The pane sees it again on the way out, because the log is where it went live.
    assert "srt: sandbox setup failed" in result.stderr


def test_a_launch_that_leaves_a_process_behind_still_closes_the_pane(
    real_subprocess: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Anything holding the launch's stderr used to keep the whole pane waiting on it.

    A pipe closes when the last writer does, and a backgrounded descendant never does,
    so a clean launch could wedge the pane. A file has no such thing to wait for.
    """
    stub_srt(tmp_path, monkeypatch, "sleep 30 >/dev/null &\nexit 0")
    backends.write_launch_script(tmp_path, "srt")

    result = run_script(tmp_path)

    assert result.returncode == 0


def test_a_failed_launch_says_what_happened_and_waits(
    real_subprocess: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_srt(tmp_path, monkeypatch, "exit 3")
    backends.write_launch_script(tmp_path, "srt --settings s.json")

    result = run_script(tmp_path)

    assert "paddock: launch failed (exit 3)" in result.stderr
    assert str(tmp_path / "pane.log") in result.stderr
    assert "press enter" in result.stderr


def test_the_hold_puts_the_terminal_back_in_order_first(
    which: dict[str, str], fake_home: Path
) -> None:
    """An agent whose interface died can leave the terminal raw, and then nothing echoes."""
    run = srt.prepare(Profile(tools=[]))

    text = (run.run_dir / "launch.sh").read_text()
    assert text.index("stty sane") < text.index("press enter")


def test_an_agent_that_ran_a_while_and_then_exited_does_not_hold_the_pane(
    real_subprocess: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ctrl-C is exit 130. Holding the pane on that would hold it hostage."""
    monkeypatch.setattr(backends, "HOLD_WITHIN_SECONDS", 1)
    stub_srt(tmp_path, monkeypatch, "sleep 2\nexit 130")
    backends.write_launch_script(tmp_path, "srt")

    result = run_script(tmp_path)

    assert result.returncode == 130
    assert "launch failed" not in result.stderr
    assert "press enter" not in result.stderr


def test_a_big_pane_log_is_moved_aside_before_the_launch(
    real_subprocess: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One generation, so a session nobody closes cannot fill the disk."""
    stub_srt(tmp_path, monkeypatch, 'echo "this run" >&2')
    backends.write_launch_script(tmp_path, "srt")
    (tmp_path / "pane.log").write_text("x" * (backends.PANE_LOG_MAX_BYTES + 1))

    run_script(tmp_path)

    assert (tmp_path / "pane.log.1").stat().st_size == backends.PANE_LOG_MAX_BYTES + 1
    assert (tmp_path / "pane.log").read_text() == "this run\n"


def test_a_small_pane_log_is_left_where_it_is(
    real_subprocess: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_srt(tmp_path, monkeypatch, 'echo "this run" >&2')
    backends.write_launch_script(tmp_path, "srt")
    (tmp_path / "pane.log").write_text("an earlier run\n")

    run_script(tmp_path)

    assert not (tmp_path / "pane.log.1").exists()
    assert (tmp_path / "pane.log").read_text() == "an earlier run\nthis run\n"


def test_a_clean_launch_closes_the_pane_as_it_always_did(
    real_subprocess: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Holding a pane open after a good run would be a new annoyance, not a fix.

    The first launch of a run has no pane.log yet, so this is also where a script that
    complains about that shows up: nothing the script does may reach the pane.
    """
    stub_srt(tmp_path, monkeypatch, "echo done")
    backends.write_launch_script(tmp_path, "srt")

    result = run_script(tmp_path)

    assert result.returncode == 0
    assert result.stdout == "done\n"  # stdout is untouched: it is where the agent draws
    assert result.stderr == ""


def test_a_second_pane_appends_to_the_same_log(
    real_subprocess: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tabs share a run, so they share its log rather than overwriting each other's."""
    stub_srt(tmp_path, monkeypatch, 'echo "first and second" >&2')
    backends.write_launch_script(tmp_path, "srt")

    run_script(tmp_path)
    run_script(tmp_path)

    assert (tmp_path / "pane.log").read_text().count("first and second") == 2


def test_the_script_names_the_pane_log_in_the_run_dir(
    which: dict[str, str], fake_home: Path
) -> None:
    run = srt.prepare(Profile(tools=[]))

    text = (run.run_dir / "launch.sh").read_text()
    assert str(run.run_dir / "pane.log") in text
    # A pipeline here would make the script wait for every process still holding fd 2.
    called = [line for line in text.splitlines() if line.startswith("paddock_launch ")]
    assert called == ['paddock_launch 2>>"$paddock_log"']


def test_an_older_launch_script_is_replaced_when_a_tab_attaches(
    which: dict[str, str], fake_home: Path
) -> None:
    """A run prepared by an older paddock gets the current launch behaviour on its next tab."""
    run = srt.prepare(Profile(tools=[]))
    (run.run_dir / "launch.sh").write_text(f"#!/bin/sh\n{run.command}\n")

    assert srt.load_run(run.run_dir) == run

    assert launch_command(run.run_dir) == run.command
    assert 'paddock_launch 2>>"$paddock_log"' in (run.run_dir / "launch.sh").read_text()
    assert (run.run_dir / "launch.sh").stat().st_mode & 0o777 == 0o700


# --- attaching a pane to a prepared run ------------------------------------


def test_a_prepared_run_reads_back_the_same(which: dict[str, str], fake_home: Path) -> None:
    """A second tab attaches to the same settings file and workdir, hours later (SPEC §3.2)."""
    run = srt.prepare(Profile(tools=[]))

    assert srt.load_run(run.run_dir) == run


def test_a_run_dir_without_a_launch_script_gets_one_back(
    which: dict[str, str], fake_home: Path
) -> None:
    """Run dirs prepared before paddock wrote a launch script have none, and the pane needs it.

    The launch record holds the exact command, so the script is written back on attach.
    """
    run = srt.prepare(Profile(tools=[]))
    script = run.run_dir / "launch.sh"
    written = script.read_text()
    script.unlink()

    loaded = srt.load_run(run.run_dir)

    assert script.read_text() == written
    assert loaded.command in written


def test_a_run_dir_with_no_launch_record_still_raises(tmp_path: Path) -> None:
    """Writing a missing script back must not paper over a run dir nothing can attach to."""
    with pytest.raises(backends.RunNotFound, match=str(tmp_path)):
        srt.load_run(tmp_path)

    assert not (tmp_path / "launch.sh").exists()


def test_open_pane_creates_the_tab_then_runs_the_command(
    which: dict[str, str], fake_home: Path, client: FakeClient
) -> None:
    run = srt.prepare(Profile(tools=[]))

    pane_id = srt.open_pane(run, label="sbx:demo")

    assert client.tabs == [(run.workdir, "sbx:demo", run.env)]
    assert client.commands == [(pane_id, f"exec /bin/sh {run.run_dir}/launch.sh")]


def test_a_second_pane_runs_the_script_the_first_one_did(
    which: dict[str, str], fake_home: Path, client: FakeClient
) -> None:
    """A run is prepared once and attached to many times, so nothing is written again."""
    run = srt.prepare(Profile(tools=[]))
    written = (run.run_dir / "launch.sh").read_text()

    srt.open_pane(srt.load_run(run.run_dir))
    srt.open_pane(srt.load_run(run.run_dir))

    assert client.commands[0][1] == client.commands[1][1]
    assert (run.run_dir / "launch.sh").read_text() == written


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


def test_prepare_shims_what_the_agent_cannot_start_without(
    which: dict[str, str], fake_home: Path
) -> None:
    """`codex` is a script whose shebang runs node, so a PATH without node is a dead pane."""
    which["codex"] = "/opt/bin/codex"
    which["node"] = "/opt/bin/node"

    run = srt.prepare(Profile(agent="codex", tools=["git"]))

    assert (run.run_dir / "bin" / "node").readlink() == Path("/opt/bin/node")
    assert sorted(path.name for path in (run.run_dir / "bin").iterdir()) == [
        "codex", "git", "node",
    ]


def test_a_required_tool_the_profile_already_ticked_is_reported_once(
    which: dict[str, str], fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """It is the same symlink either way, so it must not be named twice when it is missing."""
    which["codex"] = "/opt/bin/codex"

    srt.prepare(Profile(agent="codex", tools=["node"]))

    assert capsys.readouterr().err.strip() == "paddock: left off the sandbox PATH: node"


# --- a shell in the sandbox the agent is in ---------------------------------


def test_a_shell_tab_runs_the_users_shell_under_the_same_settings(
    which: dict[str, str], fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same policy file, same workdir, no agent: that is what makes it the same sandbox."""
    monkeypatch.setenv("SHELL", "/bin/zsh")

    run = srt.prepare(Profile(tools=["git"]))

    argv = shlex.split(run.shell_command)
    assert argv[2] == str(run.run_dir / "srt-settings.json")
    assert shlex.split(argv[4])[-1] == "/bin/zsh"


def test_a_shell_tab_gets_the_config_dir_but_not_the_agents_flags(
    which: dict[str, str], fake_home: Path
) -> None:
    """The flags are the agent's. The variable is the sandbox's, so a shell may use it."""
    (fake_home / ".claude").mkdir()

    run = srt.prepare(Profile())

    inner = shlex.split(run.shell_command)[4]
    assert "CLAUDE_CONFIG_DIR=" in inner
    assert "--strict-mcp-config" not in inner


def test_the_shell_script_is_a_second_script_beside_the_agents(
    which: dict[str, str], fake_home: Path
) -> None:
    run = srt.prepare(Profile())

    assert (run.run_dir / "shell.sh").is_file()
    assert run.shell_command in (run.run_dir / "shell.sh").read_text()


def test_a_shell_pane_runs_the_shell_script_and_the_agent_pane_the_other(
    which: dict[str, str], fake_home: Path, client: FakeClient
) -> None:
    run = srt.prepare(Profile())

    srt.open_pane(run, label="sbx:demo")
    srt.open_pane(run, label="sbx:demo (shell)", shell=True)

    assert client.commands[0][1].endswith("launch.sh")
    assert client.commands[1][1].endswith("shell.sh")


def test_a_run_prepared_before_shell_tabs_says_so_instead_of_opening_a_dead_one(
    which: dict[str, str], fake_home: Path, client: FakeClient
) -> None:
    """An older run dir has no shell command, and a tab that runs nothing is worse than a no."""
    run = srt.prepare(Profile())
    record = json.loads((run.run_dir / "launch.json").read_text())
    del record["shell_command"]
    (run.run_dir / "launch.json").write_text(json.dumps(record))

    reloaded = srt.load_run(run.run_dir)

    with pytest.raises(ValueError, match="before paddock could open a shell"):
        srt.open_pane(reloaded, shell=True)
    assert client.commands == []


# --- a shell tab is not an agent tab ----------------------------------------


def test_a_shell_tab_keeps_its_own_log(which: dict[str, str], fake_home: Path) -> None:
    """Two tabs, two stories: the agent's stderr and the shell's are not one file."""
    run = srt.prepare(Profile())

    assert "shell.log" in (run.run_dir / "shell.sh").read_text()
    assert "pane.log" in (run.run_dir / "launch.sh").read_text()
    assert "shell.log" not in (run.run_dir / "launch.sh").read_text()


def test_a_shell_that_said_nothing_on_its_way_out_does_not_hold_the_pane(
    which: dict[str, str], fake_home: Path, real_subprocess: None, tmp_path: Path
) -> None:
    """`exit 1` in an interactive shell is the user leaving, not a launch that failed."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    backends.write_launch_script(run_dir, "/bin/sh -c 'exit 3'", backends.SHELL_SCRIPT)

    done = subprocess.run(
        ["/bin/sh", str(run_dir / backends.SHELL_SCRIPT)],
        capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
    )

    assert done.returncode == 3
    assert "press enter" not in done.stderr


def test_a_shell_that_could_not_start_still_holds_the_pane_and_says_why(
    which: dict[str, str], fake_home: Path, real_subprocess: None, tmp_path: Path
) -> None:
    """One that could not start wrote the reason on stderr, and that is the difference."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    backends.write_launch_script(
        run_dir, '/bin/sh -c \'echo no such sandbox >&2; exit 1\'', backends.SHELL_SCRIPT
    )

    done = subprocess.run(
        ["/bin/sh", str(run_dir / backends.SHELL_SCRIPT)],
        capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
    )

    assert done.returncode == 1
    assert "launch failed (exit 1)" in done.stderr
    assert "no such sandbox" in done.stderr
    assert "shell.log" in done.stderr


def test_an_agent_tab_is_held_on_any_quick_failure_as_it_always_was(
    which: dict[str, str], fake_home: Path, real_subprocess: None, tmp_path: Path
) -> None:
    """An agent that exits non-zero at once failed, whether it said anything or not."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    backends.write_launch_script(run_dir, "/bin/sh -c 'exit 3'")

    done = subprocess.run(
        ["/bin/sh", str(run_dir / backends.LAUNCH_SCRIPT)],
        capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
    )

    assert "launch failed (exit 3)" in done.stderr
