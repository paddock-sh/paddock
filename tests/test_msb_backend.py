"""The microsandbox backend: the msb commands it builds, and the VM one session owns.

No test starts a real VM. `_run` is the one place the backend shells out to msb, and
the `msb_calls` fixture stands in for it, the way `client` stands in for herdr.
"""

import json
from pathlib import Path

import pytest

from paddock.backends import RunNotFound, SandboxGone
from paddock.backends import microsandbox as msb
from paddock.profiles import Profile
from tests.conftest import FakeClient

SHELL = Profile(name="offline-shell", agent="shell", network_presets=[])


@pytest.fixture
def msb_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every msb command the backend runs, in order. Nothing reaches a real msb.

    It answers `msb ls` with the VMs it was asked to create and not asked to remove, so a
    prepared session looks live and a collected one does not.
    """
    calls: list[list[str]] = []
    booted: list[str] = []

    def run(*args: str) -> str:
        calls.append(list(args))
        if args[1] == "create":
            booted.append(args[args.index("--name") + 1])
        elif args[1] == "rm":
            booted[:] = [name for name in booted if name != args[-1]]
        elif args[1] == "ls":
            return json.dumps([{"name": name, "status": "Running"} for name in booted])
        return ""

    monkeypatch.setattr(msb, "_run", run)
    return calls


def create_call(calls: list[list[str]]) -> list[str]:
    return next(call for call in calls if call[1] == "create")


def commands(calls: list[list[str]]) -> list[str]:
    return [call[1] for call in calls]


def stub_msb(monkeypatch: pytest.MonkeyPatch, listed: list[dict]) -> list[list[str]]:
    """Stand in for msb with a fixed `msb ls` answer. Returns the commands it is given."""
    calls: list[list[str]] = []

    def run(*args: str) -> str:
        calls.append(list(args))
        return json.dumps(listed) if args[1] == "ls" else ""

    monkeypatch.setattr(msb, "_run", run)
    return calls


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
    """`msb rm` refuses a running sandbox without -f, so -f is never left off."""
    assert msb.stop_argv("paddock-demo") == ["msb", "rm", "-f", "paddock-demo"]


def test_what_msb_is_running_comes_from_msb_ls(
    which: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`msb ls --format json` is msb's only machine-readable view of its sandboxes."""
    calls = stub_msb(monkeypatch, [{"name": "paddock-demo", "status": "Running"}])

    assert msb.vm_is_running("paddock-demo") is True
    assert calls == [["msb", "ls", "--format", "json"]]


def test_a_vm_msb_does_not_list_has_no_status(
    which: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_msb(monkeypatch, [])

    assert msb.vm_status("paddock-demo") is None
    assert msb.vm_is_running("paddock-demo") is False


def test_a_stopped_vm_is_listed_but_cannot_be_attached_to(
    which: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stopped sandbox still has a record, and `msb exec` into it would fail."""
    stub_msb(monkeypatch, [{"name": "paddock-demo", "status": "Stopped"}])

    assert msb.vm_status("paddock-demo") == "stopped"
    assert msb.vm_is_running("paddock-demo") is False


def test_an_msb_ls_that_is_not_json_is_a_clear_error(
    which: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(msb, "_run", lambda *args: "not json")

    with pytest.raises(msb.MsbError, match="JSON"):
        msb.vm_is_running("paddock-demo")


def test_no_msb_on_the_path_names_the_install_command(which: dict[str, str]) -> None:
    which.clear()

    with pytest.raises(msb.MsbNotFound, match="install.microsandbox.dev"):
        msb.find_msb()


# --- preparing a session ---------------------------------------------------


def test_prepare_boots_one_vm_named_after_its_run_dir(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    run = msb.prepare(SHELL)

    assert commands(msb_calls) == ["create"]
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

    assert commands(msb_calls).count("create") == 1  # one VM, two tabs
    assert client.commands[0][1] == client.commands[1][1]


def test_a_tab_cannot_be_opened_somewhere_else_on_this_backend(
    which: dict[str, str], msb_calls: list[list[str]], client: FakeClient, tmp_path: Path
) -> None:
    """A host directory would only set the tab's own cwd, which the guest shell replaces."""
    run = msb.prepare(SHELL)

    with pytest.raises(ValueError, match="guest workdir"):
        msb.open_pane(run, cwd=tmp_path)

    assert client.tabs == []


def test_a_vm_that_is_gone_opens_no_tab_and_says_so(
    which: dict[str, str],
    msb_calls: list[list[str]],
    client: FakeClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the check the tab opens, `msb exec` fails, and the pane sits there dead."""
    run = msb.prepare(SHELL)
    stub_msb(monkeypatch, [])

    with pytest.raises(SandboxGone, match=run.vm_handle):
        msb.open_pane(run)

    assert client.tabs == []
    assert client.commands == []


# --- collecting the session ------------------------------------------------


def test_collecting_a_session_removes_its_vm(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    run = msb.prepare(SHELL)

    msb.collect(run.run_dir)

    assert msb_calls[-1] == ["msb", "rm", "-f", run.vm_handle]


def test_a_vm_msb_has_never_heard_of_is_not_removed_again(
    which: dict[str, str], msb_calls: list[list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    """A crash can outlive the VM. Collecting the session finishes, and says nothing."""
    run = msb.prepare(SHELL)
    msb.collect(run.run_dir)
    before = len(msb_calls)

    msb.collect(run.run_dir)

    assert commands(msb_calls[before:]) == ["ls"]  # asked, and nothing to remove
    assert capsys.readouterr().err == ""


def test_a_stopped_vm_is_still_removed(
    which: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stopped sandbox keeps its record and its disk, so collecting one has work to do."""
    calls = stub_msb(monkeypatch, [{"name": "paddock-demo", "status": "Stopped"}])

    msb.stop_vm("paddock-demo")

    assert calls[-1] == ["msb", "rm", "-f", "paddock-demo"]


def test_a_vm_that_goes_away_mid_collection_is_not_an_error(
    which: dict[str, str],
    msb_calls: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Between the list and the remove, or an msb that will not answer at all."""
    run = msb.prepare(SHELL)

    def gone(*args: str) -> str:
        raise msb.MsbError("msb rm failed: sandbox not found")

    monkeypatch.setattr(msb, "_run", gone)
    msb.collect(run.run_dir)

    assert "sandbox not found" in capsys.readouterr().err


def test_collecting_a_run_dir_with_no_launch_record_uses_the_handle_it_was_given(
    which: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The registry keeps the handle too, so a lost run dir does not leak the VM."""
    calls = stub_msb(monkeypatch, [{"name": "paddock-from-the-registry", "status": "Running"}])

    msb.collect(tmp_path, "paddock-from-the-registry")

    assert calls[-1] == ["msb", "rm", "-f", "paddock-from-the-registry"]


def test_collecting_with_no_record_and_no_handle_stops_nothing(
    msb_calls: list[list[str]], tmp_path: Path
) -> None:
    msb.collect(tmp_path)

    assert msb_calls == []


def test_the_record_wins_over_a_handle_that_disagrees(
    which: dict[str, str], msb_calls: list[list[str]], tmp_path: Path
) -> None:
    """The run dir is what the VM was actually booted as."""
    run = msb.prepare(SHELL)

    msb.collect(run.run_dir, "paddock-stale")

    assert msb_calls[-1] == ["msb", "rm", "-f", run.vm_handle]
