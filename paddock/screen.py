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
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

# What the form gives back beside the field it was asked to open, and what the confirm
# gives back instead of a row.
OPEN, LAUNCH, SAVE = "open", "launch", "save"
BACK, CANCEL = "back", "cancel"

# The budget from the design: 80 by 24. It is the smallest terminal anyone has, so every
# screen is drawn to fit it, and a smaller one scrolls its rows rather than losing them.
WIDTH, HEIGHT = 80, 24

# The widest a block of text gets, however wide the popup is. Past about a hundred columns the
# eye loses the start of the next line, and a form is read left edge to left edge. A wider
# popup keeps its extra room as margins, with the block in the middle of it.
MAX_CONTENT_WIDTH = 110

# A list longer than this says so in its key line (section 4.3).
FILTER_FROM = 5

# The narrowest a list's labels column gets. A longer label widens it rather than being cut,
# because a truncated label can make two rows read as one.
LABEL_ROOM = 18

# How long a lone escape waits to be sure it is not the start of an arrow key. Escape has to
# feel instant, and a terminal that splits an arrow key over 100ms is broken anyway.
ESCAPE_WAIT = 0.1

# The key lines. `esc` is on every one of them, which is the cheapest usability win there is.
# What escape does, said as what it keeps rather than as what it is. The form has no back,
# because there is nothing before it, so its own key line says cancel.
BACK_KEY = "esc back (keeps your answers)"

FORM_KEYS = ("enter edit", "^v move", "L launch", "s save", "esc cancel", "? keys")
PICK_KEYS = ("enter choose", "^v move", BACK_KEY, "? keys")
TICK_KEYS = ("space toggle", "a all", "n none", "enter done", BACK_KEY)
BOX_KEYS = ("space toggle", "tab to the box", "enter done", BACK_KEY)
TYPE_KEYS = ("enter done", "esc back (keeps what you typed)")
CONFIRM_KEYS = ("enter choose", "<> move", BACK_KEY, "? keys")

# The way back, drawn as the first row of every list and every checklist. Escape is the key
# for it, and this is the row for everyone who has not learned the key.
BACK_ROW = ("← Back", "One level back, keeping every answer. The same as esc.")

# The confirm's three, in the order they are drawn. Cancel is last, as it is everywhere.
CONFIRM_BUTTONS = ((LAUNCH, "Launch"), (BACK, "← Back to the form"), (CANCEL, "Cancel"))

# How wide the labels column of the confirm is, so a long value wraps under itself.
POLICY_ROOM = 12
FILTER_KEYS = ("type to narrow", "enter keep", "esc clear")
FILTER_KEY = "/ filter"

# What `?` puts over whatever is on screen.
KEY_LIST = (
    ("up down, k j", "move between fields, or between items"),
    ("enter", "open the field, take the item, press the button"),
    ("esc", "back out one level, keeping every answer, as the Back row does"),
    ("ctrl-c", "cancel the popup, at any depth"),
    ("a digit", "jump straight to that field, on the form"),
    ("space", "tick, in a checklist"),
    ("tab", "to the box under a checklist, and back to the list"),
    ("/", "filter a long list"),
    ("a n", "tick all, tick none, in a checklist"),
    ("L", "launch"),
    ("s", "save these answers as a profile"),
    ("?", "this list"),
)


# --- measuring, cutting and fitting -----------------------------------------


def content_width(columns: int) -> int:
    """How wide to draw in a terminal of `columns`: all of it, up to what stays readable."""
    return min(columns, MAX_CONTENT_WIDTH)


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


def form_footer(fields: int, width: int = WIDTH) -> str:
    """The form's keys, including how many digits jump, which is however many fields there are."""
    return footer_line((FORM_KEYS[0], FORM_KEYS[1], f"1-{fields} jump", *FORM_KEYS[2:]), width)


def pick_footer(size: int, width: int = WIDTH) -> str:
    """A list says it can be filtered only when it is long enough for anyone to want that."""
    parts = list(PICK_KEYS)
    if size > FILTER_FROM:
        parts.insert(0, FILTER_KEY)
    return footer_line(parts, width)


def list_footer(error: str, filtering: bool, size: int, width: int = WIDTH) -> str:
    """A list's key line, unless something is wrong, which takes the row instead."""
    if error:
        return cut(error, width)
    return footer_line(FILTER_KEYS, width) if filtering else pick_footer(size, width)


def type_footer(error: str, width: int = WIDTH) -> str:
    """One row of chrome, two jobs, never both at once: the error takes the key line's place."""
    return cut(error, width) or footer_line(TYPE_KEYS, width)


def forget_error(box: TextArea, state: dict) -> None:
    """Take the error back as soon as the answer changes, so the keys have their row again."""
    box.buffer.on_text_changed += lambda _: state.update(error="")


def spread(left: str, right: str, width: int = WIDTH) -> str:
    """One line with something at each end. The left end gives way first, the right one last."""
    right = cut(right, width)
    left = cut(left, max(width - get_cwidth(right) - 2, 0))
    gap = max(width - get_cwidth(left) - get_cwidth(right), 0)
    return (left + " " * gap + right).rstrip()


# How much of a line one checklist cell wants. Two of them fit the 80 columns the mockups are
# drawn to, and a wider terminal gets another column rather than a longer walk down one.
CELL_ROOM = 28


def columns(cells: list[str], width: int = WIDTH) -> list[str]:
    """As many columns as the width holds, in reading order down one and on to the next."""
    across = max(min(width // CELL_ROOM, 4), 1)
    deep = -(-len(cells) // across)  # rounded up, so the last column is the short one
    room = width // across - 2
    lines = []
    for index in range(deep):
        places = [index + column * deep for column in range(across)]
        parts = [cells[place] for place in places if place < len(cells)]
        lines.append("  " + "".join(pad(part, room) for part in parts).rstrip())
    return lines


def key_lines(width: int = WIDTH) -> list[str]:
    """The whole key map, for `?`."""
    return ["The keys", ""] + [cut(f"  {pad(key, 14)} {what}", width) for key, what in KEY_LIST]


def form_lines(
    title: str,
    where: str,
    rows: list[tuple],
    cursor: int,
    height: int = HEIGHT,
    width: int = WIDTH,
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
        fields.append(spread(left, note, width) if note else cut(left, width))
    hint = str(rows[cursor][2]) if cursor < len(rows) else ""
    shown = window(fields, height - 8, min(cursor, max(len(fields) - 1, 0)))
    top = [spread(f"paddock   {title}", where, width), ""]
    return top + shown + ["", *wrapped(hint, 3, width), "", _buttons(cursor - len(rows))]


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
    width: int = WIDTH,
) -> list[str]:
    """A list, each row with what it means beside it, and the whole hint under the cursor.

    A hint is one line of text, or the lines of a panel when a choice needs more room than
    that to say what it is.
    """
    rows, place_of_cursor = [], 0
    labels = _label_room([choices[index][0] for index in shown])
    for place, index in enumerate(shown):
        label, hint = choices[index]
        mark = ">" if place == cursor else " "
        if place == cursor:
            place_of_cursor = len(rows)
        rows.append(cut(f"  {mark} {_widen(label, labels)} {_beside(hint)}", width))
        if index == rule_after:
            rows.append("    " + "-" * (width - 6))
    echo = _filter_lines(filter_text, filtering)
    room = height - (3 + hint_rows + len(echo))
    hint = choices[shown[cursor]][1] if shown else _nothing(filter_text)
    top = [spread(title, note, width), ""]
    panel = _panel(hint, hint_rows, width)
    return top + window(rows, room, place_of_cursor) + ["", *panel] + echo


def tick_lines(
    title: str,
    hint: str,
    rows: list[tuple[str, bool]],
    shown: list[int],
    cursor: int,
    filter_text: str = "",
    filtering: bool = False,
    box: tuple[str, str, str] | None = None,
    in_box: bool = False,
    height: int = HEIGHT,
    width: int = WIDTH,
) -> list[str]:
    """A checklist in two columns, with a count of what is ticked in the header.

    The first cell is the way back, so `cursor` counts from it and `shown` does not.

    A box under the list is for what the checklist cannot hold, such as a domain no group
    names. It is on this screen because it is the same question, and a second screen for it
    was one of the questions this design set out to kill.
    """
    ticked = sum(1 for _, on in rows if on)
    cells = [f"{'>' if cursor == 0 else ' '}     {BACK_ROW[0]}"]
    for place, index in enumerate(shown, start=1):
        label, on = rows[index]
        cells.append(f"{'>' if place == cursor else ' '} [{'x' if on else ' '}] {label}")
    lines = columns(cells, width)
    across = max(min(width // CELL_ROOM, 4), 1)
    deep = -(-len(cells) // across)
    echo = _filter_lines(filter_text, filtering) + _box_lines(box, in_box, width)
    counted = f"{ticked} of {len(rows)} ticked"
    top = [spread(title, counted, width), "", *wrapped(hint, 3, width), ""]
    room = height - (len(top) + len(echo))
    return top + window(lines, room, cursor % deep if deep else 0) + echo


def confirm_lines_drawn(
    title: str, policy: list[tuple[str, str]], chosen: int, width: int = WIDTH
) -> list[str]:
    """The resolved policy over the three buttons.

    A value too long for its line wraps under itself rather than being cut: this is the one
    screen whose whole job is saying the grant in full.
    """
    lines = [title, ""]
    for label, text in policy:
        for place, part in enumerate(_wrap(text, width - POLICY_ROOM - 3)):
            head = pad(label, POLICY_ROOM) if place == 0 else " " * POLICY_ROOM
            lines.append(f"  {head} {part}")
    return lines + ["", _confirm_buttons(chosen)]


def _confirm_buttons(chosen: int) -> str:
    drawn = []
    for place, (_, label) in enumerate(CONFIRM_BUTTONS):
        drawn.append(f"{'>' if place == chosen else ' '} [ {label} ]")
    return "  " + "    ".join(drawn)


def type_lines(title: str, hint: str, width: int = WIDTH) -> list[str]:
    """What stands above the box."""
    return [title, "", *wrapped(hint, 3, width), ""]


def matching(labels: list[str], text: str) -> list[int]:
    """The rows a filter leaves, as indexes into the whole list."""
    wanted = text.lower()
    return [index for index, label in enumerate(labels) if wanted in label.lower()]


def _buttons(chosen: int) -> str:
    """Launch and Cancel, marked when the cursor has walked off the end of the fields."""
    launch = ">" if chosen == 0 else " "
    cancel = ">" if chosen == 1 else " "
    return f"  {launch} [ Launch ]      {cancel} [ Cancel ]"


def _label_room(labels: list[str]) -> int:
    """How wide the labels column is. What a row is called wins over what it says about itself."""
    return max([LABEL_ROOM, *(get_cwidth(label) for label in labels)])


def _widen(label: str, room: int) -> str:
    """Pad a label out to the column without ever cutting it: two rows have to read as two."""
    return label + " " * max(room - get_cwidth(label), 0)


def _beside(hint: object) -> str:
    """What a row says about itself: its hint, or the first line of its panel."""
    if isinstance(hint, str):
        return hint
    lines = list(hint) if hint else []
    return str(lines[0]) if lines else ""


def _panel(hint: object, rows: int, width: int = WIDTH) -> list[str]:
    """The hint under the list, whether it is a sentence or a panel of its own lines."""
    if isinstance(hint, str):
        return wrapped(hint, rows, width)
    lines = [f"  {cut(str(line), width - 2)}" for line in list(hint)[:rows]]
    return lines + [""] * (rows - len(lines))


def _nothing(filter_text: str) -> str:
    return "nothing matches that" if filter_text else "nothing to choose from"


def _filter_lines(filter_text: str, filtering: bool) -> list[str]:
    return ["", f"  /{filter_text}"] if filtering or filter_text else []


def _box_lines(box: tuple[str, str, str] | None, in_box: bool, width: int = WIDTH) -> list[str]:
    """The box under a checklist: its label, what it holds, and what belongs in it."""
    if box is None:
        return []
    label, text, hint = box
    cursor = "_" if in_box else " "
    return [
        "",
        f"  {pad(label, 12)}[ {pad(text + cursor, width - 20)} ]",
        cut(f"  {' ' * 12}{hint}", width),
    ]


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

    def body(height: int, width: int) -> list[str]:
        return form_lines(title, where, rows, state["cursor"], height, width)

    return _run(body, lambda width: form_footer(len(rows), width), keys, state)


def pick(
    title: str,
    choices: list[tuple[str, object]],
    note: str = "",
    cursor: int = 0,
    rule_after: int = -1,
    hint_rows: int = 3,
    refused: dict[int, str] | None = None,
) -> int | None:
    """A list. The index chosen, or None when the user backed out without choosing.

    Backing out is not cancelling. The caller keeps whatever value it had.

    `refused` says which rows cannot be chosen here and why. Taking one puts the reason on
    the key line rather than quietly doing nothing, and it goes when the cursor moves on.

    The first row is always the way back, so escape has something on screen saying what it
    does. `cursor`, `rule_after` and `refused` all count the caller's own rows, not that one.
    """
    state: dict = {
        "cursor": min(max(cursor, 0), max(len(choices) - 1, 0)) + 1,  # the Back row is first
        "filter": "",
        "filtering": False,
        "keys": False,
        "error": "",
    }
    drawn = [BACK_ROW, *choices]
    labels = [label for label, _ in choices]
    why = {index + 1: reason for index, reason in (refused or {}).items()}

    def shown() -> list[int]:
        return [0, *(index + 1 for index in matching(labels, state["filter"]))]

    keys = _list_keys(state, shown)

    @keys.add("enter", filter=~_typing(state))
    def _(event) -> None:
        index = shown()[state["cursor"]]
        state["error"] = why.get(index, "")
        if state["error"]:
            return
        event.app.exit(result=None if index == 0 else index - 1)

    @keys.add("escape", filter=~_typing(state))
    def _(event) -> None:
        event.app.exit(result=None)

    _finish(keys, state)

    def body(height: int, width: int) -> list[str]:
        return list_lines(
            title, note, drawn, shown(), state["cursor"], state["filter"],
            state["filtering"], rule_after + 1, hint_rows, height, width,
        )

    def foot(width: int) -> str:
        return list_footer(state["error"], state["filtering"], len(choices), width)

    return _run(body, foot, keys, state)


def tick(
    title: str,
    rows: list[tuple[str, bool]],
    hint: str = "",
    box: tuple[str, str, str] | None = None,
) -> list[int] | tuple[list[int], str]:
    """A checklist. What is ticked, whether it was left with enter, escape or the Back row.

    Escape keeps the ticks, because escape never loses an answer: the ticks are the answer.

    With a `box` of (label, value, hint) under the list, tab moves between the two and the
    answer is both: the ticks and what the box holds.
    """
    state: dict = {
        "cursor": 1,  # the Back row is first, so the first thing to tick is second
        "filter": "",
        "filtering": False,
        "keys": False,
        "in_box": False,
        "typed": box[1] if box else "",
        "ticked": {index for index, (_, on) in enumerate(rows) if on},
    }
    labels = [label for label, _ in rows]

    def shown() -> list[int]:
        return matching(labels, state["filter"])

    def marked() -> list[tuple[str, bool]]:
        return [(label, index in state["ticked"]) for index, label in enumerate(labels)]

    keys = _list_keys(state, shown, extra=1)  # the Back row is one more place to be
    on_list = _on_list(state)

    @keys.add("space", filter=on_list)
    def _(event: object) -> None:
        places = shown()
        if state["cursor"] and places:  # the Back row has nothing to tick
            state["ticked"] ^= {places[state["cursor"] - 1]}

    @keys.add("a", filter=on_list)
    def _(event: object) -> None:
        state["ticked"] |= set(shown())  # all of what is on screen, so a filter narrows it

    @keys.add("n", filter=on_list)
    def _(event: object) -> None:
        state["ticked"] -= set(shown())

    if box is not None:
        _box_keys(keys, state)

    @keys.add("enter", filter=~_typing(state))
    @keys.add("escape", filter=~_typing(state))
    def _(event) -> None:
        ticked = sorted(state["ticked"])
        event.app.exit(result=(ticked, state["typed"].strip()) if box else ticked)

    _finish(keys, state)

    def body(height: int, width: int) -> list[str]:
        showing = (box[0], state["typed"], box[2]) if box else None
        return tick_lines(
            title, hint, marked(), shown(), state["cursor"], state["filter"],
            state["filtering"], showing, state["in_box"], height, width,
        )

    def foot(width: int) -> str:
        return _keys_or_filter(state, footer_line(BOX_KEYS if box else TICK_KEYS, width), width)

    return _run(body, foot, keys, state)


def confirm(title: str, policy: list[tuple[str, str]]) -> str:
    """The last screen: LAUNCH, BACK to the form, or CANCEL.

    It opens on Launch, because the common case is confirming what the form already said, and
    escape is the Back button: one level back, with every answer where it was.
    """
    state: dict = {"cursor": 0, "keys": False}
    keys = KeyBindings()

    @keys.add("left")
    @keys.add("h")
    def _(event: object) -> None:
        state["cursor"] = max(state["cursor"] - 1, 0)

    @keys.add("right")
    @keys.add("l")
    def _(event: object) -> None:
        state["cursor"] = min(state["cursor"] + 1, len(CONFIRM_BUTTONS) - 1)

    @keys.add("enter")
    def _(event) -> None:
        event.app.exit(result=CONFIRM_BUTTONS[state["cursor"]][0])

    @keys.add("escape")
    def _(event) -> None:
        event.app.exit(result=BACK)

    _finish(keys, state)

    def body(height: int, width: int) -> list[str]:
        return confirm_lines_drawn(title, policy, state["cursor"], width)

    return str(_run(body, lambda width: footer_line(CONFIRM_KEYS, width), keys, state))


def type_in(
    title: str, value: str = "", hint: str = "", check: Callable[[str], str] | None = None
) -> str:
    """A box. What it holds, whether it was left with enter or with escape.

    `check` is what makes an answer wrong. Its message replaces the key line and the box stays
    open, and it goes as soon as the answer changes. Escape gives back what was typed without
    running the check, so the caller validates whatever it gets from here.
    """
    return typed_in(title, value, hint, check)[0]


def typed_in(
    title: str, value: str = "", hint: str = "", check: Callable[[str], str] | None = None
) -> tuple[str, bool]:
    """The same box, and whether escape is what closed it.

    A field drawn over two screens needs that: escape backs out one level, to the screen the
    box was opened from, not all the way to the form.
    """
    state: dict = {"error": "", "keys": False}
    box = TextArea(text=value, multiline=False)
    box.buffer.cursor_position = len(value)  # typing carries on from what is there
    forget_error(box, state)
    keys = KeyBindings()

    @keys.add("enter")
    def _(event) -> None:
        typed = box.text.strip()
        state["error"] = check(typed) if check else ""
        if not state["error"]:
            event.app.exit(result=(typed, False))

    @keys.add("escape")
    def _(event) -> None:
        event.app.exit(result=(box.text.strip(), True))

    _finish(keys, state, help_key=False)

    def above() -> str:
        width = _room()[1]
        return "\n".join(_centred(type_lines(title, hint, width), width))

    def line() -> str:
        width = _room()[1]
        return _centred([type_footer(state["error"], width)], width)[0]

    layout = Layout(
        HSplit(
            [
                Window(FormattedTextControl(above), height=len(type_lines(title, hint))),
                VSplit([Window(width=_indent), box]),  # the box keeps the margin the text has
                Window(),  # the gap that puts the key line at the bottom, as on every screen
                Window(FormattedTextControl(line), height=1, style="reverse"),
            ]
        ),
        focused_element=box,
    )
    return _wait(Application(layout=layout, key_bindings=keys, full_screen=True))


def _indent() -> int:
    """The same margin as the text, for the one window that holds a box rather than lines."""
    return len(_margin())


# --- the keys every screen shares -------------------------------------------


def _typing(state: dict) -> Condition:
    """True while `/` has the keyboard, which is the only time a letter is not a shortcut."""
    return Condition(lambda: bool(state.get("filtering")))


def _on_list(state: dict) -> Condition:
    """True while the keys belong to the list, rather than to the filter or to a box."""
    return Condition(lambda: not state.get("filtering") and not state.get("in_box"))


def _box_keys(keys: KeyBindings, state: dict) -> None:
    """Tab to the box and back. What is typed there is text, the same as inside a filter."""
    in_box = Condition(lambda: bool(state["in_box"]))

    @keys.add("tab")
    def _(event: object) -> None:
        state["in_box"] = not state["in_box"]

    @keys.add("<any>", filter=in_box)
    def _(event) -> None:
        if event.data and event.data.isprintable():
            state["typed"] += event.data

    @keys.add("backspace", filter=in_box)
    def _(event: object) -> None:
        state["typed"] = state["typed"][:-1]


def _list_keys(state: dict, shown: Callable[[], list[int]], extra: int = 0) -> KeyBindings:
    """Moving and filtering, which a list and a checklist do the same way."""
    keys = KeyBindings()
    typing = _typing(state)
    on_list = _on_list(state)

    @keys.add("up", filter=on_list)
    @keys.add("k", filter=on_list)
    def _(event: object) -> None:
        state["cursor"] = max(state["cursor"] - 1, 0)
        state["error"] = ""

    @keys.add("down", filter=on_list)
    @keys.add("j", filter=on_list)
    def _(event: object) -> None:
        state["cursor"] = min(state["cursor"] + 1, max(len(shown()) - 1 + extra, 0))
        state["error"] = ""

    @keys.add("/", filter=on_list)
    def _(event: object) -> None:
        state["filtering"] = True
        state["error"] = ""

    def onto_the_first_match() -> None:
        """Narrowing moves the cursor to what is left, never onto the Back row."""
        state["cursor"] = min(1, max(len(shown()) - 1 + extra, 0))

    @keys.add("<any>", filter=typing)
    def _(event) -> None:
        if event.data and event.data.isprintable():
            state["filter"] += event.data
            onto_the_first_match()

    @keys.add("backspace", filter=typing)
    def _(event: object) -> None:
        state["filter"] = state["filter"][:-1]
        onto_the_first_match()

    @keys.add("enter", filter=typing)
    def _(event: object) -> None:
        state["filtering"] = False

    @keys.add("escape", filter=typing)
    def _(event: object) -> None:
        state.update(filtering=False, filter="")
        onto_the_first_match()

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


def _keys_or_filter(state: dict, line: str, width: int = WIDTH) -> str:
    """Filtering has its own keys, and they are the only ones that work while it is on."""
    return footer_line(FILTER_KEYS, width) if state["filtering"] else line


def _run(
    body: Callable[[int, int], list[str]],
    foot: Callable[[int], str],
    keys: KeyBindings,
    state: dict,
) -> object:
    """One screen: the body, the key line under it, and nothing else on the terminal."""

    def text() -> str:
        rows, width = _room()
        lines = key_lines(width) if state["keys"] else body(rows, width)
        if len(lines) < rows:  # a line of air at the top, when there are rows to spare
            lines = ["", *lines]
        return "\n".join(_centred(lines, width))

    layout = Layout(
        HSplit(
            [
                Window(FormattedTextControl(text, focusable=True)),
                Window(
                    FormattedTextControl(lambda: _centred([foot(_room()[1])], _room()[1])[0]),
                    height=1,
                    style="reverse",
                ),
            ]
        )
    )
    return _wait(Application(layout=layout, key_bindings=keys, full_screen=True))


def _room() -> tuple[int, int]:
    """The rows the body has and the columns it draws to, as the terminal stands right now."""
    size = get_app().output.get_size()
    return max(size.rows - 1, 6), content_width(size.columns)  # the key line has the other row


def _margin() -> str:
    """What puts the block in the middle of a popup wider than the block."""
    size = get_app().output.get_size()
    return " " * max((size.columns - content_width(size.columns)) // 2, 0)


def _centred(lines: list[str], width: int) -> list[str]:
    margin = _margin()
    return [margin + line if line else line for line in lines]


def _wait(app: Application) -> object:
    """Run one screen, with both waits that stand between escape and an answer shortened.

    `ttimeoutlen` is the wait for the rest of an escape sequence, such as an arrow key.
    `timeoutlen` is the wait for the rest of a key sequence, which is what a focused box does
    to escape while it wonders whether a meta key is coming. Escape has to feel instant on
    every screen, and a terminal that splits either over 100ms is broken anyway.
    """
    app.ttimeoutlen = ESCAPE_WAIT
    app.timeoutlen = ESCAPE_WAIT
    return app.run()
