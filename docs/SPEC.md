# paddock — Specification

Status: **pre-alpha.** Nothing in section 7 is implemented.

paddock takes over new-window creation in [herdr](https://herdr.dev) (a terminal
multiplexer for AI coding agents, **v0.8.0**) and replaces it with a popup
chooser. Per window, the user picks a plain local tab or an agent in a sandbox —
and for a sandbox, which agent, tools, network, files, skills and MCP servers it
gets.

The rule behind the design: **every permission is an active choice.** No "allow
everything" default, no authority inherited from the host shell.

Sections marked v1.1 say where the design is going, so v1 decisions are made with
the destination in view. They are not a build list and are not stubbed in code —
see [CONTRIBUTING.md § Design principles](../CONTRIBUTING.md#design-principles).

Diagrams: [`architecture.puml`](diagrams/architecture.puml),
[`launch_sequence.puml`](diagrams/launch_sequence.puml),
[`scoping_model.puml`](diagrams/scoping_model.puml).

---

## 1. Herdr integration

Verified against a local herdr 0.8.0.

### 1.1 Keybinding

In `~/.config/herdr/config.toml`:

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

### 1.2 Environment in the popup

Herdr exports these, and they are how the chooser knows where it was invoked:

| Variable | Use |
| --- | --- |
| `HERDR_SOCKET_PATH` | herdr server socket |
| `HERDR_ACTIVE_WORKSPACE_ID` | Workspace to create the tab in |
| `HERDR_ACTIVE_PANE_ID` | Pane the popup was launched over |

Treat all three as optional. Run outside herdr — during development, say — the
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
tab creation instead of being smuggled into the command string.

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
of no window.

### 1.4 Packaging as a herdr plugin (later)

Herdr 0.8.0 has a plugin system (`herdr plugin install|link|enable|list`,
`herdr plugin action`, `herdr plugin pane`). A later milestone adds a
`herdr-plugin.toml` manifest so paddock installs as a plugin — `herdr plugin
link` in development, `herdr plugin install` for users — instead of a hand-edited
keybinding. The keybinding keeps working; the manifest is sugar over it.

---

## 2. Backends

One interface:

```
Backend.launch(profile, workdir) -> pane command
```

It returns the command string that `herdr pane run` executes. Backends never
create panes. That keeps them testable with no herdr server and keeps the herdr
client mockable (§7).

The interface exists because v1 needs it, to keep srt's settings and invocation
out of the chooser — not in anticipation of a second backend.
`backends/microsandbox.py` is described below and is not created until it is
built.

### 2.1 v1 — `srt` (Anthropic sandbox-runtime)

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
    "allowRead":  [],
    "allowWrite": ["/path/to/workdir", "/tmp"],
    "denyWrite":  []
  }
}
```

Three defaults shape everything else:

- **Reads are allowed by default.** `denyRead` is a blocklist, which is why every
  profile ships a deny list for credential directories (§6). Leave it out and the
  agent can read them.
- **Writes are denied by default.** `allowWrite` gets the workdir, the run dir,
  `/tmp`, and the shared directory if there is one.
- **Network is allowlist-only.** Anything not listed is refused.

**Invocation:**

```sh
srt --settings <settings-file> "<command>"
```

The inner command is the agent, wrapped so `PATH` points at the shim dir (§4.1).

### 2.2 v1.1 — `microsandbox` (design record, not stubbed in v1)

`msb` runs workloads in libkrun microVMs from OCI images, with volume mounts and
a host/port network policy. A harder boundary than Seatbelt — its own kernel
rather than a filtered view of the host's — at the cost of an image per agent and
a heavier, still sub-second, start.

The same profile maps across:

| Profile field | `srt` | `microsandbox` |
| --- | --- | --- |
| `agent` | command on host `PATH` | agent's `image` (§5) |
| `tools` | PATH shim dir | baked into the image |
| `shared_dir` | `filesystem.allowWrite` entry | volume mount |
| isolated workdir | scratch dir under the run dir | VM filesystem |
| `network_presets` | `network.allowedDomains` | host/port rules |

It also gives each sandbox a `<name>.localhost` URL, so a dev server inside a
sandbox is reachable from the host browser with no port forwarding. That is why
the agent registry has an `image` field now (§5): profiles written for `srt` port
over without a schema change.

---

## 3. Sandbox sessions

**The boundary is always a process tree in a pane, never a Herdr UI structure.**
Tabs and workspaces organise the interface; they enforce nothing.

Tabs attach to a **session**: one running sandbox with a name.

| Backend | A session is |
| --- | --- |
| `srt` (v1) | A policy context: one settings file plus one shared workdir |
| `microsandbox` (v1.1) | A persistent microVM |

Every tab attaches to one session or to none. That one rule covers every layout
that would otherwise need its own mode:

- **Whole workspace** — every tab on one session, via the workspace default
  binding (§3.3).
- **Tab group** — some tabs on one session, their siblings local or on another.
- **Side by side** — several sessions in one workspace, each with its own name,
  backend and profile.
- **Local orchestration** — an unsandboxed tab driving the sandboxed ones through
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
name. **Attach** lists live sessions with name, backend, profile and attached tab
count, so the choice is made on what a session is, not on remembering its name.

### 3.2 Attach means different things per backend

This is why the session list shows the backend:

| | `srt` (v1) | `microsandbox` (v1.1) |
| --- | --- | --- |
| Attaching | New process under the same settings file and workdir | Execs a shell or agent into the same guest |
| Filesystem | Shared — one workdir on the host | Shared — one guest filesystem |
| Processes | **Separate trees**; tabs cannot see each other's | Shared namespace |
| Long-running state | Only what is on disk | Lives in the VM, outlives any tab |

So tab groups already work in v1 with `srt`: attached tabs share policy and
files, which is most of why people want a group. Shared *runtime* arrives with
`msb`. Seatbelt and bubblewrap wrap a process tree and have no guest for a second
process to join — srt can share policy and files, never a runtime. The UI must
not imply otherwise.

### 3.3 Workspace default binding

An optional binding — *new tabs in workspace W attach to session S* — stored in
the plugin state dir, set and unset per workspace. It saves re-asking in a
workspace dedicated to one sandbox.

It is a default answer to the chooser's first question, not a separate mode. The
chooser is still reachable, it can be overridden per tab, and removing it changes
nothing about the sessions.

### 3.4 Session registry

Sessions are tracked in the plugin state dir:

| Field | Meaning |
| --- | --- |
| `session_id` | Internal id |
| `name` | Shown in the chooser and pane labels |
| `backend` | `srt` or `microsandbox` |
| `profile` | Profile the session was created from |
| `attached_panes` | Pane ids currently attached |
| `vm_handle` | microsandbox VM handle; `null` for srt |
| `created_at` | Timestamp |

**Sessions survive Herdr detach and restart.** A microVM keeps running with no
tab attached, and an srt session is just a settings file and a workdir.
Reattaching puts the user back where they were.

**Lifecycle:** when the last tab closes, the session is neither destroyed nor
leaked silently. The user is prompted, with a keep-alive option, and unclaimed
sessions are collected. Both failure modes cost something real: a discarded
microVM loses running state, a leaked one holds memory.

### 3.5 Pane labels

Panes are labelled `sbx:<session>`, so groupings are visible in the tab bar. An
unlabelled tab is local: no session, no sandbox.

---

## 4. Three enforcement layers

The layers are not equivalent. Layers 1 and 3 are enforced outside the agent.
Layer 2 is enforced by the agent on itself — defence in depth, not a boundary.
Conflating them is the easiest way to make this tool dangerous.

### 4.1 Layer 1 — OS-level (hard)

The kernel sandbox enforces:

- **Write paths** — `allowWrite` / `denyWrite`.
- **Read denials** — `denyRead`, for credential directories.
- **Network domains** — `allowedDomains`. Everything else is refused at the
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

### 4.2 Layer 2 — Agent config (agent-enforced)

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
config from user and project scopes and the whitelist leaks.

### 4.3 Layer 3 — Synthesized config dir (hard)

The launcher builds a fresh agent config directory per run holding only:

- the credentials that agent needs, and
- the skills the user ticked (symlinked for srt, copied for microsandbox, which
  cannot follow them).

The agent is pointed at it — for Claude Code via `CLAUDE_CONFIG_DIR`, passed
through `herdr tab create --env` — and the real config dir stays outside the
readable set.

This is why it is worth doing: **unselected skills and MCP servers do not exist
inside the sandbox.** Nothing to enumerate, nothing to load, nothing for a
prompt-injected agent to reach. Layer 2 tells the agent not to; layer 3 means
there is nothing there.

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
user file wins over a built-in of the same key. Each entry:

| Field | Meaning |
| --- | --- |
| `name` | Display name |
| `command` | Executable run inside the sandbox |
| `api_domains` | Domains the agent needs; merged into the allowlist when selected |
| `auth_read_paths` | Credential paths auto-allowed for reading |
| `config_write_paths` | Paths it legitimately writes (history, session state) |
| `image` | *Future* — OCI image for the microsandbox backend |

### Auth policy

**Only the selected agent's own config directory is auto-allowed.** Choosing
`claude` grants nothing to Codex's credentials, and no agent gets `~/.ssh`,
`~/.aws`, `~/.gnupg` or `~/.config/gh` — those are denied by default (§6) and
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

---

## 7. Module plan (not yet implemented)

The first epic (`sandbox_core_launcher`) builds these as separate feature PRs,
tests first. Each should be small — mostly plain functions over a `Profile` — and
no v1.1 concern appears in any of them:

| Module | Responsibility |
| --- | --- |
| `paddock/sessions.py` | Session registry in `~/.local/state/paddock/`: create, list, attach, workspace bindings, lifecycle (§3) |
| `paddock/profiles.py` | `Profile` dataclass, network presets, tool candidates, load/save |
| `paddock/agents.py` | Agent registry and per-agent layer-2 config |
| `paddock/backends/srt.py` | srt settings JSON, PATH shim dir, `Backend.launch()` |
| `paddock/herdr_client.py` | Subprocess wrapper over the herdr CLI — the one seam tests mock |
| `paddock/synth_config.py` | Layer 3: build the config dir from credentials plus ticked skills |
| `paddock/tui.py` | The questionary chooser |
| `paddock/cli.py` | Entry point: `choose` (default), `launch <profile>`, `profiles`, `sessions` |

One constraint runs through all of it: **only `herdr_client.py` shells out to
`herdr`, and only the backend shells out to `srt`.** Everything else is pure
functions, which is what makes the behaviour testable on a Linux CI runner with
no sandbox present.

---

## 8. Open questions

- Should the popup offer "attach to the session you used last" as a zeroth
  option? Most launches are probably repeats.
- Should `deny_read` be enforced by the backend rather than the profile, so a
  malformed profile cannot widen it?
- How should a pane show what permissions it actually got? A written manifest in
  the workdir is the current favourite.
- What happens to an srt session's workdir when the session is collected?
  Deleting loses work; keeping it leaks disk.
- Can a tab move between sessions after creation, or is detach-and-relaunch the
  honest answer, given that srt cannot migrate a running process tree?
- Is v2 per-binary blocking (§4.1) worth the enumeration cost, or is the honest
  answer that PATH shimming is a usability feature and the network and write
  boundaries are the real security story?
