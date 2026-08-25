"""The install script: what it runs, and what it says when something is missing.

Every run here uses stub commands on `PATH`. Nothing reaches the network.
`start_new_session` drops the controlling terminal, so the script's prompt sees
no `/dev/tty` and takes the non-interactive path instead of blocking.
"""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"
REPO = "git+https://github.com/paddock-sh/paddock"


@pytest.fixture
def stubs(tmp_path: Path, real_subprocess: None):
    """Makes stub commands and returns (make_stub, run, log_of)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()

    def make_stub(name: str, body: str = "") -> None:
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{logs}/{name}.log"\n{body}\n')
        stub.chmod(0o755)

    def run(**env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", str(SCRIPT)],
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), **env},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=60,
        )

    def log_of(name: str) -> str:
        path = logs / f"{name}.log"
        return path.read_text() if path.exists() else ""

    return make_stub, run, log_of


def test_installs_paddock_with_uv(stubs) -> None:
    make_stub, run, log_of = stubs
    for name in ("uv", "node", "paddock"):
        make_stub(name)

    result = run()

    assert result.returncode == 0, result.stderr
    assert f"tool install --force {REPO}" in log_of("uv")
    assert "paddock init" in result.stdout
    assert "prefix+c" in result.stdout


def test_paddock_ref_installs_that_branch(stubs) -> None:
    make_stub, run, log_of = stubs
    for name in ("uv", "node", "paddock"):
        make_stub(name)

    result = run(PADDOCK_REF="develop")

    assert result.returncode == 0, result.stderr
    assert f"tool install --force {REPO}@develop" in log_of("uv")


def test_running_it_twice_does_the_same_thing(stubs) -> None:
    make_stub, run, log_of = stubs
    for name in ("uv", "node", "paddock"):
        make_stub(name)

    first = run()
    second = run()

    assert (first.returncode, second.returncode) == (0, 0)
    lines = [line for line in log_of("uv").splitlines() if line]
    assert len(lines) == 2
    assert lines[0] == lines[1]


def test_missing_uv_stops_before_installing_anything(stubs) -> None:
    make_stub, run, log_of = stubs
    for name in ("node", "paddock"):
        make_stub(name)
    make_stub("curl")

    result = run()

    assert result.returncode != 0
    assert "uv" in result.stderr
    assert "PADDOCK_YES" in result.stderr
    assert log_of("curl") == ""


def test_padddock_yes_fetches_the_official_uv_installer(stubs) -> None:
    make_stub, run, log_of = stubs
    for name in ("node", "paddock"):
        make_stub(name)
    make_stub("curl")  # writes nothing to stdout, so the piped installer is a no-op

    result = run(PADDOCK_YES="1")

    assert "https://astral.sh/uv/install.sh" in log_of("curl")
    # The stub installs no uv, so the script has to notice and say so.
    assert result.returncode != 0
    assert "uv" in result.stderr


def test_missing_node_is_a_warning_not_a_failure(stubs) -> None:
    make_stub, run, log_of = stubs
    for name in ("uv", "paddock"):
        make_stub(name)

    result = run()

    assert result.returncode == 0, result.stderr
    assert "node" in result.stderr.lower()
    assert f"tool install --force {REPO}" in log_of("uv")


def test_paddock_off_the_path_is_a_warning(stubs) -> None:
    make_stub, run, log_of = stubs
    for name in ("uv", "node"):
        make_stub(name)

    result = run()

    assert result.returncode == 0, result.stderr
    assert "PATH" in result.stderr
