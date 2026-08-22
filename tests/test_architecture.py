"""The layering, enforced: presentation, sessions, backends, and the seams they share.

SPEC §10. Reading the imports is the whole test: a rule nobody can break by accident is
worth more than a paragraph asking people not to.
"""

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "paddock"

# The presentation layer: it asks the questions and hands out a plan.
PRESENTATION = ["paddock.tui", "paddock.cli"]

# The one door to a running sandbox. Presentation goes through it; backends sit behind it.
DOOR = "paddock.sessions"

# The backend modules `sessions` is allowed to reach. A new backend adds a line here.
BACKENDS = ["paddock.backends.srt"]

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


def imports(path: Path) -> set[str]:
    """The paddock modules one file imports, by dotted name.

    `from paddock.backends import srt` counts as importing `paddock.backends.srt`, which
    is the edge that matters: the package alone says nothing.
    """
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return {name for name in found if name == "paddock" or name.startswith("paddock.")}


def edges() -> list[tuple[str, str]]:
    """Every (importer, imported) pair inside paddock."""
    return [
        (name, imported) for name, path in modules().items() for imported in sorted(imports(path))
    ]


def backend_modules() -> list[str]:
    return [name for name in modules() if name.startswith("paddock.backends.")]


def test_the_modules_this_test_names_are_all_there() -> None:
    """A renamed module must break this test, not quietly stop being checked."""
    present = set(modules())

    for name in [*PRESENTATION, DOOR, *BACKENDS, *LEAVES]:
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

    assert imports(offender) == {"paddock", "paddock.tui", "paddock.sessions"}
