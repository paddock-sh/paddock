"""`paddock init`: bind the chooser to prefix+c in herdr's config (SPEC §1.1).

The config file belongs to the user, comments and all, and a TOML round-trip loses
comments. So this edits the text instead: one managed block between markers, and one
`new_tab` line. Everything else stays byte for byte as it was, the markers make a second
run a no-op, and the result is parsed before anything is written.
"""

from __future__ import annotations

import difflib
import shutil
import sys
from datetime import datetime
from pathlib import Path

from paddock import herdr_client

try:
    import tomllib
except ModuleNotFoundError:  # python 3.10 has no tomllib; the check is skipped there
    tomllib = None

BEGIN = "# --- paddock (managed) ---"
END = "# --- end paddock ---"

# herdr 0.8.0 binds new_tab to prefix+c, which is what paddock takes for the chooser.
DEFAULT_NEW_TAB = "prefix+c"
NEW_TAB = "prefix+shift+c"

# What init leaves next to the config: a copy of what it replaced, and (on undo) a copy
# of what it took away. Only backups are ever restored from.
BACKUP_SUFFIX = ".paddock-backup-"
UNDONE_SUFFIX = ".paddock-undone-"

# The popup keybinding, in herdr 0.8.0's custom-command syntax.
POPUP = [
    "[[keys.command]]",
    f'key = "{DEFAULT_NEW_TAB}"',
    'type = "popup"',
    'command = "paddock"',
    'width = "70%"',
    'height = "70%"',
]


def config_path() -> Path:
    """herdr's config. Only HOME moves it."""
    return Path.home() / ".config" / "herdr" / "config.toml"


def run(dry_run: bool = False, undo: bool = False) -> int:
    """Wire paddock into herdr, print what that would be, or put the old config back."""
    path = config_path()
    if undo:
        return _restore(path, dry_run)

    existed = path.exists()
    if existed and not path.is_file():
        raise RuntimeError(f"cannot read {path}: it is not a file")
    # herdr writes no config until the user changes something, so a first install has
    # nothing to splice into and paddock writes the file itself.
    before = _read(path) if existed else ""

    after, notice = splice(before)
    if notice:
        print(notice)
    if after == before:
        print(f"paddock: {path} is already wired up")
        return 0
    problem = _unparsable(after)
    if problem:
        # The backstop for every shape of config this has not met: say so, change nothing.
        print(
            f"paddock did not change {path}: the result would not be valid TOML ({problem})",
            file=sys.stderr,
        )
        return 1
    if dry_run:
        print(diff(before, after, path), end="")
        return 0

    backup = None
    if existed:
        backup = _spare_path(path, BACKUP_SUFFIX)
        shutil.copy2(path, backup)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, after)

    rejected = _rejected()
    if rejected:
        # herdr knows what a valid binding is, which TOML alone cannot say.
        _put_back(path, backup)
        print(f"paddock: herdr would not accept the new config ({rejected})", file=sys.stderr)
        print(f"paddock: {path} is as it was", file=sys.stderr)
        return 1

    if backup is None:
        print(f"paddock: wrote a new herdr config at {path}")
    else:
        print(f"paddock: wired into {path}, old config kept as {backup.name}")
    _reload()
    print("paddock: press prefix+c inside herdr to open the chooser")
    return 0


def splice(text: str) -> tuple[str, str]:
    """The config with paddock's block and binding in it, plus anything the user should know.

    Any old block comes out first, so running this twice gives the same text as running it
    once, and an older block is replaced rather than repeated.
    """
    ending = _ending(text)
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        # A file with no final newline would have the next line glued onto its last one.
        lines[-1] += ending
    lines = _without_block(lines)

    loose = _keys_outside_a_table(lines)
    if loose:
        raise RuntimeError(
            f"your herdr config sets {loose} outside a [keys] table. paddock only edits a "
            "[keys] table. Move the keys settings into one by hand, then run `paddock init` again"
        )

    span = _keys_table(lines)
    if span is None:
        # No [keys] table to edit, so the block brings its own.
        return _with_block(lines, ["[keys]", f'new_tab = "{NEW_TAB}"', "", *POPUP], ending), ""
    lines, notice = _bind_new_tab(lines, span)
    return _with_block(lines, POPUP, ending), notice


def diff(before: str, after: str, path: Path) -> str:
    """The exact change, for --dry-run."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=str(path),
            tofile=f"{path} (after paddock init)",
        )
    )


def _restore(path: Path, dry_run: bool = False) -> int:
    """Put the newest backup back, keeping whatever it replaces and using the backup up."""
    saved = sorted(path.parent.glob(path.name + BACKUP_SUFFIX + "*"))
    if not saved:
        raise RuntimeError(f"no paddock backup of {path} to restore from")
    newest = saved[-1]
    before = _read(path) if path.is_file() else ""
    after = _read(newest)

    if dry_run:
        print(f"paddock: would restore {path} from {newest.name}")
        print(diff(before, after, path), end="")
        return 0

    kept = None
    if path.is_file():
        # Edits made since `paddock init` are not this command's to throw away.
        kept = _spare_path(path, UNDONE_SUFFIX)
        shutil.copy2(path, kept)
    _write(path, after)
    newest.unlink()
    print(f"paddock: restored {path} from {newest.name}")
    if kept is not None:
        print(f"paddock: what was there is kept as {kept.name}")
    _reload()
    return 0


def _put_back(path: Path, backup: Path | None) -> None:
    """Undo the write: from the backup, or by taking away a file paddock made."""
    if backup is None:
        path.unlink(missing_ok=True)
        return
    shutil.copy2(backup, path)
    backup.unlink()  # the config is what the backup held, so keeping it would only confuse


def _unparsable(text: str) -> str:
    """Why the result would not be valid TOML, or "" when it parses."""
    if tomllib is None:
        print(
            "paddock: python 3.11 or newer is needed to check the result parses. Writing it "
            "unchecked",
            file=sys.stderr,
        )
        return ""
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        return str(error)
    return ""


def _rejected() -> str:
    """What herdr says is wrong with the config now on disk, or "" when it is happy."""
    try:
        herdr_client.check_config()
    except herdr_client.HerdrMissing:
        return ""  # nothing installed to check with, which is not the config's fault
    except herdr_client.HerdrError as error:
        return str(error)
    return ""


def _reload() -> None:
    """herdr may not be running, and that is not a failure. Say what to do instead."""
    try:
        herdr_client.reload_config()
    except herdr_client.HerdrError as error:
        print(f"paddock: could not reload herdr ({error})")
        print("paddock: run `herdr server reload-config` yourself, or restart herdr")


def _read(path: Path) -> str:
    """Read the file as written: newline="" so the line endings survive a round trip."""
    try:
        with path.open(newline="") as handle:
            return handle.read()
    except OSError as error:
        raise RuntimeError(f"cannot read {path}: {error}") from error


def _write(path: Path, text: str) -> None:
    try:
        with path.open("w", newline="") as handle:
            handle.write(text)
    except OSError as error:
        raise RuntimeError(f"cannot write {path}: {error}") from error


def _spare_path(path: Path, suffix: str) -> Path:
    """A timestamped name next to the config that no file has taken."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    candidate = path.with_name(path.name + suffix + stamp)
    count = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}{suffix}{stamp}-{count}")
        count += 1
    return candidate


def _ending(text: str) -> str:
    """The line ending the file mostly uses, so paddock's own lines match it."""
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf >= text.count("\n") - crlf else "\n"


def _without_block(lines: list[str]) -> list[str]:
    """The config without paddock's block, and without the blank line paddock put before it.

    A later BEGIN wins, so a stray marker above the real block cannot swallow what is
    between them.
    """
    start = end = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == BEGIN:
            start = index
        elif stripped == END and start is not None:
            end = index
            break
    if start is None or end is None:
        return lines
    while start and not lines[start - 1].strip():
        start -= 1
    return lines[:start] + lines[end + 1 :]


def _with_block(lines: list[str], block: list[str], ending: str) -> str:
    """The config with the block at the end, one blank line clear of what came before."""
    body = "".join(lines)
    if body:
        body += ending
    return body + ending.join([BEGIN, *block, END]) + ending


def _keys_table(lines: list[str]) -> tuple[int, int] | None:
    """Where the `[keys]` table starts and ends, or None when the config has none."""
    start = None
    for index, line in enumerate(lines):
        name = _table_header(line)
        if name is None:
            continue
        if start is not None:
            return start, index
        if name == "keys":
            start = index
    return None if start is None else (start, len(lines))


def _table_header(line: str) -> str | None:
    """The table a header line opens, or None when the line opens none.

    `[keys]`, `[ keys ]`, `["keys"]` and `[keys] # a comment` all name the same table.
    """
    text = _code(line.strip())
    if not text.startswith("[") or not text.endswith("]"):
        return None
    return text[1:-1].strip().strip("\"'").strip()


def _keys_outside_a_table(lines: list[str]) -> str:
    """A top-level `keys` dotted key or inline table: a shape paddock will not edit."""
    for line in lines:
        if _table_header(line) is not None:
            return ""  # the top level ends at the first table header
        key, sep, _ = _code(line.strip()).partition("=")
        key = key.strip()
        if sep and (key == "keys" or key.startswith("keys.")):
            return key
    return ""


def _bind_new_tab(lines: list[str], span: tuple[int, int]) -> tuple[list[str], str]:
    """Move plain new-tab to prefix+shift+c, unless the user bound it somewhere themselves."""
    start, end = span
    below = start  # where an added line goes: under the header, or under herdr's own comment
    for index in range(start + 1, end):
        binding = _new_tab(lines[index])
        if binding is None:
            continue
        commented, value = binding
        if commented:
            below = index  # herdr's documented default stays; the live line goes under it
            continue
        if value == NEW_TAB:
            return lines, ""
        if value != DEFAULT_NEW_TAB:
            return lines, (
                f'paddock: new_tab is bound to "{value}", not herdr\'s default '
                f'"{DEFAULT_NEW_TAB}", so it is left alone. paddock took prefix+c for the chooser.'
            )
        lines[index] = _rewrite(lines[index], f'new_tab = "{NEW_TAB}"')
        return lines, ""
    lines.insert(below + 1, _rewrite(lines[below], f'new_tab = "{NEW_TAB}"', keep_comment=False))
    return lines, ""


def _new_tab(line: str) -> tuple[bool, str] | None:
    """(is it commented out, what it is bound to), or None when the line binds no new_tab."""
    text = line.strip()
    commented = text.startswith("#")
    if commented:
        text = text.lstrip("#").strip()
    key, sep, value = _code(text).partition("=")
    if not sep or key.strip() != "new_tab":
        return None
    return commented, value.strip().strip("\"'")


def _code(text: str) -> str:
    """The line without its trailing comment. A `#` inside a quoted value is not one."""
    quote = ""
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#":
            return text[:index].rstrip()
    return text


def _rewrite(line: str, body: str, keep_comment: bool = True) -> str:
    """`body` with the indentation, trailing comment and line ending of the line it replaces."""
    stripped = line.rstrip("\r\n")
    ending = line[len(stripped) :] or "\n"
    indent = stripped[: len(stripped) - len(stripped.lstrip())]
    comment = stripped[len(_code(stripped)) :] if keep_comment else ""
    return indent + body + comment + ending
