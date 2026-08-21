"""The one seam that shells out to herdr: exact argv, pane id parsing, clear errors."""

import json
import subprocess
from pathlib import Path

import pytest

from paddock.herdr_client import HerdrError, create_tab, run_in_pane

PANE_JSON = json.dumps({"result": {"root_pane": {"pane_id": "wA:p2"}}})


class FakeHerdr:
    """Stands in for the herdr CLI: records the argv, replays one canned result."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.stdout = PANE_JSON
        self.error: Exception | None = None

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, 0, stdout=self.stdout, stderr="")

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
    """Run outside herdr — during development, say — the launcher still creates a tab."""
    create_tab(tmp_path)

    assert "--workspace" not in herdr.argv
    assert herdr.argv == ["herdr", "tab", "create", "--cwd", str(tmp_path), "--focus"]


def test_create_tab_ignores_an_empty_workspace(
    herdr: FakeHerdr, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERDR_ACTIVE_WORKSPACE_ID", "")

    create_tab(tmp_path)

    assert "--workspace" not in herdr.argv


def test_create_tab_returns_the_pane_id(herdr: FakeHerdr, tmp_path: Path) -> None:
    assert create_tab(tmp_path) == "wA:p2"


def test_run_in_pane_passes_the_command_as_one_argument(herdr: FakeHerdr) -> None:
    command = "srt --settings /run/s.json 'env PATH=/run/bin claude'"

    run_in_pane("wA:p2", command)

    assert herdr.argv == ["herdr", "pane", "run", "wA:p2", command]


def test_a_missing_herdr_binary_is_a_clear_error(herdr: FakeHerdr, tmp_path: Path) -> None:
    herdr.error = FileNotFoundError(2, "No such file or directory")

    with pytest.raises(HerdrError, match="herdr"):
        create_tab(tmp_path)


def test_a_failing_herdr_command_reports_its_stderr(herdr: FakeHerdr, tmp_path: Path) -> None:
    herdr.error = subprocess.CalledProcessError(1, "herdr", stderr="no such workspace\n")

    with pytest.raises(HerdrError, match="no such workspace"):
        create_tab(tmp_path)


def test_output_that_is_not_json_is_a_clear_error(herdr: FakeHerdr, tmp_path: Path) -> None:
    herdr.stdout = "herdr: command not understood"

    with pytest.raises(HerdrError, match="JSON"):
        create_tab(tmp_path)


def test_json_without_a_pane_id_is_a_clear_error(herdr: FakeHerdr, tmp_path: Path) -> None:
    herdr.stdout = json.dumps({"result": {}})

    with pytest.raises(HerdrError, match="pane id"):
        create_tab(tmp_path)
