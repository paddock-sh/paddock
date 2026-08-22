"""`paddock init`: bind the chooser to prefix+s in herdr's config (SPEC §1.1).

The config file belongs to the user, comments and all, and a TOML round-trip loses
comments. So this edits the text instead: one managed block between markers, and the
herdr bindings paddock has to move out of its way. Everything else stays byte for byte
as it was, the markers make a second run a no-op, and the result is parsed before
anything is written.

The scheme is the one in docs/design/session-control.md §3: `prefix+c` is herdr's plain
new tab and paddock is not involved in it, `prefix+s` opens the chooser and
`prefix+shift+s` opens it on the list of live sessions. herdr's settings screen moves to
`prefix+comma`, because paddock took its key.
"""

from __future__ import annotations

import difflib
import shutil
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from paddock import herdr_client, log

logger = log.get_logger(__name__)

BEGIN = "# --- paddock (managed) ---"
END = "# --- end paddock ---"

# The first line inside the block when paddock wrote the file itself. A first install has
# no config to splice into and so no backup either, and `--undo` needs to know that taking
# paddock out means taking the whole file away (SPEC §1.1).
CREATED = "# paddock wrote this file; `paddock init --undo` removes it"

# herdr 0.8.0's own defaults, read out of `herdr --default-config`.
HERDR_NEW_TAB = "prefix+c"
HERDR_SETTINGS = "prefix+s"

# The keys paddock binds, and the one herdr action it has to move to free one of them.
CHOOSER = "prefix+s"
ATTACH = "prefix+shift+s"
SETTINGS = "prefix+comma"

# What an older paddock did: the chooser on herdr's new-tab key, and new_tab moved out of
# its way. Both are undone on the next `paddock init`, and the user is told.
OLD_CHOOSER = "prefix+c"
OLD_NEW_TAB = "prefix+shift+c"

# What init leaves next to the config: a copy of what it replaced, and (on undo) a copy
# of what it took away. Only backups are ever restored from.
BACKUP_SUFFIX = ".paddock-backup-"
UNDONE_SUFFIX = ".paddock-undone-"

# Each popup paddock binds, as (key, command, what it is for).
POPUPS = (
    (CHOOSER, "paddock", "the chooser"),
    (ATTACH, "paddock choose --attach", "attach to a session"),
)


def config_path() -> Path:
    """herdr's config. Only HOME moves it."""
    return Path.home() / ".config" / "herdr" / "config.toml"


def run(dry_run: bool = False, undo: bool = False) -> int:
    """Wire paddock into herdr, print what that would be, or put the old config back."""
    path = config_path()
    logger.info("init %s", log.context(config=path, dry_run=dry_run, undo=undo))
    if undo:
        return _restore(path, dry_run)

    existed = path.exists()
    if existed and not path.is_file():
        raise RuntimeError(f"cannot read {path}: it is not a file")
    # herdr writes no config until the user changes something, so a first install has
    # nothing to splice into and paddock writes the file itself.
    before = _read(path) if existed else ""

    after, notice = splice(before, created=not existed)
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

    # What herdr says about the config as it stands, asked before it is touched. paddock
    # must not refuse over a problem it did not cause, and a config herdr already had
    # something to say about is exactly the config people run `paddock init` on.
    already = _rejected()

    backup = None
    if existed:
        backup = _spare_path(path, BACKUP_SUFFIX)
        shutil.copy2(path, backup)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, after)
    logger.info(
        "init wrote the herdr config %s",
        log.context(config=path, backup=backup, size=f"{len(after)} bytes"),
    )

    rejected = _rejected()
    if rejected and not already:
        # herdr knows what a valid binding is, which TOML alone cannot say.
        _put_back(path, backup)
        logger.info("init put the herdr config back %s", log.context(config=path, why=rejected))
        print(f"paddock: herdr would not accept the new config ({rejected})", file=sys.stderr)
        print(f"paddock: {path} is as it was", file=sys.stderr)
        return 1
    if rejected:
        logger.info("init kept a config with warnings %s", log.context(why=already))
        print(
            f"paddock: herdr had warnings about {path} before paddock edited it, and still "
            f"has ({rejected})",
            file=sys.stderr,
        )
        print("paddock: the chooser is wired in. Those warnings are yours to fix", file=sys.stderr)

    if backup is None:
        print(f"paddock: wrote a new herdr config at {path}")
    else:
        print(f"paddock: wired into {path}, old config kept as {backup.name}")
    _reload()
    print(keys_notice(migrating(before)), end="")
    return 0


def keys_notice(migrated: bool) -> str:
    """What the keys do now. A config that had the old block has had one taken away.

    Anyone running the old scheme has been pressing prefix+shift+c for a plain tab and
    prefix+c for the chooser, and both of those stop being true here. A migration that
    said nothing would look like a launcher that had broken.
    """
    lines = ["paddock: your keybindings have changed." if migrated else "paddock: the keys:", ""]
    was = {CHOOSER: f"(was {OLD_CHOOSER})", ATTACH: "(new)"}
    plain = "a plain new tab"
    if migrated:
        lines.append(f"  {HERDR_NEW_TAB:<18}{plain:<26}(was {OLD_NEW_TAB})")
    else:
        lines.append(f"  {HERDR_NEW_TAB:<18}{plain:<26}herdr's own, untouched by paddock")
    for key, _, what in POPUPS:
        lines.append(f"  {key:<18}{what:<26}{was[key] if migrated else ''}".rstrip())
    lines += [
        "",
        f"  herdr's own settings screen moved to {SETTINGS}, because paddock took its key.",
    ]
    if migrated:
        lines.append(f"  {OLD_NEW_TAB} is no longer bound to anything.")
    return "\n".join(lines) + "\n"


def splice(text: str, created: bool = False) -> tuple[str, str]:
    """The config with paddock's block and binding in it, plus anything the user should know.

    Any old block comes out first, so running this twice gives the same text as running it
    once, and an older block is replaced rather than repeated.

    `created` marks a file paddock wrote itself. The mark lives in the block and survives
    every later run, because a second `paddock init` finds a file that exists and would
    otherwise forget who wrote it.
    """
    created = created or written_by_paddock(text)
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

    head = [CREATED] if created else []
    popups = _popups()
    span = _keys_table(lines)
    if span is None:
        # No [keys] table to edit, so the block brings its own.
        block = [*head, "[keys]", f'settings = "{SETTINGS}"', "", *popups]
        return _with_block(lines, block, ending), ""
    # herdr's settings screen is on the key the chooser wants, so it moves; new_tab goes
    # back to herdr, so the line an older paddock wrote comes out.
    lines, notice = _rebind(lines, span, "settings", HERDR_SETTINGS, SETTINGS)
    lines = _unbind(lines, _keys_table(lines) or span, "new_tab", OLD_NEW_TAB)
    return _with_block(lines, [*head, *popups], ending), notice


def _popups() -> list[str]:
    """Every popup binding, in herdr 0.8.0's custom-command syntax, one blank line apart."""
    lines: list[str] = []
    for key, command, _ in POPUPS:
        if lines:
            lines.append("")
        lines += [
            "[[keys.command]]",
            f'key = "{key}"',
            'type = "popup"',
            f'command = "{command}"',
            'width = "70%"',
            'height = "70%"',
        ]
    return lines


def migrating(text: str) -> bool:
    """Whether this config holds the block an older paddock wrote, which took prefix+c."""
    return f'key = "{OLD_CHOOSER}"' in _block(text)


def written_by_paddock(text: str) -> bool:
    """Whether this config is one paddock wrote, rather than one it edited."""
    return any(line.strip() == CREATED for line in text.splitlines())


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
        return _remove_created(path, dry_run)
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
    logger.info(
        "init restored the herdr config %s",
        log.context(config=path, backup=newest, kept=kept),
    )
    print(f"paddock: restored {path} from {newest.name}")
    if kept is not None:
        print(f"paddock: what was there is kept as {kept.name}")
    _reload()
    return 0


def _remove_created(path: Path, dry_run: bool = False) -> int:
    """Undo a config paddock wrote itself, which has no backup because it replaced nothing.

    The marker in the managed block is what says paddock wrote it. What is on disk is kept
    all the same: a file paddock created is still one the user may have added to since.
    """
    if not path.is_file() or not written_by_paddock(_read(path)):
        raise RuntimeError(f"no paddock backup of {path} to restore from")
    if dry_run:
        print(f"paddock: would remove {path}, which paddock wrote")
        return 0
    kept = _spare_path(path, UNDONE_SUFFIX)
    shutil.copy2(path, kept)
    path.unlink()
    logger.info("init removed the herdr config it wrote %s", log.context(config=path, kept=kept))
    print(f"paddock: removed {path}, which paddock wrote")
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


def _block(text: str) -> str:
    """What is between paddock's markers, or nothing when the config has no block."""
    lines = text.splitlines(keepends=True)
    start = end = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == BEGIN:
            start = index
        elif stripped == END and start is not None:
            end = index
            break
    return "" if start is None or end is None else "".join(lines[start : end + 1])


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


def _rebind(
    lines: list[str], span: tuple[int, int], action: str, default: str, key: str
) -> tuple[list[str], str]:
    """Move one herdr action off the key paddock is taking, unless the user moved it first.

    A value that is not herdr's default is the user's own answer, so it is left alone and
    reported: paddock takes the key either way, and saying so is better than a binding the
    user set quietly stopping working.
    """
    start, end = span
    below = start  # where an added line goes: under the header, or under herdr's own comment
    for index in range(start + 1, end):
        binding = _binding(lines[index], action)
        if binding is None:
            continue
        commented, value = binding
        if commented:
            below = index  # herdr's documented default stays; the live line goes under it
            continue
        if value == key:
            return lines, ""
        if value != default:
            return lines, (
                f'paddock: {action} is bound to "{value}", not herdr\'s default "{default}", '
                f"so it is left alone. paddock took {default} for the chooser."
            )
        lines[index] = _rewrite(lines[index], f'{action} = "{key}"')
        return lines, ""
    lines.insert(below + 1, _rewrite(lines[below], f'{action} = "{key}"', keep_comment=False))
    return lines, ""


def _unbind(lines: list[str], span: tuple[int, int], action: str, written: str) -> list[str]:
    """Take away a line an older paddock wrote, so herdr's own default applies again.

    Only the exact value paddock set is removed. Anything else on that key is the user's,
    and a key paddock no longer wants is not a key paddock gets to clear.
    """
    start, end = span
    for index in range(start + 1, end):
        binding = _binding(lines[index], action)
        if binding is None or binding[0]:
            continue
        return lines[:index] + lines[index + 1 :] if binding[1] == written else lines
    return lines


def _binding(line: str, action: str) -> tuple[bool, str] | None:
    """(is it commented out, what it is bound to), or None when the line binds something else."""
    text = line.strip()
    commented = text.startswith("#")
    if commented:
        text = text.lstrip("#").strip()
    key, sep, value = _code(text).partition("=")
    if not sep or key.strip() != action:
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
