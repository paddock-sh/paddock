"""The layering, enforced: presentation, sessions, backends, and the seams they share.

SPEC §10. Reading the imports is the whole test: a rule nobody can break by accident is
worth more than a paragraph asking people not to.
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "paddock"

# The presentation layer: it asks the questions and hands out a plan. `screen` draws, `tui`
# decides what is drawn, and `cli` is the only one of the three that acts on the answer.
PRESENTATION = ["paddock.tui", "paddock.screen", "paddock.cli"]

# The one door to a running sandbox. Presentation goes through it; backends sit behind it.
DOOR = "paddock.sessions"

# `paddock run`: the same session in the terminal it was typed in (SPEC §11). It is paddock
# without herdr, so the one thing it may never reach is the module that shells out to herdr.
STANDALONE = "paddock.standalone"

# The backend modules `sessions` is allowed to reach. A new backend adds a line here.
# In the order the import graph reports them, which is sorted by name.
BACKENDS = ["paddock.backends.microsandbox", "paddock.backends.srt"]

# Leaves anything may use: the process seam, and the log.
LEAVES = ["paddock.herdr_client", "paddock.log"]


def modules() -> dict[str, Path]:
    """Every paddock module, by dotted name."""
    found = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        parts = path.relative_to(PACKAGE.parent).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        found[".".join(parts)] = path
    return found


def imports(name: str, path: Path) -> set[str]:
    """The paddock modules one file imports, by dotted name.

    `from paddock.backends import srt` counts as importing `paddock.backends.srt`, which
    is the edge that matters: the package alone says nothing. A relative import is resolved
    against the module's own package, so `from .. import sessions` is the same edge as
    spelling it out.
    """
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module if node.level == 0 else _relative(name, path, node)
            if not base:
                continue
            found.add(base)
            found |= {f"{base}.{alias.name}" for alias in node.names}
    return {name for name in found if name == "paddock" or name.startswith("paddock.")}


def _relative(name: str, path: Path, node: ast.ImportFrom) -> str:
    """What `from ..x import y` means, written out, inside the module called `name`."""
    package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
    parts = package.split(".")
    base = ".".join(parts[: len(parts) - (node.level - 1)])
    return f"{base}.{node.module}" if node.module else base


def edges() -> list[tuple[str, str]]:
    """Every (importer, imported) pair inside paddock."""
    return [
        (name, imported)
        for name, path in modules().items()
        for imported in sorted(imports(name, path))
    ]


def backend_modules() -> list[str]:
    return [name for name in modules() if name.startswith("paddock.backends.")]


def test_the_modules_this_test_names_are_all_there() -> None:
    """A renamed module must break this test, not quietly stop being checked."""
    present = set(modules())

    for name in [*PRESENTATION, DOOR, STANDALONE, *BACKENDS, *LEAVES]:
        assert name in present, name


def test_a_backend_imports_nothing_above_it() -> None:
    """A backend enforces a launch. How the launch was chosen is none of its business."""
    above = {*PRESENTATION, DOOR}

    broken = [
        f"{importer} imports {imported}"
        for importer, imported in edges()
        if importer in backend_modules() and imported in above
    ]

    assert broken == []


def test_the_presentation_layer_imports_no_backend() -> None:
    """The TUI and the CLI ask sessions for a session. sessions picks the backend."""
    broken = [
        f"{importer} imports {imported}"
        for importer, imported in edges()
        if importer in PRESENTATION and imported.startswith("paddock.backends")
    ]

    assert broken == []


def test_sessions_reaches_backends_only_through_its_own_list() -> None:
    """One place names the backends, so adding one is one edit and not a search."""
    reached = [
        imported
        for importer, imported in edges()
        if importer == DOOR
        and imported.startswith("paddock.backends.")
        and imported in modules()
    ]

    assert reached == BACKENDS


def test_the_standalone_run_reaches_no_herdr() -> None:
    """`paddock run` is paddock without herdr, and an import is how that would creep back."""
    broken = [
        f"{importer} imports {imported}"
        for importer, imported in edges()
        if importer == STANDALONE and imported.startswith("paddock.herdr_client")
    ]

    assert broken == []


def test_the_standalone_run_still_goes_through_the_one_door() -> None:
    """It runs the session in this terminal. Which backend runs it stays sessions' business."""
    reached = [
        imported
        for importer, imported in edges()
        if importer == STANDALONE and imported in BACKENDS
    ]

    assert reached == []


def test_the_shared_leaves_import_no_layer() -> None:
    """A leaf both sides use cannot import either of them, or the layering is a circle."""
    layers = {*PRESENTATION, DOOR, *backend_modules()}

    broken = [
        f"{importer} imports {imported}"
        for importer, imported in edges()
        if importer in LEAVES and imported in layers
    ]

    assert broken == []


def test_both_ways_of_writing_an_import_are_seen(tmp_path: Path) -> None:
    """`from paddock import sessions` is the same edge as `import paddock.sessions`."""
    offender = tmp_path / "srt.py"
    offender.write_text("import paddock.tui\nfrom paddock import sessions\nimport json\n")

    assert imports("paddock.backends.srt", offender) == {
        "paddock",
        "paddock.tui",
        "paddock.sessions",
    }


def test_a_relative_import_is_the_same_edge_written_shorter(tmp_path: Path) -> None:
    """`from .. import sessions` must not be a way round the rule."""
    offender = tmp_path / "srt.py"
    offender.write_text("from .. import sessions\nfrom ..tui import choose\nfrom . import msb\n")

    assert imports("paddock.backends.srt", offender) == {
        "paddock",
        "paddock.sessions",
        "paddock.tui",
        "paddock.tui.choose",
        "paddock.backends",
        "paddock.backends.msb",
    }


def test_a_relative_import_inside_a_package_starts_from_that_package(tmp_path: Path) -> None:
    """In `paddock/backends/__init__.py`, one dot is `paddock.backends`, not `paddock`."""
    offender = tmp_path / "__init__.py"
    offender.write_text("from . import srt\nfrom .. import sessions\n")

    assert imports("paddock.backends", offender) == {
        "paddock.backends",
        "paddock.backends.srt",
        "paddock",
        "paddock.sessions",
    }
