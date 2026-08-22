"""Where paddock writes what it did, at what level, and what never reaches the log."""

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from paddock import log, sessions
from paddock.profiles import Profile
from tests.conftest import FakeClient

# Planted in the Keychain, in a credential file and in a proxy URL, then looked for in the log.
FAKE_TOKEN = "sk-ant-oat01-plantedfaketoken-0123456789"


def handlers() -> list[logging.Handler]:
    """paddock's own handlers. pytest hangs its log capture on the same logger."""
    return [
        handler
        for handler in logging.getLogger(log.ROOT).handlers
        if isinstance(handler, RotatingFileHandler) or type(handler) is logging.StreamHandler
    ]


def file_handler() -> RotatingFileHandler:
    found = [handler for handler in handlers() if isinstance(handler, RotatingFileHandler)]
    assert len(found) == 1
    return found[0]


def stream_handler() -> logging.Handler:
    found = [handler for handler in handlers() if not isinstance(handler, RotatingFileHandler)]
    assert len(found) == 1
    return found[0]


# --- where the log lives ---------------------------------------------------


def test_the_log_lives_under_the_state_dir(state_dir: Path) -> None:
    assert log.log_path() == state_dir / "logs" / "paddock.log"


def test_the_log_file_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADDOCK_LOG_FILE", str(tmp_path / "elsewhere.log"))

    assert log.log_path() == tmp_path / "elsewhere.log"


def test_an_empty_log_file_override_falls_back_to_the_state_dir(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty value must not resolve to the current directory."""
    monkeypatch.setenv("PADDOCK_LOG_FILE", "")

    assert log.log_path() == state_dir / "logs" / "paddock.log"


def test_a_pane_log_sits_in_the_run_dir(tmp_path: Path) -> None:
    assert log.pane_log_path(tmp_path) == tmp_path / "pane.log"


# --- setting the handlers up -----------------------------------------------


def test_setup_attaches_one_file_handler_and_one_stream_handler() -> None:
    log.setup()

    assert len(handlers()) == 2


def test_setup_is_idempotent() -> None:
    """Every entry point may call it, and the popup runs one process per launch."""
    log.setup()
    log.setup()
    log.setup()

    assert len(handlers()) == 2


def test_the_file_takes_everything_and_the_popup_only_warnings() -> None:
    """The chooser is a popup: a debug line on stderr is in the user's face."""
    log.setup()

    assert logging.getLogger(log.ROOT).level == logging.DEBUG
    assert file_handler().level == logging.DEBUG
    assert stream_handler().level == logging.WARNING


def test_the_file_rotates_at_a_megabyte_and_keeps_three() -> None:
    log.setup()

    assert file_handler().maxBytes == 1_000_000
    assert file_handler().backupCount == 3


def test_the_log_does_not_reach_the_root_logger() -> None:
    """paddock's log is paddock's own, not whatever else configured logging in this process."""
    log.setup()

    assert logging.getLogger(log.ROOT).propagate is False


@pytest.mark.parametrize(
    ("wanted", "level"),
    [
        ("debug", logging.DEBUG),
        ("DEBUG", logging.DEBUG),
        ("info", logging.INFO),
        ("error", logging.ERROR),
    ],
)
def test_paddock_log_raises_what_the_popup_sees(
    wanted: str, level: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PADDOCK_LOG", wanted)

    log.setup()

    assert stream_handler().level == level


@pytest.mark.parametrize("wanted", ["", "   ", "shouty"])
def test_an_empty_or_unknown_level_leaves_the_popup_at_warnings(
    wanted: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PADDOCK_LOG", wanted)

    log.setup()

    assert stream_handler().level == logging.WARNING


def test_a_state_dir_that_cannot_be_written_still_leaves_a_working_logger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No log file is worth a launcher that will not start."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory\n")
    monkeypatch.setenv("PADDOCK_LOG_FILE", str(blocked / "paddock.log"))

    log.setup()

    assert len(handlers()) == 1
    log.get_logger("paddock.demo").info("still fine")


# --- what a line looks like ------------------------------------------------


def test_a_module_logger_hangs_off_the_paddock_root() -> None:
    assert log.get_logger("paddock.backends.srt").name == "paddock.backends.srt"
    assert log.get_logger("paddock.tui").name == "paddock.tui"
    # A bare name is still paddock's, so a caller cannot log outside the file by accident.
    assert log.get_logger("demo").name == "paddock.demo"


def test_a_line_says_when_at_what_level_and_from_which_module() -> None:
    log.setup()

    log.get_logger("paddock.demo").info("hello %s", "world")

    line = log.log_path().read_text().splitlines()[-1]
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} INFO paddock\.demo hello world", line
    ), line


def test_context_is_name_equals_value_pairs() -> None:
    written = log.context(session="a1b2", run_dir=Path("/runs/one"))

    assert written == "session=a1b2 run_dir=/runs/one"


def test_context_leaves_out_what_was_not_set() -> None:
    assert log.context(session="a1b2", cwd="", label=None, panes=0) == "session=a1b2 panes=0"


# --- reading it back -------------------------------------------------------


def test_tail_gives_the_last_lines(tmp_path: Path) -> None:
    path = tmp_path / "some.log"
    path.write_text("".join(f"line {number}\n" for number in range(10)))

    assert log.tail(path, 3) == "line 7\nline 8\nline 9\n"


def test_tail_of_a_short_file_is_the_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "some.log"
    path.write_text("only\n")

    assert log.tail(path, 40) == "only\n"


def test_tail_of_a_file_that_is_not_there_says_so(tmp_path: Path) -> None:
    missing = tmp_path / "nothing.log"

    assert str(missing) in log.tail(missing, 40)


# --- the secrets rule ------------------------------------------------------


def test_a_full_launch_writes_no_secrets_to_the_log(
    which: dict[str, str],
    client: FakeClient,
    keychain: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPEC §4.3: tokens, credential contents and proxy URLs are never written down.

    The launch gets everything a real one has: a token in the Keychain that the config dir
    exports to a file, a config file that is copied, a skill that is linked, and a proxy
    URL with a password in it.
    """
    home = tmp_path / "home"
    (home / ".claude" / "skills" / "writing").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {}, "token": FAKE_TOKEN}))
    keychain["Claude Code-credentials"] = json.dumps({"claudeAiOauth": {"token": FAKE_TOKEN}})
    # srt puts the proxy password in the URL, so the whole URL is a secret.
    monkeypatch.setenv("HTTPS_PROXY", f"http://srt.local:{FAKE_TOKEN}@127.0.0.1:9000")
    monkeypatch.setenv("PADDOCK_LOG", "debug")
    log.setup()

    sessions.launch(Profile(name="claude-default", tools=["git"], skills=["writing"]))

    written = log.log_path().read_text()
    assert "session created" in written  # the launch really was logged
    assert FAKE_TOKEN not in written
    assert "://srt." not in written
    assert "claudeAiOauth" not in written


def test_the_command_is_logged_by_length_not_by_content(
    which: dict[str, str], client: FakeClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composed command carries every kept environment value, so none of it is written."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PADDOCK_LOG", "debug")
    log.setup()

    sessions.launch(Profile(tools=[]))

    written = log.log_path().read_text()
    assert "env -i" not in written
    assert re.search(r"command=\d+ chars", written), written


def test_an_environment_value_never_reaches_the_log(
    which: dict[str, str], client: FakeClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """herdr is given `--env NAME=VALUE`, and the value can be anything."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    log.setup()

    log.get_logger("paddock.demo").debug(
        "herdr %s", log.redact_env(("tab", "create", "--env", f"TOKEN={FAKE_TOKEN}", "--focus"))
    )

    written = log.log_path().read_text()
    assert FAKE_TOKEN not in written
    assert "TOKEN=..." in written
