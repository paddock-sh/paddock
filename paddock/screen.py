"""The screens the chooser draws, and the keys that work on them.

Four screens: the form of fields, a list to pick from, a checklist to tick, and a box to type
in. Each is one prompt_toolkit Application over one body, with the same key map: enter takes
what the cursor is on, escape backs out one level and keeps what was done, ctrl-c cancels the
popup at any depth by raising KeyboardInterrupt, and `?` puts the key list over the screen.
Filtering is a mode `/` opens, never something bare typing starts, which is what keeps every
letter free as a shortcut.

Nothing here knows about profiles, sessions or plans. A screen takes rows of text and hands
back what the user picked, so the rest of the chooser stays plain functions over an answers
dict. Every line a screen draws is built by a plain function here, so the layout is tested
without a terminal and the Applications stay thin.

Widths are counted in terminal columns, not characters, because a wide character takes two of
them and a line that counts wrong wraps and pushes the layout around.
"""

from __future__ import annotations

from collections.abc import Callable

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

# What the form gives back beside the field it was asked to open.
OPEN, LAUNCH, SAVE = "open", "launch", "save"

# The budget from the design: 80 by 24. The popup is usually larger, so nothing may need more,
# and a smaller terminal scrolls the rows rather than losing them.
WIDTH, HEIGHT = 80, 24

# A list longer than this says so in its key line (section 4.3).
FILTER_FROM = 5

# How long a lone escape waits to be sure it is not the start of an arrow key. Escape has to
# feel instant, and a terminal that splits an arrow key over 100ms is broken anyway.
ESCAPE_WAIT = 0.1

# The key lines. `esc` is on every one of them, which is the cheapest usability win there is.
FORM_KEYS = ("enter edit", "^v move", "L launch", "s save", "esc close", "? keys")
PICK_KEYS = ("enter choose", "^v move", "esc back", "? keys")
TICK_KEYS = (
    "space toggle",
    "a all",
    "n none",
    "/ filter",
    "enter done",
    "esc back (keeps ticks)",
)
TYPE_KEYS = ("enter done", "esc back (keeps what you typed)")
FILTER_KEYS = ("type to narrow", "enter keep", "esc clear")
FILTER_KEY = "/ filter"

# What `?` puts over whatever is on screen.
KEY_LIST = (
    ("up down, k j", "move between fields, or between items"),
    ("enter", "open the field, take the item, press the button"),
    ("esc", "back out one level, keeping every answer"),
    ("ctrl-c", "cancel the popup, at any depth"),
    ("a digit", "jump straight to that field, on the form"),
    ("space", "tick, in a checklist"),
    ("/", "filter a long list"),
    ("a n", "tick all, tick none, in a checklist"),
    ("L", "launch"),
    ("s", "save these answers as a profile"),
    ("?", "this list"),
)


# --- measuring, cutting and fitting -----------------------------------------


def cut(text: str, width: int) -> str:
    """Text no wider than `width` columns, saying so with an ellipsis when it had to give."""
    if get_cwidth(text) <= width:
        return text
    kept = ""
    for char in text:
        if get_cwidth(kept + char) > max(width - 3, 0):
            break
        kept += char
    return f"{kept}..."[:max(width, 0)] if width >= 3 else ""


def pad(text: str, width: int) -> str:
    """Text padded out to `width` columns, cut first if it is already wider."""
    text = cut(text, width)
    return text + " " * max(width - get_cwidth(text), 0)


def wrapped(text: str, lines: int, width: int = WIDTH) -> list[str]:
    """A hint as a fixed number of lines, so a longer one never moves what is under it."""
    shown = [f"  {line}" for line in _wrap(text, width - 2)[:lines]] if text else []
    return shown + [""] * (lines - len(shown))


def window(lines: list[str], room: int, cursor: int) -> list[str]:
    """The part of a block that fits, kept around the cursor so it never leaves the screen."""
    if room <= 0 or len(lines) <= room:
        return lines
    start = min(max(cursor - room // 2, 0), len(lines) - room)
    return lines[start : start + room]


def _wrap(text: str, width: int) -> list[str]:
    """Wrapping by column, because textwrap counts characters and a wide one is two columns."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        joined = f"{line} {word}".strip()
        if line and get_cwidth(joined) > width:
            lines.append(line)
            line = word if get_cwidth(word) <= width else cut(word, width)
        else:
            line = joined
    return lines + [line] if line else lines


# --- the lines a screen draws ----------------------------------------------


def footer_line(parts: tuple[str, ...] | list[str], width: int = WIDTH) -> str:
    """The key line. It truncates rather than wraps, so the help can never move the layout."""
    return cut("   ".join(parts), width)


def form_footer(fields: int) -> str:
    """The form's keys, including how many digits jump, which is however many fields there are."""
    return footer_line((FORM_KEYS[0], FORM_KEYS[1], f"1-{fields} jump", *FORM_KEYS[2:]))


def pick_footer(size: int) -> str:
    """A list says it can be filtered only when it is long enough for anyone to want that."""
    parts = list(PICK_KEYS)
    if size > FILTER_FROM:
        parts.insert(0, FILTER_KEY)
    return footer_line(parts)


def type_footer(error: str) -> str:
    """One row of chrome, two jobs, never both at once: the error takes the key line's place."""
    return error or footer_line(TYPE_KEYS)


def forget_error(box: TextArea, state: dict) -> None:
    """Take the error back as soon as the answer changes, so the keys have their row again."""
    box.buffer.on_text_changed += lambda _: state.update(error="")


def spread(left: str, right: str, width: int = WIDTH) -> str:
    """One line with something at each end. The left end gives way first, the right one last."""
    right = cut(right, width)
    left = cut(left, max(width - get_cwidth(right) - 2, 0))
    gap = max(width - get_cwidth(left) - get_cwidth(right), 0)
    return (left + " " * gap + right).rstrip()


def columns(cells: list[str], width: int = WIDTH) -> list[str]:
    """Two columns in reading order, because eighteen short names do not fit in one."""
    half = (len(cells) + 1) // 2
    left, right = cells[:half], cells[half:]
    room = width // 2 - 4
    lines = []
    for index in range(half):
        line = f"  {pad(left[index], room)}"
        if index < len(right):
            line += f"  {cut(right[index], room)}"
        lines.append(line.rstrip())
    return lines


def key_lines() -> list[str]:
    """The whole key map, for `?`."""
    return ["The keys", ""] + [f"  {pad(key, 14)} {what}" for key, what in KEY_LIST]


def form_lines(
    title: str,
    where: str,
    rows: list[tuple],
    cursor: int,
    height: int = HEIGHT,
) -> list[str]:
    """The home screen: every field, the hint for the one the cursor is on, and the buttons.

    A row is a label, its value, its hint, and an optional note kept at the right edge, which
    is where a count of what a value holds belongs.
    """
    fields = []
    for index, row in enumerate(rows):
        mark = ">" if index == cursor else " "
        left = f"  {mark} {index + 1} {pad(str(row[0]), 10)} {row[1]}"
        note = str(row[3]) if len(row) > 3 else ""
        fields.append(spread(left, note) if note else cut(left, WIDTH))
    hint = str(rows[cursor][2]) if cursor < len(rows) else ""
    shown = window(fields, height - 8, min(cursor, max(len(fields) - 1, 0)))
    top = [spread(f"paddock   {title}", where), ""]
    return top + shown + ["", *wrapped(hint, 3), "", _buttons(cursor - len(rows))]


def list_lines(
    title: str,
    note: str,
    choices: list[tuple[str, object]],
    shown: list[int],
    cursor: int,
    filter_text: str = "",
    filtering: bool = False,
    rule_after: int = -1,
    hint_rows: int = 3,
    height: int = HEIGHT,
) -> list[str]:
    """A list, each row with what it means beside it, and the whole hint under the cursor.

    A hint is one line of text, or the lines of a panel when a choice needs more room than
    that to say what it is.
    """
    rows, place_of_cursor = [], 0
    for place, index in enumerate(shown):
        label, hint = choices[index]
        mark = ">" if place == cursor else " "
        if place == cursor:
            place_of_cursor = len(rows)
        rows.append(cut(f"  {mark} {pad(label, 18)} {_beside(hint)}", WIDTH))
        if index == rule_after:
            rows.append("    " + "-" * (WIDTH - 6))
    echo = _filter_lines(filter_text, filtering)
    room = height - (3 + hint_rows + len(echo))
    hint = choices[shown[cursor]][1] if shown else _nothing(filter_text)
    top = [spread(title, note), ""]
    return top + window(rows, room, place_of_cursor) + ["", *_panel(hint, hint_rows)] + echo


def tick_lines(
    title: str,
    hint: str,
    rows: list[tuple[str, bool]],
    shown: list[int],
    cursor: int,
    filter_text: str = "",
    filtering: bool = False,
    height: int = HEIGHT,
) -> list[str]:
    """A checklist in two columns, with a count of what is ticked in the header."""
    ticked = sum(1 for _, on in rows if on)
    cells = []
    for place, index in enumerate(shown):
        label, on = rows[index]
        cells.append(f"{'>' if place == cursor else ' '} [{'x' if on else ' '}] {label}")
    lines = columns(cells)
    half = (len(cells) + 1) // 2
    echo = _filter_lines(filter_text, filtering)
    top = [spread(title, f"{ticked} of {len(rows)} ticked"), "", *wrapped(hint, 3), ""]
    room = height - (len(top) + len(echo))
    return top + window(lines, room, cursor if cursor < half else cursor - half) + echo


def type_lines(title: str, hint: str) -> list[str]:
    """What stands above the box."""
    return [title, "", *wrapped(hint, 3), ""]


def matching(labels: list[str], text: str) -> list[int]:
    """The rows a filter leaves, as indexes into the whole list."""
    wanted = text.lower()
    return [index for index, label in enumerate(labels) if wanted in label.lower()]


def _buttons(chosen: int) -> str:
    """Launch and Cancel, marked when the cursor has walked off the end of the fields."""
    launch = ">" if chosen == 0 else " "
    cancel = ">" if chosen == 1 else " "
    return f"  {launch} [ Launch ]      {cancel} [ Cancel ]"


def _beside(hint: object) -> str:
    """What a row says about itself: its hint, or the first line of its panel."""
    if isinstance(hint, str):
        return hint
    lines = list(hint) if hint else []
    return str(lines[0]) if lines else ""


def _panel(hint: object, rows: int) -> list[str]:
    """The hint under the list, whether it is a sentence or a panel of its own lines."""
    if isinstance(hint, str):
        return wrapped(hint, rows)
    lines = [f"  {cut(str(line), WIDTH - 2)}" for line in list(hint)[:rows]]
    return lines + [""] * (rows - len(lines))


def _nothing(filter_text: str) -> str:
    return "nothing matches that" if filter_text else "nothing to choose from"


def _filter_lines(filter_text: str, filtering: bool) -> list[str]:
    return ["", f"  /{filter_text}"] if filtering or filter_text else []


# --- the screens ------------------------------------------------------------


def form(
    title: str, where: str, rows: list[tuple], cursor: int = 0
) -> tuple[str, int] | None:
    """The home screen. What to do and where the cursor was, or None to close the popup.

    Every field is one arrow key away, so there is no walk to go back through and no summary
    to edit from: coming back from an editor puts the cursor where it left.
    """
    state: dict = {"cursor": min(max(cursor, 0), len(rows) + 1), "keys": False}
    keys = KeyBindings()
    last = len(rows) + 1  # the two buttons live after the fields

    @keys.add("up")
    @keys.add("k")
    def _(event: object) -> None:
        state["cursor"] = max(state["cursor"] - 1, 0)

    @keys.add("down")
    @keys.add("j")
    def _(event: object) -> None:
        state["cursor"] = min(state["cursor"] + 1, last)

    for digit in range(1, min(len(rows), 9) + 1):

        @keys.add(str(digit))
        def _(event: object, place: int = digit - 1) -> None:
            state["cursor"] = place

    @keys.add("enter")
    def _(event) -> None:
        if state["cursor"] < len(rows):
            event.app.exit(result=(OPEN, state["cursor"]))
        elif state["cursor"] == len(rows):
            event.app.exit(result=(LAUNCH, state["cursor"]))
        else:
            event.app.exit(result=None)

    @keys.add("L")
    def _(event) -> None:
        event.app.exit(result=(LAUNCH, state["cursor"]))

    @keys.add("s")
    def _(event) -> None:
        event.app.exit(result=(SAVE, state["cursor"]))

    @keys.add("escape")
    def _(event) -> None:
        event.app.exit(result=None)

    _finish(keys, state)

    def body(height: int) -> list[str]:
        return form_lines(title, where, rows, state["cursor"], height)

    return _run(body, lambda: form_footer(len(rows)), keys, state)


def pick(
    title: str,
    choices: list[tuple[str, object]],
    note: str = "",
    cursor: int = 0,
    rule_after: int = -1,
    hint_rows: int = 3,
) -> int | None:
    """A list. The index chosen, or None when the user backed out without choosing.

    Backing out is not cancelling. The caller keeps whatever value it had.
    """
    state: dict = {
        "cursor": min(max(cursor, 0), max(len(choices) - 1, 0)),
        "filter": "",
        "filtering": False,
        "keys": False,
    }
    labels = [label for label, _ in choices]

    def shown() -> list[int]:
        return matching(labels, state["filter"])

    keys = _list_keys(state, shown)

    @keys.add("enter", filter=~_typing(state))
    def _(event) -> None:
        rows = shown()
        if rows:
            event.app.exit(result=rows[state["cursor"]])

    @keys.add("escape", filter=~_typing(state))
    def _(event) -> None:
        event.app.exit(result=None)

    _finish(keys, state)

    def body(height: int) -> list[str]:
        return list_lines(
            title, note, choices, shown(), state["cursor"], state["filter"],
            state["filtering"], rule_after, hint_rows, height,
        )

    return _run(body, lambda: _keys_or_filter(state, pick_footer(len(choices))), keys, state)


def tick(title: str, rows: list[tuple[str, bool]], hint: str = "") -> list[int]:
    """A checklist. What is ticked, whether it was left with enter or with escape.

    Escape keeps the ticks, because escape never loses an answer: the ticks are the answer.
    """
    state: dict = {
        "cursor": 0,
        "filter": "",
        "filtering": False,
        "keys": False,
        "ticked": {index for index, (_, on) in enumerate(rows) if on},
    }
    labels = [label for label, _ in rows]

    def shown() -> list[int]:
        return matching(labels, state["filter"])

    def marked() -> list[tuple[str, bool]]:
        return [(label, index in state["ticked"]) for index, label in enumerate(labels)]

    keys = _list_keys(state, shown)
    typing = _typing(state)

    @keys.add("space", filter=~typing)
    def _(event: object) -> None:
        places = shown()
        if places:
            state["ticked"] ^= {places[state["cursor"]]}

    @keys.add("a", filter=~typing)
    def _(event: object) -> None:
        state["ticked"] |= set(shown())  # all of what is on screen, so a filter narrows it

    @keys.add("n", filter=~typing)
    def _(event: object) -> None:
        state["ticked"] -= set(shown())

    @keys.add("enter", filter=~typing)
    @keys.add("escape", filter=~typing)
    def _(event) -> None:
        event.app.exit(result=sorted(state["ticked"]))

    _finish(keys, state)

    def body(height: int) -> list[str]:
        return tick_lines(
            title, hint, marked(), shown(), state["cursor"], state["filter"],
            state["filtering"], height,
        )

    return _run(body, lambda: _keys_or_filter(state, footer_line(TICK_KEYS)), keys, state)


def type_in(
    title: str, value: str = "", hint: str = "", check: Callable[[str], str] | None = None
) -> str:
    """A box. What it holds, whether it was left with enter or with escape.

    `check` is what makes an answer wrong. Its message replaces the key line and the box stays
    open, and it goes as soon as the answer changes. Escape gives back what was typed without
    running the check, so the caller validates whatever it gets from here.
    """
    state: dict = {"error": "", "keys": False}
    box = TextArea(text=value, multiline=False)
    forget_error(box, state)
    keys = KeyBindings()

    @keys.add("enter")
    def _(event) -> None:
        typed = box.text.strip()
        state["error"] = check(typed) if check else ""
        if not state["error"]:
            event.app.exit(result=typed)

    @keys.add("escape")
    def _(event) -> None:
        event.app.exit(result=box.text.strip())

    _finish(keys, state, help_key=False)
    above = type_lines(title, hint)
    layout = Layout(
        HSplit(
            [
                Window(FormattedTextControl(lambda: "\n".join(above)), height=len(above)),
                box,
                Window(),  # the gap that puts the key line at the bottom, as on every screen
                Window(
                    FormattedTextControl(lambda: type_footer(state["error"])),
                    height=1,
                    style="reverse",
                ),
            ]
        ),
        focused_element=box,
    )
    return _wait(Application(layout=layout, key_bindings=keys, full_screen=True))


# --- the keys every screen shares -------------------------------------------


def _typing(state: dict) -> Condition:
    """True while `/` has the keyboard, which is the only time a letter is not a shortcut."""
    return Condition(lambda: bool(state.get("filtering")))


def _list_keys(state: dict, shown: Callable[[], list[int]]) -> KeyBindings:
    """Moving and filtering, which a list and a checklist do the same way."""
    keys = KeyBindings()
    typing = _typing(state)

    @keys.add("up", filter=~typing)
    @keys.add("k", filter=~typing)
    def _(event: object) -> None:
        state["cursor"] = max(state["cursor"] - 1, 0)

    @keys.add("down", filter=~typing)
    @keys.add("j", filter=~typing)
    def _(event: object) -> None:
        state["cursor"] = min(state["cursor"] + 1, max(len(shown()) - 1, 0))

    @keys.add("/", filter=~typing)
    def _(event: object) -> None:
        state["filtering"] = True

    @keys.add("<any>", filter=typing)
    def _(event) -> None:
        if event.data and event.data.isprintable():
            state["filter"] += event.data
            state["cursor"] = 0

    @keys.add("backspace", filter=typing)
    def _(event: object) -> None:
        state["filter"] = state["filter"][:-1]
        state["cursor"] = 0

    @keys.add("enter", filter=typing)
    def _(event: object) -> None:
        state["filtering"] = False

    @keys.add("escape", filter=typing)
    def _(event: object) -> None:
        state.update(filtering=False, filter="", cursor=0)

    return keys


def _finish(keys: KeyBindings, state: dict, help_key: bool = True) -> None:
    """The keys every screen ends with, added last because the last binding added wins."""
    showing = Condition(lambda: bool(state["keys"]))

    if help_key:

        @keys.add("?", filter=~showing & ~_typing(state))
        def _(event: object) -> None:
            state["keys"] = True

        # Eager, so the key list swallows whatever key takes it off rather than acting on it.
        @keys.add("<any>", filter=showing, eager=True)
        def _(event: object) -> None:
            state["keys"] = False

    # Eager too, and last: ctrl-c has to beat the key list's catch-all from under it.
    @keys.add("c-c", eager=True)
    def _(event) -> None:
        event.app.exit(exception=KeyboardInterrupt)


def _keys_or_filter(state: dict, line: str) -> str:
    """Filtering has its own keys, and they are the only ones that work while it is on."""
    return footer_line(FILTER_KEYS) if state["filtering"] else line


def _run(
    body: Callable[[int], list[str]], foot: Callable[[], str], keys: KeyBindings, state: dict
) -> object:
    """One screen: the body, the key line under it, and nothing else on the terminal."""

    def text() -> str:
        room = max(get_app().output.get_size().rows - 1, 6)  # the key line has the other row
        return "\n".join(key_lines() if state["keys"] else body(room))

    layout = Layout(
        HSplit(
            [
                Window(FormattedTextControl(text, focusable=True)),
                Window(FormattedTextControl(foot), height=1, style="reverse"),
            ]
        )
    )
    return _wait(Application(layout=layout, key_bindings=keys, full_screen=True))


def _wait(app: Application) -> object:
    """Run one screen. Escape answers in `ESCAPE_WAIT`, not in prompt_toolkit's half second."""
    app.ttimeoutlen = ESCAPE_WAIT
    return app.run()
