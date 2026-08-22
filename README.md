<p align="center"><img src="assets/logo.png" alt="paddock: four horses at a fence" width="360"></p>

# paddock

A paddock for your herd: sandboxed agent environments for
[Herdr](https://herdr.dev).

Press `prefix+s` in [Herdr](https://herdr.dev) (`prefix` is herdr's prefix key,
`ctrl+b` by default). Pick an agent, say what it may reach, and launch it into a
tab of its own.

You choose what each sandbox gets:

- **Agent**: Claude Code, Codex, OpenCode, Aider, Gemini, or any command
- **Tools**: only the binaries you tick are on its `PATH`
- **Network**: a domain allowlist; everything else is refused
- **Skills and MCP servers**: unticked ones do not exist inside the sandbox
- **Files**: writes denied by default, plus one optional shared directory

Enforced by the OS: Seatbelt on macOS, bubblewrap on Linux. Saved profiles make
it two keystrokes. A session can run in a microVM instead, with
`paddock launch <profile> --backend msb`: its own kernel, and only the directory
you shared. The agent is installed in the guest on the way up, so a microVM
session takes about 40 seconds to its first tab and an `srt` one starts at once.
Per-session VPNs and isolated IPs are on the
[roadmap](docs/ROADMAP.md). Targets **herdr 0.8.0**.

> **Status:** the v1 launcher works end to end. It has had no outside security
> review, so read the [trust model](#trust-model) before you point it at anything
> valuable.

## The chooser

`prefix+s` opens a popup with one screen on it (this is what it looks like after
a few runs, with a profile saved and last used):

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

On a first run there is nothing to remember yet, so the form opens on **Custom**,
paddock's own defaults: Claude Code, a few tools, the anthropic and github domain
groups, and an isolated scratch directory. Everything below the Profile row is
yours to change before you launch, and `s` saves the answers as a profile so the
next run is one pick.

The form is filled in already, so the ordinary case is two key presses: `L`, then
enter on the confirm. The confirm stays even for a repeat launch, because it is
the whole promise of the tool: the thing that grants the permissions says out
loud what it is granting. Every field is one arrow key or one digit away, and
enter opens it. Escape closes what
it opened without losing what you did there, and every list and checklist draws a
**← Back** row that does the same thing for anyone who would rather see it than
know it. Escape on the form itself cancels, because nothing is before it. Ctrl-c
cancels from any depth, and nothing has been launched or written by then.

| Field | What it decides |
| --- | --- |
| `Open` | A new sandbox, an ordinary local tab, or a second tab on a session already running |
| `Profile` | What everything below starts from. Change anything and the session runs as "the profile + changes", never under its name |
| `Backend` | `srt`, a policy sandbox around the process, or `msb`, a microVM that starts slower and shares less |
| `Agent` | `claude`, `codex`, `opencode`, `aider`, `gemini`, a plain shell, or a command you type. One this machine has not got says `(not installed)` and cannot be picked |
| `Tools` | The binaries on the sandbox `PATH`, plus whatever the agent cannot start without, which is named with it. An absolute path still runs anything, so this is convenience, not a boundary |
| `Network` | The domain allowlist, by group, plus any domain you add. Everything else is refused by the OS |
| `Files` | An isolated scratch directory, or the one host directory the sandbox may change |
| `Skills` | Only the ticked ones exist inside the sandbox at all |
| `Advanced` | The session name, saving these answers as a profile, keeping the session running after its last tab, MCP servers, extra writable paths, denied reads and the system PATH |

`Tools`, `Network` and `Skills` each offer an all-of-it row at the top of their
checklist: every binary on the host `PATH`, any domain with no allowlist at all,
every installed skill. Ticking one clears the individual ticks below it, and the
confirm says what it granted in as many words: `can run: the full host PATH`,
`can reach: ANY domain (unrestricted)`, `can see: all skills`. The `srt` backend
has no way to express "any domain", so that one row is refused there and says to
use the `msb` backend instead. There is deliberately no all-of-it row for writes:
a sandbox with no filesystem fence is a Local tab, which is one field up.

Ticking every tool hands the sandbox this machine's own `PATH` rather than a
directory of symlinks, and changes **nothing** about what it may write or reach.
The `PATH` was never the fence: an absolute path reaches any binary on the machine
whatever is ticked, which is why the tool list is described as convenience rather
than as a boundary.

`Open` lists every live session by its name, backend, agent, profile and
attached tabs, so a second tab on one of them is a pick and not a screen of its
own. Picking one asks what goes in the tab: the agent again, or a plain shell
inside the same sandbox, which is `paddock attach <session> --shell` on the
command line. A shell tab is labelled `sbx:<name> (shell)`, counts as a tab of the
session like any other, and puts `paddock:` in front of its prompt, so the one tab
that could be mistaken for an ordinary shell says what it is. Your own prompt is
kept: a zsh theme comes back as `paddock:` and then the theme. A `bash` whose
`~/.bashrc` writes its own prompt keeps that, and there the label is what says it.

Launching a sandbox ends on a confirm: the one screen that shows the policy
resolved, with the domain groups expanded into the domains they open, and Launch,
Back to the form and Cancel on it. A local tab has no policy, so it has no
confirm. A launch that never gets as far as a pane comes back on a screen of its
own, with the reason, the log path and the way back to the form.

The form opens on the profile that workspace launched last, so the ordinary run
is the same sandbox as yesterday's, unchanged, in those two presses. `s` saves the
answers as a profile, which makes them one pick anywhere.

### The keys

| Key | What it does |
| --- | --- |
| `prefix+c` | A plain new tab. herdr's own, and paddock is not involved in it |
| `prefix+s` | The chooser |
| `prefix+shift+s` | The chooser, opened on the list of live sessions, for attaching |

`prefix` is herdr's prefix key, `ctrl+b` unless you have changed it. `paddock
init` writes those two bindings and moves herdr's own settings screen to
`prefix+comma`, because paddock took its key. It tells you every binding that
changed, and `paddock init --undo` puts them all back.

An earlier paddock took `prefix+c` for the chooser and moved plain new-tab to
`prefix+shift+c`. Running `paddock init` again migrates that config to the
scheme above and says so. `prefix+shift+c` then goes back to being unbound.

The popup herdr opens is smaller than the terminal it is in, so the screens are
built for a small one: they scroll their rows, they pin what must never scroll
off, such as the confirm's buttons, and they grow into a bigger popup up to a
line length that is still comfortable to read.

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

When a session's last tab closes it is collected: dropped from the registry, its
copied credentials deleted, and its microVM destroyed if it had one. Nothing is
running to watch for that, so it happens at the next `paddock` command rather
than the instant the tab closes. `paddock gc` forces it.

**What collection does not delete.** The session's run directory stays on disk,
under `~/.local/state/paddock/runs/`. It holds the settings file, the shim dir,
the synthesized config dir (minus the credentials, which are deleted) and, for a
session that shared no host directory, the scratch workdir with whatever the
agent wrote in it. Deleting that would lose work, so paddock keeps it and you
delete it when you are done with it. `paddock gc` removes only the ones that hold
nothing: a launch that failed before its first tab, or a session whose scratch
workdir is empty.

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
paddock attach review --shell   # ... or a plain shell inside its sandbox
paddock profiles                # list saved profiles
paddock gc                      # collect sessions whose tabs are all closed, and
                                # sweep sandboxes and run dirs nothing claims
paddock logs                    # where paddock logged what it did, and the end of it
paddock init                    # wire the chooser into herdr's config
paddock --version               # print the version and exit
```

`--dry-run` prints what would happen instead of doing it, and `paddock init`
also takes `--undo`. `attach` takes `--shell` for a plain shell inside the
sandbox instead of the agent.

**`--cwd` is a permission, not a working directory.** On `launch` it is the one
directory on this machine the sandbox may read and write, mounted into a microVM
or unlocked in the OS policy, and it overrides whatever the profile shared. Point
it at the project you want the agent to work on, never at your home directory. On
`attach` it only says where the new tab opens, because the session's permissions
were settled when it was created.

A launch on the `msb` backend takes about **40 seconds** before its first tab:
it pulls the guest image and installs the agent inside the guest. The chooser
draws a screen saying so, and the command line prints the same lines before it
blocks. `srt` starts at once.

## Install

**1. Prerequisites**

- [herdr](https://herdr.dev) 0.8.0 or newer.
- Python 3.11 or newer, and [uv](https://docs.astral.sh/uv/), which installs
  paddock and brings its own Python if yours is older: `brew install uv`, or
  `curl -LsSf https://astral.sh/uv/install.sh | sh`. The install script below
  offers to install uv for you.
- Node.js, so `npx` can fetch the sandbox runtime on first use. To install it
  instead: `npm i -g @anthropic-ai/sandbox-runtime`.
- On Linux, also `bubblewrap`, `socat` and `ripgrep`. On Ubuntu 24.04 and newer,
  AppArmor blocks the unprivileged user namespaces bubblewrap needs. Allow them
  with `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, and add
  it to `/etc/sysctl.d/` to make it stick.

**2. Install paddock**

```sh
curl -fsSL https://raw.githubusercontent.com/desquaredp/paddock/main/install.sh | sh
```

Or, if you already have uv and would rather see the command:

```sh
uv tool install git+https://github.com/desquaredp/paddock
```

If the shell then says `paddock: command not found`, the tool directory is not on
your `PATH` yet. `uv tool update-shell` adds it, and a new shell picks it up.

**3. Wire it into herdr**

```sh
paddock init
```

That backs up `~/.config/herdr/config.toml`, binds the chooser to `prefix+s` and
the attach list to `prefix+shift+s`, moves herdr's settings screen to
`prefix+comma` to free that key, and asks herdr to reload. It prints every
binding that changed. It writes the file if herdr has not written one yet, and
running it twice changes nothing. `paddock init --dry-run` shows the change
first; `paddock init --undo` puts the old config back. Every run that changes
something keeps a `config.toml.paddock-backup-*` copy next to the config; delete
them when you no longer want them.

**4. Press `prefix+s` inside herdr** (`prefix` is herdr's prefix key, `ctrl+b`
by default).

To check herdr is happy with the result: `herdr config check`.

## Uninstall

In this order, because the first step needs paddock to still be installed:

```sh
paddock init --undo                     # put herdr's keybindings back
uv tool uninstall paddock               # remove the command
rm -rf ~/.local/state/paddock           # run dirs, session registry, logs
rm -rf ~/.config/paddock                # saved profiles and agent entries
```

The last two are separate on purpose. The state directory holds the scratch
workdirs of every session that ever ran, so look in it before you delete it. The
config directory holds the profiles you saved, which are worth keeping if you
might come back.

## Troubleshooting

`paddock logs` prints where the log is and the last 40 lines of it, and
`paddock logs <session>` prints what that session's tabs printed: the agent's
log, the shell tabs' log, or both. Those files hold the pane's **stderr** only,
so a session that is going well leaves them empty: the agent draws its interface
on stdout, and an empty log means it had nothing to complain about. A launch that
fails leaves its pane open with the reason on screen, and one that never got as
far as a pane says why on a screen of its own, with the way back to the form.

## Docs

- [docs/SPEC.md](docs/SPEC.md) covers herdr integration, backends, sessions,
  enforcement layers, the agent registry and profiles.
- [docs/ROADMAP.md](docs/ROADMAP.md) says where this is going.
- [docs/diagrams/](docs/diagrams/) holds the PlantUML sources.
- [CONTRIBUTING.md](CONTRIBUTING.md) covers branching, TDD and design principles.
