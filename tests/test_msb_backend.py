"""The microsandbox backend: the msb commands it builds, and the VM one session owns.

No test starts a real VM. `_run` is the one place the backend shells out to msb, and
the `msb_calls` fixture stands in for it, the way `client` stands in for herdr.
"""

import json
from pathlib import Path

import pytest

from paddock.backends import RunNotFound
from paddock.backends import microsandbox as msb
from paddock.profiles import Profile
from tests.conftest import FakeClient

SHELL = Profile(name="offline-shell", agent="shell", network_presets=[])


@pytest.fixture
def msb_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every msb command the backend runs, in order. Nothing reaches a real msb."""
    calls: list[list[str]] = []

    def run(*args: str) -> str:
        calls.append(list(args))
        return ""

    monkeypatch.setattr(msb, "_run", run)
    return calls


def create_call(calls: list[list[str]]) -> list[str]:
    return next(call for call in calls if call[1] == "create")


def flag(argv: list[str], name: str) -> list[str]:
    """Every value the argv gives this flag, in order."""
    return [argv[index + 1] for index, word in enumerate(argv) if word == name]


# --- the create command ----------------------------------------------------


def test_create_boots_a_named_vm_from_an_image(which: dict[str, str], tmp_path: Path) -> None:
    argv = msb.create_argv("paddock-demo", "alpine", tmp_path, [])

    assert argv[:2] == ["msb", "create"]
    assert flag(argv, "--name") == ["paddock-demo"]
    assert argv[-1] == "alpine"  # the image is positional, and comes last


def test_the_workdir_is_mounted_read_write_and_the_guest_starts_there(
    which: dict[str, str], tmp_path: Path
) -> None:
    argv = msb.create_argv("paddock-demo", "alpine", tmp_path, [])

    assert flag(argv, "--mount-dir") == [f"{tmp_path}:/work"]
    assert flag(argv, "--workdir") == ["/work"]


def test_a_symlinked_mount_source_is_resolved(which: dict[str, str], tmp_path: Path) -> None:
    """msb does not follow a symlinked source: /tmp/x has to arrive as /private/tmp/x."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    workdir = msb.workdir_for(Profile(shared_dir=str(link)), tmp_path / "run")

    assert workdir == real
    assert flag(msb.create_argv("paddock-demo", "alpine", workdir, []), "--mount-dir") == [
        f"{real}:/work"
    ]


def test_without_a_shared_dir_the_run_dirs_own_work_dir_is_mounted(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"

    workdir = msb.workdir_for(Profile(shared_dir=""), run_dir)

    assert workdir == (run_dir / "work").resolve()
    assert workdir.is_dir()


def test_the_network_is_denied_by_default(which: dict[str, str], tmp_path: Path) -> None:
    argv = msb.create_argv("paddock-demo", "alpine", tmp_path, [])

    assert flag(argv, "--net-default") == ["deny"]


def test_one_allow_rule_per_domain_the_profile_named(
    which: dict[str, str], tmp_path: Path
) -> None:
    argv = msb.create_argv("paddock-demo", "alpine", tmp_path, ["github.com", "*.github.com"])

    assert flag(argv, "--net-rule") == [
        "allow@dns",
        "allow@github.com:tcp:443",
        "allow@*.github.com:tcp:443",
    ]


def test_a_profile_with_no_domains_gets_no_network_at_all(
    which: dict[str, str], tmp_path: Path
) -> None:
    """Not even DNS: nothing is allowed out, so nothing needs resolving."""
    argv = msb.create_argv("paddock-demo", "alpine", tmp_path, [])

    assert flag(argv, "--net-rule") == []
    assert flag(argv, "--net-default") == ["deny"]


# --- the attach and stop commands ------------------------------------------


def test_attaching_execs_a_terminal_shell_into_the_same_vm(which: dict[str, str]) -> None:
    assert msb.attach_command("paddock-demo") == "msb exec --tty paddock-demo"


def test_stopping_a_vm_forces_it(which: dict[str, str]) -> None:
    """`msb rm` without -f is a silent no-op on a running sandbox."""
    assert msb.stop_argv("paddock-demo") == ["msb", "rm", "-f", "paddock-demo"]


def test_no_msb_on_the_path_names_the_install_command(which: dict[str, str]) -> None:
    which.clear()

    with pytest.raises(msb.MsbNotFound, match="install.microsandbox.dev"):
        msb.find_msb()


# --- preparing a session ---------------------------------------------------


def test_prepare_boots_one_vm_named_after_its_run_dir(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    run = msb.prepare(SHELL)

    assert [call[1] for call in msb_calls] == ["create"]
    assert run.vm_handle == f"paddock-{run.run_dir.name}"
    assert flag(create_call(msb_calls), "--name") == [run.vm_handle]


def test_prepare_boots_the_default_image(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    msb.prepare(SHELL)

    assert create_call(msb_calls)[-1] == msb.DEFAULT_IMAGE


def test_an_agent_with_an_image_of_its_own_is_booted_from_it(
    which: dict[str, str], msb_calls: list[list[str]], config_dir: Path
) -> None:
    """The registry's `image` field is what a profile written for srt ports over on."""
    (config_dir / "agents").mkdir(parents=True)
    (config_dir / "agents" / "shell.json").write_text(
        json.dumps({"command": "/bin/sh", "image": "busybox"})
    )

    msb.prepare(SHELL)

    assert create_call(msb_calls)[-1] == "busybox"


def test_prepare_mounts_the_profiles_shared_dir(
    which: dict[str, str], msb_calls: list[list[str]], tmp_path: Path
) -> None:
    shared = tmp_path / "repo"
    shared.mkdir()

    run = msb.prepare(Profile(agent="shell", shared_dir=str(shared)))

    assert run.workdir == shared.resolve()
    assert flag(create_call(msb_calls), "--mount-dir") == [f"{shared.resolve()}:/work"]


def test_prepare_opens_the_domains_the_profile_named(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    profile = Profile(agent="shell", network_presets=["github"], extra_domains=[])

    msb.prepare(profile)

    assert flag(create_call(msb_calls), "--net-rule") == [
        "allow@dns",
        *[f"allow@{domain}:tcp:443" for domain in profile.allowed_domains()],
    ]


def test_this_backend_runs_the_shell_agent_only(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    """Provisioning an agent inside the guest is the next feature, not a silent fallback."""
    with pytest.raises(ValueError, match="next feature"):
        msb.prepare(Profile(agent="claude"))

    assert msb_calls == []


def test_prepare_rejects_a_profile_naming_an_unknown_agent(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    with pytest.raises(ValueError, match="unknown agent"):
        msb.prepare(Profile(agent="nope"))

    assert msb_calls == []


def test_prepare_fails_before_anything_when_msb_is_missing(
    which: dict[str, str], msb_calls: list[list[str]], state_dir: Path
) -> None:
    which.clear()

    with pytest.raises(msb.MsbNotFound):
        msb.prepare(SHELL)

    assert msb_calls == []
    assert not (state_dir / "runs").exists()


def test_prepare_opens_no_tab(
    which: dict[str, str], msb_calls: list[list[str]], client: FakeClient
) -> None:
    msb.prepare(SHELL)

    assert client.tabs == []
    assert client.commands == []


# --- the launch script -----------------------------------------------------


def test_prepare_writes_the_attach_command_to_the_launch_script(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    run = msb.prepare(SHELL)

    script = (run.run_dir / "launch.sh").read_text()
    assert script == f"#!/bin/sh\nmsb exec --tty {run.vm_handle}\n"


def test_the_pane_line_is_a_short_exec_of_the_script(
    which: dict[str, str], msb_calls: list[list[str]], client: FakeClient
) -> None:
    """The pane is sent a line, not a file, and a tty drops one past 1024 bytes."""
    run = msb.prepare(SHELL)

    msb.open_pane(run)

    assert client.commands[0][1] == f"exec /bin/sh {run.run_dir}/launch.sh"
    assert len(client.commands[0][1]) < 512


# --- attaching a pane to a prepared run ------------------------------------


def test_a_prepared_run_reads_back_the_same(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    """A second tab hours later execs into the VM the first one booted (SPEC §3.2)."""
    run = msb.prepare(SHELL)

    assert msb.load_run(run.run_dir) == run


def test_a_run_dir_with_no_launch_record_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(RunNotFound, match=str(tmp_path)):
        msb.load_run(tmp_path)


def test_open_pane_creates_the_tab_then_runs_the_script(
    which: dict[str, str], msb_calls: list[list[str]], client: FakeClient
) -> None:
    run = msb.prepare(SHELL)

    pane_id = msb.open_pane(run, label="sbx:demo")

    assert client.tabs == [(run.workdir, "sbx:demo", {})]
    assert client.commands == [(pane_id, f"exec /bin/sh {run.run_dir}/launch.sh")]


def test_a_second_tab_execs_into_the_vm_the_first_one_booted(
    which: dict[str, str], msb_calls: list[list[str]], client: FakeClient
) -> None:
    run = msb.prepare(SHELL)

    msb.open_pane(msb.load_run(run.run_dir))
    msb.open_pane(msb.load_run(run.run_dir))

    assert [call[1] for call in msb_calls] == ["create"]  # one VM, two tabs
    assert client.commands[0][1] == client.commands[1][1]


def test_open_pane_can_start_the_tab_elsewhere(
    which: dict[str, str], msb_calls: list[list[str]], client: FakeClient, tmp_path: Path
) -> None:
    run = msb.prepare(SHELL)

    msb.open_pane(run, cwd=tmp_path)

    assert client.tabs[0][0] == tmp_path


# --- collecting the session ------------------------------------------------


def test_collecting_a_session_removes_its_vm(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    run = msb.prepare(SHELL)

    msb.collect(run.run_dir)

    assert msb_calls[-1] == ["msb", "rm", "-f", run.vm_handle]


def test_a_vm_that_is_already_gone_is_not_an_error(
    which: dict[str, str],
    msb_calls: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A crash can outlive the VM. Collecting the session must still finish."""
    run = msb.prepare(SHELL)

    def gone(*args: str) -> str:
        raise msb.MsbError("msb rm failed: sandbox not found")

    monkeypatch.setattr(msb, "_run", gone)
    msb.collect(run.run_dir)

    assert "sandbox not found" in capsys.readouterr().err


def test_collecting_a_run_dir_with_no_launch_record_stops_nothing(
    msb_calls: list[list[str]], tmp_path: Path
) -> None:
    msb.collect(tmp_path)

    assert msb_calls == []
