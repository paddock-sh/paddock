<p align="center"><img src="assets/logo.png" alt="paddock: four horses at a fence" width="360"></p>

# paddock

A paddock for your herd: sandboxed agent environments for
[Herdr](https://herdr.dev).

Press `prefix+c` in [Herdr](https://herdr.dev). Pick a plain local tab, or an
agent in a sandbox.

You choose what each sandbox gets:

- **Agent**: Claude Code, Codex, OpenCode, Aider, Gemini, or any command
- **Tools**: only the binaries you tick are on its `PATH`
- **Network**: a domain allowlist; everything else is refused
- **Skills and MCP servers**: unticked ones do not exist inside the sandbox
- **Files**: writes denied by default, plus one optional shared directory

Enforced by the OS: Seatbelt on macOS, bubblewrap on Linux. Saved profiles make
it two keystrokes. Per-session VPNs, isolated IPs, and microVMs are on the
[roadmap](docs/ROADMAP.md). Targets **herdr 0.8.0**.

> **Status:** the v1 launcher works end to end. It has had no outside security
> review, so read the [trust model](#trust-model) before you point it at anything
> valuable.

## The chooser

`prefix+c` opens a popup with one screen on it:

```
paddock                          claude-default            in ~/dev/paddock

   1 Open       New sandbox
 > 2 Profile    claude-default
   3 Backend    srt (instant, a policy sandbox)
   4 Agent      Claude Code (claude)
   5 Tools      git rg fd jq curl node npm npx uv python3               (10)
   6 Network    anthropic, github, npm, pypi/uv                (12 domains)
   7 Files      an isolated scratch directory
   8 Skills     none
   9 Advanced   name, save as profile, MCP

 Fills in everything below. Change anything and the title says "+ changes",
 because the session will then not be what the profile says.

   [ Launch ]        [ Cancel ]

enter edit   ^v move   1-9 jump   L launch   s save   esc cancel   ? keys
```

The form is filled in already, so the ordinary case is one key press: `L`. Every
field is one arrow key or one digit away, and enter opens it. Escape closes what
it opened without losing what you did there, and every list and checklist draws a
**← Back** row that does the same thing for anyone who would rather see it than
know it. Escape on the form itself cancels, because nothing is before it. Ctrl-c
cancels from any depth, and nothing has been launched or written by then.

| Field | What it decides |
| --- | --- |
| `Open` | A new sandbox, an ordinary local tab, or a second tab on a session already running |
| `Profile` | What everything below starts from. Change anything and the session runs as "the profile + changes", never under its name |
| `Backend` | `srt`, a policy sandbox around the process, or `msb`, a microVM that starts slower and shares less |
| `Agent` | `claude`, `codex`, `opencode`, `aider`, `gemini`, a plain shell, or a command you type |
| `Tools` | The binaries on the sandbox `PATH`. An absolute path still runs anything, so this is convenience, not a boundary |
| `Network` | The domain allowlist, by group, plus any domain you add. Everything else is refused by the OS |
| `Files` | An isolated scratch directory, or the one host directory the sandbox may change |
| `Skills` | Only the ticked ones exist inside the sandbox at all |
| `Advanced` | The session name, saving these answers as a profile, keeping the session running after its last tab, MCP servers, extra writable paths, denied reads and the system PATH |

Launching a sandbox ends on a confirm: the one screen that shows the policy
resolved, with the domain groups expanded into the domains they open, and Launch,
Back to the form and Cancel on it. A local tab has no policy, so it has no
confirm.

The form opens on the profile that workspace launched last, so the ordinary run
is the same sandbox as yesterday's, unchanged, in one key press. `s` saves the
answers as a profile, which makes them one pick anywhere. Plain new-tab moves to
`prefix+shift+c`.

The screens are drawn to fit an 80 by 24 terminal and grow into a wider popup,
centred, up to a line length that is still comfortable to read.

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
