<p align="center"><img src="assets/logo.png" alt="paddock: four horses at a fence" width="360"></p>

# paddock

A paddock for your herd: sandboxed agent environments for
[Herdr](https://herdr.dev).

Herdr is a terminal multiplexer for AI coding agents. paddock takes over
new-window creation: press `prefix+c` and choose a plain local tab or a named
sandbox session. For each sandbox you pick the agent (Claude Code, Codex,
OpenCode, Aider, Gemini, or any command), the tools on its `PATH`, the network
domains it may reach (everything else is refused), the skills and MCP servers
it can see, and an optional shared read-write directory. Saved profiles make
it two keystrokes. Isolation is enforced at the OS level: Seatbelt on macOS,
bubblewrap on Linux. Per-session VPNs, isolated per-session IPs, and microVM
isolation are on the [roadmap](docs/ROADMAP.md). Targets **herdr 0.8.0**.

> **Status:** the v1 launcher works end to end. It has had no outside security
> review, so read the [trust model](#trust-model) before you point it at anything
> valuable. Where this is going: [docs/ROADMAP.md](docs/ROADMAP.md).

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
| Which agent? | `claude`, `codex`, `opencode`, `aider`, `gemini`, a plain shell, or any command you type |
| Which tools? | The binaries on the sandbox `PATH` |
| Which network? | The domain allowlist. Everything else is refused |
| Share a directory? | A host directory, read-write, or an isolated scratch workdir |
| Which skills / MCP servers? | Only the ones you tick exist inside the sandbox |

**Attach to an existing session** lists live sessions with their name, agent,
profile and attached tabs.

Save any set of answers as a **profile** and reuse it next time. Plain new-tab
moves to `prefix+shift+c`.

## Sessions

A **session** is one running sandbox with a name. Every tab attaches to one
session, or to none, and sandboxed tabs are labelled `sbx:<session>` in the tab
bar. That one rule covers the layouts people want:

- a whole workspace in one sandbox,
- a group of tabs sharing one session's policy and workdir while sibling tabs
  stay local,
- several sandboxes side by side in one workspace,
- a local tab running an orchestrating agent that drives the sandboxed ones
  through the herdr CLI.

Sessions outlive the popup that made them and survive Herdr restarts. With the
v1 backend, attached tabs share a settings file and a workdir but get **separate
process trees**: shared files, never a shared runtime. See
[docs/SPEC.md §3](docs/SPEC.md#3-sandbox-sessions).

## Trust model

Sandboxes run under [Anthropic's sandbox-runtime](https://github.com/anthropics/sandbox-runtime)
(`srt`): Seatbelt on macOS, bubblewrap on Linux. Three layers of permission sit
on top of it, and two of them are hard:

1. **OS-level (hard).** Write paths and network domains, enforced by the kernel
   sandbox. Writes are denied by default, the network is allowlist-only, and the
   agent cannot argue its way out.
2. **Agent config (agent-enforced).** Generated permission config the agent
   applies to itself, so its prompts agree with the sandbox. Useful friction, not
   a boundary.
3. **Synthesized config dir (hard).** The agent's config directory is rebuilt per
   session with only its credentials and the skills you ticked, so unselected
   skills and MCP servers are not there to find.

**Credentials.** A sandbox gets the selected agent's own login and nothing else:
its credential file, or on macOS the token exported from the login Keychain into
that session's config dir. Files the agent writes back to are copies, so your
real config is never touched, and no agent is given another agent's keys, your
SSH keys or your cloud credentials. Verified live: a sandboxed Claude Code
authenticates, answers, and cannot write outside its paddock.

[docs/SPEC.md §4](docs/SPEC.md#4-three-enforcement-layers) covers what each layer
does and does not stop, including known bypasses.

## Command line

The popup is the usual way in. The same jobs work without questions:

```sh
paddock launch claude-default   # start a session from a saved profile
paddock attach review           # put a new tab on a running session
paddock profiles                # list saved profiles
paddock init                    # wire the chooser into herdr's config
```

`launch` and `attach` take `--cwd` to say which directory to work in, and
`--dry-run` prints what would happen instead of doing it. `paddock init` also
takes `--undo`.

## Install

**1. Prerequisites**

- [herdr](https://herdr.dev) 0.8.0 or newer.
- Node.js, so `npx` can fetch the sandbox runtime on first use. To install it
  instead: `npm i -g @anthropic-ai/sandbox-runtime`.
- On Linux, also `bubblewrap`, `socat` and `ripgrep`. On Ubuntu 24.04 and newer,
  AppArmor blocks the unprivileged user namespaces bubblewrap needs. Allow them
  with `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, and add
  it to `/etc/sysctl.d/` to make it stick.

**2. Install paddock**

```sh
uv tool install git+https://github.com/desquaredp/paddock
```

**3. Wire it into herdr**

```sh
paddock init
```

That backs up `~/.config/herdr/config.toml`, binds the chooser to `prefix+c`,
moves plain new-tab to `prefix+shift+c`, and asks herdr to reload. It writes the
file if herdr has not written one yet, and running it twice changes nothing.
`paddock init --dry-run` shows the change first; `paddock init --undo` puts the
old config back. Every run that changes something keeps a
`config.toml.paddock-backup-*` copy next to the config; delete them when you no
longer want them.

**4. Press `prefix+c` inside herdr.**

To check herdr is happy with the result: `herdr config check`.

## Docs

- [docs/SPEC.md](docs/SPEC.md) covers herdr integration, backends, sessions,
  enforcement layers, the agent registry and profiles.
- [docs/ROADMAP.md](docs/ROADMAP.md) says where this is going.
- [docs/diagrams/](docs/diagrams/) holds the PlantUML sources.
- [CONTRIBUTING.md](CONTRIBUTING.md) covers branching, TDD and design principles.
