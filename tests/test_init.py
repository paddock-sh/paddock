"""`paddock init`: wire the chooser into herdr's config without losing anything else."""

import difflib
from pathlib import Path

import pytest

from paddock import herdr_client, init

# A herdr 0.8.0 config as a user's looks: comments, other tables, new_tab left at its default.
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

tomllib = pytest.importorskip("tomllib", reason="python 3.11+ parses the result")


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The herdr config paddock will edit. HOME is the only way to point it elsewhere."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    path = home / ".config" / "herdr" / "config.toml"
    path.parent.mkdir(parents=True)
    write(path, PRISTINE)
    return path


@pytest.fixture
def empty_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A machine where herdr has never written a config, which is the usual case."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / ".config" / "herdr" / "config.toml"


@pytest.fixture(autouse=True)
def herdr(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What paddock asked of herdr. Nothing here may reach a real one."""
    calls: list[str] = []
    monkeypatch.setattr(herdr_client, "reload_config", lambda: calls.append("reload"))
    monkeypatch.setattr(herdr_client, "check_config", lambda: calls.append("check") or "")
    return calls


def write(path: Path, text: str) -> None:
    """Write a config as given: the line endings are what several tests are about."""
    with path.open("w", newline="") as handle:
        handle.write(text)


def read(path: Path) -> str:
    with path.open(newline="") as handle:
        return handle.read()


def backups(config: Path) -> list[Path]:
    return sorted(config.parent.glob("config.toml.paddock-backup-*"))


def undone(config: Path) -> list[Path]:
    return sorted(config.parent.glob("config.toml.paddock-undone-*"))


def removed_lines(before: str, after: str) -> list[str]:
    """Lines the edit took out or changed. Everything else is byte-identical."""
    delta = difflib.ndiff(before.splitlines(), after.splitlines())
    return [line[2:] for line in delta if line.startswith("- ")]


def added_lines(before: str, after: str) -> list[str]:
    delta = difflib.ndiff(before.splitlines(), after.splitlines())
    return [line[2:] for line in delta if line.startswith("+ ")]


def unmanaged(text: str) -> list[str]:
    """The lines that are the user's, not paddock's block."""
    kept = init._without_block(text.splitlines(keepends=True))
    return [line.strip() for line in kept if line.strip()]


# --- splicing the config ---------------------------------------------------


def test_the_block_and_the_new_tab_line_are_the_only_changes(config: Path) -> None:
    """Comments and unrelated tables survive byte for byte."""
    assert init.run() == 0

    after = read(config)
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

    assert '# new_tab = "prefix+c"\nnew_tab = "prefix+shift+c"\n' in read(config)


def test_the_written_config_is_the_toml_herdr_expects(config: Path) -> None:
    init.run()

    keys = tomllib.loads(read(config))["keys"]
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
    write(config, PRISTINE.replace('# new_tab = "prefix+c"', 'new_tab = "prefix+c"'))
    before = read(config)

    assert init.run() == 0

    after = read(config)
    assert removed_lines(before, after) == ['new_tab = "prefix+c"']
    assert after.count("new_tab") == 1


def test_a_custom_binding_is_left_alone_and_said_so(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Their binding is their decision. paddock still takes prefix+c for the chooser."""
    write(config, PRISTINE.replace('# new_tab = "prefix+c"', 'new_tab = "ctrl+n"'))
    before = read(config)

    assert init.run() == 0

    after = read(config)
    assert removed_lines(before, after) == []
    assert "new_tab" not in "".join(added_lines(before, after))
    assert '"ctrl+n"' in capsys.readouterr().out


def test_a_config_with_no_keys_table_gets_one(config: Path) -> None:
    write(config, '[ui]\ntheme = "dark"\n')

    assert init.run() == 0

    parsed = tomllib.loads(read(config))
    assert parsed["keys"]["new_tab"] == "prefix+shift+c"
    assert parsed["keys"]["command"][0]["command"] == "paddock"
    assert parsed["ui"]["theme"] == "dark"


def test_a_config_with_no_keys_table_is_idempotent_too(config: Path) -> None:
    write(config, '[ui]\ntheme = "dark"\n')
    init.run()
    once = read(config)

    init.run()

    assert read(config) == once


# --- the shapes a config file comes in -------------------------------------


@pytest.mark.parametrize(
    ("shape", "text"),
    [
        ("plain", PRISTINE),
        ("no keys table", '[ui]\ntheme = "dark"\n'),
        ("empty file", ""),
        ("keys table last, no final newline", '[server]\nport = 1\n\n[keys]\nprefix = "ctrl+a"'),
        ("commented default last, no final newline", '[keys]\n# new_tab = "prefix+c"'),
        ("keys header with a comment", PRISTINE.replace("[keys]", "[keys] # the keybindings")),
        ("keys header with spaces", PRISTINE.replace("[keys]", "[ keys ]")),
        ("quoted keys header", PRISTINE.replace("[keys]", '["keys"]')),
        ("crlf", PRISTINE.replace("\n", "\r\n")),
        ("orphan begin marker", f"{init.BEGIN}\n{PRISTINE}"),
        ("orphan end marker", f"{init.END}\n{PRISTINE}"),
        ("two begin markers", f"{init.BEGIN}\n{PRISTINE}\n{init.BEGIN}\nstale = 1\n{init.END}\n"),
        (
            "hand-edited managed block",
            f'{PRISTINE}\n{init.BEGIN}\n[[keys.command]]\nkey = "prefix+c"\nwidth = "50%"\n'
            f"{init.END}\n",
        ),
    ],
)
def test_every_config_shape_is_spliced_into_valid_toml(shape: str, text: str, config: Path) -> None:
    """Whatever the file looks like: it parses afterwards, and no line of the user's is lost."""
    write(config, text)

    assert init.run() == 0

    after = read(config)
    keys = tomllib.loads(after)["keys"]
    assert keys["new_tab"] == "prefix+shift+c"
    assert keys["command"][0]["command"] == "paddock"
    assert [line for line in unmanaged(text) if line not in unmanaged(after)] == []
    once = after
    assert init.run() == 0
    assert read(config) == once  # and a second run still changes nothing


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "tail", ['[keys]\nprefix = "ctrl+a"', '[keys]\n# new_tab = "prefix+c"', "[keys]"]
)
def test_a_config_with_no_final_newline_does_not_glue_lines_together(
    config: Path, tail: str, ending: str
) -> None:
    """The inserted binding must start its own line, whatever the file ended with."""
    write(config, ('[ui]\ntheme = "dark"\n\n' + tail).replace("\n", ending))

    assert init.run() == 0

    after = read(config)
    assert tomllib.loads(after)["keys"]["new_tab"] == "prefix+shift+c"
    assert 'new_tab = "prefix+shift+c"' in [line.strip() for line in after.splitlines()]


def test_a_crlf_config_stays_crlf(config: Path) -> None:
    write(config, PRISTINE.replace("\n", "\r\n"))

    init.run()

    after = read(config)
    assert "\n" not in after.replace("\r\n", "")
    assert after.count("\r\n") == len(after.splitlines())


def test_a_dotted_keys_config_is_refused_rather_than_edited(config: Path) -> None:
    """`keys.new_tab = ...` is a shape paddock will not do surgery on."""
    write(config, 'keys.new_tab = "prefix+c"\n\n[ui]\ntheme = "dark"\n')
    before = read(config)

    with pytest.raises(RuntimeError, match=r"\[keys\] table"):
        init.run()

    assert read(config) == before
    assert backups(config) == []


def test_an_inline_table_keys_config_is_refused_rather_than_edited(config: Path) -> None:
    write(config, 'keys = { prefix = "ctrl+a" }\n')
    before = read(config)

    with pytest.raises(RuntimeError, match=r"\[keys\] table"):
        init.run()

    assert read(config) == before


def test_a_commented_out_dotted_key_is_not_a_refusal(config: Path) -> None:
    write(config, '# keys.new_tab = "prefix+c"\n[ui]\ntheme = "dark"\n')

    assert init.run() == 0
    assert tomllib.loads(read(config))["keys"]["new_tab"] == "prefix+shift+c"


def test_a_dotted_key_under_another_table_is_not_ours(config: Path) -> None:
    """`keys.new_tab` inside [plugin] belongs to that table, not to herdr's keys."""
    write(config, '[plugin]\nkeys.new_tab = "x"\n')

    assert init.run() == 0
    assert tomllib.loads(read(config))["keys"]["new_tab"] == "prefix+shift+c"


# --- new_tab lines with something after them -------------------------------


def test_a_custom_binding_with_a_comment_after_it_is_quoted_exactly(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(config, PRISTINE.replace('# new_tab = "prefix+c"', 'new_tab = "ctrl+n"  # mine'))

    assert init.run() == 0

    out = capsys.readouterr().out
    assert '"ctrl+n"' in out  # the value, not the rest of the line
    assert "mine" not in out
    assert 'new_tab = "ctrl+n"  # mine' in read(config)


def test_a_default_binding_keeps_the_comment_written_after_it(config: Path) -> None:
    write(
        config,
        PRISTINE.replace('# new_tab = "prefix+c"', 'new_tab = "prefix+c"  # herdr default'),
    )

    assert init.run() == 0

    assert 'new_tab = "prefix+shift+c"  # herdr default' in read(config)


# --- doing it twice --------------------------------------------------------


def test_the_second_run_changes_nothing(
    config: Path, herdr: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    init.run()
    once = read(config)
    capsys.readouterr()

    assert init.run() == 0
    assert read(config) == once
    assert len(backups(config)) == 1  # nothing changed, so nothing to back up
    assert herdr.count("reload") == 1  # and nothing for herdr to re-read
    assert "already" in capsys.readouterr().out


def test_an_old_block_is_updated_not_duplicated(config: Path) -> None:
    """A block from an older paddock is replaced whole, so there is only ever one."""
    stale = f'{init.BEGIN}\n[[keys.command]]\nkey = "prefix+c"\nwidth = "50%"\n{init.END}\n'
    write(config, PRISTINE + "\n" + stale)

    assert init.run() == 0

    after = read(config)
    assert after.count(init.BEGIN) == 1
    assert "50%" not in after
    assert 'height = "70%"' in after


def test_a_custom_binding_is_still_reported_on_a_later_run(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(config, PRISTINE.replace('# new_tab = "prefix+c"', 'new_tab = "ctrl+n"'))
    init.run()
    capsys.readouterr()

    init.run()

    assert "ctrl+n" in capsys.readouterr().out


# --- a config that is not there yet ----------------------------------------


def test_a_missing_config_is_written_fresh(
    empty_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """herdr ships no config.toml, so a first install has nothing to splice into."""
    assert init.run() == 0

    keys = tomllib.loads(read(empty_home))["keys"]
    assert keys["new_tab"] == "prefix+shift+c"
    assert keys["command"][0]["command"] == "paddock"
    assert backups(empty_home) == []  # there was nothing to back up
    assert "wrote a new herdr config" in capsys.readouterr().out


def test_a_dry_run_writes_no_config_at_all(
    empty_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert init.run(dry_run=True) == 0

    assert not empty_home.exists()
    assert not empty_home.parent.exists()
    assert "[[keys.command]]" in capsys.readouterr().out


def test_a_config_that_cannot_be_read_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config" / "herdr" / "config.toml").mkdir(parents=True)

    with pytest.raises(RuntimeError, match="cannot read"):
        init.run()


# --- the result has to parse -----------------------------------------------


@pytest.fixture
def broken_splice(monkeypatch: pytest.MonkeyPatch) -> None:
    """A splice that would produce nonsense: the backstop is what has to catch it."""
    monkeypatch.setattr(init, "splice", lambda text: (text + "\nnot = = toml\n", ""))


def test_a_result_that_would_not_parse_is_never_written(
    config: Path, broken_splice: None, herdr: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert init.run() == 1

    assert read(config) == PRISTINE
    assert backups(config) == []
    assert herdr == []
    assert "would not be valid TOML" in capsys.readouterr().err


def test_a_result_that_would_not_parse_is_not_offered_as_a_diff_either(
    config: Path, broken_splice: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert init.run(dry_run=True) == 1

    captured = capsys.readouterr()
    assert "would not be valid TOML" in captured.err
    assert captured.out == ""


# --- backups ---------------------------------------------------------------


def test_the_backup_holds_what_the_config_said_before(config: Path) -> None:
    init.run()

    saved = backups(config)
    assert len(saved) == 1
    assert read(saved[0]) == PRISTINE


def test_a_backup_never_lands_on_one_that_is_already_there(config: Path) -> None:
    """Two runs in the same microsecond are unlikely, and would still not lose a backup."""
    first = init._spare_path(config, init.BACKUP_SUFFIX)
    first.write_text("kept")

    assert init._spare_path(config, init.BACKUP_SUFFIX) != first
    assert first.read_text() == "kept"


def test_undo_puts_the_old_config_back(config: Path) -> None:
    init.run()
    assert read(config) != PRISTINE

    assert init.run(undo=True) == 0

    assert read(config) == PRISTINE
    assert backups(config) == []  # the backup was used up, so a second undo has nothing to do


def test_undo_keeps_what_it_is_replacing(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Edits made after `paddock init` are not lost to an undo."""
    init.run()
    edited = read(config) + '\n[plugin]\nname = "mine"\n'
    write(config, edited)
    capsys.readouterr()

    assert init.run(undo=True) == 0

    kept = undone(config)
    assert len(kept) == 1
    assert read(kept[0]) == edited
    assert kept[0].name in capsys.readouterr().out


def test_undo_takes_the_newest_backup(config: Path) -> None:
    older = config.parent / "config.toml.paddock-backup-20200101-000000-000000"
    older.write_text("# an older config\n")
    newer = config.parent / "config.toml.paddock-backup-20991231-235959-000000"
    write(newer, PRISTINE)

    assert init.run(undo=True) == 0

    assert read(config) == PRISTINE
    assert older.is_file()


def test_undo_ignores_what_it_kept_from_an_earlier_undo(config: Path) -> None:
    """An undone copy is not a backup, so undo cannot walk back into it."""
    init.run()
    init.run(undo=True)

    with pytest.raises(RuntimeError, match="no paddock backup"):
        init.run(undo=True)


def test_undo_with_nothing_to_restore_says_so(config: Path) -> None:
    with pytest.raises(RuntimeError, match="no paddock backup"):
        init.run(undo=True)


def test_undo_reloads_herdr(config: Path, herdr: list[str]) -> None:
    init.run()

    init.run(undo=True)

    assert herdr.count("reload") == 2


def test_a_dry_run_undo_names_the_backup_and_touches_nothing(
    config: Path, herdr: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    init.run()
    wired = read(config)
    saved = backups(config)
    capsys.readouterr()
    herdr.clear()

    assert init.run(dry_run=True, undo=True) == 0

    out = capsys.readouterr().out
    assert saved[0].name in out
    assert '-new_tab = "prefix+shift+c"' in out  # the diff, the other way round
    assert read(config) == wired
    assert backups(config) == saved
    assert undone(config) == []
    assert herdr == []


# --- --dry-run -------------------------------------------------------------


def test_a_dry_run_prints_the_diff_and_touches_nothing(
    config: Path, herdr: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert init.run(dry_run=True) == 0

    out = capsys.readouterr().out
    assert '+new_tab = "prefix+shift+c"' in out
    assert "+[[keys.command]]" in out
    assert str(config) in out
    assert read(config) == PRISTINE
    assert backups(config) == []
    assert herdr == []


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


def test_herdr_checks_the_config_then_reloads(config: Path, herdr: list[str]) -> None:
    init.run()

    assert herdr == ["check", "reload"]


def test_a_config_herdr_will_not_accept_is_put_back(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The backstop parses TOML; herdr is the one that knows what a valid binding is."""

    def refuse() -> None:
        raise herdr_client.HerdrError("herdr config check failed: unknown key")

    monkeypatch.setattr(herdr_client, "check_config", refuse)

    assert init.run() == 1

    assert read(config) == PRISTINE
    assert backups(config) == []
    assert "unknown key" in capsys.readouterr().err


def test_a_fresh_config_herdr_will_not_accept_is_taken_away_again(
    empty_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse() -> None:
        raise herdr_client.HerdrError("herdr config check failed")

    monkeypatch.setattr(herdr_client, "check_config", refuse)

    assert init.run() == 1
    assert not empty_home.exists()


def test_no_herdr_to_check_with_is_not_a_refusal(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> None:
        raise herdr_client.HerdrMissing("herdr not found on PATH")

    monkeypatch.setattr(herdr_client, "check_config", missing)

    assert init.run() == 0
    assert 'command = "paddock"' in read(config)


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
    assert 'command = "paddock"' in read(config)
