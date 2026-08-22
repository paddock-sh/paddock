# paddock specification

Status: **pre-alpha.** Section 7 is built. Profiles, the agent registry, the
herdr client, the srt backend, sessions, the synthesized config dir, the chooser
TUI, the CLI and `paddock init` all exist now; §7 says what each still owes.

paddock takes over new-window creation in [herdr](https://herdr.dev) (a terminal
multiplexer for AI coding agents, **v0.8.0**) and replaces it with a popup
chooser. Per window, the user picks a plain local tab or an agent in a sandbox,
and for a sandbox: which agent, tools, network, files, skills and MCP servers it
gets.

The rule behind the design: **every permission is an active choice.** No "allow
everything" default, no authority inherited from the host shell.

Sections marked v1.1 say where the design is going, so v1 decisions are made with
the destination in view. They are not a build list and are not stubbed in code:
see [CONTRIBUTING.md § Design principles](../CONTRIBUTING.md#design-principles).

Diagrams: [`architecture.puml`](diagrams/architecture.puml),
[`launch_sequence.puml`](diagrams/launch_sequence.puml),
[`msb_flow.puml`](diagrams/msb_flow.puml),
[`scoping_model.puml`](diagrams/scoping_model.puml),
[`profiles_and_agents.puml`](diagrams/profiles_and_agents.puml),
[`chooser_flow.puml`](diagrams/chooser_flow.puml),
[`init_flow.puml`](diagrams/init_flow.puml).

---

## 1. Herdr integration

Verified against a local herdr 0.8.0.

### 1.1 Keybinding

**Shipped: `paddock init` writes this.** In `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+c"
type = "popup"
command = "paddock"
width = "70%"
height = "70%"

[keys]
new_tab = "prefix+shift+c"
```

`type = "popup"` runs the command in an overlay, which is where the questionary
TUI draws. Plain new-tab moves to `prefix+shift+c`.

The popup is transient: it asks, it creates the pane, it exits.

`paddock init` splices both into the config, and writes the file when herdr has
not written one yet. It edits the text, not a parsed document, because a TOML
round trip drops comments: the `[[keys.command]]` block sits between
`# --- paddock (managed) ---` markers, so a second run replaces it rather than
repeating it, and the `new_tab` line is inserted or rewritten on its own.
Everything else in the file stays byte for byte, line endings included.

Three things stand between that splice and the user's config. The result is
parsed as TOML before anything is written, and a result that will not parse is
reported and dropped. The old config is copied to
`config.toml.paddock-backup-<timestamp>` first. `herdr config check` then runs on
what was written, and a config herdr refuses is put straight back.
`herdr server reload-config` runs last: herdr may not be running, which is a
message, not a failure.

`--dry-run` prints the diff and touches nothing. `--undo` restores the newest
backup, keeping what it replaces as `config.toml.paddock-undone-<timestamp>`, so
edits made since the last init are not lost. A `new_tab` the user has bound to
something of their own is left alone and reported: paddock still takes
`prefix+c`. A config that sets `keys` outside a `[keys]` table, as a dotted key
or an inline table, is refused rather than edited.

### 1.2 Environment in the popup

Herdr exports these, and they are how the chooser knows where it was invoked:

| Variable | Use |
| --- | --- |
| `HERDR_SOCKET_PATH` | herdr server socket |
| `HERDR_ACTIVE_WORKSPACE_ID` | Workspace to create the tab in |
| `HERDR_ACTIVE_PANE_ID` | Pane the popup was launched over |

Treat all three as optional. Run outside herdr (during development, say), the
launcher omits `--workspace` rather than failing.

### 1.3 Launching panes

Two calls, in order.

**Create the tab:**

```sh
herdr tab create --workspace <workspace_id> --cwd <path> --label <label> --focus
```

Options in 0.8.0: `--workspace <WORKSPACE_ID>`, `--cwd <PATH>`, `--label <TEXT>`,
`--env <KEY=VALUE>` (repeatable), `--focus` / `--no-focus`.

`--env` matters for layer 3 (§4.3): `CLAUDE_CONFIG_DIR` and friends are set at
tab creation, so the pane's own shell agrees with the sandbox. The sandbox command
starts from `env -i` (§2.1), which wipes that, so the launcher writes the same
variables into the command as well. Both, on purpose: neither covers the other.

Output:

```json
{"result": {"root_pane": {"pane_id": "wA:p2"}}}
```

The pane id (`<workspace>:<pane>`) comes from `result.root_pane.pane_id`.

**Run the command:**

```sh
herdr pane run <pane_id> <command>...
```

Creation and execution are separate on purpose. The tab exists with the right cwd
and label before anything starts, so a failed launch leaves a usable pane instead
of no window. The command is typed into the pane's shell, so it has to be short:
§2.1 keeps it to one `exec` of a script.

### 1.4 v1.1: packaging as a herdr plugin (design record, not stubbed in v1)

Herdr 0.8.0 has a plugin system (`herdr plugin install|link|enable|list`,
`herdr plugin action`, `herdr plugin pane`). A later milestone adds a
`herdr-plugin.toml` manifest so paddock installs as a plugin (`herdr plugin
link` in development, `herdr plugin install` for users) instead of the
keybinding `paddock init` writes. The keybinding keeps working; the manifest is
sugar over it. No manifest is written in v1.

---

## 2. Backends

One interface, in four calls:

```
Backend.prepare(profile) -> run          # everything the run needs, written or booted
Backend.load_run(run_dir) -> run         # a prepared run read back, for a later tab
Backend.open_pane(run, label) -> pane id # a tab on a prepared run
Backend.collect(run_dir)                 # nobody is attached any more: stop what is running
```

They are separate because a session is prepared once and attached to many times
(§3.2), and because what a session leaves running outlives its last tab. A backend
works the whole launch out as plain functions, then opens the tab and starts the
command through `herdr_client` (§7). `herdr_client` is the seam every test mocks, so
a backend is still testable with no herdr server and no sandbox. Sessions (§3) drive
all four calls; a backend knows nothing about the registry. Which module runs a
session is the `backend` name in its record (§3.4), looked up in a dict of name to
module in `sessions.py`.

Every backend uses the same run directory, `<state>/runs/<timestamp>-<random>/`. Two
things in it are the same whichever backend wrote it: `launch.json`, holding what a
later tab attaches with, and `launch.sh`, holding the command, because the pane is
sent a line and not a file (§1.3). `backends/__init__.py` holds those two and nothing
else. What else goes in the directory is the backend's own business.

The interface exists because v1 needs it, to keep srt's settings and invocation
out of the chooser. `backends/microsandbox.py` (§2.2) is the second implementation.

### 2.1 v1: `srt` (Anthropic sandbox-runtime)

Package `@anthropic-ai/sandbox-runtime`, CLI `srt`. Resolution order:

1. `srt` on `PATH`
2. `npx -y @anthropic-ai/sandbox-runtime`

Seatbelt (`sandbox-exec`) on macOS, bubblewrap on Linux. It starts in
milliseconds, which is what makes a per-window chooser workable.

**Settings JSON:**

```json
{
  "network": {
    "allowedDomains": ["api.anthropic.com", "github.com"],
    "deniedDomains": []
  },
  "filesystem": {
    "denyRead":   ["/Users/me/.ssh", "/Users/me/.aws"],
    "allowRead":  ["/Users/me/.claude/.credentials.json"],
    "allowWrite": ["/path/to/workdir", "/tmp", "/private/tmp", "/dev/null"],
    "denyWrite":  ["/Users/me/.ssh", "/Users/me/.aws"]
  }
}
```

srt validates the file against a schema and refuses to start when a key is
missing, so every key is written even when its list is empty.

Three defaults shape everything else:

- **Reads are allowed by default.** `denyRead` is a blocklist, which is why every
  profile ships a deny list for credential directories (§6). Leave it out and the
  agent can read them. `allowRead` holds the selected agent's own credentials, so
  a broad `deny_read` cannot lock the agent out of itself.
- **Writes are denied by default.** `allowWrite` gets the workdir, the shared
  directory if there is one, `/tmp` and `/private/tmp` (one directory under two
  names on macOS, and srt matches the path as written), and `/dev/null`, so
  discarded output works. `$TMPDIR` joins them when the host sets one, resolved
  through its symlinks, because the sandbox keeps that variable and tools write
  where it points. The run directory itself is **not** writable: it holds
  the settings file and the shim dir, which the sandbox only reads. Its `config/`
  and, for an isolated profile, its `work/` subdirectory are the exceptions:
  the synthesized config dir (§4.3) and the workdir. srt matches paths as
  written, so allowing a subdirectory does not allow its parent. `denyWrite`
  mirrors `denyRead`, so a denied path is off limits both ways.
- **Network is allowlist-only.** Anything not listed is refused. `deniedDomains`
  stays empty; it is written because the schema wants the key.

**srt checks the path an access resolves to**, not the path the agent typed. A
symlink is therefore governed by its target: what the policy has to name is the
real file, never the link. That is why the synthesized config dir (§4.3) works,
and why the settings have to allow its targets by name.

**The agent's config directory** depends on whether layer 3 can redirect it
(§4.3). When it can, as Claude Code does today, the real directory is denied for reading
and writing, and the synthesized one under the run dir is writable instead. What
that directory *links* to is allowed back by name, because srt sees the target:
the agent's key, and the real directories of the skills the user ticked. What it
*copies* is denied on the host instead, because the sandbox has its own. Every
credential path is in `denyWrite` either way, so the host's copies are never
written. When the agent cannot be redirected, `allowWrite` gets its
`config_write_paths`, its real config directory, because blocking it breaks the
agent. That is a **known gap** for those agents, and it closes when they get a
redirection.

Paths are stored as written: `~/.ssh`, not `/Users/me/.ssh`. The backend expands
`~` for every configured path (`deny_read`, the agent's `auth_read_paths` and
`config_write_paths`, `shared_dir`, `extra_allow_write`) when it generates the
settings file, so profiles stay portable between machines.

Each session gets its own timestamped directory under
`~/.local/state/paddock/runs/`, holding the settings file, the PATH shim dir, the
synthesized config dir, the scratch workdir when the profile shares no host
directory, a small `launch.json` holding the command, workdir and environment, so a
tab attaching later gets exactly what the first one got, and `launch.sh`, the same
command as a script. `PADDOCK_STATE_DIR`
overrides the state directory; tests point it at a temporary one. Nothing collects
old run directories yet, including those of collected sessions: only the
credential file inside one goes with the session (§3.4, §8).

**Invocation:**

```sh
srt --settings <settings-file> -c "<command>"
```

`-c` matters. srt shell-quotes each argument and runs the result under bash, so
the command has to arrive as one string; passed as bare words, srt's own flag
parser reads the agent's flags as its own.

**The pane is sent a short line, not that command.** `herdr pane run` types what
it is given into the pane's shell, and a tty drops everything past 1024 bytes on
one line. The composed command is longer than that, so it is written to
`run_dir/launch.sh` (mode 0700) when the run is prepared, and the pane gets
`exec /bin/sh <run_dir>/launch.sh`. The run dir is not writable from inside the
sandbox, so the agent cannot rewrite its own launcher. `exec` replaces the pane's
shell, so closing the agent closes the pane. Every tab on the session runs the
same script.

The inner command is the agent, wrapped so it starts from an empty environment:

```sh
env -i HOME=... USER=... LOGNAME=... SHELL=... TERM=... LANG=... LC_ALL=... \
       TMPDIR=... CLAUDE_CONFIG_DIR=... \
       http_proxy="$http_proxy" ... GIT_SSH_COMMAND="$GIT_SSH_COMMAND" \
       PATH=<shim dir>:/usr/bin:/bin \
       <agent> <layer-2 flags>
```

The keep list is deliberately short. Everything the popup inherited, API tokens
above all, stays outside the sandbox. `PATH` points at the shim dir (§4.1). The
config dir variable and the flags after the agent come from layer 3 (§4.3); an
agent with neither gets the line as it was.

**The proxy variables are the exception, and they are passed by name, not by
value.** srt sets its own proxy environment in the shell it spawns, per
invocation: `http_proxy`, `HTTP_PROXY`, `https_proxy`, `HTTPS_PROXY`,
`all_proxy`, `ALL_PROXY`, `no_proxy`, `NO_PROXY`, `ftp_proxy`, `FTP_PROXY`,
`grpc_proxy`, `GRPC_PROXY`, `RSYNC_PROXY`, `DOCKER_HTTP_PROXY`,
`DOCKER_HTTPS_PROXY`, `npm_config_noproxy`, `SANDBOX_RUNTIME`,
`GIT_CONFIG_PARAMETERS`, `GIT_SSH_COMMAND`,
and that proxy is the sandbox's only way out. `env -i` wipes them, and then the
agent resolves no name at all. So each one is written into the command as
`VAR="$VAR"`, unquoted, for srt's shell to expand. No value is ever read from the
popup's own environment.

### 2.2 `microsandbox` (`msb`), registered as `"msb"`

`msb` boots an OCI image in a libkrun microVM. A harder boundary than Seatbelt (its
own kernel rather than a filtered view of the host's) at the cost of an image per
agent. There is no server and no daemon: each running VM is one `msb` process, and
the runtime installs in user space with no privileged step. The
[microVM spike](spikes/microvm.md) measured 175ms from `msb create` to a usable VM.
Every number in this section is from that spike.

**A session is a persistent VM.** `prepare()` boots it, every tab execs into it, and
`collect()` destroys it when the last tab is gone:

| Session operation | `msb` |
| --- | --- |
| create | `msb create --name <handle> --mount-dir <workdir>:/work --workdir /work <net rules> <image>` |
| attach a tab | `msb exec --tty <handle>`, in `launch.sh` like any other pane command |
| collect | `msb rm -f <handle>`. Without `-f`, `msb rm` is a silent no-op on a running sandbox |

The handle is `paddock-<run dir name>`. It has to be unique among live sandboxes on
the host, not only among paddock sessions, because `msb create` fails on a name
collision. The session record keeps it as `vm_handle` (§3.4). A VM that is already
gone is reported and not raised: the session is over either way.

The same profile maps across:

| Profile field | `srt` | `microsandbox` |
| --- | --- | --- |
| `agent` | command on host `PATH` | the agent's `image` (§5), else `alpine` |
| `tools` | PATH shim dir | baked into the image |
| `shared_dir` | `filesystem.allowWrite` entry | `--mount-dir <resolved path>:/work`, read-write |
| isolated workdir | scratch dir under the run dir | that same directory, mounted at `/work` |
| `network_presets` | `network.allowedDomains` | `--net-default deny`, then one allow rule each |
| `deny_read` | `filesystem.denyRead` | nothing to deny: an unmounted path is not in the guest |

Three of those differ in kind, not in spelling:

- **Mount sources are resolved.** `msb` mounts the path as written, and `/tmp` is a
  symlink to `/private/tmp` on macOS, which fails as a mount source. The backend
  passes `Path.resolve()` for every mount.
- **Network rules name a host, a protocol and a port, not a URL path.** srt allows a
  domain through its proxy; an msb rule is `allow@<domain>:tcp:443`, so it is https to
  that host and nothing else. `allow@dns` is added with the first domain. A profile
  with no domains gets no network at all, DNS included.
- **The PATH shim dir has no job here.** The guest holds what the image holds, so the
  image is the tool selection, and the absolute-path bypass §4.1 documents is not
  available: `/opt/homebrew/bin/docker` is not in the guest to be run.

**This backend runs the `shell` agent only.** Any other agent on an msb profile is
refused at create, before a VM is booted. Provisioning an agent inside the guest is
the next feature, and the spike showed layer 3 needs no new mechanism for it: mount
`run_dir/config` and point the config-dir variable at it with `-e`.

Not built, and not stubbed:

- **Agents in the guest**, so §4.2 and §4.3 do not apply to an msb session yet.
- **Port forwarding.** `-p <host port>:<guest port>` on `msb create` works and binds
  loopback, but no profile field asks for one. A per-sandbox `<name>.localhost` URL is
  a portless feature paddock would have to build (see [ROADMAP](ROADMAP.md)), not
  something `msb` provides.
- **A memory budget and a session cap.** Memory, not boot time, caps how many VM
  sessions run at once: an agent VM that has installed a toolchain holds 0.8GB to
  1.3GB resident and does not give it back. An idle shell VM settles around 65MB.
  Nothing enforces a cap.
- **A host and guest channel.** A guest reaches no host service by default and there
  is no host alias, so §3's local orchestration cannot reach the `herdr` CLI from
  inside a guest. `vsock` is the candidate; it is not decided.

The chooser does not offer msb yet. `paddock launch <profile> --backend msb` does,
which is how it is tested by hand.

---

## 3. Sandbox sessions

**The boundary is always a process tree in a pane, never a Herdr UI structure.**
Tabs and workspaces organise the interface; they enforce nothing.

Tabs attach to a **session**: one running sandbox with a name.

| Backend | A session is |
| --- | --- |
| `srt` | A policy context: one settings file plus one shared workdir |
| `msb` | A persistent microVM |

Every tab attaches to one session or to none. That one rule covers every layout
that would otherwise need its own mode:

- **Whole workspace**: every tab on one session, via the workspace default
  binding (§3.3).
- **Tab group**: some tabs on one session, their siblings local or on another.
- **Side by side**: several sessions in one workspace, each with its own name,
  backend and profile.
- **Local orchestration**: an unsandboxed tab driving the sandboxed ones through
  the herdr CLI. The orchestrator keeps host access; the agents it supervises do
  not.

### 3.1 The chooser

The first question is about sessions:

```
New window:
  > Local namespace (no sandbox)
    New sandbox session
    Attach to an existing session
```

**New sandbox session** runs the permissions questionnaire (§6) and asks for a
name. **Attach** lists live sessions with name, backend, agent, profile and
attached tab count, so the choice is made on what a session is, not on
remembering its name:

```
Attach to:
  > review [srt]: claude / hardened, 2 tabs
```

### 3.2 Attach means different things per backend

This is why the session list shows the backend:

| | `srt` | `msb` |
| --- | --- | --- |
| Attaching | New process under the same settings file and workdir | Execs a shell or agent into the same guest |
| Filesystem | Shared: one workdir on the host | Shared: one guest filesystem |
| Processes | **Separate trees**; tabs cannot see each other's | Shared namespace |
| Long-running state | Only what is on disk | Lives in the VM, outlives any tab |

So tab groups work with `srt`: attached tabs share policy and files, which is most
of why people want a group. Shared *runtime* is what `msb` adds. Seatbelt and
bubblewrap wrap a process tree and have no guest for a second process to join, so
srt can share policy and files, never a runtime. The UI must not imply otherwise.

### 3.3 Workspace default binding

An optional binding (*new tabs in workspace W attach to session S*) stored in
the plugin state dir, set and unset per workspace. It saves re-asking in a
workspace dedicated to one sandbox.

It is a default answer to the chooser's first question, not a separate mode. The
chooser is still reachable, it can be overridden per tab, and removing it changes
nothing about the sessions.

### 3.4 Session registry

Sessions are tracked in `<state>/sessions.json`, a list of records:

| Field | Meaning |
| --- | --- |
| `session_id` | Internal id, short |
| `name` | Shown in the chooser and pane labels; unique among live sessions |
| `profile_name` | Profile the session was created from |
| `agent` | Registry key of the agent it runs |
| `created_at` | ISO 8601 UTC timestamp |
| `run_dir` | Its directory under `runs/`: settings, shim dir, config dir, workdir |
| `keep_alive` | Survives its last pane |
| `pane_ids` | Pane ids currently attached |
| `backend` | Which backend runs it: `srt` or `msb`. Absent in a record written before the field, which means `srt` |
| `vm_handle` | The `msb` sandbox it runs in. Blank when the backend has no VM to name |

An unnamed session is named after its profile plus a short suffix. A name may not
be another session's id either, since both are references a caller can look up.

Two popups are two processes, so every write takes an exclusive lock on
`<state>/sessions.lock` and re-reads the registry inside it. The file is written
whole and swapped in, so a crash mid-write leaves the previous registry rather
than half of one. A file that will not parse is reported and treated as empty,
and a record of the wrong shape is dropped rather than half-applied. Keys a
record carries that this paddock has no field for are kept on the session and
written back, so a registry shared with a newer paddock does not lose what that
one added.

`backend` is what `sessions.attach` dispatches on: a small dict maps the name to
the module that runs it, and a name this paddock does not have is a message at
attach rather than a registry it refuses to read. `vm_handle` is the name every
`msb` subcommand takes, so a VM session needs no other handle, and the registry
can be reconciled against `msb ls` after a crash.

**Sessions survive Herdr detach and restart.** A microVM keeps running with no
tab attached, and an srt session is just a settings file and a workdir.
Reattaching puts the user back where they were.

**Lifecycle:** when the last tab closes, the session is neither destroyed nor
leaked silently. A session with `keep_alive` set stays; every other one is
dropped from the registry, and its backend is then asked to `collect` the run:
srt has nothing to stop, msb destroys the VM. The run directory is left on disk,
because deleting a workdir would lose work (§8), except for the credential file in
its config dir, which may be an exported token (§4.3) and does not outlive the
session. The prompt that offers keep-alive arrives with the TUI.
Both failure modes cost something real: a discarded microVM loses running state, a
leaked one holds memory.

### 3.5 Pane labels

Panes are labelled `sbx:<session>`, so groupings are visible in the tab bar. An
unlabelled tab is local: no session, no sandbox.

---

## 4. Three enforcement layers

The layers are not equivalent. Layers 1 and 3 are enforced outside the agent.
Layer 2 is enforced by the agent on itself: defence in depth, not a boundary.
Conflating them is the easiest way to make this tool dangerous.

### 4.1 Layer 1: OS-level (hard)

The kernel sandbox enforces:

- **Write paths**: `allowWrite` / `denyWrite`.
- **Read denials**: `denyRead`, for credential directories.
- **Network domains**: `allowedDomains`. Everything else is refused at the
  network layer.

**Tool selection is the weak part, and is a soft allowlist.** The launcher builds
a shim directory of symlinks, one per selected tool, and sets the sandbox `PATH`
to it (plus `/usr/bin` and `/bin` when `include_system_path` is set, so a shell
and coreutils work).

> **Known bypass:** `PATH` only governs bare-name lookup. An agent that runs
> `/opt/homebrew/bin/docker` gets a binary nobody ticked. The shim dir shapes
> what the agent finds and defaults to; it does not stop a determined caller.
>
> Hard per-binary blocking is a **v2 option**: put unselected binaries in
> `denyRead`, which the kernel does enforce. Not in v1 because enumerating every
> binary on a dev machine is expensive and brittle, and getting it wrong breaks
> the shell silently.

All of that is about `srt`. An **`msb` session has no shim dir**: the guest holds
what the image holds, so tool selection is the image, and there is no host binary
behind an absolute path for the bypass to reach (§2.2).

### 4.2 Layer 2: agent config (agent-enforced)

Each adapter generates that agent's own permission config at launch, so the
agent's prompts agree with the sandbox instead of fighting it. This prevents
accidents and cuts prompt noise. It does not stop an agent that decides
otherwise, and the UI says so.

| Agent | Mechanism |
| --- | --- |
| Claude Code | `--settings <json>` with `permissions.allow` / `permissions.deny`; generated `.mcp.json` plus `--strict-mcp-config` |
| Codex CLI | `-c` overrides on the command line |
| OpenCode | generated `opencode.json` with a `permissions` block |

`--strict-mcp-config` is the important one. Without it Claude Code merges MCP
config from user and project scopes and the whitelist leaks. It is passed with
`--mcp-config <file>`, which names the generated file: strict mode on its own
loads no servers at all.

v1 generates the MCP whitelist. The server definitions are read from the agent's
own config files (whichever of its `auth_read_paths` holds an `mcpServers`
object, which for Claude Code is `~/.claude.json`) and filtered to the names the
profile ticked. An empty list means an empty whitelist, not an absent one. The
`permissions` block is not generated yet.

### 4.3 Layer 3: synthesized config dir (hard)

The launcher builds a fresh agent config directory per session, `run_dir/config`,
holding only:

- the credentials that agent needs, by filename. A file it only reads is a
  symlink. A file it writes back to is a **copy**, so the agent keeps working and
  the host's file is never touched: for Claude Code that is `.claude.json`, which
  it rewrites constantly.
- the skills the user ticked (symlinked for srt, copied for microsandbox, where a
  symlink resolves only when its target is inside the same mount, and a host
  skill directory is not), and
- the generated MCP whitelist (§4.2).

The agent is pointed at it, for Claude Code via `CLAUDE_CONFIG_DIR` passed
through `herdr tab create --env` and written into the sandbox command (§1.3), and
its real config dir is denied for reading and writing. The symlinks stay
readable because the settings allow their targets by name (§2.1); denying the
directory and allowing those few paths is what leaves nothing else in it.

This is why it is worth doing: **unselected skills and MCP servers do not exist
inside the sandbox.** Nothing to enumerate, nothing to load, nothing for a
prompt-injected agent to reach. Layer 2 tells the agent not to; layer 3 means
there is nothing there.

Everything else comes from the agent registry (§5): credentials from
`auth_read_paths`, skills from `skills/` under any of the agent's
`config_write_paths`. The chooser offers the same set, so what it lists and what
the sandbox gets are the same thing.

The copy has every `mcpServers` key taken out, at the top level and inside each
project scope, so the generated whitelist stays the only place a server can come
from (§4.2). Everything else in the file, for Claude Code the project list,
travels into the sandbox with it, and a file with no servers in it is copied
unchanged.

**A Keychain login is exported into the config dir when the run is built.** Claude
Code keeps its token in the macOS login Keychain when it uses the default config
dir, and looks for `.credentials.json` inside the directory `CLAUDE_CONFIG_DIR`
names. Verified against the real binary: with the variable set and no such file,
`claude -p` answers `Not logged in · Please run /login`, sandbox or no sandbox. So
when the host has no `~/.claude/.credentials.json`, the launcher runs `security
find-generic-password -s "Claude Code-credentials" -w` and writes what comes back
to `<run dir>/config/.credentials.json`, mode 0600.

**Only the agent's own login is written out.** That Keychain entry also holds a
token per MCP server the user has authorised, under `mcpOAuth`. No whitelist loads
those servers, so their tokens have no business in the run dir: the file gets the
`claudeAiOauth` key and nothing else.

Be clear about what that is: **a copy of the token on disk**, in a directory only
its owner can read, for as long as the session lives. It is deleted when the
session is collected (§3.4). A real credential file always wins: the Keychain is
a fallback source, never a replacement. It is a macOS-only path: without
`security`, without that entry, or with an entry of a shape the launcher does not
recognise, no file is written and the agent asks the user to log in.

**Only Claude Code has a config-dir variable today.** An agent without one is
launched as before: no synthesized directory, and its real config dir stays
readable and writable (§2.1). Adding one is a line in the redirection table.

---

## 5. Agent registry

Data-driven, not hardcoded branching. Built-ins:

| Key | Agent | Command |
| --- | --- | --- |
| `claude` | Claude Code | `claude` |
| `codex` | Codex CLI | `codex` |
| `opencode` | OpenCode | `opencode` |
| `aider` | Aider | `aider` |
| `gemini` | Gemini CLI | `gemini` |
| `shell` | plain shell | `$SHELL` |

Users add or override entries with JSON in `~/.config/paddock/agents/*.json`; a
user file wins over a built-in of the same key. The chooser writes one of these
files when the user types a command instead of picking an agent, because a
profile names a registry key, not a command. It refuses a key that already runs
something else: a user file replaces an entry whole, so overwriting one would
drop its domains and credential paths for every profile that names it. Each
entry:

| Field | Meaning |
| --- | --- |
| `name` | Display name |
| `command` | Executable run inside the sandbox |
| `api_domains` | Domains the agent needs; merged into the allowlist when selected |
| `auth_read_paths` | Credential paths auto-allowed for reading |
| `config_write_paths` | Paths it legitimately writes (history, session state) |
| `image` | OCI image the `msb` backend boots for this agent. Blank means `alpine` (§2.2) |

### Auth policy

**Only the selected agent's own config directory is auto-allowed.** Choosing
`claude` grants nothing to Codex's credentials, and no agent gets `~/.ssh`,
`~/.aws`, `~/.gnupg` or `~/.config/gh`. Those are denied by default (§6) and
stay denied unless the user says otherwise.

The reasoning: an agent needs its own key to work at all, so auto-allowing it is
the difference between a usable tool and a prompt on every launch. Every other
credential is a lateral-movement target with no bearing on whether the agent
runs.

---

## 6. Profiles

JSON files in `~/.config/paddock/profiles/`, loaded into a dataclass. A profile is
exactly the set of answers the chooser asks for, so saving and loading are
lossless.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | `str` | `"custom"` | Profile name and filename stem |
| `agent` | `str` | `"claude"` | Registry key |
| `tools` | `list[str]` | `["git", "rg", "curl"]` | Binaries in the PATH shim dir |
| `include_system_path` | `bool` | `true` | Append `/usr/bin:/bin` |
| `network_presets` | `list[str]` | `["anthropic", "github"]` | Named domain groups (anthropic, github, npm, pypi/uv, go, crates.io, homebrew) |
| `extra_domains` | `list[str]` | `[]` | Extra allowed domains |
| `shared_dir` | `str` | `""` | Host dir, read-write; **`""` means an isolated scratch workdir** |
| `skills` | `list[str]` | `[]` | Skills put in the synth config dir |
| `mcp` | `list[str]` | `[]` | MCP servers allowed |
| `deny_read` | `list[str]` | `["~/.ssh", "~/.aws", "~/.gnupg", "~/.config/gh"]` | Hard read denials |
| `extra_allow_write` | `list[str]` | `[]` | Writable paths beyond workdir, run dir and `/tmp` |

Notes:

- `shared_dir = ""` is the safe default: a fresh scratch workdir, nothing on the
  host tree touched.
- `deny_read` defaults apply to user-written profiles too. A profile that wants a
  credential directory readable has to say so.
- The effective allowlist is `network_presets` expanded, plus `extra_domains`,
  plus the agent's `api_domains`, deduplicated.
- Two profiles ship built in: `claude-default` (Claude Code with the usual dev
  tools and registries) and `offline-shell` (a plain shell, no network). A user
  file of the same name **replaces** the built-in whole: fields the file leaves
  out fall back to the defaults in the table above, not to the built-in's values.
- The filename is the profile name, so a saved profile writes back to the file it
  came from. A profile file that will not parse, or that has a field of the wrong
  type, including a list holding anything but strings, is skipped whole, never
  half-applied.
- `PADDOCK_CONFIG_DIR` overrides `~/.config/paddock`, for both `profiles/` and
  `agents/`. Tests point it at a temporary directory.

---

## 7. Module plan

The first epic (`sandbox_core_launcher`) built these as separate feature PRs, tests
first, and `microvm_backend` adds the second backend to the same shape. Each module
should be small, and mostly plain functions over a `Profile`:

| Module | Responsibility | Status |
| --- | --- | --- |
| `paddock/sessions.py` | Session registry in `~/.local/state/paddock/`: create, list, attach, lifecycle (§3), and the backend dispatch dict (§2) | Done; workspace bindings (§3.3) wait for the TUI |
| `paddock/profiles.py` | `Profile` dataclass, network presets, tool candidates, load/save | Done |
| `paddock/agents.py` | Agent registry and per-agent layer-2 config | Registry done; the layer-2 `permissions` block is not generated yet |
| `paddock/backends/srt.py` | srt settings JSON, PATH shim dir, `prepare()` / `open_pane()` | Done |
| `paddock/backends/microsandbox.py` | msb: boot the session's VM, exec a tab into it, destroy it (§2.2) | Shell sessions done; agents in the guest are next |
| `paddock/herdr_client.py` | Subprocess wrapper over the herdr CLI: the one seam tests mock | Done |
| `paddock/synth_config.py` | Layer 3: build the config dir from credentials plus ticked skills | Done for Claude Code; other agents have no redirection (§4.3) |
| `paddock/tui.py` | The questionary chooser: questions in, one plan out | Done; the workspace default binding (§3.3) is not asked about |
| `paddock/cli.py` | Entry point: `choose` (default), `launch <profile>`, `attach <session>`, `profiles`, `init` | Done |
| `paddock/init.py` | `paddock init`: splice the keybinding into herdr's config, back it up, reload (§1.1) | Done; the plugin manifest (§1.4) is v1.1 |

One constraint runs through all of it: **only `herdr_client.py` shells out to
`herdr`, and only a backend shells out to its own sandbox runtime**, `srt` or
`msb`. Everything else is pure functions, which is what makes the behaviour
testable on a Linux CI runner with no sandbox present.

---

## 8. Open questions

- Should the popup offer "attach to the session you used last" as a zeroth
  option? Most launches are probably repeats.
- Should `deny_read` be enforced by the backend rather than the profile, so a
  malformed profile cannot widen it?
- How should a pane show what permissions it actually got? A written manifest in
  the workdir is the current favourite.
- What happens to an srt session's run directory when the session is collected?
  The credential file goes, because it may be an exported token (§4.3). The rest
  stays: deleting a workdir loses work, keeping it leaks disk.
- Can a tab move between sessions after creation, or is detach-and-relaunch the
  honest answer, given that srt cannot migrate a running process tree?
- Is v2 per-binary blocking (§4.1) worth the enumeration cost, or is the honest
  answer that PATH shimming is a usability feature and the network and write
  boundaries are the real security story?
