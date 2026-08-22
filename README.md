<p align="center"><img src="assets/logo.png" alt="paddock — four horses at a fence" width="360"></p>

# paddock

A paddock for your herd — sandboxed agent environments for
[Herdr](https://herdr.dev).

Herdr is a terminal multiplexer for AI coding agents. paddock takes over
new-window creation and asks what you want: a plain local tab, or an agent in a
sandbox whose permissions you pick on the spot. Targets **herdr 0.8.0**.

> **Status: pre-alpha.** The spec is written; the modules are being built. Nothing
> here is a security boundary yet. See [docs/SPEC.md](docs/SPEC.md) for the
> design and [CONTRIBUTING.md](CONTRIBUTING.md) for how work lands.

## The chooser

`prefix+c` opens a popup instead of creating a window:

```
New window:
  > Local namespace (no sandbox)
    New sandbox session
    Attach to an existing session
```

**Local namespace** makes an ordinary herdr tab in the current directory.

**New sandbox session** asks what this sandbox may do, then asks for a name:

| Question | What it controls |
| --- | --- |
| Which agent? | `claude`, `codex`, `opencode`, `aider`, `gemini`, or a plain shell |
| Which tools? | The binaries on the sandbox `PATH` |
| Which network? | The domain allowlist — everything else is refused |
| Share a directory? | A host directory, read-write, or an isolated scratch workdir |
| Which skills / MCP servers? | Only the ones you tick exist inside the sandbox |

**Attach to an existing session** lists live sessions with their name, backend,
profile and attached tabs.

Save any set of answers as a **profile** and reuse it next time. Plain new-tab
moves to `prefix+shift+c`.

## Sessions

A **session** is one running sandbox with a name. Every tab attaches to one
session, or to none. That covers the layouts people want:

- a whole workspace in one sandbox (new tabs auto-attach via a workspace default),
- a group of tabs in one sandbox while sibling tabs stay local,
- several sandboxes side by side in one workspace,
- a local tab running an orchestrating agent that drives the sandboxed ones
  through the herdr CLI.

What attaching means depends on the backend. With `microsandbox`, tabs share one
guest: same filesystem, same processes. With `srt`, tabs share a settings file
and a workdir but get **separate process trees**. So tab groups are useful in v1;
shared runtime arrives with `msb`.

Sessions survive Herdr restarts and are labelled `sbx:<session>` in the tab bar.
See [docs/SPEC.md §3](docs/SPEC.md#3-sandbox-sessions).

## Backends

| | `srt` (v1) | `microsandbox` (v1.1, spec only) |
| --- | --- | --- |
| Technology | [Anthropic sandbox-runtime](https://github.com/anthropics/sandbox-runtime) | libkrun microVMs (`msb`) |
| Isolation | Seatbelt (macOS) / bubblewrap (Linux) | Hardware-virtualised microVM |
| Filesystem | Host FS, write-path allowlist | OCI image plus volume mounts |
| Network | Domain allowlist | Host/port policy, `.localhost` URLs per sandbox |
| Startup | Milliseconds | Sub-second |
| Status | Being built | Specified, not implemented |

Both sit behind one interface, so a profile can move between them.

## Trust model

Three layers. Two are hard:

1. **OS-level (hard)** — write paths and network domains, enforced by the kernel
   sandbox. The agent cannot argue its way out.
2. **Agent config (agent-enforced)** — generated permission config the agent
   applies to itself. Useful friction, not a boundary.
3. **Synthesized config dir (hard)** — the agent's config directory is rebuilt
   with only its credentials and the skills you ticked, so unselected skills and
   MCP servers are not there to find.

[docs/SPEC.md §4](docs/SPEC.md#4-three-enforcement-layers) covers what each layer
does and does not stop, including known bypasses.

## Install

**1. Prerequisites**

- [herdr](https://herdr.dev) 0.8 or newer.
- Node.js, so `npx` can fetch the sandbox runtime on first use. To install it
  instead: `npm i -g @anthropic-ai/sandbox-runtime`.
- On Linux, also `bubblewrap`, `socat` and `ripgrep`. On Ubuntu 24.04 and newer,
  AppArmor blocks the unprivileged user namespaces bubblewrap needs — allow them
  with `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0` (add it to
  `/etc/sysctl.d/` to make it stick).

**2. Install paddock**

```sh
uv tool install git+https://github.com/desquaredp/paddock
```

**3. Wire it into herdr**

```sh
paddock init
```

That backs up `~/.config/herdr/config.toml`, binds the chooser to `prefix+c`,
moves plain new-tab to `prefix+shift+c`, and asks herdr to reload. Run it twice
and the second run changes nothing. `paddock init --dry-run` shows the change
first; `paddock init --undo` puts the old config back.

**4. Press `prefix+c` inside herdr.**

## Docs

- [docs/SPEC.md](docs/SPEC.md) — herdr integration, backends, sessions,
  enforcement layers, agent registry, profiles, module plan.
- [docs/diagrams/](docs/diagrams/) — PlantUML sources.
- [CONTRIBUTING.md](CONTRIBUTING.md) — branching, TDD, design principles.
