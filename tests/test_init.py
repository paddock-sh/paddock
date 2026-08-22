"""`paddock init`: wire the chooser into herdr's config without losing anything else."""

import difflib
import tomllib
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
# settings = "prefix+s"
# close_pane = "prefix+x"

[ui]
theme = "dark"
"""

# The block paddock writes: the chooser, and the chooser on the session list.
BLOCK_LINES = [
    "[[keys.command]]",
    'key = "prefix+s"',
    'type = "popup"',
    'command = "paddock"',
    'width = "70%"',
    'height = "70%"',
    "",
    "[[keys.command]]",
    'key = "prefix+shift+s"',
    'type = "popup"',
    'command = "paddock choose --attach"',
    'width = "70%"',
    'height = "70%"',
]

# What an older paddock wrote, which `paddock init` migrates away from.
OLD_BLOCK = (
    f'{init.BEGIN}\n[[keys.command]]\nkey = "prefix+c"\ntype = "popup"\n'
    f'command = "paddock"\nwidth = "70%"\nheight = "70%"\n{init.END}\n'
)


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


def test_the_block_and_the_settings_line_are_the_only_changes(config: Path) -> None:
    """Comments and unrelated tables survive byte for byte."""
    assert init.run() == 0

    after = read(config)
    assert removed_lines(PRISTINE, after) == []
    assert added_lines(PRISTINE, after) == [
        'settings = "prefix+comma"',
        "",
        init.BEGIN,
        *BLOCK_LINES,
        init.END,
    ]


def test_the_commented_default_is_left_where_it_was(config: Path) -> None:
    """It says what herdr's default is. The active line below it is what changes."""
    init.run()

    assert '# settings = "prefix+s"\nsettings = "prefix+comma"\n' in read(config)


def test_the_new_tab_key_is_left_to_herdr(config: Path) -> None:
    """The point of the scheme: a plain tab is one key and no paddock at all."""
    init.run()

    assert "new_tab" not in tomllib.loads(read(config))["keys"]
    assert 'new_tab = "prefix+shift+c"' not in read(config)


def test_the_written_config_is_the_toml_herdr_expects(config: Path) -> None:
    init.run()

    keys = tomllib.loads(read(config))["keys"]
    assert keys["settings"] == "prefix+comma"
    assert keys["prefix"] == "ctrl+a"
    assert keys["command"] == [
        {
            "key": "prefix+s",
            "type": "popup",
            "command": "paddock",
            "width": "70%",
            "height": "70%",
        },
        {
            "key": "prefix+shift+s",
            "type": "popup",
            "command": "paddock choose --attach",
            "width": "70%",
            "height": "70%",
        },
    ]


def test_an_active_default_binding_is_moved_in_place(config: Path) -> None:
    write(config, PRISTINE.replace('# settings = "prefix+s"', 'settings = "prefix+s"'))
    before = read(config)

    assert init.run() == 0

    after = read(config)
    assert removed_lines(before, after) == ['settings = "prefix+s"']
    assert added_lines(before, after)[0] == 'settings = "prefix+comma"'
    assert after.count('settings = "prefix+comma"') == 1


def test_a_custom_binding_is_left_alone_and_said_so(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Their binding is their decision. paddock still takes prefix+s for the chooser."""
    write(config, PRISTINE.replace('# settings = "prefix+s"', 'settings = "ctrl+n"'))
    before = read(config)

    assert init.run() == 0

    after = read(config)
    assert removed_lines(before, after) == []
    assert "settings = " not in "".join(added_lines(before, after))
    assert '"ctrl+n"' in capsys.readouterr().out


def test_a_config_with_no_keys_table_gets_one(config: Path) -> None:
    write(config, '[ui]\ntheme = "dark"\n')

    assert init.run() == 0

    parsed = tomllib.loads(read(config))
    assert parsed["keys"]["settings"] == "prefix+comma"
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
        ("commented default last, no final newline", '[keys]\n# settings = "prefix+s"'),
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
    assert keys["settings"] == "prefix+comma"
    assert keys["command"][0]["command"] == "paddock"
    assert [line for line in unmanaged(text) if line not in unmanaged(after)] == []
    once = after
    assert init.run() == 0
    assert read(config) == once  # and a second run still changes nothing


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "tail", ['[keys]\nprefix = "ctrl+a"', '[keys]\n# settings = "prefix+s"', "[keys]"]
)
def test_a_config_with_no_final_newline_does_not_glue_lines_together(
    config: Path, tail: str, ending: str
) -> None:
    """The inserted binding must start its own line, whatever the file ended with."""
    write(config, ('[ui]\ntheme = "dark"\n\n' + tail).replace("\n", ending))

    assert init.run() == 0

    after = read(config)
    assert tomllib.loads(after)["keys"]["settings"] == "prefix+comma"
    assert 'settings = "prefix+comma"' in [line.strip() for line in after.splitlines()]


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
    assert tomllib.loads(read(config))["keys"]["settings"] == "prefix+comma"


def test_a_dotted_key_under_another_table_is_not_ours(config: Path) -> None:
    """`keys.new_tab` inside [plugin] belongs to that table, not to herdr's keys."""
    write(config, '[plugin]\nkeys.new_tab = "x"\n')

    assert init.run() == 0
    assert tomllib.loads(read(config))["keys"]["settings"] == "prefix+comma"


# --- binding lines with something after them -------------------------------


def test_a_custom_binding_with_a_comment_after_it_is_quoted_exactly(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(config, PRISTINE.replace('# settings = "prefix+s"', 'settings = "ctrl+n"  # mine'))

    assert init.run() == 0

    out = capsys.readouterr().out
    assert '"ctrl+n"' in out  # the value, not the rest of the line
    assert "mine" not in out
    assert 'settings = "ctrl+n"  # mine' in read(config)


def test_a_default_binding_keeps_the_comment_written_after_it(config: Path) -> None:
    write(
        config,
        PRISTINE.replace('# settings = "prefix+s"', 'settings = "prefix+s"  # herdr default'),
    )

    assert init.run() == 0

    assert 'settings = "prefix+comma"  # herdr default' in read(config)


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
    write(config, PRISTINE.replace('# settings = "prefix+s"', 'settings = "ctrl+n"'))
    init.run()
    capsys.readouterr()

    init.run()

    assert "ctrl+n" in capsys.readouterr().out


# --- migrating an install that has the old scheme ---------------------------


def old_scheme(text: str = PRISTINE) -> str:
    """A config an older paddock wired up: the chooser on prefix+c, new_tab moved aside."""
    moved = text.replace(
        '# new_tab = "prefix+c"', '# new_tab = "prefix+c"\nnew_tab = "prefix+shift+c"'
    )
    return f"{moved}\n{OLD_BLOCK}"


def test_the_old_block_is_replaced_by_the_new_scheme(config: Path) -> None:
    write(config, old_scheme())

    assert init.run() == 0

    keys = tomllib.loads(read(config))["keys"]
    assert [command["key"] for command in keys["command"]] == ["prefix+s", "prefix+shift+s"]
    assert keys["settings"] == "prefix+comma"


def test_migrating_gives_the_new_tab_key_back_to_herdr(config: Path) -> None:
    """Anyone on the old scheme has been pressing prefix+shift+c, and that line goes."""
    write(config, old_scheme())

    init.run()

    after = read(config)
    assert "new_tab" not in tomllib.loads(after)["keys"]
    assert '# new_tab = "prefix+c"' in after  # herdr's own commented default is not paddock's


def test_migrating_says_every_binding_that_changed(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(config, old_scheme())

    init.run()

    said = capsys.readouterr().out
    assert "your keybindings have changed" in said
    assert "prefix+shift+c is no longer bound to anything." in said
    assert "prefix+s" in said and "prefix+shift+s" in said
    assert "prefix+comma" in said


def test_a_fresh_install_does_not_claim_anything_changed(
    empty_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init.run()

    said = capsys.readouterr().out
    assert "your keybindings have changed" not in said
    assert "prefix+s" in said


def test_a_dry_run_shows_the_migration_and_touches_nothing(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(config, old_scheme())
    before = read(config)

    assert init.run(dry_run=True) == 0

    out = capsys.readouterr().out
    assert '-new_tab = "prefix+shift+c"' in out
    assert '+key = "prefix+s"' in out
    assert read(config) == before


def test_a_new_tab_the_user_bound_survives_the_migration(config: Path) -> None:
    """Only the value paddock wrote is taken away. Anything else is the user's answer."""
    write(config, old_scheme().replace('new_tab = "prefix+shift+c"', 'new_tab = "ctrl+n"'))

    assert init.run() == 0

    assert tomllib.loads(read(config))["keys"]["new_tab"] == "ctrl+n"


def test_a_config_on_the_new_scheme_is_not_migrating(config: Path) -> None:
    init.run()

    assert not init.migrating(read(config))


# --- a config that is not there yet ----------------------------------------


def test_a_missing_config_is_written_fresh(
    empty_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """herdr ships no config.toml, so a first install has nothing to splice into."""
    assert init.run() == 0

    keys = tomllib.loads(read(empty_home))["keys"]
    assert keys["settings"] == "prefix+comma"
    assert "new_tab" not in keys
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
    monkeypatch.setattr(
        init, "splice", lambda text, created=False: (text + "\nnot = = toml\n", "")
    )


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


def test_undo_removes_a_config_paddock_wrote_itself(
    empty_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fresh herdr install has no config, so init writes one and there is no backup.

    Undo used to say "no paddock backup" and leave the file paddock made behind.
    """
    init.run()
    capsys.readouterr()

    assert init.run(undo=True) == 0

    assert not empty_home.exists()
    assert "removed" in capsys.readouterr().out


def test_undo_keeps_a_copy_of_the_config_it_removes(empty_home: Path) -> None:
    """Whatever the user added to it after paddock wrote it is not undo's to throw away."""
    init.run()
    wired = read(empty_home)

    init.run(undo=True)

    kept = undone(empty_home)
    assert len(kept) == 1
    assert read(kept[0]) == wired


def test_undo_reloads_herdr_after_removing_the_config_it_wrote(
    empty_home: Path, herdr: list[str]
) -> None:
    init.run()

    init.run(undo=True)

    assert herdr.count("reload") == 2


def test_a_second_undo_of_a_removed_config_has_nothing_to_do(empty_home: Path) -> None:
    init.run()
    init.run(undo=True)

    with pytest.raises(RuntimeError, match="no paddock backup"):
        init.run(undo=True)


def test_a_dry_run_undo_of_a_written_config_removes_nothing(
    empty_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    init.run()
    capsys.readouterr()

    assert init.run(dry_run=True, undo=True) == 0

    assert empty_home.exists()
    assert "would remove" in capsys.readouterr().out


def test_a_config_paddock_wrote_says_so_inside_its_block(empty_home: Path) -> None:
    """The mark is what a later undo reads, so it lives in the config and not in a state file."""
    init.run()

    assert init.CREATED in read(empty_home)
    assert tomllib.loads(read(empty_home))["keys"]["settings"] == "prefix+comma"


def test_a_second_init_keeps_the_mark_on_a_config_paddock_wrote(empty_home: Path) -> None:
    """The file exists by then, so only the mark it carries says who wrote it."""
    init.run()

    init.run()

    assert read(empty_home).count(init.CREATED) == 1


def test_undo_after_a_second_init_puts_back_the_config_paddock_wrote(
    empty_home: Path,
) -> None:
    """The second run backs up the first one's file, so undo restores it before removing it."""
    init.run()
    write(empty_home, read(empty_home) + '\n[ui]\ntheme = "dark"\n')
    init.run()

    init.run(undo=True)

    assert empty_home.exists()
    assert init.CREATED in read(empty_home)

    init.run(undo=True)

    assert not empty_home.exists()


def test_undo_leaves_a_config_paddock_only_edited_alone(config: Path) -> None:
    """No mark means the file was the user's before paddock touched it, backup or no backup."""
    init.run()
    for backup in backups(config):
        backup.unlink()

    with pytest.raises(RuntimeError, match="no paddock backup"):
        init.run(undo=True)

    assert config.exists()


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
    assert '-settings = "prefix+comma"' in out  # the diff, the other way round
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
    assert '+settings = "prefix+comma"' in out
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


def test_herdr_checks_the_config_before_and_after_the_edit_then_reloads(
    config: Path, herdr: list[str]
) -> None:
    """The first check is what tells a problem paddock caused from one it only found."""
    init.run()

    assert herdr == ["check", "check", "reload"]


def refuse_after_the_edit(monkeypatch: pytest.MonkeyPatch, why: str) -> None:
    """herdr is happy with the config as it stands, and refuses what paddock made of it."""
    checks: list[int] = []

    def check() -> str:
        checks.append(1)
        if len(checks) > 1:
            raise herdr_client.HerdrError(why)
        return "config: ok"

    monkeypatch.setattr(herdr_client, "check_config", check)


def test_a_config_herdr_will_not_accept_is_put_back(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The backstop parses TOML; herdr is the one that knows what a valid binding is."""
    refuse_after_the_edit(monkeypatch, "herdr config check failed: unknown key")

    assert init.run() == 1

    assert read(config) == PRISTINE
    assert backups(config) == []
    assert "unknown key" in capsys.readouterr().err


def test_a_fresh_config_herdr_will_not_accept_is_taken_away_again(
    empty_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refuse_after_the_edit(monkeypatch, "herdr config check failed")

    assert init.run() == 1
    assert not empty_home.exists()


def test_a_config_herdr_already_had_warnings_about_is_still_wired_up(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """paddock must not refuse over a problem it did not cause: it is not the config's owner."""

    def refuse() -> None:
        raise herdr_client.HerdrError("herdr config check failed: unknown config key ui.wat")

    monkeypatch.setattr(herdr_client, "check_config", refuse)

    assert init.run() == 0

    assert '[[keys.command]]' in read(config)
    assert len(backups(config)) == 1
    assert "before paddock edited it" in capsys.readouterr().err


def test_a_pre_existing_warning_is_named_so_nobody_hunts_for_it(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse() -> None:
        raise herdr_client.HerdrError("herdr config check failed: unknown config key ui.wat")

    monkeypatch.setattr(herdr_client, "check_config", refuse)

    init.run()

    assert "unknown config key ui.wat" in capsys.readouterr().err


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
