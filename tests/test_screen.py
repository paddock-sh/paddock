"""The screens, driven by real key presses with no terminal at all.

Two halves, like the module: the lines each screen draws are plain functions and are asserted
as text, and the keys are pressed for real through a pipe, which is a better test than faking
a prompt library. Where what is on screen is the point, the screen is rendered to a string and
read back.
"""

import io
import time
from collections.abc import Callable

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.output.plain_text import PlainTextOutput
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

from paddock import screen

# Escape as the parser sees it. One is enough: the screens wait ESCAPE_WAIT for it, not
# prompt_toolkit's half second.
ESC = "\x1b"
CTRL_C = "\x03"
DOWN, UP = "\x1b[B", "\x1b[A"
RIGHT, LEFT = "\x1b[C", "\x1b[D"

# Every character of this is two columns wide.
WIDE = "日本語のテキストはとても長い"


class Sized(DummyOutput):
    """A terminal of whatever size the test wants."""

    def __init__(self, rows: int = 24, columns: int = screen.WIDTH) -> None:
        self.size = Size(rows=rows, columns=columns)

    def get_size(self) -> Size:
        return self.size


class Readable(PlainTextOutput):
    """A terminal a test can read back, for when what is on screen is the point."""

    def __init__(self, sink: io.StringIO, rows: int = 24, columns: int = screen.WIDTH) -> None:
        super().__init__(sink)
        self.size = Size(rows=rows, columns=columns)

    def get_size(self) -> Size:
        return self.size


def drive(
    keys: str, show: Callable[[], object], rows: int = 24, columns: int = screen.WIDTH
) -> object:
    """Run one screen with the keys already typed."""
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        with create_app_session(input=pipe, output=Sized(rows, columns)):
            return show()


def drawn(
    keys: str, show: Callable[[], object], rows: int = 24, columns: int = screen.WIDTH
) -> str:
    """Everything one screen put on the terminal while those keys were pressed."""
    sink = io.StringIO()
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        with create_app_session(input=pipe, output=Readable(sink, rows, columns)):
            show()
    return sink.getvalue()


FIELDS = [
    ("Open", "New sandbox", "New sandbox: an agent under the OS sandbox."),
    ("Profile", "claude-default", "Fills in everything below."),
    ("Agent", "Claude Code (claude)", "The command that runs inside the sandbox."),
    ("Tools", "git rg fd (3)", "Ticked binaries are on the sandbox PATH."),
    ("Network", "anthropic (2 domains)", "Only these domains are reachable."),
    ("Files", "an isolated scratch directory", "Isolated: a fresh scratch directory."),
    ("Skills", "none", "Unticked skills are not in the config dir at all."),
    ("Advanced", "name, save as profile, MCP", "The session name, MCP servers, denied reads."),
]

PROFILES = [
    ("claude-default", "Claude Code, 10 tools, 4 network groups"),
    ("offline-shell", "Shell, 4 tools, no network"),
    ("review", "Claude Code, 3 tools, github only"),
]

# A list where a navigation key also matches a row, which is what makes bare typing a trap.
MENU = [("jq", "a filter for json"), ("git", "version control"), ("rg", "a faster grep")]

TOOLS = [("git", True), ("rg", True), ("fd", False), ("jq", False), ("curl", True)]


# --- the lines a screen draws ----------------------------------------------


def test_the_key_line_truncates_rather_than_wraps() -> None:
    """A wrapped footer would push the layout around, which is the one thing it may not do."""
    line = screen.footer_line(["enter done", "esc back"])
    long = screen.footer_line([f"key {index} does something" for index in range(9)])

    assert line == "enter done   esc back"
    assert get_cwidth(long) <= screen.WIDTH
    assert long.endswith("...")


def test_the_form_says_how_many_digits_jump() -> None:
    """Section 6 puts a ninth field on this form, and the key line has to say so when it does."""
    assert "1-8 jump" in screen.form_footer(8)
    assert "1-9 jump" in screen.form_footer(9)


def test_a_line_with_two_ends_keeps_the_right_one() -> None:
    line = screen.spread("paddock   claude-default", "in ~/dev/paddock")

    assert line.startswith("paddock   claude-default")
    assert line.endswith("in ~/dev/paddock")
    assert get_cwidth(line) == screen.WIDTH


def test_a_long_left_end_is_cut_short() -> None:
    line = screen.spread("x" * 200, "in ~/dev")

    assert get_cwidth(line) == screen.WIDTH
    assert "..." in line
    assert line.endswith("in ~/dev")


def test_a_right_end_wider_than_the_line_is_cut_too() -> None:
    assert get_cwidth(screen.spread("left", "x" * 200)) <= screen.WIDTH


def test_a_hint_is_always_the_same_number_of_lines() -> None:
    """A longer hint must not move the buttons under it."""
    assert len(screen.wrapped("short", 3)) == 3
    assert len(screen.wrapped("word " * 60, 3)) == 3
    assert screen.wrapped("", 3) == ["", "", ""]


def test_a_wide_character_is_two_columns_everywhere() -> None:
    """Counting characters instead of columns wraps the line and moves the layout."""
    rows = [(WIDE, WIDE, WIDE, WIDE)]
    lines = [
        screen.footer_line([WIDE] * 6),
        screen.spread(WIDE * 3, WIDE),
        *screen.wrapped(f"{WIDE} " * 6, 3),
        *screen.columns([WIDE] * 5),
        *screen.form_lines(WIDE, WIDE, rows, 0),
        *screen.list_lines(WIDE, WIDE, [(WIDE, WIDE)], [0], 0),
        *screen.tick_lines(WIDE, WIDE, [(WIDE, True)], [0], 0),
    ]

    assert [line for line in lines if get_cwidth(line) > screen.WIDTH] == []


def test_a_checklist_reads_down_the_left_column_first() -> None:
    """Eighteen short names do not fit one under another, and reading order is the only order."""
    lines = screen.columns([f"item{index}" for index in range(5)])

    assert len(lines) == 3
    assert lines[0].split() == ["item0", "item3"]
    assert lines[2].split() == ["item2"]


def test_the_form_numbers_its_fields_and_marks_the_cursor() -> None:
    lines = screen.form_lines("claude-default", "in ~/dev/paddock", FIELDS, 1)

    assert lines[0].startswith("paddock")
    assert lines[0].endswith("in ~/dev/paddock")
    assert "  > 2 Profile" in lines[3]
    assert "    1 Open" in lines[2]
    assert any("Fills in everything below." in line for line in lines)  # the cursor's own hint
    assert lines[-1].strip().startswith("[ Launch ]")


def test_the_form_moves_the_cursor_onto_the_buttons() -> None:
    lines = screen.form_lines("claude-default", "", FIELDS, len(FIELDS))

    assert lines[-1].strip() == "> [ Launch ]        [ Cancel ]"
    assert not any(line.startswith("  >") for line in lines[2:10])


def test_a_row_can_keep_a_count_at_the_right_edge() -> None:
    """Section 5.1 puts the count of what a value holds in a column of its own."""
    line = screen.form_lines("p", "", [("Tools", "git rg fd", "hint", "(10)")], 0)[2]

    assert line.endswith("(10)")
    assert get_cwidth(line) <= screen.WIDTH


def test_a_value_too_long_for_the_line_says_it_was_cut() -> None:
    line = screen.form_lines("p", "", [("Tools", "git " * 40, "hint")], 0)[2]

    assert get_cwidth(line) <= screen.WIDTH
    assert line.endswith("...")


def test_a_short_terminal_scrolls_the_fields_and_keeps_the_buttons() -> None:
    """Clipping would lose the buttons and the cursor. The rows move instead."""
    lines = screen.form_lines("p", "", FIELDS, 6, height=14)

    assert len(lines) <= 14
    assert any(line.startswith("  > 7 Skills") for line in lines)
    assert lines[-1].strip().endswith("[ Cancel ]")
    assert any("Unticked skills" in line for line in lines)


def test_a_list_shows_a_rule_and_the_hint_of_the_row_you_are_on() -> None:
    lines = screen.list_lines("Profile", "3 saved", PROFILES, [0, 1, 2], 1, rule_after=1)

    assert lines[0] == screen.spread("Profile", "3 saved")
    assert "    claude-default" in lines[2]
    assert "  > offline-shell" in lines[3]
    assert lines[4].strip().startswith("---")
    assert any("Shell, 4 tools, no network" in line for line in lines)


def test_a_choice_can_show_a_panel_instead_of_a_line() -> None:
    """Section 5.3 picks a profile on what it is, which takes more than one line to say."""
    panel = ["agent    Claude Code", "tools    git rg fd", "network  4 groups"]

    lines = screen.list_lines("Profile", "", [("claude-default", panel)], [0], 0, hint_rows=6)

    assert "agent    Claude Code" in lines[2]  # the first line of it stands beside the row
    assert any("tools    git rg fd" in line for line in lines[3:])
    assert any("network  4 groups" in line for line in lines[3:])


def test_an_empty_list_says_which_kind_of_empty_it_is() -> None:
    empty = screen.list_lines("Profile", "", [], [], 0)
    filtered = screen.list_lines("Profile", "", PROFILES, [], 0, "zzz", True)

    assert any("nothing to choose from" in line for line in empty)
    assert any("nothing matches that" in line for line in filtered)


def test_a_short_terminal_keeps_the_filter_echo_on_a_list() -> None:
    """What you have typed is the one thing a narrowed list may not drop off the bottom."""
    lines = screen.list_lines("Profile", "", PROFILES, [0, 1, 2], 2, "rev", True, height=10)

    assert len(lines) <= 10
    assert lines[-1].strip() == "/rev"
    assert any("review" in line for line in lines)


def test_a_checklist_counts_what_is_ticked() -> None:
    hint = "Ticked binaries are on the PATH."

    lines = screen.tick_lines("Tools it can run", hint, TOOLS, [0, 1, 2, 3, 4], 1)

    assert lines[0] == screen.spread("Tools it can run", "3 of 5 ticked")
    assert "> [x] git" in lines[7]
    assert any("[ ] fd" in line for line in lines)
    assert any(screen.BACK_ROW[0] in line for line in lines)


def test_a_short_terminal_scrolls_a_checklist_around_the_cursor() -> None:
    rows = [(f"tool{index}", False) for index in range(20)]

    lines = screen.tick_lines("Tools", "hint", rows, list(range(20)), 13, height=12)

    assert len(lines) <= 12
    assert any("tool12" in line for line in lines)


def test_the_key_list_names_the_two_promises() -> None:
    """Escape never loses an answer and ctrl-c always cancels, so both are on the list."""
    lines = screen.key_lines()

    assert any(line.startswith("  esc") for line in lines)
    assert any(line.startswith("  ctrl-c") for line in lines)
    assert any("jump straight to that field" in line for line in lines)


# --- the room the terminal actually has -------------------------------------


def test_the_content_width_grows_with_the_terminal_up_to_a_point() -> None:
    """A line far past a hundred columns is hard to track back to the start of the next one."""
    assert screen.content_width(80) == 80
    assert screen.content_width(60) == 60
    assert screen.content_width(120) == screen.MAX_CONTENT_WIDTH
    assert screen.content_width(200) == screen.MAX_CONTENT_WIDTH


def test_a_wide_terminal_stops_cutting_what_fits_in_it() -> None:
    """The complaint was words cut short with the room to spare sitting beside them."""
    value = "git rg fd jq curl node npm npx uv python3 go cargo make cmake gh docker psql"
    rows = [("Tools", value, "a hint", "(17)")]

    narrow = screen.form_lines("p", "", rows, 0)
    wide = screen.form_lines("p", "", rows, 0, width=screen.MAX_CONTENT_WIDTH)

    assert "..." in narrow[2]  # at 80 columns there is nowhere to put the rest of it
    assert value in wide[2]
    assert get_cwidth(wide[2]) <= screen.MAX_CONTENT_WIDTH


def test_a_wide_screen_puts_the_block_in_the_middle_of_it() -> None:
    """A form in the top left corner of a 200 column popup reads as a bug, and looked like one."""
    shown = drawn("\r", lambda: screen.form("p", "", FIELDS), columns=200)
    indents = [len(line) - len(line.lstrip()) for line in shown.splitlines() if "1 Open" in line]

    assert indents
    assert indents[0] == (200 - screen.MAX_CONTENT_WIDTH) // 2 + 2  # plus the row's own indent


def test_a_checklist_uses_the_room_it_has() -> None:
    cells = [f"[ ] tool{index}" for index in range(18)]

    assert len(screen.columns(cells, 80)) == 9  # two columns, as the mockups are drawn
    assert len(screen.columns(cells, screen.MAX_CONTENT_WIDTH)) == 6  # three, given the room


def test_eighty_by_twenty_four_is_still_what_the_mockups_show() -> None:
    """The design is drawn to the smallest terminal anyone has, and that has not moved."""
    lines = screen.form_lines("claude-default", "in ~/dev/paddock", FIELDS, 1)

    assert [line for line in lines if get_cwidth(line) > 80] == []
    assert "  > 2 Profile" in lines[3]
    assert lines[-1].strip().startswith("[ Launch ]")


# The popup herdr opens is 70% of the terminal minus its sidebar and border, so these are the
# sizes people actually get: a 100 by 30 terminal gives about this, and 140 by 40 is needed
# before it reaches the 80 by 24 the design is drawn to.
POPUP = (18, 48)


def test_the_confirm_keeps_its_buttons_on_an_ordinary_popup() -> None:
    """A confirm whose buttons scrolled off would cancel the launch the user thought it made."""
    policy = [("can reach", ", ".join(f"host{index}.example.com" for index in range(12)))]

    for rows, width in ((16, 80), POPUP):
        lines = screen.confirm_lines_drawn("Launch this sandbox?", policy, 0, width, rows)

        assert len(lines) <= rows
        assert lines[0] == "Launch this sandbox?"
        assert "[ Launch ]" in lines[-1] or "[ Launch ]" in "".join(lines[-3:])
        assert "[ Cancel ]" in "".join(lines[-3:])


def test_the_confirm_scrolls_rather_than_leaving_anything_out() -> None:
    """Saying the grant in full is the whole job, so no part of it may be unreachable."""
    policy = [("can reach", ", ".join(f"host{index}.example.com" for index in range(40)))]
    every = screen.policy_lines(policy, POPUP[1])

    seen = set()
    for scroll in range(len(every)):
        drawn = screen.confirm_lines_drawn("Launch?", policy, 0, POPUP[1], POPUP[0], scroll)
        seen |= {line for line in drawn if line in every}
        assert drawn[-1].strip().startswith("> [ Launch ]")  # the buttons never scroll off

    assert seen == set(every)
    assert not any("more lines" in line for line in every)


def test_the_confirm_never_cuts_a_path_it_is_granting() -> None:
    """An ellipsis in the middle of a path would hide what is being handed over."""
    granted = "/Users/someone/very/long/path/that/will/not/fit/on/one/line/of/a/small/popup"
    policy = [("can write", f"its own workdir, plus {granted}")]

    lines = screen.confirm_lines_drawn("Launch?", policy, 0, POPUP[1], 24)

    assert "..." not in "".join(lines)
    assert granted.replace("/", "") in "".join(lines).replace("/", "").replace(" ", "")


def test_the_key_list_scrolls_on_a_small_popup_too() -> None:
    lines = screen.key_lines(POPUP[1], POPUP[0])

    assert len(lines) <= POPUP[0]
    assert lines[0] == "The keys"


def test_the_form_and_a_checklist_hold_up_at_the_size_a_popup_really_is() -> None:
    form = screen.form_lines("claude-default", "in ~/dev", FIELDS, 4, POPUP[0], POPUP[1])
    checklist = screen.tick_lines(
        "Tools", "a hint", TOOLS, [0, 1, 2, 3, 4], 1, height=POPUP[0], width=POPUP[1]
    )

    assert len(form) <= POPUP[0]
    assert "[ Launch ]" in form[-1]
    assert any(line.startswith("  > 5 ") for line in form)  # the cursor is still on screen
    assert len(checklist) <= POPUP[0]
    assert [line for line in form + checklist if get_cwidth(line) > POPUP[1]] == []


def test_the_checklist_key_line_says_it_can_be_filtered() -> None:
    """The two longest lists in the chooser are the two this matters most for."""
    line = screen.footer_line(screen.TICK_KEYS)

    assert "/ filter" in line
    assert get_cwidth(line) <= screen.WIDTH


def test_the_confirm_offers_the_save_the_design_puts_on_it() -> None:
    assert "s save" in screen.footer_line(screen.CONFIRM_KEYS)
    assert drive("s", lambda: screen.confirm("Launch?", POLICY)) == screen.SAVE


# --- the form ---------------------------------------------------------------


def test_enter_on_a_field_opens_it() -> None:
    assert drive(f"{DOWN}{DOWN}\r", lambda: screen.form("p", "", FIELDS)) == (screen.OPEN, 2)


def test_k_and_j_move_the_way_the_arrows_do() -> None:
    assert drive("jjjk\r", lambda: screen.form("p", "", FIELDS)) == (screen.OPEN, 2)


def test_a_digit_jumps_straight_to_a_field() -> None:
    """The form is a fixed list that never reorders, so a digit cannot lie."""
    assert drive("5\r", lambda: screen.form("p", "", FIELDS)) == (screen.OPEN, 4)


def test_the_cursor_comes_back_where_it_was() -> None:
    assert drive("\r", lambda: screen.form("p", "", FIELDS, cursor=6)) == (screen.OPEN, 6)


def test_l_launches_from_anywhere_on_the_form() -> None:
    what, _ = drive("L", lambda: screen.form("p", "", FIELDS))

    assert what == screen.LAUNCH


def test_s_saves_the_answers_as_a_profile() -> None:
    what, _ = drive(f"{DOWN}s", lambda: screen.form("p", "", FIELDS))

    assert what == screen.SAVE


def test_the_buttons_are_the_last_two_places_the_cursor_goes() -> None:
    to_launch = DOWN * len(FIELDS)
    launch = drive(f"{to_launch}\r", lambda: screen.form("p", "", FIELDS))
    cancel = drive(f"{to_launch}{DOWN}\r", lambda: screen.form("p", "", FIELDS))

    assert launch == (screen.LAUNCH, len(FIELDS))
    assert cancel is None


def test_the_cursor_stops_at_both_ends() -> None:
    assert drive(f"{UP}{UP}\r", lambda: screen.form("p", "", FIELDS)) == (screen.OPEN, 0)
    assert drive(DOWN * 20 + "\r", lambda: screen.form("p", "", FIELDS)) is None  # on Cancel


def test_escape_on_the_form_closes_the_popup_with_nothing_done() -> None:
    assert drive(ESC * 2, lambda: screen.form("p", "", FIELDS)) is None


def test_a_bare_escape_answers_on_its_own() -> None:
    """One escape byte, nothing after it: the wait for an arrow key has to end quickly."""
    assert drive(ESC, lambda: screen.form("p", "", FIELDS)) is None


def test_a_short_terminal_still_answers() -> None:
    assert drive("\r", lambda: screen.form("p", "", FIELDS), rows=10) == (screen.OPEN, 0)
    assert drive(" \r", lambda: screen.tick("Tools", TOOLS), rows=10) == [1, 4]


def test_a_short_terminal_still_draws_the_buttons_and_the_key_line() -> None:
    """At sixteen rows there is no room for everything, and these two are what must stay."""
    shown = drawn(f"{DOWN}\r", lambda: screen.form("p", "", FIELDS), rows=16)

    assert "[ Launch ]" in shown
    assert "esc cancel" in shown  # the form has nothing before it, so back would be a lie


def test_ctrl_c_cancels_from_any_screen() -> None:
    for show in (
        lambda: screen.form("p", "", FIELDS),
        lambda: screen.pick("Profile", PROFILES),
        lambda: screen.tick("Tools", TOOLS),
        lambda: screen.type_in("Name", ""),
    ):
        with pytest.raises(KeyboardInterrupt):
            drive(CTRL_C, show)


def test_ctrl_c_cancels_from_under_the_key_list() -> None:
    """The key list catches every key to take itself off. Ctrl-c is the one that gets through."""
    for show in (
        lambda: screen.form("p", "", FIELDS),
        lambda: screen.pick("Profile", PROFILES),
        lambda: screen.tick("Tools", TOOLS),
    ):
        with pytest.raises(KeyboardInterrupt):
            drive(f"?{CTRL_C}", show)


def test_the_key_list_goes_over_the_screen_and_comes_off_again() -> None:
    assert drive("?q\r", lambda: screen.form("p", "", FIELDS)) == (screen.OPEN, 0)


def test_the_key_list_swallows_the_key_that_takes_it_off() -> None:
    """It is over the screen, so the key that dismisses it must not also do its own job."""
    assert drive("?5\r", lambda: screen.form("p", "", FIELDS)) == (screen.OPEN, 0)


# --- the list picker --------------------------------------------------------


def test_a_list_gives_back_what_was_chosen() -> None:
    assert drive(f"{DOWN}\r", lambda: screen.pick("Profile", PROFILES)) == 1


def test_backing_out_of_a_list_chooses_nothing() -> None:
    """Not a cancel: the caller keeps whatever value it had."""
    assert drive(ESC * 2, lambda: screen.pick("Profile", PROFILES)) is None


def test_a_cursor_past_the_end_of_a_list_lands_on_the_last_row() -> None:
    """The caller remembers where it was, and the list it comes back to can be shorter."""
    assert drive("\r", lambda: screen.pick("Profile", PROFILES[:2], cursor=5)) == 1


def test_typing_on_a_list_never_filters_it() -> None:
    """`j` matches a row here: if bare typing filtered, enter would take that row instead."""
    assert drive("j\r", lambda: screen.pick("Tools", MENU)) == 1


def test_the_filter_key_narrows_a_list_and_keeps_the_real_index() -> None:
    assert drive("/rev\r\r", lambda: screen.pick("Profile", PROFILES)) == 2


def test_escape_leaves_the_filter_and_the_list_stands() -> None:
    assert drive(f"/rev{ESC}\r", lambda: screen.pick("Profile", PROFILES)) == 0


def test_a_filter_that_matches_nothing_takes_nothing() -> None:
    assert drive(f"/zzz\r\r{ESC}{ESC}", lambda: screen.pick("Profile", PROFILES)) is None


def test_a_row_that_cannot_be_chosen_is_not_chosen() -> None:
    why = {1: "msb is not installed"}

    assert drive(f"{DOWN}\r{UP}\r", lambda: screen.pick("Backend", MENU, refused=why)) == 0
    assert drive(f"{DOWN}\r{ESC}{ESC}", lambda: screen.pick("Backend", MENU, refused=why)) is None


def test_a_refused_row_says_why_on_the_key_line() -> None:
    """Greying a row without saying why is the questionnaire's habit, not this one's."""
    assert screen.list_footer("msb is not installed", False, 3) == "msb is not installed"
    assert screen.list_footer("", False, 3) == screen.pick_footer(3)
    assert screen.list_footer("", True, 9) == screen.footer_line(screen.FILTER_KEYS)


def test_the_filter_key_is_advertised_only_on_a_list_worth_filtering() -> None:
    assert screen.FILTER_KEY not in screen.pick_footer(3)
    assert screen.FILTER_KEY in screen.pick_footer(9)


def test_the_way_back_is_the_first_row_of_a_list() -> None:
    """Escape is the key, and the row is for everyone who has not learned the key."""
    lines = screen.list_lines("Profile", "", [screen.BACK_ROW, *PROFILES], [0, 1, 2, 3], 0)

    assert screen.BACK_ROW[0] in lines[2]
    assert drive(f"{UP}\r", lambda: screen.pick("Profile", PROFILES)) is None
    assert drive("\r", lambda: screen.pick("Profile", PROFILES)) == 0


def test_the_back_row_and_escape_are_the_same_answer() -> None:
    assert drive(f"{UP}{UP}\r", lambda: screen.pick("Profile", PROFILES, cursor=1)) is None
    assert drive(ESC * 2, lambda: screen.pick("Profile", PROFILES, cursor=1)) is None


def test_a_refused_row_still_counts_from_the_callers_own_rows() -> None:
    """The Back row is the screen's, so what a caller says about row 1 is about its row 1."""
    why = {1: "msb is not installed"}

    assert drive(f"{DOWN}\r{UP}\r", lambda: screen.pick("Backend", MENU, refused=why)) == 0


# --- the checklist ----------------------------------------------------------


def test_space_ticks_and_enter_is_done() -> None:
    assert drive(f"{DOWN}{DOWN} \r", lambda: screen.tick("Tools", TOOLS)) == [0, 1, 2, 4]


def test_escape_from_a_checklist_keeps_the_ticks() -> None:
    """The promise: escape closes what is open and never loses an answer."""
    assert drive(f" {ESC}{ESC}", lambda: screen.tick("Tools", TOOLS)) == [1, 4]


def test_the_way_back_is_the_first_row_of_a_checklist_too() -> None:
    """Taking it is what escape does: back one level, with the ticks as they now stand."""
    assert drive(f" {UP}\r", lambda: screen.tick("Tools", TOOLS)) == [1, 4]
    assert drive(f"{UP} \r", lambda: screen.tick("Tools", TOOLS)) == [0, 1, 4]


def test_all_and_none_are_one_key_each() -> None:
    assert drive("a\r", lambda: screen.tick("Tools", TOOLS)) == [0, 1, 2, 3, 4]
    assert drive("n\r", lambda: screen.tick("Tools", TOOLS)) == []


def test_all_and_none_reach_only_what_is_on_screen() -> None:
    """A filter narrows what a key does. It must not throw away a tick you cannot see."""
    assert drive("/fd\ra\r", lambda: screen.tick("Tools", TOOLS)) == [0, 1, 2, 4]
    assert drive("/git\rn\r", lambda: screen.tick("Tools", TOOLS)) == [1, 4]


def test_letters_inside_the_filter_stay_text() -> None:
    """`a` and `n` are keys on a checklist, and plain text while the filter has the keyboard."""
    assert drive("/an\r \r", lambda: screen.tick("Tools", TOOLS)) == [0, 1, 4]


def test_a_filtered_checklist_ticks_the_row_it_shows() -> None:
    assert drive("/fd\r \r", lambda: screen.tick("Tools", TOOLS)) == [0, 1, 2, 4]


def test_the_key_line_says_how_to_reach_the_box() -> None:
    """A composite nobody can find is a composite nobody has: tab has to be on the line."""
    line = screen.footer_line(screen.BOX_KEYS)

    assert "tab to the box" in line
    assert get_cwidth(line) <= screen.WIDTH
    assert any("tab" in key for key, _ in screen.KEY_LIST)


def test_escape_answers_at_once_from_a_box_as_well_as_a_list() -> None:
    """A box that hangs for a second on escape reads as a screen that ignored the key."""
    started = time.monotonic()
    drive(f"review{ESC}{ESC}", lambda: screen.type_in("Name", ""))
    box = time.monotonic() - started

    started = time.monotonic()
    drive(ESC * 2, lambda: screen.pick("Profile", PROFILES))
    listed = time.monotonic() - started

    assert box < 0.5  # a tenth of a second is the wait, and the rest is the machine
    assert listed < 0.5


def test_two_sessions_with_long_names_are_two_different_rows() -> None:
    """The label column gives way to what tells one row from another."""
    choices = [
        ("review-of-the-parser: claude / claude-default, 2 tabs", "one"),
        ("review-of-the-backend: claude / claude-default, 1 tab", "two"),
    ]

    lines = screen.list_lines("Open", "", choices, [0, 1], 0)

    assert lines[2] != lines[3]
    assert "review-of-the-parser" in lines[2]
    assert "review-of-the-backend" in lines[3]


def test_a_checklist_can_carry_a_box_under_it() -> None:
    """Section 5.5 puts the groups and the extra domains on one screen, which kills a question."""
    box = ("Also allow", "", "space separated")

    ticked, typed = drive(
        "\texample.com\r", lambda: screen.tick("Network", TOOLS, box=box)
    )

    assert ticked == [0, 1, 4]
    assert typed == "example.com"


def test_tab_goes_to_the_box_and_back_to_the_list() -> None:
    ticked, typed = drive(
        "\texample.com\t \r", lambda: screen.tick("Network", TOOLS, box=("Also allow", "", ""))
    )

    assert ticked == [1, 4]  # the space came back to the list and unticked the first row
    assert typed == "example.com"


def test_letters_inside_the_box_stay_text() -> None:
    """`a` and `n` are keys on the list and text in the box, the same as inside the filter."""
    ticked, typed = drive(
        "\tan\r", lambda: screen.tick("Network", TOOLS, box=("Also allow", "", ""))
    )

    assert ticked == [0, 1, 4]
    assert typed == "an"


def test_the_box_and_its_line_are_drawn_under_the_list() -> None:
    lines = screen.tick_lines(
        "Network", "hint", TOOLS, [0, 1, 2, 3, 4], 0, box=("Also allow", "a.com", "space separated")
    )

    assert any("Also allow" in line and "a.com" in line for line in lines)
    assert any("space separated" in line for line in lines)


# --- the confirm ------------------------------------------------------------


POLICY = [
    ("session", "claude-default-7f2a"),
    ("can reach", "12 domains: " + ", ".join(f"host{index}.example.com" for index in range(9))),
    ("can run", "git rg fd, plus /usr/bin:/bin"),
]


def test_the_confirm_puts_the_policy_over_three_buttons() -> None:
    """The only screen that says the whole grant out loud, and the last one before it."""
    lines = screen.confirm_lines_drawn("Launch this sandbox?", POLICY, 0)

    assert lines[0] == "Launch this sandbox?"
    assert any("session" in line and "claude-default-7f2a" in line for line in lines)
    assert lines[-1].strip() == "> [ Launch ]      [ ← Back to the form ]      [ Cancel ]"
    assert [line for line in lines if get_cwidth(line) > screen.WIDTH] == []


def test_a_policy_line_too_long_for_the_screen_wraps_under_its_own_label() -> None:
    """Cutting the domains would be the one place this screen may not be brief."""
    lines = screen.confirm_lines_drawn("Launch this sandbox?", POLICY, 0)
    reached = [line for line in lines if "host8.example.com" in line]

    assert reached  # the last domain is on screen, on a line of its own if it has to be
    assert reached[0].startswith(" " * 12)  # under the label, not beside it


def test_the_confirm_starts_on_launch_because_that_is_the_common_case() -> None:
    """Section 3.1's budget: the same sandbox as last time is enter, then enter."""
    assert drive("\r", lambda: screen.confirm("Launch?", POLICY)) == screen.LAUNCH


def test_the_confirm_moves_between_its_buttons() -> None:
    right = "\x1b[C"
    left = "\x1b[D"

    assert drive(f"{right}\r", lambda: screen.confirm("Launch?", POLICY)) == screen.BACK
    assert drive(f"{right}{right}\r", lambda: screen.confirm("Launch?", POLICY)) == screen.CANCEL
    assert drive(f"{right}{left}\r", lambda: screen.confirm("Launch?", POLICY)) == screen.LAUNCH
    assert drive("ll\r", lambda: screen.confirm("Launch?", POLICY)) == screen.CANCEL


def test_escape_on_the_confirm_is_the_back_button() -> None:
    """One level back is the form, with every answer on it as it was."""
    assert drive(ESC * 2, lambda: screen.confirm("Launch?", POLICY)) == screen.BACK


def test_ctrl_c_cancels_the_confirm_too() -> None:
    with pytest.raises(KeyboardInterrupt):
        drive(CTRL_C, lambda: screen.confirm("Launch?", POLICY))


# --- the text box -----------------------------------------------------------


def test_what_is_typed_comes_back() -> None:
    assert drive("review\r", lambda: screen.type_in("Name", "")) == "review"


def test_the_box_starts_on_the_value_it_was_given() -> None:
    name = "claude-default-7f2a"

    assert drive("\r", lambda: screen.type_in("Name", name)) == name


def test_escape_from_a_box_keeps_what_was_typed() -> None:
    assert drive(f"review{ESC}{ESC}", lambda: screen.type_in("Name", "")) == "review"


def test_a_box_says_whether_escape_was_what_closed_it() -> None:
    """Two screens for one field, so the inner one has to back out to the outer one."""
    assert drive("review\r", lambda: screen.typed_in("Name", "")) == ("review", False)
    assert drive(f"review{ESC}{ESC}", lambda: screen.typed_in("Name", "")) == ("review", True)


def test_a_bad_answer_is_reported_on_the_key_line_and_the_box_stays_open() -> None:
    """huh's rule: one row of chrome, two jobs, never both at once."""
    said: list[str] = []

    def check(text: str) -> str:
        said.append(text)
        return "" if text.startswith("/") else "a shared directory has to be a path"

    assert drive(f"repo\rx{ESC}{ESC}", lambda: screen.type_in("Files", "", check=check)) == "repox"
    assert said == ["repo"]


def test_the_error_goes_as_soon_as_the_answer_changes() -> None:
    """It is the key line's row. Holding it after the answer changed would keep the keys off."""
    box, state = TextArea(text="repo", multiline=False), {"error": "not a path"}

    screen.forget_error(box, state)
    box.text = "repo/x"

    assert state["error"] == ""
    assert screen.type_footer("not a path") == "not a path"
    assert screen.type_footer("") == screen.footer_line(screen.TYPE_KEYS)


def test_a_box_opens_with_the_cursor_after_what_is_in_it() -> None:
    """Typing into a filled box carries on from the value rather than in front of it."""
    assert drive("-2\r", lambda: screen.type_in("Name", "review")) == "review-2"
# --- the screen a failed launch ends on -------------------------------------


def test_the_failure_screen_says_what_went_wrong_and_where_the_log_is() -> None:
    lines = screen.failed_lines("the microVM would not install claude", "/state/paddock.log")

    assert lines[0] == screen.FAILED_TITLE
    assert "the microVM would not install claude" in "\n".join(lines)
    assert "  log: /state/paddock.log" in lines
    assert lines[-1].strip().startswith("> [ ← Back to the form ]")


def test_a_failure_with_no_log_file_names_none() -> None:
    """Nothing to read is better said by leaving the row out than by naming an empty path."""
    lines = screen.failed_lines("no msb on PATH")

    assert not [line for line in lines if line.startswith("  log:")]


def test_a_long_failure_message_wraps_instead_of_running_off_the_screen() -> None:
    lines = screen.failed_lines("word " * 60)

    assert max(get_cwidth(line) for line in lines) <= screen.WIDTH


def test_enter_on_back_returns_to_the_form_and_enter_on_cancel_does_not() -> None:
    """The buttons are on left and right, as they are on the confirm: up and down scroll."""
    assert drive("\r", lambda: screen.failed("nope")) is True
    assert drive(f"{RIGHT}\r", lambda: screen.failed("nope")) is False


def test_escape_off_the_failure_screen_is_the_way_back_like_everywhere_else() -> None:
    assert drive(ESC * 2, lambda: screen.failed("nope")) is True


def test_ctrl_c_cancels_the_popup_from_the_failure_screen_too() -> None:
    with pytest.raises(KeyboardInterrupt):
        drive(CTRL_C, lambda: screen.failed("nope"))


def test_the_failure_message_is_on_the_screen_that_was_drawn() -> None:
    assert "would not install" in drawn(f"{DOWN}\r", lambda: screen.failed("would not install"))


# --- the screen in front of a step that blocks ------------------------------


def test_the_progress_screen_is_the_title_and_the_steps_under_it() -> None:
    lines = screen.progress_lines("Starting review", ["pulling the image", "installing claude"])

    assert lines == ["Starting review", "  pulling the image", "  installing claude"]


def test_a_long_step_wraps_rather_than_losing_its_end() -> None:
    """Nothing is drawn under it, so a row is cheaper than an ellipsis on the reason."""
    lines = screen.progress_lines("t", ["word " * 40])

    assert len(lines) > 2
    assert max(get_cwidth(line) for line in lines) <= screen.WIDTH
    assert "".join(lines[1:]).split() == ["word"] * 40


def test_the_progress_screen_goes_to_stderr_so_stdout_stays_the_pane_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    screen.progress("Starting review", ["pulling the image"])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Starting review\n  pulling the image\n"


# --- everything a small popup has to keep reachable -------------------------

# The smallest popup the design admits: 70% of a small terminal, and then some.
SMALL = (18, 48)


def test_every_policy_line_is_reachable_in_the_smallest_popup() -> None:
    """The grant is the whole point of the screen, so none of it may be off the end."""
    policy = [
        ("can write", "its own workdir, /tmp and /dev/null, plus /Users/someone/work/repo"),
        ("can read", "your disk, except ~/.ssh ~/.aws ~/.gnupg ~/.config/gh"),
        ("can reach", ", ".join(f"host{index}.example.com" for index in range(12))),
        ("can run", "git rg fd jq curl node npm npx uv python3, plus /usr/bin:/bin"),
        ("can see", "its own Claude Code login. No other agent's keys. No skills."),
    ]
    every = screen.policy_lines(policy, SMALL[1])

    seen: set[str] = set()
    for scroll in range(len(every)):
        drawn = screen.confirm_lines_drawn("Launch?", policy, 0, SMALL[1], SMALL[0], scroll)
        seen |= {line for line in drawn if line in every}
        assert any("Launch" in line for line in drawn[-len(drawn) // 3 :])
        assert max(get_cwidth(line) for line in drawn) <= SMALL[1]

    assert seen == set(every)


def test_the_confirm_keeps_its_buttons_whatever_the_scroll(
) -> None:
    policy = [("can reach", ", ".join(f"host{index}.example.com" for index in range(40)))]

    for scroll in (0, 5, 40, 400):
        drawn = screen.confirm_lines_drawn("Launch?", policy, 2, SMALL[1], SMALL[0], scroll)
        assert "Cancel" in drawn[-1]
        assert len(drawn) <= SMALL[0]


def test_a_long_failure_message_is_reachable_to_its_last_word() -> None:
    """The reason a backend gives is at the end of its sentence, so the tail is the point."""
    message = " ".join(f"word{index}" for index in range(80)) + " because npm was refused"
    seen: set[str] = set()

    for scroll in range(60):
        drawn = screen.failed_lines(message, "/state/paddock.log", 0, SMALL[1], SMALL[0], scroll)
        seen |= set(drawn)
        assert drawn[0] == screen.FAILED_TITLE
        assert any("Back to the form" in line or "Back" in line for line in drawn[-3:])

    assert any("refused" in line for line in seen)
    assert any("log:" in line for line in seen)


def test_up_and_down_scroll_the_failure_message_and_left_right_move_the_buttons() -> None:
    long_message = " ".join(f"word{index}" for index in range(200))

    assert drive(f"{DOWN * 3}\r", lambda: screen.failed(long_message)) is True
    assert drive(f"{DOWN * 3}{RIGHT}\r", lambda: screen.failed(long_message)) is False


def test_the_progress_screen_wraps_to_the_terminal_it_prints_into(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """It is printed, not drawn, so the terminal is asked directly rather than assumed."""
    monkeypatch.setattr(screen.shutil, "get_terminal_size", lambda fallback=None: Size(40, 30))

    screen.progress("Starting review", ["the in-guest install needs npm; add the npm preset"])

    lines = capsys.readouterr().err.splitlines()
    assert len(lines) > 2
    assert max(get_cwidth(line) for line in lines) <= 30


def test_the_form_header_keeps_the_profile_when_the_popup_forces_a_choice() -> None:
    """The title says which profile runs and whether it still matches. The path is context."""
    line = screen.header("paddock   claude-default + changes", "in /Users/someone/dev/repo", 40)

    assert line.startswith("paddock   claude-default + changes")
    assert get_cwidth(line) <= 40


def test_the_form_header_keeps_both_ends_when_there_is_room() -> None:
    line = screen.header("paddock   custom", "in /work", 40)

    assert line.startswith("paddock   custom")
    assert line.endswith("in /work")
