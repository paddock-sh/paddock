"""`paddock init`: bind the chooser to prefix+c in herdr's config (SPEC §1.1).

The config file belongs to the user, comments and all, and a TOML round-trip loses
comments. So this edits the text instead: one managed block between markers, and one
`new_tab` line. Everything else stays byte for byte as it was, and the markers make a
second run a no-op.
"""

from __future__ import annotations

import difflib
import shutil
from datetime import datetime
from pathlib import Path

from paddock import herdr_client

BEGIN = "# --- paddock (managed) ---"
END = "# --- end paddock ---"

# herdr 0.8.0 binds new_tab to prefix+c, which is what paddock takes for the chooser.
DEFAULT_NEW_TAB = "prefix+c"
NEW_TAB = "prefix+shift+c"

BACKUP_SUFFIX = ".paddock-backup-"

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
        return _restore(path)
    if not path.is_file():
        raise RuntimeError(f"no herdr config at {path} — run herdr once first, then `paddock init`")

    before = path.read_text()
    after, notice = splice(before)
    if notice:
        print(notice)
    if after == before:
        print(f"paddock: {path} is already wired up")
        return 0
    if dry_run:
        print(diff(before, after, path), end="")
        return 0

    backup = path.with_name(path.name + BACKUP_SUFFIX + datetime.now().strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(path, backup)
    path.write_text(after)
    print(f"paddock: wired into {path}, old config kept as {backup.name}")
    _reload()
    print("paddock: press prefix+c inside herdr to open the chooser")
    return 0


def splice(text: str) -> tuple[str, str]:
    """The config with paddock's block and binding in it, plus anything the user should know.

    Any old block comes out first, so running this twice gives the same text as running it
    once, and an older block is replaced rather than repeated.
    """
    lines = _without_block(text.splitlines(keepends=True))
    span = _keys_table(lines)
    if span is None:
        # No [keys] table to edit, so the block brings its own.
        return _with_block(lines, ["[keys]", f'new_tab = "{NEW_TAB}"', "", *POPUP]), ""
    lines, notice = _bind_new_tab(lines, span)
    return _with_block(lines, POPUP), notice


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


def _restore(path: Path) -> int:
    """Put the newest backup back, and use it up so a second undo steps further back."""
    saved = sorted(path.parent.glob(path.name + BACKUP_SUFFIX + "*"))
    if not saved:
        raise RuntimeError(f"no paddock backup of {path} to restore from")
    newest = saved[-1]
    shutil.copy2(newest, path)
    newest.unlink()
    print(f"paddock: restored {path} from {newest.name}")
    _reload()
    return 0


def _reload() -> None:
    """herdr may not be running, and that is not a failure — say what to do instead."""
    try:
        herdr_client.reload_config()
    except herdr_client.HerdrError as error:
        print(f"paddock: could not reload herdr ({error})")
        print("paddock: run `herdr server reload-config` yourself, or restart herdr")


def _without_block(lines: list[str]) -> list[str]:
    """The config without paddock's block, and without the blank line paddock put before it."""
    start = end = None
    for index, line in enumerate(lines):
        if start is None and line.strip() == BEGIN:
            start = index
        elif start is not None and line.strip() == END:
            end = index
            break
    if start is None or end is None:
        return lines
    while start and not lines[start - 1].strip():
        start -= 1
    return lines[:start] + lines[end + 1 :]


def _with_block(lines: list[str], block: list[str]) -> str:
    """The config with the block at the end, one blank line clear of what came before."""
    body = "".join(lines)
    if body and not body.endswith("\n"):
        body += "\n"
    return body + "\n" + "\n".join([BEGIN, *block, END]) + "\n"


def _keys_table(lines: list[str]) -> tuple[int, int] | None:
    """Where the `[keys]` table starts and ends, or None when the config has none."""
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if start is None:
            if stripped == "[keys]":
                start = index
        elif stripped.startswith("["):
            return start, index
    return None if start is None else (start, len(lines))


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
                f'"{DEFAULT_NEW_TAB}" — left alone. paddock took prefix+c for the chooser.'
            )
        lines[index] = _rewrite(lines[index], f'new_tab = "{NEW_TAB}"')
        return lines, ""
    lines.insert(below + 1, _rewrite(lines[below], f'new_tab = "{NEW_TAB}"'))
    return lines, ""


def _new_tab(line: str) -> tuple[bool, str] | None:
    """(is it commented out, what it is bound to) for a new_tab line, else None."""
    text = line.strip()
    commented = text.startswith("#")
    key, sep, value = text.lstrip("#").strip().partition("=")
    if not sep or key.strip() != "new_tab":
        return None
    return commented, value.strip().strip("\"'")


def _rewrite(line: str, body: str) -> str:
    """`body` with the indentation and line ending of the line it stands next to."""
    stripped = line.rstrip("\r\n")
    ending = line[len(stripped) :] or "\n"
    indent = stripped[: len(stripped) - len(stripped.lstrip())]
    return indent + body + ending
