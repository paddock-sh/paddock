"""`paddock init`: wire the chooser into herdr's config without losing anything else."""

import difflib
from pathlib import Path

import pytest

from paddock import herdr_client, init

# A herdr 0.8.0 config as it comes: comments, other tables, and new_tab left at its default.
PRISTINE = """\
# herdr configuration
# Anything not set here keeps its default.

[server]
socket_path = "~/.local/state/herdr/herdr.sock"

[keys]
prefix = "ctrl+a"
# new_tab = "prefix+c"
# close_pane = "prefix+x"

[ui]
theme = "dark"
"""

BLOCK_LINES = [
    'key = "prefix+c"',
    'type = "popup"',
    'command = "paddock"',
    'width = "70%"',
    'height = "70%"',
]


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The herdr config paddock will edit. HOME is the only way to point it elsewhere."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    path = home / ".config" / "herdr" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(PRISTINE)
    return path


@pytest.fixture(autouse=True)
def reload_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count the reloads. Nothing here may reach a real herdr."""
    calls: list[str] = []
    monkeypatch.setattr(herdr_client, "reload_config", lambda: calls.append("reload"))
    return calls


def backups(config: Path) -> list[Path]:
    return sorted(config.parent.glob("config.toml.paddock-backup-*"))


def removed_lines(before: str, after: str) -> list[str]:
    """Lines the edit took out or changed. Everything else is byte-identical."""
    delta = difflib.ndiff(before.splitlines(), after.splitlines())
    return [line[2:] for line in delta if line.startswith("- ")]


def added_lines(before: str, after: str) -> list[str]:
    delta = difflib.ndiff(before.splitlines(), after.splitlines())
    return [line[2:] for line in delta if line.startswith("+ ")]


# --- splicing the config ---------------------------------------------------


def test_the_block_and_the_new_tab_line_are_the_only_changes(config: Path) -> None:
    """Comments and unrelated tables survive byte for byte."""
    assert init.run() == 0

    after = config.read_text()
    assert removed_lines(PRISTINE, after) == []
    assert added_lines(PRISTINE, after) == [
        'new_tab = "prefix+shift+c"',
        "",
        init.BEGIN,
        "[[keys.command]]",
        *BLOCK_LINES,
        init.END,
    ]


def test_the_commented_default_is_left_where_it_was(config: Path) -> None:
    """It says what herdr's default is. The active line below it is what changes."""
    init.run()

    text = config.read_text()
    assert '# new_tab = "prefix+c"\nnew_tab = "prefix+shift+c"\n' in text


def test_the_written_config_is_the_toml_herdr_expects(config: Path) -> None:
    tomllib = pytest.importorskip("tomllib")
    init.run()

    keys = tomllib.loads(config.read_text())["keys"]
    assert keys["new_tab"] == "prefix+shift+c"
    assert keys["prefix"] == "ctrl+a"
    assert keys["command"] == [
        {
            "key": "prefix+c",
            "type": "popup",
            "command": "paddock",
            "width": "70%",
            "height": "70%",
        }
    ]


def test_an_active_default_binding_is_moved_in_place(config: Path) -> None:
    config.write_text(PRISTINE.replace('# new_tab = "prefix+c"', 'new_tab = "prefix+c"'))
    before = config.read_text()

    assert init.run() == 0

    after = config.read_text()
    assert removed_lines(before, after) == ['new_tab = "prefix+c"']
    assert after.count("new_tab") == 1


def test_a_custom_binding_is_left_alone_and_said_so(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Their binding is their decision. paddock still takes prefix+c for the chooser."""
    config.write_text(PRISTINE.replace('# new_tab = "prefix+c"', 'new_tab = "ctrl+n"'))
    before = config.read_text()

    assert init.run() == 0

    after = config.read_text()
    assert removed_lines(before, after) == []
    assert "new_tab" not in "".join(added_lines(before, after))
    assert "ctrl+n" in capsys.readouterr().out


def test_a_config_with_no_keys_table_gets_one(config: Path) -> None:
    tomllib = pytest.importorskip("tomllib")
    config.write_text('[ui]\ntheme = "dark"\n')

    assert init.run() == 0

    parsed = tomllib.loads(config.read_text())
    assert parsed["keys"]["new_tab"] == "prefix+shift+c"
    assert parsed["keys"]["command"][0]["command"] == "paddock"
    assert parsed["ui"]["theme"] == "dark"


def test_a_config_with_no_keys_table_is_idempotent_too(config: Path) -> None:
    config.write_text('[ui]\ntheme = "dark"\n')
    init.run()
    once = config.read_text()

    init.run()

    assert config.read_text() == once


# --- doing it twice --------------------------------------------------------


def test_the_second_run_changes_nothing(
    config: Path, reload_calls: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    init.run()
    once = config.read_text()
    capsys.readouterr()

    assert init.run() == 0
    assert config.read_text() == once
    assert len(backups(config)) == 1  # nothing changed, so nothing to back up
    assert len(reload_calls) == 1  # and nothing for herdr to re-read
    assert "already" in capsys.readouterr().out


def test_an_old_block_is_updated_not_duplicated(config: Path) -> None:
    """A block from an older paddock is replaced whole, so there is only ever one."""
    stale = f'{init.BEGIN}\n[[keys.command]]\nkey = "prefix+c"\nwidth = "50%"\n{init.END}\n'
    config.write_text(PRISTINE + "\n" + stale)

    assert init.run() == 0

    after = config.read_text()
    assert after.count(init.BEGIN) == 1
    assert '50%' not in after
    assert 'height = "70%"' in after


def test_a_custom_binding_is_still_reported_on_a_later_run(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config.write_text(PRISTINE.replace('# new_tab = "prefix+c"', 'new_tab = "ctrl+n"'))
    init.run()
    capsys.readouterr()

    init.run()

    assert "ctrl+n" in capsys.readouterr().out


# --- backups ---------------------------------------------------------------


def test_the_backup_holds_what_the_config_said_before(config: Path) -> None:
    init.run()

    saved = backups(config)
    assert len(saved) == 1
    assert saved[0].read_text() == PRISTINE


def test_undo_puts_the_old_config_back(config: Path) -> None:
    init.run()
    assert config.read_text() != PRISTINE

    assert init.run(undo=True) == 0

    assert config.read_text() == PRISTINE
    assert backups(config) == []  # the backup was used up, so a second undo has nothing to do


def test_undo_takes_the_newest_backup(config: Path) -> None:
    older = config.parent / "config.toml.paddock-backup-20200101-000000"
    older.write_text("# an older config\n")
    newer = config.parent / "config.toml.paddock-backup-20991231-235959"
    newer.write_text(PRISTINE)

    assert init.run(undo=True) == 0

    assert config.read_text() == PRISTINE
    assert older.is_file()


def test_undo_with_nothing_to_restore_says_so(config: Path) -> None:
    with pytest.raises(RuntimeError, match="no paddock backup"):
        init.run(undo=True)


def test_undo_reloads_herdr(config: Path, reload_calls: list[str]) -> None:
    init.run()

    init.run(undo=True)

    assert len(reload_calls) == 2


# --- --dry-run -------------------------------------------------------------


def test_a_dry_run_prints_the_diff_and_touches_nothing(
    config: Path, reload_calls: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert init.run(dry_run=True) == 0

    out = capsys.readouterr().out
    assert '+new_tab = "prefix+shift+c"' in out
    assert "+[[keys.command]]" in out
    assert str(config) in out
    assert config.read_text() == PRISTINE
    assert backups(config) == []
    assert reload_calls == []


def test_a_dry_run_with_nothing_left_to_do_says_so(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init.run()
    capsys.readouterr()

    assert init.run(dry_run=True) == 0
    assert "already" in capsys.readouterr().out


# --- herdr ----------------------------------------------------------------


def test_the_config_is_the_one_under_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert init.config_path() == tmp_path / ".config" / "herdr" / "config.toml"


def test_a_missing_config_is_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """herdr writes the file on first run, so paddock has nothing to splice into yet."""
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(RuntimeError, match="run herdr once"):
        init.run()


def test_herdr_is_asked_to_reload_after_the_edit(config: Path, reload_calls: list[str]) -> None:
    init.run()

    assert reload_calls == ["reload"]


def test_a_reload_that_fails_is_only_a_message(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """herdr may not be running. The config is written either way."""

    def fail() -> None:
        raise herdr_client.HerdrError("herdr not found on PATH")

    monkeypatch.setattr(herdr_client, "reload_config", fail)

    assert init.run() == 0

    out = capsys.readouterr().out
    assert "herdr server reload-config" in out
    assert 'command = "paddock"' in config.read_text()
