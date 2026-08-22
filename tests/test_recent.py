"""What the chooser opens on: the profile this workspace launched last."""

from pathlib import Path

import pytest

from paddock import recent


def test_nothing_has_been_launched_yet(state_dir: Path) -> None:
    """A first run has no memory, and paddock's own defaults are what stand."""
    assert recent.last_profile() == ""


def test_the_last_launch_is_what_comes_back(state_dir: Path) -> None:
    recent.remember("claude-default")

    assert recent.last_profile() == "claude-default"
    assert (state_dir / recent.MEMORY_FILE).is_file()


def test_each_workspace_remembers_its_own(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One workspace for the parser and one for the docs is the whole point of workspaces."""
    monkeypatch.setenv("HERDR_ACTIVE_WORKSPACE_ID", "w1")
    recent.remember("claude-default")
    monkeypatch.setenv("HERDR_ACTIVE_WORKSPACE_ID", "w2")
    recent.remember("offline-shell")

    assert recent.last_profile() == "offline-shell"
    monkeypatch.setenv("HERDR_ACTIVE_WORKSPACE_ID", "w1")
    assert recent.last_profile() == "claude-default"


def test_a_workspace_with_no_memory_of_its_own_falls_back_to_the_last_one(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERDR_ACTIVE_WORKSPACE_ID", "w1")
    recent.remember("claude-default")

    monkeypatch.setenv("HERDR_ACTIVE_WORKSPACE_ID", "brand-new")

    assert recent.last_profile() == "claude-default"


def test_a_memory_that_will_not_parse_is_no_memory_at_all(state_dir: Path) -> None:
    """It is a convenience, so a broken one costs the convenience and nothing else."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / recent.MEMORY_FILE).write_text("{not json")

    assert recent.last_profile() == ""

    recent.remember("claude-default")
    assert recent.last_profile() == "claude-default"


def test_a_memory_of_the_wrong_shape_is_no_memory_either(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / recent.MEMORY_FILE).write_text('["claude-default"]')

    assert recent.last_profile() == ""


def test_a_state_dir_that_cannot_be_written_costs_only_the_memory(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing to remember must never cost the launch that was about to happen."""
    monkeypatch.setenv("PADDOCK_STATE_DIR", "/dev/null/nowhere")

    recent.remember("claude-default")

    assert recent.last_profile() == ""
