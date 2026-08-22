# Releasing paddock

One tag cuts one release. The tag is the trigger: everything else is automatic.

## How people install it

```sh
curl -LsSf https://raw.githubusercontent.com/desquaredp/paddock/main/install.sh | sh
```

`install.sh` at the repo root installs uv if it is missing, then runs
`uv tool install --force git+https://github.com/desquaredp/paddock`. It is safe
to run twice. `PADDOCK_REF=<branch|tag>` installs a different ref, and
`PADDOCK_YES=1` answers the prompts for unattended installs.

The raw GitHub URL is the interim home. It works on a public repo with nothing
to set up, and a friendlier domain can front the same file later without
touching the script. The URL points at `main`, so the installer people run is
whatever is on `main`: treat a change to `install.sh` as a release in itself.

That means the tag flow below is about the GitHub release and PyPI, not about
how most people get paddock today.

Both paths are drawn in
[`diagrams/release_flow.puml`](diagrams/release_flow.puml).

## Cut a release

1. Start from a green `main`. Only promoted code is released.

2. Bump the version in two places, in a PR like any other change:

   - `pyproject.toml`: `version = "X.Y.Z"`
   - `paddock/__init__.py`: `__version__ = "X.Y.Z"`

   `tests/test_smoke.py` pins the number and checks the two files agree, so
   update it in the same commit. A mismatch fails the build.

3. Merge, then tag the merge commit and push the tag:

   ```sh
   git checkout main && git pull
   git tag v0.2.0
   git push origin v0.2.0
   ```

   The tag is `vX.Y.Z`. The version strings are `X.Y.Z`, with no `v`.

## What the workflow does

`.github/workflows/release.yml` runs on the tag push, in three jobs:

| Job | What it does |
| --- | --- |
| `build` | Runs the tests, `uv build` for the sdist and wheel, checks the tag matches the built version, and runs `twine check` on both files. |
| `github-release` | Creates the GitHub release from the tag with generated notes, and attaches both files. |
| `pypi` | Publishes to PyPI, if PyPI is turned on. See below. |

A failed check stops the release before anything is published. Nothing is
deleted or overwritten on a rerun: fix the problem, bump the version and tag
again.

## Try it without releasing

Run the workflow by hand from the Actions tab with **Run workflow**. The
`dry_run` input defaults to true, which builds and checks the artifacts and
publishes nothing. Any ref can be picked, but GitHub only offers the button once
`release.yml` is on `main`, so the first dry run has to wait for this to be
promoted there.

To rerun a real release, for example after a transient failure, dispatch it on
the tag with `dry_run` set to false. Do that only when the GitHub release does
not exist yet, since creating it twice fails.

## Turn on PyPI publishing

The `pypi` job is off until two things are done. It reports that it skipped and
the release still succeeds.

1. **Claim the name.** `paddock` on PyPI is taken by an unrelated project (an
   iRacing SDK, last released in 2020), so the distribution name has to be
   settled first. `paddock-cli`, `herdr-paddock` and `paddock-herdr` are all
   free. Changing `name` in `pyproject.toml` changes nothing else: the import
   package and the command stay `paddock`.

2. **Add a trusted publisher** on PyPI, under the project's *Publishing*
   settings. For a name that does not exist yet, use *Your account* >
   *Publishing* to add a pending publisher:

   | Field | Value |
   | --- | --- |
   | Owner | `desquaredp` |
   | Repository | `paddock` |
   | Workflow | `release.yml` |
   | Environment | leave empty |

   Trusted publishing swaps an API token for a short-lived OIDC identity, which
   is why the job asks for `id-token: write` and why there is no secret to store
   or rotate.

3. **Set the switch.** In repository *Settings* > *Secrets and variables* >
   *Actions* > *Variables*, add `PYPI_PUBLISH` with the value `true`. Set it
   only after step 2, or the release fails at the publish step.

Test it once on TestPyPI first if you want: add a second trusted publisher there
and point the publish step at `https://test.pypi.org/legacy/` with the action's
`repository-url` input.

## Before the repo goes public

- **Repository visibility.** Switch it to public in *Settings*. Check first that
  no path, hostname or token from a private setup is in the history. The install
  one-liner needs a public repo: the raw URL 404s while it is private.
- **Social preview.** In *Settings* > *General* > *Social preview*, upload a
  1280x640 image. Without it, links to the repo show a grey placeholder.
- **README on PyPI.** The README is the PyPI project page. Its image and doc
  links are relative, so they break there. Make them absolute
  `https://github.com/desquaredp/paddock/blob/main/...` URLs before the first
  publish.
- **README install line.** The README still says
  `uv tool install git+https://github.com/desquaredp/paddock`. Swap it for the
  one-liner above in the docs pass after the chooser epic lands, so this does not
  fight that PR for the same lines.
- **Herdr version.** The README and SPEC name the herdr version paddock is
  verified against. Check it still matches.
