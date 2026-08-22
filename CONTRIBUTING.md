# Contributing to paddock

## Branch model

| Branch | Meaning |
| --- | --- |
| `main` | Stable. Only receives promotions from `develop`, via PR. |
| `develop` | Integration. Epic branches merge here. |

Nothing is committed directly to either.

### Epics

Every major change is an EPIC with a `snake_case` slug. The first is
`sandbox_core_launcher`.

The epic branch is named exactly the slug, not `epic/<slug>`, and is cut from
`develop`:

```sh
git checkout develop && git pull
git checkout -b sandbox_core_launcher
git push -u origin sandbox_core_launcher
```

It merges back into `develop` by PR, after review.

### Feature branches

Work inside an epic happens on `<slug>-<feature>`:

```sh
git checkout sandbox_core_launcher && git pull
git checkout -b sandbox_core_launcher-profiles_and_registry
```

The separator is a dash, not a slash: git will not hold both a ref and a directory
at the same path, so `sandbox_core_launcher/<feature>` cannot exist while the epic
branch `sandbox_core_launcher` does.

Feature PRs target the epic branch, never `develop` or `main`. Title them with
the slug in brackets:

```
[sandbox_core_launcher] Add Profile dataclass and JSON round-trip
```

### Promotion path

```
<slug>-<feature>  --PR-->  <slug>  --PR-->  develop  --PR-->  main
```

`develop` is promoted to `main` by PR when it is stable, not on a schedule.

## Development process

### TDD

Write the tests first, with `pytest`. A PR that adds implementation without
tests will be sent back.

**Sandbox backends are mocked.** CI runs on `ubuntu-latest` only (no macOS
runners, a cost decision), so no test may need Seatbelt, bubblewrap, `srt`,
`msb`, or a running herdr server. Assert on the generated settings JSON, shim dir
contents and command strings instead.

This is cheap because of how the code is arranged: only `herdr_client.py` shells
out to `herdr`, only a backend shells out to its own runtime (`srt`, `msb`), and
everything else is plain functions over a `Profile`.

### Diagrams

Every feature PR adds or updates PlantUML diagrams in `docs/diagrams/` (`*.puml`)
for the components it touches. Component diagrams for modules, sequence diagrams
for flows. A PR that leaves the diagrams stale is incomplete, the same as one
that leaves tests stale.

Three diagrams are seeded:

- `architecture.puml` holds the components: keybinding popup, chooser TUI, sessions,
  profiles, agent registry, `Backend` interface, `srt` / `microsandbox`, herdr
  CLI.
- `launch_sequence.puml` is a sandboxed launch end to end, from `prefix+c` to
  `herdr pane run`.
- `scoping_model.puml` covers sandbox sessions: one workspace with local tabs, a
  two-tab group on a microVM session, and a tab on an srt session.

Name the real modules and commands, label the arrows with what crosses them, and
mark trust boundaries. If a diagram needs a paragraph to explain it, redraw it.

Only the `.puml` sources are committed. CI runs `plantuml -checkonly`, so a
syntax error fails the build. Locally:

```sh
brew install plantuml          # or: sudo apt-get install -y plantuml
plantuml -checkonly docs/diagrams/*.puml
plantuml -tsvg docs/diagrams/architecture.puml   # to look at one
```

### Local loop

```sh
uv sync --dev
uv run ruff check .
uv run pytest -q
```

All three must be clean before you push. `uv.lock` is committed: if your change
moves dependencies, commit the updated lockfile with it.

### CI

`.github/workflows/ci.yml` runs on every push to `main` / `develop` and on every
PR. Two jobs: `test` (ruff and pytest on Python 3.11 and 3.13) and `diagrams`
(`plantuml -checkonly`, skipped when there are no `.puml` files). CI must be
green before a merge, including the epic → `develop` and `develop` → `main`
promotions.

### Review

Every PR goes through an automated review loop before merge: review, fix on the
same branch, repeat until clean. Merge only when the review is satisfied and CI
is green.

Reviews check correctness and tests, and also flag **overengineering** and
**wordy documentation**, per the principles below.

## Design principles

**Keep it simple.** Write the simplest code that meets the requirement. This is a
launcher: it asks some questions, writes some JSON, and runs two commands.

- **No speculative abstractions.** Add an interface when a second implementation
  exists, not before. The `Backend` interface earns its place because v1 needs it
  to keep srt logic out of the chooser.
- **No config options nobody asked for.** Every option is something to explain,
  test, and pick a wrong default for. Add one on request, not on a hunch.
- **No framework-building.** No plugin systems, no base classes waiting for
  subclasses. The agent registry is data-driven because users really do need to
  add agents; that is the exception.
- **Unbuilt design stays out of the code.** Portless URLs, agents inside the
  microVM guest and workspace bindings are in [docs/SPEC.md](docs/SPEC.md) and
  the diagrams. They are not stubbed, not `NotImplementedError`-ed, and not
  allowed for by spare parameters. A spec holds a future design; dead code does
  not. One carve-out: a **data-schema field** the SPEC already fixes may ship
  early. The agent registry's
  `image` ([§2.2](docs/SPEC.md#22-microsandbox-msb-registered-as-msb)) meant user
  files survived the second backend without a migration, and that backend reads
  it now. Spare *code* paths stay banned.
- **Small modules, plain functions.** Prefer a function to a class and a class to
  a hierarchy. Most of this code should take a `Profile` and return a string or a
  dict, which is also what makes it testable without a sandbox.
- **The layers hold.** `tui.py` and `cli.py` never import a backend, and no
  backend imports them or `sessions.py`: `sessions` is the one door
  ([docs/SPEC.md §10](docs/SPEC.md#10-architecture-layers-and-the-one-door-rule)).
  `tests/test_architecture.py` reads the imports and fails on any edge that
  breaks it, so this is enforced, not agreed.
- **Few dependencies.** `prompt_toolkit` is the only runtime dependency, and the
  chooser's screens are written against it directly. Argue for a second one in
  the PR. The standard library covers JSON, paths, subprocess and dataclasses.

**Documentation is concise and plain English.**

- Short sentences. Everyday words over jargon. No filler.
- **No em dashes anywhere in the repo**, docs, comments, diagrams and test names
  alike. Rewrite the sentence around it: a full stop, a comma, a colon, or
  brackets. A double hyphen is not a substitute.
- The README says what paddock is and how to use it, in as few words as stay
  clear.
- SPEC sections lead with the point, then give the detail.
- Comments and docstrings: one line on what or why, and only where the code
  cannot say it itself.

Removing an abstraction or cutting a paragraph is as legitimate a change as
adding a feature.

## PR descriptions and comments

Three headings, a few lines each:

**What**: what changed. **Why**: why it needed changing. **Tests**: what
proves it works.

Plain English, no jargon, readable at a glance. `.github/pull_request_template.md`
holds the skeleton. The same standard applies to comments on a PR.

## Commit messages

Conventional prefixes (`feat:`, `fix:`, `chore:`, `docs:`, `test:`,
`refactor:`). Agent-authored commits carry:

```
Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
```

## Scope discipline

This repository is a launcher, not an agent framework. Two rules hold:

1. **Permissions are always an active choice.** No feature may add an implicit
   "allow everything" path or silently widen a profile.
2. **Be honest about the trust model.** Layer 2 is enforced by the agent on
   itself and must never be described in code, docs or UI as a boundary. Known
   bypasses, such as absolute paths defeating the `PATH` shim dir, are
   documented, not omitted. See
   [docs/SPEC.md §4](docs/SPEC.md#4-three-enforcement-layers).
