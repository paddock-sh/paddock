"""The one seam that shells out to herdr: exact argv, pane id parsing, clear errors."""

import json
import subprocess
from pathlib import Path

import pytest

from paddock.herdr_client import (
    HerdrError,
    HerdrMissing,
    check_config,
    create_tab,
    list_pane_ids,
    reload_config,
    run_in_pane,
)

PANE_JSON = json.dumps({"result": {"root_pane": {"pane_id": "wA:p2"}}})


def pane_list_json(*pane_ids: str) -> str:
    """What `herdr pane list` answers, cut down to the one field paddock reads."""
    panes = [{"pane_id": pane_id, "tab_id": "w1:t1", "workspace_id": "w1"} for pane_id in pane_ids]
    return json.dumps({"id": "cli:pane:list", "result": {"panes": panes, "type": "pane_list"}})


class FakeHerdr:
    """Stands in for the herdr CLI: records the argv, replays one canned result."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.stdout = PANE_JSON
        self.stderr = ""
        self.returncode = 0
        self.error: Exception | None = None

    def __call__(
        self, argv: list[str], check: bool = False, **kwargs: object
    ) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        if self.error is not None:
            raise self.error
        if check and self.returncode:
            raise subprocess.CalledProcessError(self.returncode, argv, self.stdout, self.stderr)
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)

    @property
    def argv(self) -> list[str]:
        return self.calls[-1]


@pytest.fixture
def herdr(monkeypatch: pytest.MonkeyPatch) -> FakeHerdr:
    fake = FakeHerdr()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def test_create_tab_uses_the_workspace_herdr_exported(
    herdr: FakeHerdr, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERDR_ACTIVE_WORKSPACE_ID", "wA")

    create_tab(tmp_path, label="sbx:demo")

    assert herdr.argv == [
        "herdr", "tab", "create",
        "--workspace", "wA",
        "--cwd", str(tmp_path),
        "--label", "sbx:demo",
        "--focus",
    ]


def test_create_tab_omits_the_workspace_outside_herdr(herdr: FakeHerdr, tmp_path: Path) -> None:
    """Run outside herdr (during development, say), the launcher still creates a tab."""
    create_tab(tmp_path)

    assert "--workspace" not in herdr.argv
    assert herdr.argv == ["herdr", "tab", "create", "--cwd", str(tmp_path), "--focus"]


def test_create_tab_ignores_an_empty_workspace(
    herdr: FakeHerdr, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERDR_ACTIVE_WORKSPACE_ID", "")

    create_tab(tmp_path)

    assert "--workspace" not in herdr.argv


def test_create_tab_sets_the_environment_the_sandbox_config_needs(
    herdr: FakeHerdr, tmp_path: Path
) -> None:
    """One --env per variable: this is how CLAUDE_CONFIG_DIR reaches the pane (SPEC §1.3)."""
    create_tab(tmp_path, env={"CLAUDE_CONFIG_DIR": "/run/config", "FOO": "bar"})

    assert herdr.argv == [
        "herdr", "tab", "create",
        "--cwd", str(tmp_path),
        "--env", "CLAUDE_CONFIG_DIR=/run/config",
        "--env", "FOO=bar",
        "--focus",
    ]


def test_create_tab_without_environment_passes_no_env_flag(
    herdr: FakeHerdr, tmp_path: Path
) -> None:
    create_tab(tmp_path)

    assert "--env" not in herdr.argv


def test_create_tab_returns_the_pane_id(herdr: FakeHerdr, tmp_path: Path) -> None:
    assert create_tab(tmp_path) == "wA:p2"


def test_run_in_pane_passes_the_command_as_one_argument(herdr: FakeHerdr) -> None:
    command = "srt --settings /run/s.json 'env PATH=/run/bin claude'"

    run_in_pane("wA:p2", command)

    assert herdr.argv == ["herdr", "pane", "run", "wA:p2", command]


def test_list_pane_ids_asks_herdr_for_every_pane(herdr: FakeHerdr) -> None:
    """No workspace filter: a session's tabs can be in any of them."""
    herdr.stdout = pane_list_json("w1:p1", "wA:p2")

    list_pane_ids()

    assert herdr.argv == ["herdr", "pane", "list"]


def test_list_pane_ids_returns_the_ids(herdr: FakeHerdr) -> None:
    herdr.stdout = pane_list_json("w1:p1", "wA:p2")

    assert list_pane_ids() == {"w1:p1", "wA:p2"}


def test_no_panes_open_is_an_empty_answer_not_an_error(herdr: FakeHerdr) -> None:
    herdr.stdout = pane_list_json()

    assert list_pane_ids() == set()


def test_a_pane_list_that_is_not_json_is_a_clear_error(herdr: FakeHerdr) -> None:
    herdr.stdout = "herdr: command not understood"

    with pytest.raises(HerdrError, match="JSON"):
        list_pane_ids()


def test_a_pane_list_without_panes_is_a_clear_error(herdr: FakeHerdr) -> None:
    """Empty is one answer; the wrong shape is another, and guessing between them is worse."""
    herdr.stdout = json.dumps({"result": {}})

    with pytest.raises(HerdrError, match="pane"):
        list_pane_ids()


def test_a_pane_without_an_id_is_a_clear_error(herdr: FakeHerdr) -> None:
    herdr.stdout = json.dumps({"result": {"panes": [{"tab_id": "w1:t1"}]}})

    with pytest.raises(HerdrError, match="pane"):
        list_pane_ids()


def test_no_herdr_server_to_ask_is_an_error_the_caller_can_catch(herdr: FakeHerdr) -> None:
    """Reconciling has to survive this, so it must arrive as HerdrError and not a crash."""
    herdr.returncode = 1
    herdr.stderr = "no herdr server\n"

    with pytest.raises(HerdrError, match="no herdr server"):
        list_pane_ids()


def test_reload_config_asks_the_server_to_re_read_its_config(herdr: FakeHerdr) -> None:
    reload_config()

    assert herdr.argv == ["herdr", "server", "reload-config"]


def test_a_reload_with_no_server_running_is_an_error_the_caller_can_catch(
    herdr: FakeHerdr,
) -> None:
    herdr.returncode = 1
    herdr.stderr = "no herdr server\n"

    with pytest.raises(HerdrError, match="no herdr server"):
        reload_config()


def test_check_config_asks_herdr_whether_the_config_is_usable(herdr: FakeHerdr) -> None:
    check_config()

    assert herdr.argv == ["herdr", "config", "check"]


def test_a_config_herdr_will_not_have_is_an_error_the_caller_can_catch(herdr: FakeHerdr) -> None:
    herdr.returncode = 1
    herdr.stderr = "unknown key `popup`\n"

    with pytest.raises(HerdrError, match="unknown key"):
        check_config()


def test_a_missing_herdr_binary_is_a_clear_error(herdr: FakeHerdr, tmp_path: Path) -> None:
    herdr.error = FileNotFoundError(2, "No such file or directory")

    with pytest.raises(HerdrError, match="herdr"):
        create_tab(tmp_path)


def test_a_missing_binary_is_told_apart_from_a_refusal(herdr: FakeHerdr) -> None:
    """A caller that can carry on without herdr still has to know a real refusal."""
    herdr.error = FileNotFoundError(2, "No such file or directory")

    with pytest.raises(HerdrMissing):
        check_config()


def test_a_failing_herdr_command_reports_its_stderr(herdr: FakeHerdr, tmp_path: Path) -> None:
    herdr.returncode = 1
    herdr.stderr = "no such workspace\n"

    with pytest.raises(HerdrError, match="no such workspace"):
        create_tab(tmp_path)


def test_a_pane_id_that_is_not_a_string_is_a_clear_error(
    herdr: FakeHerdr, tmp_path: Path
) -> None:
    herdr.stdout = json.dumps({"result": {"root_pane": None}})

    with pytest.raises(HerdrError, match="pane id"):
        create_tab(tmp_path)


def test_output_that_is_not_json_is_a_clear_error(herdr: FakeHerdr, tmp_path: Path) -> None:
    herdr.stdout = "herdr: command not understood"

    with pytest.raises(HerdrError, match="JSON"):
        create_tab(tmp_path)


def test_json_without_a_pane_id_is_a_clear_error(herdr: FakeHerdr, tmp_path: Path) -> None:
    herdr.stdout = json.dumps({"result": {}})

    with pytest.raises(HerdrError, match="pane id"):
        create_tab(tmp_path)
