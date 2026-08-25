"""The microsandbox backend: the msb commands it builds, and the VM one session owns.

No test starts a real VM. `_run` is the one place the backend shells out to msb, and
the `msb_calls` fixture stands in for it, the way `client` stands in for herdr.
"""

import json
from pathlib import Path

import pytest

from paddock import backends
from paddock.agents import AgentSpec
from paddock.backends import RunNotFound, SandboxGone, Swept
from paddock.backends import microsandbox as msb
from paddock.backends.microsandbox import GUEST_CONFIG_SRC
from paddock.profiles import LOCAL_SERVICES, NETWORK_ALL, Profile
from paddock.synth_config import SynthConfig
from tests.conftest import FakeClient, launch_command

SHELL = Profile(name="offline-shell", agent="shell", network_presets=[])
CLAUDE = Profile(name="claude-vm", agent="claude", network_presets=["anthropic", "npm"])

# What a shell session has: no synthesized config dir to mount or point at.
NO_CONFIG = SynthConfig()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A host home with the agent's real config dir in it, for the synthesized config dir."""
    home = tmp_path / "home"
    (home / ".claude" / "skills" / "writing").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}")
    (home / ".claude.json").write_text("{}")
    monkeypatch.setenv("HOME", str(home))
    return home


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
    argv = msb.create_argv("paddock-demo", "alpine", tmp_path, [], NO_CONFIG)

    assert argv[:2] == ["msb", "create"]
    assert flag(argv, "--name") == ["paddock-demo"]
    assert argv[-1] == "alpine"  # the image is positional, and comes last


def test_the_workdir_is_mounted_read_write_and_the_guest_starts_there(
    which: dict[str, str], tmp_path: Path
) -> None:
    argv = msb.create_argv("paddock-demo", "alpine", tmp_path, [], NO_CONFIG)

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
    argv = msb.create_argv("paddock-demo", "alpine", workdir, [], NO_CONFIG)
    assert flag(argv, "--mount-dir") == [f"{real}:/work"]


def test_without_a_shared_dir_the_run_dirs_own_work_dir_is_mounted(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"

    workdir = msb.workdir_for(Profile(shared_dir=""), run_dir)

    assert workdir == (run_dir / "work").resolve()
    assert workdir.is_dir()


def test_the_network_is_denied_by_default(which: dict[str, str], tmp_path: Path) -> None:
    argv = msb.create_argv("paddock-demo", "alpine", tmp_path, [], NO_CONFIG)

    assert flag(argv, "--net-default") == ["deny"]


def test_one_allow_rule_per_domain_the_profile_named(
    which: dict[str, str], tmp_path: Path
) -> None:
    argv = msb.create_argv(
        "paddock-demo", "alpine", tmp_path, ["github.com", "*.github.com"], NO_CONFIG
    )

    assert flag(argv, "--net-rule") == [
        "allow@dns",
        "allow@domain=github.com:tcp:443",
        "allow@domain=*.github.com:tcp:443",
    ]


@pytest.mark.parametrize("group", ["public", "private", "host", "multicast"])
def test_a_domain_that_collides_with_an_msb_group_is_never_a_group_grant(
    which: dict[str, str], group: str
) -> None:
    """msb's rule targets share one namespace with its groups, so a bare name is ambiguous.

    Measured on 0.6.13: a profile naming `public` used to emit `allow@public:tcp:443`,
    which msb read as the group, and the guest reached example.com and api.github.com,
    neither of them on the allowlist. `domain=` says which kind of target this is, and
    the same guest is then refused: the name is looked up and nothing answers to it.
    """
    rules = msb.net_rules([group])

    assert f"allow@domain={group}:tcp:443" in rules
    assert f"allow@{group}:tcp:443" not in rules
    assert f"allow@{group}" not in rules


def test_a_profile_with_no_domains_gets_no_network_at_all(
    which: dict[str, str], tmp_path: Path
) -> None:
    """Not even DNS: nothing is allowed out, so nothing needs resolving."""
    argv = msb.create_argv("paddock-demo", "alpine", tmp_path, [], NO_CONFIG)

    assert flag(argv, "--net-rule") == []
    assert flag(argv, "--net-default") == ["deny"]


def test_allow_all_is_a_default_and_not_a_rule(which: dict[str, str], tmp_path: Path) -> None:
    """msb can express what srt cannot: no allowlist at all, DNS and every port with it."""
    argv = msb.create_argv("paddock-demo", "alpine", tmp_path, ["github.com"], NO_CONFIG, True)

    assert flag(argv, "--net-default") == ["allow"]
    assert flag(argv, "--net-rule") == []


def test_the_ordinary_case_is_untouched_by_the_allow_all_flag(
    which: dict[str, str], tmp_path: Path
) -> None:
    assert msb.net_rules(["github.com"]) == [
        "--net-default", "deny",
        "--net-rule", "allow@dns",
        "--net-rule", "allow@domain=github.com:tcp:443",
    ]
    assert msb.net_rules([]) == ["--net-default", "deny"]
    assert msb.net_rules([], everything=True) == ["--net-default", "allow"]


# --- a local server, which is a host rule here ------------------------------


def test_a_named_local_port_becomes_a_port_scoped_rule_at_the_gateway(
    which: dict[str, str], tmp_path: Path
) -> None:
    """The guest's own loopback is not the host's, so the rule aims at msb's `host` group.

    Measured on 0.6.13: the group is the gateway alone, and the gateway arrives at the
    host's loopback, so a 127.0.0.1-bound server is reachable and the host's LAN address
    is not (SPEC §2.2).
    """
    argv = msb.create_argv("paddock-demo", "alpine", tmp_path, ["localhost:8080"], NO_CONFIG)

    assert flag(argv, "--net-rule") == ["allow@host:tcp:8080"]
    assert flag(argv, "--net-default") == ["deny"]


def test_the_port_is_whatever_the_profile_named(which: dict[str, str]) -> None:
    """No port is paddock's own. A server on 1234 is reached by saying 1234."""
    assert msb.net_rules(["127.0.0.1:1234"]) == [
        "--net-default", "deny", "--net-rule", "allow@host:tcp:1234"
    ]


def test_two_named_local_ports_are_two_rules(which: dict[str, str]) -> None:
    """An inference server and a database are two grants, each as narrow as it was written."""
    assert msb.net_rules(["localhost:5432", "127.0.0.1:8080"]) == [
        "--net-default", "deny",
        "--net-rule", "allow@host:tcp:5432",
        "--net-rule", "allow@host:tcp:8080",
    ]


def test_a_loopback_entry_with_no_port_opens_every_port_on_this_machine(
    which: dict[str, str],
) -> None:
    """The preset's own spelling names no port, so the grant is the width srt gives it.

    That is the reason to name a port instead, and the reason the preset is never ticked
    by default (SPEC §2.1, §2.2).
    """
    assert msb.net_rules(["127.0.0.1", "localhost"]) == [
        "--net-default", "deny", "--net-rule", "allow@host"
    ]


def test_a_named_port_folds_into_a_grant_that_already_covers_it(which: dict[str, str]) -> None:
    """Ticking the preset and naming a port is the wider of the two, said once."""
    assert msb.net_rules(["localhost", "localhost:8080"]) == [
        "--net-default", "deny", "--net-rule", "allow@host"
    ]


def test_a_port_scoped_local_profile_gets_no_dns_rule(which: dict[str, str]) -> None:
    """No `allow@dns`: the gateway is a name msb writes into the guest's own /etc/hosts,
    so nothing has to be resolved (SPEC §2.2).

    For a port-scoped grant that is also the end of it: no rule reaches the gateway's
    port 53, so names do not resolve, which the live gate measured. It is **not** true of
    the portless `allow@host`, where 53 is one of the host ports the grant covers.
    """
    assert "allow@dns" not in msb.net_rules(["localhost:8080"])


def test_a_loopback_domain_is_never_also_allowed_as_a_remote_host(which: dict[str, str]) -> None:
    """The old spelling was `allow@127.0.0.1:tcp:443`, which named the guest's own loopback."""
    rules = msb.net_rules(["127.0.0.1:8080", "github.com"])

    assert rules == [
        "--net-default", "deny",
        "--net-rule", "allow@dns",
        "--net-rule", "allow@domain=github.com:tcp:443",
        "--net-rule", "allow@host:tcp:8080",
    ]


def test_allow_all_still_writes_no_rule_when_the_profile_also_names_loopback(
    which: dict[str, str],
) -> None:
    assert msb.net_rules(["localhost:8080"], everything=True) == ["--net-default", "allow"]


# --- the endpoint the guest is given ---------------------------------------


def test_opening_one_local_port_hands_the_guest_the_endpoint(
    which: dict[str, str], tmp_path: Path
) -> None:
    """`OPENAI_BASE_URL` is what a client reads, and the alias is a courtesy beside it."""
    argv = msb.create_argv("paddock-demo", "alpine", tmp_path, ["localhost:8080"], NO_CONFIG)

    assert flag(argv, "-e") == [
        "OPENAI_BASE_URL=http://host.microsandbox.internal:8080/v1",
        "OLLAMA_HOST=http://host.microsandbox.internal:8080",
    ]


def test_the_endpoint_is_a_name_and_not_an_address(which: dict[str, str]) -> None:
    """Every sandbox gets its own /30 and its own gateway, so no address is knowable here.

    msb writes the alias into the guest's /etc/hosts at boot, which is what makes a fixed
    string correct for every sandbox.
    """
    assert msb.inference_env(["127.0.0.1:1234"]) == {
        "OPENAI_BASE_URL": f"http://{msb.HOST_ALIAS}:1234/v1",
        "OLLAMA_HOST": f"http://{msb.HOST_ALIAS}:1234",
    }


def test_a_profile_that_opened_no_local_port_is_told_about_no_endpoint(
    which: dict[str, str], tmp_path: Path
) -> None:
    """Naming an endpoint the rules refuse would be a lie the guest cannot see through."""
    argv = msb.create_argv("paddock-demo", "alpine", tmp_path, ["github.com"], NO_CONFIG)

    assert flag(argv, "-e") == []
    assert msb.inference_env([]) == {}


def test_two_local_ports_name_no_endpoint_at_all(which: dict[str, str]) -> None:
    """One of them answers prompts and paddock cannot tell which, so it points at neither."""
    assert msb.inference_env(["127.0.0.1:5432", "localhost:8080"]) == {}


def test_the_whole_machine_grant_names_no_endpoint(which: dict[str, str]) -> None:
    """The preset alone named no port, so there is no endpoint to hand on."""
    assert msb.inference_env(["localhost", "127.0.0.1"]) == {}


def test_a_named_port_still_names_the_endpoint_inside_a_whole_machine_grant(
    which: dict[str, str],
) -> None:
    """Ticking the preset and naming a port: the rule is the wider one, the endpoint stands.

    The port was still declared, and it is still where the server is. The wider rule
    reaches it, so the variable is true; it is the rule that is generous, not the name.
    """
    rules = msb.net_rules(["localhost", "localhost:8080"])

    assert rules == ["--net-default", "deny", "--net-rule", "allow@host"]
    assert msb.inference_env(["localhost", "localhost:8080"]) == {
        "OPENAI_BASE_URL": f"http://{msb.HOST_ALIAS}:8080/v1",
        "OLLAMA_HOST": f"http://{msb.HOST_ALIAS}:8080",
    }


def test_an_agents_own_api_domains_configure_the_port_end_to_end(
    which: dict[str, str], msb_calls: list[list[str]], config_dir: Path
) -> None:
    """Where the port comes from: one entry in the agent's registry file, and nothing else.

    A local-model agent declares the server it calls the same way any agent declares its
    API. The rule and the endpoint both follow from that one string (SPEC §2.2, §5).
    """
    agents = config_dir / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "local.json").write_text(
        json.dumps({"command": "sh", "image": "alpine", "api_domains": ["localhost:1234"]})
    )

    msb.prepare(Profile(agent="local", network_presets=[]))

    argv = create_call(msb_calls)
    assert flag(argv, "--net-rule") == ["allow@host:tcp:1234"]
    assert "OPENAI_BASE_URL=http://host.microsandbox.internal:1234/v1" in flag(argv, "-e")


def test_the_preset_carries_the_whole_machine_grant_through_a_prepared_session(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    """End to end from the ticked box: the preset, and the rule it becomes on this backend."""
    msb.prepare(Profile(agent="shell", network_presets=[LOCAL_SERVICES]))

    argv = create_call(msb_calls)
    assert flag(argv, "--net-rule") == ["allow@host"]
    assert flag(argv, "-e") == []


@pytest.mark.parametrize("port", ["11434", "1234", "8000"])
def test_no_inference_port_is_baked_into_paddock(which: dict[str, str], port: str) -> None:
    """The port is configuration, so no module may carry a default for one.

    These are the out-of-the-box ports of the servers the docs name: ollama, LM Studio,
    vLLM. A literal here would mean paddock had been written for one of them rather than
    for a server on whatever port the profile named. (8080 is deliberately not on this
    list: it is nobody's default, which is why the docstrings use it as the example.)
    """
    sources = Path(msb.__file__).parent.parent.rglob("*.py")

    assert [path.name for path in sources if port in path.read_text()] == []


def test_a_profile_that_opens_every_domain_boots_a_vm_that_can_reach_anything(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    """The sentinel reaches the backend through the profile, not through a domain list."""
    msb.prepare(Profile(agent="shell", network_presets=[NETWORK_ALL]))

    argv = create_call(msb_calls)
    assert flag(argv, "--net-default") == ["allow"]
    assert flag(argv, "--net-rule") == []


# --- the attach and stop commands ------------------------------------------


def test_attaching_execs_a_terminal_shell_into_the_same_vm(which: dict[str, str]) -> None:
    assert msb.attach_command("paddock-demo", []) == "msb exec --tty paddock-demo"


def test_attaching_to_an_agent_session_runs_the_agent_in_the_guest(
    which: dict[str, str],
) -> None:
    """A tab on an agent session is the agent itself, not a shell that could start it."""
    argv = ["claude", "--mcp-config", "/paddock-config/.mcp.json", "--strict-mcp-config"]

    assert msb.attach_command("paddock-demo", argv) == (
        "msb exec --tty paddock-demo -- claude "
        "--mcp-config /paddock-config/.mcp.json --strict-mcp-config"
    )


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
        *[f"allow@domain={domain}:tcp:443" for domain in profile.allowed_domains()],
    ]


def test_an_agent_with_no_image_is_refused_before_a_vm_exists(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    """The guest holds what the image holds, so an agent with no image has nothing to run."""
    with pytest.raises(ValueError, match="no image"):
        msb.prepare(Profile(agent="codex"))

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


# --- an agent inside the guest ---------------------------------------------


def test_an_agent_session_boots_the_agents_own_image(
    which: dict[str, str], msb_calls: list[list[str]], home: Path
) -> None:
    msb.prepare(CLAUDE)

    assert create_call(msb_calls)[-1] == "node:22-slim"


def test_the_synthesized_config_dir_is_mounted_read_only(
    which: dict[str, str], msb_calls: list[list[str]], home: Path
) -> None:
    """The host side is a source to copy from, never something the guest can write back to."""
    run = msb.prepare(CLAUDE)

    mounts = flag(create_call(msb_calls), "--mount-dir")
    assert f"{run.run_dir}/config:{msb.GUEST_CONFIG_SRC}:ro" in mounts
    assert not any(mount.endswith(msb.GUEST_CONFIG) for mount in mounts)


def test_the_guest_gets_its_own_copy_of_the_config_dir(
    which: dict[str, str], msb_calls: list[list[str]], home: Path
) -> None:
    """On the overlay, so what the agent writes there dies with the VM and never comes back."""
    run = msb.prepare(CLAUDE)

    assert msb_calls[-1] == [
        "msb",
        "exec",
        "--timeout",
        msb.BOOT_TIMEOUT,
        run.vm_handle,
        "--",
        "/bin/sh",
        "-c",
        f"mkdir -p {msb.GUEST_CONFIG} && cp -a {msb.GUEST_CONFIG_SRC}/. {msb.GUEST_CONFIG}/",
    ]


def test_the_config_mount_source_is_resolved_too(
    which: dict[str, str],
    msb_calls: list[list[str]],
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state dir reached through a symlink would fail the mount, the same as a workdir."""
    real = tmp_path / "linked-state"
    real.mkdir()
    link = tmp_path / "state-link"
    link.symlink_to(real)
    monkeypatch.setenv("PADDOCK_STATE_DIR", str(link))

    run = msb.prepare(CLAUDE)

    mounts = flag(create_call(msb_calls), "--mount-dir")
    assert f"{(run.run_dir / 'config').resolve()}:{msb.GUEST_CONFIG_SRC}:ro" in mounts
    assert str(link) not in " ".join(mounts)


def test_the_config_dir_variable_is_set_on_create(
    which: dict[str, str], msb_calls: list[list[str]], home: Path
) -> None:
    """`msb create -e` reaches every later exec, including the shell a tab attaches to."""
    msb.prepare(CLAUDE)

    assert f"CLAUDE_CONFIG_DIR={msb.GUEST_CONFIG}" in flag(create_call(msb_calls), "-e")


def test_the_host_tab_is_given_no_environment(
    which: dict[str, str], msb_calls: list[list[str]], client: FakeClient, home: Path
) -> None:
    """A variable on the host tab would stop at the host: the guest gets its own."""
    run = msb.prepare(CLAUDE)

    msb.open_pane(run)

    assert client.tabs[0][2] == {}


def test_the_boot_script_installs_the_agent_when_the_image_has_none(
    which: dict[str, str], msb_calls: list[list[str]], home: Path
) -> None:
    """One exec after create, before any tab: the image is stock, so claude is not in it."""
    run = msb.prepare(CLAUDE)

    assert commands(msb_calls) == ["create", "exec", "exec"]  # install, then the config copy
    assert msb_calls[1] == [
        "msb",
        "exec",
        "--timeout",
        msb.BOOT_TIMEOUT,
        run.vm_handle,
        "--",
        "/bin/sh",
        "-c",
        "command -v claude >/dev/null 2>&1 || npm install -g @anthropic-ai/claude-code@2.1.239",
    ]


def test_the_boot_script_asks_before_it_installs() -> None:
    """A custom image with the agent baked in pays nothing, and gets no second version."""
    agent = AgentSpec(command="claude", install="npm install -g x")

    assert msb.boot_script(agent).startswith("command -v claude >/dev/null 2>&1 || ")


def test_an_agent_with_nothing_to_install_runs_no_boot_script(
    which: dict[str, str], msb_calls: list[list[str]], config_dir: Path, home: Path
) -> None:
    """An image that ships the agent needs no install command, and gets no exec."""
    (config_dir / "agents").mkdir(parents=True)
    # A whole entry: a user file replaces a built-in, redirection paths included.
    (config_dir / "agents" / "claude.json").write_text(
        json.dumps(
            {
                "command": "claude",
                "image": "my/claude:1",
                "auth_read_paths": ["~/.claude/.credentials.json", "~/.claude.json"],
                "config_write_paths": ["~/.claude"],
            }
        )
    )

    msb.prepare(CLAUDE)

    assert commands(msb_calls) == ["create", "exec"]  # the config copy, and no install


def test_the_shell_agent_gets_no_config_dir_and_no_boot_script(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    """A shell has no credentials to synthesize and nothing to install."""
    run = msb.prepare(SHELL)

    argv = create_call(msb_calls)
    assert flag(argv, "--mount-dir") == [f"{run.workdir}:/work"]
    assert flag(argv, "-e") == []
    assert commands(msb_calls) == ["create"]


def failing_msb(
    monkeypatch: pytest.MonkeyPatch, fails: str, message: str
) -> list[list[str]]:
    """Stand in for an msb whose `fails` command raises. Returns the commands it is given."""
    calls: list[list[str]] = []
    booted: list[str] = []

    def run(*args: str) -> str:
        calls.append(list(args))
        if args[1] == fails:
            raise msb.MsbError(message)
        if args[1] == "create":
            booted.append(args[args.index("--name") + 1])
        elif args[1] == "ls":
            return json.dumps([{"name": name, "status": "Running"} for name in booted])
        return ""

    monkeypatch.setattr(msb, "_run", run)
    return calls


def credential_files(state_dir: Path) -> list[Path]:
    return list((state_dir / "runs").glob("*/config/.credentials.json"))


def test_the_token_is_placed_only_once_the_agent_is_installed(
    which: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    state_dir: Path,
    keychain: dict[str, str],
) -> None:
    """Deferred on purpose: an install can fail, and a token must not be on disk if it does."""
    keychain["Claude Code-credentials"] = json.dumps({"claudeAiOauth": {"accessToken": "t"}})
    seen: list[tuple[str, bool]] = []
    booted: list[str] = []

    def run(*args: str) -> str:
        seen.append((args[1], bool(credential_files(state_dir))))
        if args[1] == "create":
            booted.append(args[args.index("--name") + 1])
        elif args[1] == "ls":
            return json.dumps([{"name": name, "status": "Running"} for name in booted])
        return ""

    monkeypatch.setattr(msb, "_run", run)

    msb.prepare(CLAUDE)

    # create and the install run with no token on disk; only the copy into the guest sees one.
    assert seen == [("create", False), ("exec", False), ("exec", True)]
    assert credential_files(state_dir)


def test_an_install_that_fails_takes_the_vm_and_the_token_with_it(
    which: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    state_dir: Path,
    keychain: dict[str, str],
) -> None:
    """The session is never registered, so nothing else would ever collect that VM."""
    keychain["Claude Code-credentials"] = json.dumps({"claudeAiOauth": {"accessToken": "t"}})
    calls = failing_msb(monkeypatch, "exec", "msb exec failed: npm ERR! network request failed")

    with pytest.raises(msb.MsbError, match="npm ERR") as raised:
        msb.prepare(CLAUDE)

    assert "npm install -g @anthropic-ai/claude-code" in str(raised.value)
    assert commands(calls) == ["create", "exec", "ls", "rm"]
    assert credential_files(state_dir) == []


def test_a_create_that_fails_leaves_no_token_and_asks_msb_to_clean_up(
    which: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    state_dir: Path,
    keychain: dict[str, str],
) -> None:
    """A timeout can leave a VM behind, so the rollback runs whatever failed."""
    keychain["Claude Code-credentials"] = json.dumps({"claudeAiOauth": {"accessToken": "t"}})
    calls = failing_msb(monkeypatch, "create", "msb create gave up after 120s")

    with pytest.raises(msb.MsbError, match="gave up"):
        msb.prepare(CLAUDE)

    assert commands(calls) == ["create", "ls"]  # asked msb what is there, and it is not
    assert credential_files(state_dir) == []


def test_an_agent_with_an_image_but_no_config_dir_says_it_is_unauthenticated(
    which: dict[str, str],
    msb_calls: list[list[str]],
    config_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only Claude Code has a redirection, so anything else boots with no credentials."""
    (config_dir / "agents").mkdir(parents=True)
    (config_dir / "agents" / "codex.json").write_text(
        json.dumps({"command": "codex", "image": "node:22-slim"})
    )

    msb.prepare(Profile(agent="codex"))

    assert "unauthenticated" in capsys.readouterr().err


def test_the_agents_own_domains_reach_the_allow_rules(
    which: dict[str, str], msb_calls: list[list[str]], home: Path
) -> None:
    """The registry's domains are merged by the profile, so the guest can reach the API."""
    msb.prepare(CLAUDE)

    rules = flag(create_call(msb_calls), "--net-rule")
    assert "allow@domain=api.anthropic.com:tcp:443" in rules
    assert "allow@domain=registry.npmjs.org:tcp:443" in rules  # the boot script reaches npm


def test_the_ticked_skills_are_copied_into_the_dir_that_gets_mounted(
    which: dict[str, str], msb_calls: list[list[str]], home: Path
) -> None:
    """A symlink to a host skill would dangle in the guest, where only the mount exists."""
    (home / ".claude" / "skills" / "writing" / "SKILL.md").write_text("skill body")

    run = msb.prepare(Profile(agent="claude", skills=["writing"]))

    copied = run.run_dir / "config" / "skills" / "writing"
    assert not copied.is_symlink()
    assert (copied / "SKILL.md").read_text() == "skill body"


def test_the_launch_script_starts_the_agent_in_the_guest(
    which: dict[str, str], msb_calls: list[list[str]], home: Path
) -> None:
    run = msb.prepare(CLAUDE)

    assert launch_command(run.run_dir) == (
        f"msb exec --tty {run.vm_handle} -- claude "
        f"--mcp-config {msb.GUEST_CONFIG}/.mcp.json --strict-mcp-config"
    )


# --- the launch script -----------------------------------------------------


def test_prepare_writes_the_attach_command_to_the_launch_script(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    run = msb.prepare(SHELL)

    assert launch_command(run.run_dir) == f"msb exec --tty {run.vm_handle}"
    assert (run.run_dir / "launch.sh").stat().st_mode & 0o777 == 0o700


def test_the_msb_launch_script_is_the_shared_one_pane_log_and_all(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    """The attach line is the backend's; the pane around it is every backend's (SPEC §9).

    An msb tab that fails to exec used to close on the error the same way an srt one did,
    so it gets the same log, the same hold and the same tidy-up as srt for free.
    """
    run = msb.prepare(SHELL)

    script = (run.run_dir / "launch.sh").read_text()
    assert str(run.run_dir / "pane.log") in script
    assert 'paddock_launch 2>>"$paddock_log"' in script
    assert f'-ge {backends.HOLD_WITHIN_SECONDS} ] && exit "$paddock_exit"' in script
    assert script.index("stty sane") < script.index("press enter")


def test_an_older_msb_launch_script_is_replaced_when_a_tab_attaches(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    """A VM prepared by an older paddock gets today's launch behaviour on its next tab."""
    run = msb.prepare(SHELL)
    script = run.run_dir / "launch.sh"
    script.write_text(f"#!/bin/sh\n{run.command}\n")

    assert msb.load_run(run.run_dir) == run

    assert launch_command(run.run_dir) == run.command
    assert str(run.run_dir / "pane.log") in script.read_text()


def test_an_msb_run_dir_without_a_launch_script_gets_one_back(
    which: dict[str, str], msb_calls: list[list[str]]
) -> None:
    """The launch record holds the exact attach command, so the script is written back."""
    run = msb.prepare(SHELL)
    written = (run.run_dir / "launch.sh").read_text()
    (run.run_dir / "launch.sh").unlink()

    msb.load_run(run.run_dir)

    assert (run.run_dir / "launch.sh").read_text() == written


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


def test_ensure_live_is_the_check_both_ways_in_share(
    which: dict[str, str],
    msb_calls: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tab and a terminal ask the same question, so neither joins a VM that has gone."""
    run = msb.prepare(SHELL)

    assert msb.ensure_live(run) is None

    stub_msb(monkeypatch, [])
    with pytest.raises(SandboxGone, match=run.vm_handle):
        msb.ensure_live(run)


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

    said = capsys.readouterr().err
    assert "sandbox not found" in said
    # Removing a VM is best effort, so the message has to leave the user able to finish it.
    assert f"msb rm -f {run.vm_handle}" in said


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


# --- a shell in the guest the agent is in -----------------------------------


def test_a_shell_tab_execs_the_guests_own_shell_into_the_running_vm(
    which: dict[str, str], msb_calls: list[list[str]], home: Path
) -> None:
    """Same guest, same /work, same process namespace: that is what `msb exec` joins.

    The shell is named. `msb exec` with no argv runs the image's own command, which for
    node:22-slim is the Node REPL, and a tab that drops into that is not a shell tab.
    """
    run = msb.prepare(CLAUDE)

    assert run.shell_command == f"msb exec --tty {run.vm_handle} -- /bin/sh"
    assert run.command.startswith(f"msb exec --tty {run.vm_handle} -- claude")


def test_the_shell_script_is_a_second_script_beside_the_agents(
    which: dict[str, str], msb_calls: list[list[str]], home: Path
) -> None:
    run = msb.prepare(CLAUDE)

    assert (run.run_dir / "shell.sh").is_file()
    assert run.shell_command in (run.run_dir / "shell.sh").read_text()


def test_a_shell_pane_runs_the_shell_script_and_the_agent_pane_the_other(
    which: dict[str, str], msb_calls: list[list[str]], home: Path, client: FakeClient
) -> None:
    run = msb.prepare(CLAUDE)

    msb.open_pane(run, label="sbx:demo")
    msb.open_pane(run, label="sbx:demo (shell)", shell=True)

    assert client.commands[0][1].endswith("launch.sh")
    assert client.commands[1][1].endswith("shell.sh")
    assert [label for _, label, _ in client.tabs] == ["sbx:demo", "sbx:demo (shell)"]


def test_a_shell_tab_is_refused_when_the_vm_is_gone_like_any_other(
    which: dict[str, str], msb_calls: list[list[str]], home: Path, client: FakeClient
) -> None:
    run = msb.prepare(CLAUDE)
    msb._run(*msb.stop_argv(run.vm_handle))

    with pytest.raises(SandboxGone):
        msb.open_pane(run, shell=True)


def test_a_run_prepared_before_shell_tabs_says_so_instead_of_opening_a_dead_one(
    which: dict[str, str], msb_calls: list[list[str]], home: Path, client: FakeClient
) -> None:
    run = msb.prepare(CLAUDE)
    record = json.loads((run.run_dir / "launch.json").read_text())
    del record["shell_command"]
    (run.run_dir / "launch.json").write_text(json.dumps(record))

    reloaded = msb.load_run(run.run_dir)

    with pytest.raises(ValueError, match="before paddock could open a shell"):
        msb.open_pane(reloaded, shell=True)
    assert client.commands == []


# --- a launch nobody could roll back ----------------------------------------


def test_ctrl_c_mid_launch_takes_the_microvm_down_with_it(
    which: dict[str, str], msb_calls: list[list[str]], home: Path
) -> None:
    """Nearly the whole 20 to 70 second wait is after the VM exists, so this is the window."""
    real = msb._run

    def interrupted(*args: str) -> str:
        if args[1] == "exec" and "npm install" in args[-1]:
            raise KeyboardInterrupt
        return real(*args)

    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(msb, "_run", interrupted)
    try:
        with pytest.raises(KeyboardInterrupt):  # never wrapped: the exit code depends on it
            msb.prepare(CLAUDE)
    finally:
        monkeypatched.undo()

    assert "rm" in commands(msb_calls)
    assert msb_calls[-1][-1].startswith("paddock-")


def test_an_interrupt_after_the_token_is_placed_still_discards_it(
    which: dict[str, str], msb_calls: list[list[str]], home: Path,
    state_dir: Path, keychain: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The copy step runs after place_credentials, so this is where a token can be left."""
    keychain["Claude Code-credentials"] = "sk-planted"
    real = msb._run
    placed: list[Path] = []

    def interrupted(*args: str) -> str:
        if args[1] == "exec" and GUEST_CONFIG_SRC in args[-1]:
            placed.extend((state_dir / "runs").glob("*/config/.credentials.json"))
            raise KeyboardInterrupt
        return real(*args)

    monkeypatch.setattr(msb, "_run", interrupted)
    with pytest.raises(KeyboardInterrupt):
        msb.prepare(CLAUDE)

    assert placed, "the token has to be on disk for its removal to mean anything"
    assert not placed[0].exists()
    assert "rm" in commands(msb_calls)


def test_a_sweep_removes_a_sandbox_no_session_claims(
    which: dict[str, str], msb_calls: list[list[str]], home: Path
) -> None:
    """A process killed outright rolls nothing back, so gc asks msb what it is still running."""
    run = msb.prepare(SHELL)

    swept = msb.sweep(set(), {run.run_dir.name})

    assert swept == Swept(removed=[run.vm_handle], unowned=[])
    assert msb_calls[-1] == msb.stop_argv(run.vm_handle)


def test_a_sweep_leaves_the_sandbox_its_session_still_claims(
    which: dict[str, str], msb_calls: list[list[str]], home: Path
) -> None:
    run = msb.prepare(SHELL)

    assert msb.sweep({run.vm_handle}, {run.run_dir.name}) == Swept(removed=[], unowned=[])
    assert "rm" not in commands(msb_calls)


def test_a_sweep_never_touches_a_sandbox_paddock_did_not_make(
    which: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another tool's microVMs are none of paddock's business."""
    calls = stub_msb(monkeypatch, [{"name": "someone-elses", "status": "Running"}])

    assert msb.sweep(set(), set()) == Swept(removed=[], unowned=[])
    assert "rm" not in commands(calls)


def test_a_sweep_names_a_paddock_sandbox_this_state_dir_does_not_own(
    which: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No run of this state dir made it, so it is another context's VM, or a test's."""
    theirs = "paddock-20260822-155634-elsewhere"
    calls = stub_msb(monkeypatch, [{"name": theirs, "status": "Running"}])

    swept = msb.sweep(set(), {"20260822-000000-ours"})

    assert swept == Swept(removed=[], unowned=[theirs])
    assert "rm" not in commands(calls)


def test_a_machine_with_no_msb_is_swept_without_asking_it_anything(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """`paddock gc` must not say it could not sweep a backend this host has never had."""
    monkeypatch.setattr(msb.shutil, "which", lambda name: None)
    calls = stub_msb(monkeypatch, [])

    assert msb.sweep(set(), set()) == Swept(removed=[], unowned=[])
    assert calls == []
