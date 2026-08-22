"""The screens, driven by real key presses with no terminal at all.

Two halves, like the module: the lines each screen draws are plain functions and are asserted
as text, and the keys are pressed for real through a pipe, which is a better test than faking
a prompt library.
"""

from collections.abc import Callable

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from paddock import screen

# Escape as the parser sees it. A lone one waits for the escape-sequence timeout when it ends
# the input, so a screen that closes on escape is sent two.
ESC = "\x1b"
CTRL_C = "\x03"
DOWN, UP = "\x1b[B", "\x1b[A"


def drive(keys: str, show: Callable[[], object]) -> object:
    """Run one screen with the keys already typed."""
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        with create_app_session(input=pipe, output=DummyOutput()):
            return show()


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

TOOLS = [("git", True), ("rg", True), ("fd", False), ("jq", False), ("curl", True)]


# --- the lines a screen draws ----------------------------------------------


def test_the_key_line_truncates_rather_than_wraps() -> None:
    """A wrapped footer would push the layout around, which is the one thing it may not do."""
    line = screen.footer_line(["enter done", "esc back"])
    long = screen.footer_line([f"key {index} does something" for index in range(9)])

    assert line == "enter done   esc back"
    assert len(long) == screen.WIDTH
    assert long.endswith("...")


def test_a_line_with_two_ends_keeps_the_right_one() -> None:
    line = screen.spread("paddock   claude-default", "in ~/dev/paddock")

    assert line.startswith("paddock   claude-default")
    assert line.endswith("in ~/dev/paddock")
    assert len(line) == screen.WIDTH


def test_a_long_left_end_is_cut_short() -> None:
    line = screen.spread("x" * 200, "in ~/dev")

    assert len(line) == screen.WIDTH
    assert "..." in line
    assert line.endswith("in ~/dev")


def test_a_hint_is_always_the_same_number_of_lines() -> None:
    """A longer hint must not move the buttons under it."""
    assert len(screen.wrapped("short", 3)) == 3
    assert len(screen.wrapped("word " * 60, 3)) == 3
    assert screen.wrapped("", 3) == ["", "", ""]


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


def test_a_list_shows_a_rule_and_the_hint_of_the_row_you_are_on() -> None:
    lines = screen.list_lines("Profile", "3 saved", PROFILES, [0, 1, 2], 1, rule_after=1)

    assert lines[0] == screen.spread("Profile", "3 saved")
    assert "    claude-default" in lines[2]
    assert "  > offline-shell" in lines[3]
    assert lines[4].strip().startswith("---")
    assert any("Shell, 4 tools, no network" in line for line in lines)


def test_a_checklist_counts_what_is_ticked() -> None:
    hint = "Ticked binaries are on the PATH."

    lines = screen.tick_lines("Tools it can run", hint, TOOLS, [0, 1, 2, 3, 4], 0)

    assert lines[0] == screen.spread("Tools it can run", "3 of 5 ticked")
    assert "> [x] git" in lines[6]
    assert any("[ ] fd" in line for line in lines)


def test_the_key_list_names_the_two_promises() -> None:
    """Escape never loses an answer and ctrl-c always cancels, so both are on the list."""
    lines = screen.key_lines()

    assert any(line.startswith("  esc") for line in lines)
    assert any(line.startswith("  ctrl-c") for line in lines)
    assert any("1 to 8" in line for line in lines)


# --- the form ---------------------------------------------------------------


def test_enter_on_a_field_opens_it() -> None:
    assert drive(f"{DOWN}{DOWN}\r", lambda: screen.form("p", "", FIELDS)) == (screen.OPEN, 2)


def test_k_and_j_move_the_way_the_arrows_do() -> None:
    assert drive("jjjk\r", lambda: screen.form("p", "", FIELDS)) == (screen.OPEN, 2)


def test_a_digit_jumps_straight_to_a_field() -> None:
    """The form is a fixed list of eight that never reorders, so a digit cannot lie."""
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


def test_ctrl_c_cancels_from_any_screen() -> None:
    for show in (
        lambda: screen.form("p", "", FIELDS),
        lambda: screen.pick("Profile", PROFILES),
        lambda: screen.tick("Tools", TOOLS),
        lambda: screen.type_in("Name", ""),
    ):
        with pytest.raises(KeyboardInterrupt):
            drive(CTRL_C, show)


def test_the_key_list_goes_over_the_screen_and_comes_off_again() -> None:
    assert drive("?q\r", lambda: screen.form("p", "", FIELDS)) == (screen.OPEN, 0)


# --- the list picker --------------------------------------------------------


def test_a_list_gives_back_what_was_chosen() -> None:
    assert drive(f"{DOWN}\r", lambda: screen.pick("Profile", PROFILES)) == 1


def test_backing_out_of_a_list_chooses_nothing() -> None:
    """Not a cancel: the caller keeps whatever value it had."""
    assert drive(ESC * 2, lambda: screen.pick("Profile", PROFILES)) is None


def test_typing_on_a_list_never_filters_it() -> None:
    """Bare typing filtering would cost every letter as a shortcut, on every screen."""
    assert drive("j\r", lambda: screen.pick("Profile", PROFILES)) == 1


def test_the_filter_key_narrows_a_list_and_keeps_the_real_index() -> None:
    assert drive("/rev\r\r", lambda: screen.pick("Profile", PROFILES)) == 2


def test_escape_leaves_the_filter_and_the_list_stands() -> None:
    assert drive(f"/rev{ESC}\r", lambda: screen.pick("Profile", PROFILES)) == 0


def test_a_filter_that_matches_nothing_takes_nothing() -> None:
    assert drive(f"/zzz\r\r{ESC}{ESC}", lambda: screen.pick("Profile", PROFILES)) is None


def test_the_filter_key_is_advertised_only_on_a_list_worth_filtering() -> None:
    assert screen.FILTER_KEY not in screen.pick_footer(3)
    assert screen.FILTER_KEY in screen.pick_footer(9)


# --- the checklist ----------------------------------------------------------


def test_space_ticks_and_enter_is_done() -> None:
    assert drive(f"{DOWN}{DOWN} \r", lambda: screen.tick("Tools", TOOLS)) == [0, 1, 2, 4]


def test_escape_from_a_checklist_keeps_the_ticks() -> None:
    """The promise: escape closes what is open and never loses an answer."""
    assert drive(f" {ESC}{ESC}", lambda: screen.tick("Tools", TOOLS)) == [1, 4]


def test_all_and_none_are_one_key_each() -> None:
    assert drive("a\r", lambda: screen.tick("Tools", TOOLS)) == [0, 1, 2, 3, 4]
    assert drive("n\r", lambda: screen.tick("Tools", TOOLS)) == []


def test_all_and_none_reach_only_what_is_on_screen() -> None:
    """A filter narrows what a key does. It must not throw away a tick you cannot see."""
    assert drive("/fd\ra\r", lambda: screen.tick("Tools", TOOLS)) == [0, 1, 2, 4]
    assert drive("/git\rn\r", lambda: screen.tick("Tools", TOOLS)) == [1, 4]


def test_a_filtered_checklist_ticks_the_row_it_shows() -> None:
    assert drive("/fd\r \r", lambda: screen.tick("Tools", TOOLS)) == [0, 1, 2, 4]


# --- the text box -----------------------------------------------------------


def test_what_is_typed_comes_back() -> None:
    assert drive("review\r", lambda: screen.type_in("Name", "")) == "review"


def test_the_box_starts_on_the_value_it_was_given() -> None:
    name = "claude-default-7f2a"

    assert drive("\r", lambda: screen.type_in("Name", name)) == name


def test_escape_from_a_box_keeps_what_was_typed() -> None:
    assert drive(f"review{ESC}{ESC}", lambda: screen.type_in("Name", "")) == "review"


def test_a_bad_answer_is_reported_on_the_key_line_and_the_box_stays_open() -> None:
    """huh's rule: one row of chrome, two jobs, never both at once."""
    said: list[str] = []

    def check(text: str) -> str:
        said.append(text)
        return "" if text.startswith("/") else "a shared directory has to be a path"

    assert drive(f"repo\rx{ESC}{ESC}", lambda: screen.type_in("Files", "", check=check)) == "repox"
    assert said == ["repo"]
