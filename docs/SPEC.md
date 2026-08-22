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

`type = "popup"` runs the command in an overlay, which is where the chooser
draws its form. Plain new-tab moves to `prefix+shift+c`.

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
Backend.prepare(profile) -> run             # everything the run needs, written or booted
Backend.load_run(run_dir) -> run            # a prepared run read back, for a later tab
Backend.open_pane(run, label) -> pane id    # a tab on a prepared run
Backend.collect(run_dir, vm_handle)         # nobody is attached: stop what is running
```

`open_pane` raises `SandboxGone` when the sandbox the run names is not there any more,
and sessions ends that session rather than leaving a tab that cannot open (§3.4).
`collect` is given the registry's `vm_handle` as well as the run dir, so a run
directory that lost its record does not leak a VM.

They are separate because a session is prepared once and attached to many times
(§3.2), and because what a session leaves running outlives its last tab. A backend
works the whole launch out as plain functions, then opens the tab and starts the
command through `herdr_client` (§7). `herdr_client` is the seam every test mocks, so
a backend is still testable with no herdr server and no sandbox. Sessions (§3) drive
all four calls; a backend knows nothing about the registry. Which module runs a
session is the `backend` name in its record (§3.4), looked up in a dict of name to
module in `sessions.py`.

Every backend uses the same run directory, `<state>/runs/<timestamp>-<random>/`. Three
things in it are the same whichever backend wrote it: `launch.json`, holding what a
later tab attaches with, `launch.sh`, holding the command, because the pane is sent a
line and not a file (§1.3), and `pane.log`, where that script keeps the launch's
stderr (§9). `backends/__init__.py` holds those three and nothing else, which is why
a failed msb launch is held and replayed exactly as a failed srt one is: the wrapper
is shared, and only the command inside it is the backend's. What else goes in the
directory is the backend's own business.

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
  },
  "allowPty": true
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

**`allowPty` is on, and it is wider than its name suggests.** A TUI agent puts
its terminal in raw mode. Without this key Seatbelt denies the file ioctl on the
pane's `/dev/ttysNNN`, so `stty` fails with `EPERM`: claude draws gibberish and
takes no typing, and codex exits the moment it starts. The cost is that the key
compiles to srt's own hardcoded grant of read, write and ioctl on the regex
`^/dev/ttys`. A sandboxed agent can therefore write to **any** terminal the user
owns, not just its own pane, which is enough to fake output in another window.
srt 0.0.73 has no narrower knob, so the choice is TUI agents with this grant or
no TUI agents at all. paddock takes the grant, deliberately, and says so here
rather than leaving it out.

**Local services are a separate grant, and the domain allowlist cannot make it.**
An agent that calls a server on this machine — a local model, a dev server, a
database — fails without it, and the failure is a permission error, not a
connection error:

```
Error: Head "http://127.0.0.1:11434/": dial tcp 127.0.0.1:11434: connect: operation not permitted
```

Adding `localhost` to `allowedDomains` changes nothing, because the allowlist is
enforced by the proxy and loopback never reaches it. srt sets its own
`NO_PROXY=localhost,127.0.0.1,::1,169.254.0.0/16,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16`
in the shell it spawns, so a client aimed at loopback dials it directly, and
Seatbelt refuses the connect with `EPERM`. The one key that changes it is
`network.allowLocalBinding`, which compiles to three lines of the Seatbelt
profile:

```scheme
(allow network-bind (local ip "*:*"))
(allow network-inbound (local ip "*:*"))
(allow network-outbound (remote ip "localhost:*"))
```

**Two things follow from those lines, and both are wider than "reach my ollama".**
The outbound rule takes no port, so the grant is every service listening on this
machine's loopback: a sandbox given it for a model server on 11434 reaches a
local admin panel on 18099 just as well, and the domain allowlist is not
consulted for either. The bind and inbound rules take no address, so the sandbox
can also open a listening socket on `0.0.0.0` and be reached from the LAN — both
measured against srt 0.0.73. Everything else still holds: a non-loopback host
outside `allowedDomains` is still refused by the proxy (403), and `denyRead` /
`denyWrite` are untouched.

**Port scoping is not available for the case that needs it.** `allowedDomains`
does take a `:port` suffix, and `127.0.0.1:11434` is enforced — but only for
traffic that reaches the proxy, which means clearing `no_proxy` in the launch
environment first. Go clients cannot be routed that way at all: the standard
library's `httpproxy.useProxy` returns `false` for `localhost` and for any
`ip.IsLoopback()` before it ever looks at `NO_PROXY`, so `ollama`, `docker` and
every other Go CLI dials loopback direct whatever the environment says. Port
scoping would therefore work for `curl` and fail for the tools people actually
point at a local model, so paddock does not trim `no_proxy` and does not pretend
to scope: it takes the whole-loopback grant and says so on the confirm screen.

**It is written only when the profile asks for it.** The
`local services (localhost)` network preset is the way to tick it, and no
built-in profile ships with it on. `build_settings` emits the key when the
profile's *resolved* domains name loopback under any spelling — `localhost`,
`127.0.0.1`, `::1`, `[::1]`, with or without a `:port` suffix — which covers the
preset, a domain typed into the extra-domains box, and a local-model agent entry
whose `api_domains` declare it. Anything else gets no `allowLocalBinding` key at
all, so the default is the denial above.

**For `microsandbox` the answer is different, and worse.** A guest has its own
kernel, so host loopback is not the guest's loopback: the spike measured a
default-network guest reaching nothing on a host listener — not `127.0.0.1`, not
the gateway `172.16.0.81`, not the host's LAN address — and `--net host` alone
did not change it. The gateway is a router, not a proxy to host services, so
there is no `host.docker.internal` equivalent to aim at (every such name is
`NXDOMAIN`). What works is naming the host's real address in a rule:
`msb run --net-rule "allow@<host LAN address>" ...`, or the blunter
`--net private`. That is only half of it, because a host-side server bound to
loopback is unreachable from another kernel no matter what the rule says —
`ollama` binds `127.0.0.1:11434` by default, so it also has to be started with
`OLLAMA_HOST=0.0.0.0`, which exposes it to the LAN. The stable answer is
`--vsock HOST_PATH:PORT`, host IPC with no address to expose; it was not
exercised in the spike and is the thing to build the msb side of this preset on.
Until then the preset is **srt-only**: `net_rules` (§2.2) turns its two entries
into `allow@localhost:tcp:443` and `allow@127.0.0.1:tcp:443`, which name the
guest's own loopback on a port nothing is listening on, so an msb session ticking
it gets no host service and no error saying why. The chooser should say so before
that gap is closed.

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
same script. A script that is not the one this paddock would write is replaced
when a tab attaches, which covers a run directory with none (one prepared before
paddock wrote them) and one an upgrade has moved on from: `launch.json` holds
the exact command either way. What the script does around that command, and what
a failed launch leaves behind, is §9.

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
Nothing in this section is estimated: it was measured, in that spike or against
`msb` 0.6.13 while the backend was built.

**A session is a persistent VM.** `prepare()` boots it, every tab execs into it, and
`collect()` destroys it when the last tab is gone:

| Session operation | `msb` |
| --- | --- |
| create | `msb create --name <handle> --mount-dir <workdir>:/work --workdir /work [--mount-dir <config>:/paddock-config -e <config var>] <net rules> <image>`, then the boot script for an agent |
| attach a tab | `msb exec --tty <handle> [-- <agent> <flags>]`, in `launch.sh` like any other pane command |
| collect | `msb rm -f <handle>`, best effort. Without `-f`, `msb rm` refuses a running sandbox: `sandbox still running`, exit 1 |

The handle is `paddock-<run dir name>`. It has to be unique among live sandboxes on
the host, not only among paddock sessions, because `msb create` fails on a name
collision. The session record keeps it as `vm_handle` (§3.4), which is the fallback
handle when a run directory has lost its `launch.json`. A VM that is already gone is
reported and not raised: the session is over either way. Removal is best effort. A pane
closing is no place to raise, so an `msb` that refuses or cannot be reached leaves the VM
up, and the message names the handle and the `msb rm -f` that finishes the job.

**A VM outlives its last tab, and then some.** Nothing watches herdr, so a session
whose tabs have all closed is collected at the next paddock invocation rather than
when the tab closed (§3.4). Until then the microVM is still running and still holding
memory. `paddock gc` is the explicit run, and `msb ls` is how to check.

**Attaching checks the VM first**, with `msb ls --format json`. `msb exec` into a VM
that is gone fails after the pane exists, which leaves a dead tab and a pane id nothing
can use. A session whose VM has gone is dropped from the registry there and then, the
same ending its last tab closing would have given it. A stopped sandbox counts as gone:
its record survives, but nothing can exec into it.

**An msb tab always opens in the guest workdir.** `paddock attach <session> --cwd <dir>`
is refused on an msb session: that flag sets the host tab's own directory, which the
guest shell replaces, so honouring it silently would be a lie. On srt it still does what
it says.

The same profile maps across:

| Profile field | `srt` | `microsandbox` |
| --- | --- | --- |
| `agent` | command on host `PATH` | the agent's `image` and `install` (§5); `shell` gets `alpine` |
| `tools` | PATH shim dir | baked into the image |
| `shared_dir` | `filesystem.allowWrite` entry | `--mount-dir <resolved path>:/work`, read-write |
| isolated workdir | scratch dir under the run dir | that same directory, mounted at `/work` |
| `network_presets` | `network.allowedDomains` | `--net-default deny`, then one allow rule each |
| `deny_read` | `filesystem.denyRead` | nothing to deny: an unmounted path is not in the guest |
| `skills`, `mcp` | synthesized config dir (§4.3) | that same directory, mounted at `/paddock-config` |

Four of those differ in kind, not in spelling:

- **Mount sources are resolved.** `msb` mounts the path as written, and `/tmp` is a
  symlink to `/private/tmp` on macOS, which fails as a mount source. The backend
  passes `Path.resolve()` for every mount.
- **Network rules name a host, a protocol and a port, not a URL path.** srt allows a
  domain through its proxy; an msb rule is `allow@<domain>:tcp:443`, so it is https to
  that host and nothing else. A profile with no domains gets no network at all, DNS
  included.
- **DNS is all or nothing.** `allow@dns` goes in with the first allowed domain, and it
  opens the gateway resolver for **every** name, not only the allowed ones. Lookups of
  anything else succeed and the connection is then refused. srt's proxy sees the request
  itself, so it has no equivalent hole. Names are a low-bandwidth channel out, and this
  is the honest limit of the msb network policy.
- **The PATH shim dir has no job here.** The guest holds what the image holds, so the
  image is the tool selection, and the absolute-path bypass §4.1 documents is not
  available: `/opt/homebrew/bin/docker` is not in the guest to be run.

#### Provisioning an agent in the guest

**An agent needs an image, and usually an install.** Both come from the registry (§5).
`prepare` runs a fixed order, and the order is the point:

1. build `run_dir/config` (§4.3) **without the token**,
2. `msb create`, with that directory mounted **read-only** at `/paddock-config-src` and
   `-e CLAUDE_CONFIG_DIR=/paddock-config`,
3. the boot script, which installs the agent when the image lacks it,
4. **only then** write the token into `run_dir/config`,
5. one exec copying the mount onto the guest's own filesystem:
   `mkdir -p /paddock-config && cp -a /paddock-config-src/. /paddock-config/`.

```sh
msb exec --timeout 110s <handle> -- /bin/sh -c \
  'command -v claude >/dev/null 2>&1 || npm install -g @anthropic-ai/claude-code@2.1.239'
msb exec --tty <handle> -- claude --mcp-config /paddock-config/.mcp.json --strict-mcp-config
```

Anything that fails from step 2 on deletes the token and removes the VM before raising, so
a launch that never finished leaves neither behind. Deferring the token means a failed
install never had one on disk to begin with. This is not theoretical: one live install hung
and the launch ended with no VM, no token and no session record.

**Two timeouts, and the outer one matters.** The boot execs pass `--timeout 110s`, inside
paddock's own 120s limit on any `msb` command. msb's timer covers the command once it is
running in the guest, so an exec that never gets that far is not on it: the hang above ran
past 110s and was caught by the outer limit. Both are needed. The spike saw the same hang
once and could not reproduce it.

**The install is pinned.** `install` names a version (`@2.1.239` today), so a session
cannot pick up a new agent release on its own and two sessions a week apart run the same
binary. Bumping it is a one-line registry edit.

An agent with no image is refused at create, before a VM is booted: the guest holds what
the image holds, so there would be nothing to run. `shell` is the exception that needs
neither, and attaches to whatever shell the image ships. An agent that has an image but no
config-dir redirection (§4.3) boots and says on stderr that it starts unauthenticated:
nothing carries its credentials in.

**The install runs once per session, not once per image.** Measured on `msb` 0.6.13 on
the spike's machine, with `claude` (`node:22-slim`, msb's default 1 CPU and 512 MiB, which
it runs in):

| Step | Cold, first session on the image | Warm, image in the layer cache |
| --- | --- | --- |
| pull `node:22-slim` | 20.1s | none |
| `msb create` | 0.15s | 0.15s |
| `npm install -g @anthropic-ai/claude-code` | 20.9s | 20.9s |
| to a usable `claude` | about 41s | about 21s |

The first two rows are one command: `msb create` pulls the image when the layer cache does
not have it, so the cold column is a decomposition of that step, not two waits in a row.

`paddock launch <profile> --backend msb` measured 22.1s warm, from the command to a tab
with Claude Code running in the guest.

msb's layer cache saves the pull, not the install. Every session is a new sandbox with its
own clone of the image, and a sandbox that installed `claude` leaves nothing for the next
one: `claude` is absent again in a second sandbox from the same image. An image that
already ships the agent pays neither, which is what the `command -v` guard is for, but
building one is not paddock's job today. That 21s is the honest price of an msb agent
session.

**The profile has to allow what the install downloads, for the whole session.** The boot
script runs under the same deny-by-default network as everything else in the guest, so a
`claude` session needs the `npm` preset as well as `anthropic`. Nothing is added to the
allowlist behind the user's back. What cannot be done is take it away again after the
install: network rules are fixed at `msb create` and `msb modify` has no network option, so
the registry the agent needed for one minute stays reachable for as long as the session
lives. A prebuilt image would close that, and is the strongest argument for building one.

**Layer 3 arrives as a mount and one variable.** `msb create -e CLAUDE_CONFIG_DIR` points
the agent at `/paddock-config`. Verified on 0.6.13: a variable set on `create` reaches every
later `exec`, the interactive shell `msb exec --tty` attaches to included, so the host tab
passes no environment at all. The directory holds copies rather than symlinks, because a
link to a host path leads outside the mount (§4.3). The host's own config dir needs no deny
rule, unlike srt: it is simply not in the guest.

#### What the guest actually is

Three facts that "its own kernel, and only what is mounted" does not convey. All three
were measured on `msb` 0.6.13.

- **The guest runs as root.** `id` in a paddock guest is `uid=0(root)`, because that is
  what the image's default user is and paddock passes no `--user`. It is root in the
  guest only: a different kernel, and the only host state it reaches is what is mounted.
  It does mean anything running in the session can write anywhere in the guest, so the
  image is not a boundary within the session.
- **`/.msb` is mounted, and paddock did not ask for it.** msb gives every guest a
  read-write virtiofs mount at `/.msb` (`msb_runtime`), holding the rootfs layers, its
  script directory and its TLS material. It is backed by
  `~/.microsandbox/sandboxes/<handle>/runtime/` on the host, and the guest can create
  files there. So "only what is mounted exists" is exact, but paddock's mount list is
  not the whole list: msb adds its own, per sandbox, and it goes when the sandbox does.
- **The agent's config dir in the guest is a copy, and only the copy.** paddock mounts
  `run_dir/config` read-only at `/paddock-config-src` and the guest copies it to
  `/paddock-config`, which is what `CLAUDE_CONFIG_DIR` names. So the guest reads the
  credentials and skills the run dir holds, and everything it writes back, the rewritten
  `.claude.json`, its session state, and the MCP whitelist it loads, lives on the guest's
  own filesystem and dies with the VM. Two consequences worth stating: an agent's config
  changes do not survive the session, and an agent talked into rewriting its own
  `.mcp.json` rewrites a file in the guest that no later session reads. Under srt the same
  directory is writable and shared, because there is no guest to copy it into.
- **Guest writes into `shared_dir` come back changed.** A file the guest creates as
  `-rw-r--r-- root root` arrives on the host owned by you, mode `600`, carrying a
  `user.msb.override_stat` extended attribute, which is how msb keeps the guest's view
  of ownership. Files the guest never touches are untouched. Under srt the agent writes
  as you, with your umask, so this is a visible difference for anything that shares a
  directory with tools on the host.

Not built, and not stubbed:

- **A prebuilt agent image.** Every session installs the agent again, and no paddock
  command builds or publishes an image that would skip it.
- **Port forwarding.** `-p <host port>:<guest port>` on `msb create` works and binds
  loopback, but no profile field asks for one. A per-sandbox `<name>.localhost` URL is
  a portless feature paddock would have to build (see [ROADMAP](ROADMAP.md)), not
  something `msb` provides.
- **A memory budget and a session cap.** Memory, not boot time, caps how many VM
  sessions run at once: an agent VM that has installed a toolchain holds 0.8GB to
  1.3GB resident and does not give it back. An idle shell VM settles around 65MB.
  Nothing enforces a cap.
- **A memory setting for the guest.** Every VM gets msb's default 512 MiB, which Claude
  Code runs in, and no profile field changes it. A process the guest kernel kills for
  running out of memory dies inside the guest, so paddock has nothing to report about it:
  the pane shows whatever the agent showed.
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

One screen, drawn by `paddock/screen.py`, over an answers dict that
`paddock/tui.py` keeps consistent. The fields are `Open`, `Profile`, `Backend`,
`Agent`, `Tools`, `Network`, `Files`, `Skills` and `Advanced`, each showing the
value it currently holds and, for the field the cursor is on, one line saying
what that value means.

**Open** is the session decision: a new sandbox, an ordinary local tab, or any
live session to attach a second tab to. Live sessions are on that same list with
their name, backend, agent, profile and tab count, so the choice is made on what
a session is, not on remembering its name:

```
  > review [srt]: claude / hardened, 2 tabs
```

The backend is in the label because attaching means a different thing on each one
(§3.2). That list also answers §8's open question about offering the last session
as a zeroth option: every session is one field away.

**Backend** is which sandbox runs it (§3.2). A backend this machine has no binary
for stays on the list and says why it cannot be chosen, rather than vanishing.

The popup is 70% of the terminal minus herdr's sidebar and border, so an ordinary
one is about 48 by 18 and only a large terminal gives the 80 by 24 the design is
drawn to. Every screen scrolls its rows rather than clipping them, and pins what
must never scroll off: the cursor, the hint, what has been typed, and the
confirm's buttons. They grow to fill a bigger popup up to 110 columns, centred,
because a line much past that is hard to read back, and a checklist takes another
column when the width holds one.

The keys match herdr's navigate mode: arrows with `hjkl` beside them, enter to
take, escape to back out one level, and a digit to jump to a field. Every list
and checklist draws the way back as its first row as well, so the key is never
the only way anyone could find. The form has no such row, because nothing is
before it. Two promises hold everywhere. **Escape never loses an answer**: it closes what is open and
leaves the value it was editing in place. **Ctrl-c cancels the whole popup**, at
any depth, and costs nothing, because the chooser returns a plan and `cli.py` is
the only thing that acts on one. Filtering a long list is a mode `/` opens, so
every letter stays a shortcut everywhere else.

**An agent this machine cannot run** is on the Agent list and says
`(not installed)`, the way a tool the host lacks does and a backend without its
binary does. Enter on it gives the reason instead of launching, because the tab
it would open dies on `No such file or directory` before the user sees anything.
What counts as cannot run depends on the backend, because the two run an agent
from different places. On srt it is the host's own binary, so a missing
`command` stops it, and so does a missing `required_tools` entry (§5): a script
with nothing to run it is not an agent either. A command written as a path is
left alone, which is what the `shell` agent's `$SHELL` is. On msb the host PATH
says nothing at all, because the guest holds what its image holds and installs
the rest (§2.2), so what stops an agent there is having no image to boot, which
is what the backend itself refuses on. A registry entry whose command cannot even
be parsed is refused with the parse error as its reason: this is drawn for every
agent on the list, before anything is chosen, so it may not raise.

**Nothing the popup was asked to do dies without a screen.** The popup is
transient (§1.1): it closes when `paddock` exits, so a message printed after the
form has gone is written to a terminal nobody is left looking at. Two screens
close that hole.

- Before the launch, `screen.progress` says what it is about to do: the image it
  pulls, the agent it installs in the guest, and how long the first start takes.
  It is printed once and left there rather than drawn and animated, because the
  call it stands in front of blocks the whole process. An msb launch that has to
  install an agent takes about 40 seconds with the image already pulled, and used
  to be a frozen form with nothing on it.
- The confirm and the failure screen both scroll, the way the form scrolls its
  fields, with their buttons pinned to the bottom. Nothing is elided: at 48 by 18,
  the smallest popup the design admits, every line of the grant and every word of
  a failure is reachable, because saying them in full is what those screens are
  for. The confirm describes the backend it is about to use: a microVM session
  says what its guest is, not a list of host paths that are not mounted into it.
- After a launch that never opened a pane, `screen.failed` shows what went wrong
  and where the log is, with two ways on: **← Back to the form**, which reopens it
  with every answer that made the plan, and **Cancel**. Escape is Back here as it
  is everywhere else. Every exception is caught, not only the ones `main` knows
  about: a traceback into a popup that is closing is no more use than a message.

`paddock launch` and the other no-terminal entry points keep the stderr they
always had. They are a script's way in, not a popup's.

**An msb launch that cannot reach its install's registry is warned about before
the wait, not after it.** The install runs inside the guest, where the profile's
domains are the whole of the network (§2.2), so an agent installed from npm needs
the `npm` preset. The confirm and the progress screen both say so. The profile is
never changed to suit: what a sandbox may reach is an answer the user gives (§6).

The design and the reasoning behind it are in
[docs/design/chooser-redesign.md](design/chooser-redesign.md).

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

**A tab can hold a shell instead of the agent.** `paddock attach <session>
--shell`, or the second question the chooser's Open field asks about a live
session, opens the user's shell inside the sandbox the agent is already in: srt
runs it under the same settings file and workdir, and msb execs the guest's `/bin/sh`
into the running VM, so it lands beside the agent in one process namespace.
Named, not left to the image: `msb exec` with no argv runs the image's own
command, which for `node:22-slim` is the Node REPL. It is a tab on the session like any other, registered and counted, and
the session ends when the last of them closes, whichever kind it was. Each run
dir holds two scripts for this, `launch.sh` and `shell.sh`, composed the same way
and wrapped by the same launcher (§9), so a shell tab that cannot start is held,
logged and replayed exactly as an agent tab is.

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
| `keep_alive` | Survives its last pane. Asked for under the chooser's Advanced screen |
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

**How a closed tab is noticed: reconciliation.** herdr sends pane events over its
socket, but nothing on paddock's side is running to hear them: paddock starts, does
one job and exits. So `sessions.reconcile()` compares the registry with `herdr pane
list`, drops the pane ids herdr no longer has, and collects the sessions that ran out
of them. It runs first in every command that opens or lists sessions (`choose`,
`launch`, `attach`), and `paddock gc` is that on its own. A herdr that does not answer
is a no-op: no pane list is not the same as no panes, and treating it as one would
collect every session.

That makes collection **prompt, not instant**. A closed tab's session lives on until
the next paddock invocation. So the §8 guarantee is that the token does not outlive
the session, enforced at every paddock invocation, and the same holds for the VM an
msb session leaves running. `paddock gc` is how to force it.

Two reconciles at once are safe. The pane list and the registry are both read with the
lock held, so a tab another paddock opened and registered a moment earlier is in the
pane list too and never reads as closed. A session with no panes at all is left alone:
that is a session between `create_session` and its first `attach`, not one that lost
its last tab.

A watcher process subscribed to herdr's `pane_closed` events was the alternative.
Nothing owns its lifecycle, so it would have to be started, restarted after a crash
and stopped, and all it buys is the gap between a tab closing and the next paddock
command. It was not built.

Two other endings get the same treatment, because `create_session` boots a sandbox
before any tab exists:

- **The first tab fails to open.** `launch` rolls the session back: out of the
  registry, credentials discarded, backend asked to collect. The error names the
  session and its VM. Without that, a failed `herdr tab create` would leave a running
  microVM and a registered session with no tabs, which nothing would ever collect.
- **The sandbox is gone before a tab attaches.** `attach` ends the session the same
  way and says so, rather than opening a tab that cannot join anything (§2.2).

### 3.5 Pane labels

Panes are labelled `sbx:<session>`, so groupings are visible in the tab bar. A
shell tab is `sbx:<session> (shell)`, because it is the same sandbox and not the
same thing. An unlabelled tab is local: no session, no sandbox.

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

Three things are on that PATH without being ticked, and all three are the agent
itself. Its `command`, because `env PATH=<shim> claude` only works if claude is
on that PATH. Its `required_tools` (§5), because `codex` is a script whose
shebang runs `node` and a PATH without node is a pane that dies on
`env: node: No such file or directory`. And whatever `/usr/bin` and `/bin` hold.
Choosing an agent is consenting to what it runs on, which is why this is not a
permission the user is asked for a second time, but it is never silent: the
agent list says what the choice puts on the PATH, the confirm names each one with
the agent that asked for it, and `--dry-run` prints the same line.

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
behind an absolute path for the bypass to reach. What an msb guest is instead, root
inside it included, is in [§2.2](#what-the-guest-actually-is).

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

**On msb the same directory is built out of copies, and mounted read-only.** srt
reads it where it was built, so a symlink is fine and the settings re-open its
target by name. msb mounts it at `/paddock-config-src`, where a link to a host
path resolves to nothing, so the credentials and the skills are copied in and the
directory stands on its own. A relative link whose target is inside the mount
would work; a host skill directory is never inside it. The guest then copies the
mount to `/paddock-config` and works there, so the run dir is a source it reads
and never a file it writes. The variable is set with `msb create -e`, naming that
copy, and there is no real config dir to deny: it is not in the guest to begin
with (§2.2).

**The token is written last.** On msb the directory is built without it, the
guest is created and provisioned, and only then is the token placed and the copy
taken. An install that fails, or a create that times out, leaves no token on disk
at all, and the VM is removed before the failure is raised.

This is why it is worth doing: **the session starts with no unselected skill or
MCP server anywhere in reach.** Nothing to enumerate, nothing to load, nothing
for a prompt-injected agent to find. Layer 2 tells the agent not to; layer 3
means there is nothing there.

Be exact about how long that holds. On srt the synthesized directory is
writable, because the agent has to write it, so what stops an agent adding a
server to its own `.mcp.json` mid-session is layer 2 and the domain allowlist,
not layer 3. The guarantee layer 3 makes on srt is about what the session
starts with. On msb the guest writes to its own copy, so a rewrite there reaches
no host file and no later session (§2.2).

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
| `required_tools` | Tools the command cannot start without, put on the srt shim dir when the agent is selected. `codex` is a `#!/usr/bin/env node` script, so it names `node`. Blank for an agent that is a binary |
| `auth_read_paths` | Credential paths auto-allowed for reading |
| `config_write_paths` | Paths it legitimately writes (history, session state) |
| `image` | OCI image the `msb` backend boots for this agent. Blank means srt-only, except `shell`, which gets `alpine` (§2.2) |
| `install` | Shell command that puts `command` in an msb guest that lacks it, version pinned. Blank means the image ships it |

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
| `network_presets` | `list[str]` | `["anthropic", "github"]` | Named domain groups (anthropic, github, npm, pypi/uv, go, crates.io, homebrew), plus `local services (localhost)`, which is not a domain group but the loopback grant of §2.1 |
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
  plus the agent's `api_domains`, deduplicated. Loopback appearing anywhere in
  that resolved set is what writes `allowLocalBinding` into the settings (§2.1);
  no profile ships with it.
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
| `paddock/sessions.py` | Session registry in `~/.local/state/paddock/`: create, list, attach, lifecycle and reconciliation (§3), and the backend dispatch dict (§2) | Done; workspace bindings (§3.3) wait for the TUI |
| `paddock/profiles.py` | `Profile` dataclass, network presets, tool candidates, load/save | Done |
| `paddock/agents.py` | Agent registry and per-agent layer-2 config | Registry done; the layer-2 `permissions` block is not generated yet |
| `paddock/backends/srt.py` | srt settings JSON, PATH shim dir, `prepare()` / `open_pane()` | Done |
| `paddock/backends/microsandbox.py` | msb: boot the session's VM, exec a tab into it, destroy it (§2.2) | Shell sessions done; agents in the guest are next |
| `paddock/herdr_client.py` | Subprocess wrapper over the herdr CLI: the one seam tests mock | Done |
| `paddock/synth_config.py` | Layer 3: build the config dir from credentials plus ticked skills | Done for Claude Code; other agents have no redirection (§4.3) |
| `paddock/tui.py` | The chooser's fields, words and rules: answers in, one plan out | Done; the workspace default binding (§3.3) is not asked about |
| `paddock/screen.py` | The screens: the form, a list, a checklist, a box, the confirm and the one a failed launch ends on, over prompt_toolkit | Done |
| `paddock/recent.py` | What the form opens on: the profile each workspace launched last | Done |
| `paddock/cli.py` | Entry point: `choose` (default), `launch <profile>`, `attach <session>`, `profiles`, `gc`, `logs`, `init` | Done |
| `paddock/init.py` | `paddock init`: splice the keybinding into herdr's config, back it up, reload (§1.1) | Done; the plugin manifest (§1.4) is v1.1 |
| `paddock/log.py` | Where paddock logs, at what level, and what never reaches the file (§9) | Done |

One constraint runs through all of it: **only `herdr_client.py` shells out to
`herdr`, and only a backend shells out to its own sandbox runtime**, `srt` or
`msb`. Everything else is pure functions, which is what makes the behaviour
testable on a Linux CI runner with no sandbox present.

---

## 8. Open questions

**Answered: what happens to a sandbox a launch never registered.** `prepare`
rolls its own boot back on any failure, ctrl-c included, so an interrupted launch
takes its microVM and its token with it. A process killed outright cannot roll
anything back, and what that leaves is a sandbox no session claims. `paddock gc`
sweeps for them: it asks each backend what it is running, and removes what the
registry has never heard of. Only handles paddock would have made are touched
(`paddock-<run dir name>`), because another tool's sandboxes are none of its
business, and a backend whose binary is not on this machine is skipped without a
word rather than reported as unsweepable.

- Should the chooser remember more than the profile per workspace, such as the
  whole set of answers, or the session a workspace attaches to by default (§3.3)?
- Should `deny_read` be enforced by the backend rather than the profile, so a
  malformed profile cannot widen it?
- How should a pane show what permissions it actually got? A written manifest in
  the workdir is the current favourite.
- What happens to an srt session's run directory when the session is collected?
  The credential file goes, because it may be an exported token (§4.3), at the
  next paddock invocation that reconciles (§3.4) and not the instant the tab
  closed. The rest stays: deleting a workdir loses work, keeping it leaks disk.
- Can a tab move between sessions after creation, or is detach-and-relaunch the
  honest answer, given that srt cannot migrate a running process tree?
- Is v2 per-binary blocking (§4.1) worth the enumeration cost, or is the honest
  answer that PATH shimming is a usability feature and the network and write
  boundaries are the real security story?

---

## 9. Logging

paddock writes down what it did, so a pane that vanished can still be explained.

**Where.** `~/.local/state/paddock/logs/paddock.log`, rotated at 1 MB with three
backups kept. `PADDOCK_LOG_FILE` moves it. Each run also gets
`<run_dir>/pane.log`: the stderr of every agent pane that launched on that run,
appended by `launch.sh` as it happens, with one earlier generation kept as
`pane.log.1` once it passes 1 MB. A shell tab keeps its own `shell.log` next to
it, written by `shell.sh`, because a session with both has two stories and the
one that explains a launch is not always the one the agent wrote. `paddock logs
<session>` prints whichever of the two the run has, and names the agent's when
it has neither yet.

**A failed launch keeps its pane.** `launch.sh` runs the command in the
foreground with its stderr appended to `pane.log`, not piped: a pipe closes only
when its last writer does, and an agent that backgrounds anything holds stderr
open for as long as it lives, which would hang the pane on a launch that went
fine. On a non-zero exit the script puts the terminal back in order with `stty
sane`, prints `paddock: launch failed (exit N), log: <path>`, replays the last
20 lines of `pane.log` and waits for a keypress. It only does that when the exit
came within 10 seconds of the start: a non-zero exit later than that is the
agent ending, ctrl-c (130) included, and holding the pane on that would hold it
hostage. `load_run` rewrites a launch script that is not the one this paddock
would write, so a session prepared before an upgrade gets the current behaviour
on its next tab.

**A shell tab is held to a different test.** `exit 1` in an interactive shell is
the user leaving, not a launch that failed, and holding the pane on it would hold
the user hostage for typing. So `shell.sh` holds the pane only when the run wrote
something to `shell.log`: a shell that could not start says why on stderr, and one
the user ended says nothing at all. Everything else about it is the same script.

**A launch that never got a pane keeps the popup.** There is no `pane.log` to
hold, because nothing was opened: `prepare` refused, or the guest would not
install the agent. From the chooser that goes on a screen with the log path on
it (§3.1), and the popup stays up until the user has read it. From `paddock
launch` it goes to stderr, as it always did. Either way the line in
`paddock.log` is the same one, scrubbed of URL credentials, with no traceback.

**One script, both backends.** `launch.sh` is written by
`paddock/backends/__init__.py`, not by a backend, because none of the above is
about which sandbox is behind the pane (§2). A backend composes its own command,
`srt --settings <file> -c ...` or `msb exec --tty <handle> ...`, and the shared
script wraps it. An msb tab that cannot exec into its guest is therefore held,
logged and replayed exactly as an srt launch that cannot start is.

**Levels.** The file takes everything from DEBUG up. stderr takes WARNING and
worse, because the chooser is a popup and every line there is in the user's
face. `PADDOCK_LOG=debug|info|warning|error|critical` lowers that bar for one
run.

**What a line looks like.** `<ISO timestamp> <LEVEL> <module> <message>`. The
module name says which layer wrote it: `paddock.tui`, `paddock.sessions`,
`paddock.backends.srt`, `paddock.backends.microsandbox`. Launch and session
lines carry the session id, the backend, the run directory, the pane id and the
microVM handle when there is one, so one launch can be followed end to end.
Each backend logs its own shell-outs at DEBUG through the single function that
makes them: herdr's argv in `herdr_client`, and msb's `create`, `exec`, `ls` and
`rm` in `microsandbox._run`.

**What is never logged.** Tokens. What a credential file holds. Keychain output.
Proxy URLs, because srt puts the password in the URL. Environment values, any
one of which may be a token: herdr's `--env NAME=VALUE` and msb's `-e
NAME=VALUE` are both logged as `NAME=...`. The composed launch command, which
carries every environment value the sandbox keeps: its length is logged instead.
Paths, byte counts and lengths are what the log is made of. `tests/test_log.py`
plants a fake token in the Keychain, in a config file and in a proxy URL, runs a
whole launch at DEBUG on each backend and a failing one that makes herdr quote
the proxy URL back, and fails if either the token or the proxy URL reaches the
file. The msb launch asserts the same of the argv msb was given, so an msb
argument that started carrying a secret would fail the test rather than quietly
reach the log: today that argv is mounts, sandbox names, net rules and the guest
path of the config dir, and no credential is passed to msb at all (§4.3). Error text from other programs is scrubbed of URL
credentials on the way in, and no traceback is ever logged: a traceback carries
the arguments of every frame, which is exactly what the redaction takes out.

**What that guarantee does not cover.** `pane.log` is the agent's own stderr,
written straight through. paddock does not read it, filter it or redact it, and
an agent that prints a token prints it there. The rule above is about paddock's
own lines. `paddock logs <session>` says so before it prints one.

**Reading it back.** `paddock logs` prints the path and the last 40 lines.
`paddock logs <session>` prints that session's run directory and the end of its
`pane.log`. It looks the session up after reconciling, like every other entry
point that takes a session reference (§3.4), so a reference to a session whose
tabs are all gone says so rather than printing the log of a session that is over.

---

## 10. Architecture: layers and the one-door rule

paddock is four layers, and calls only ever go down them:

| Layer | Modules | Job |
| --- | --- | --- |
| Presentation | `tui.py`, `screen.py`, `cli.py` | Ask the questions, or read the arguments. Produce a plan. `screen.py` draws; `tui.py` decides what is drawn. |
| API | `sessions.py` | The one door to a running sandbox: create, attach, remove. |
| Isolation | `backends/srt.py`, `backends/microsandbox.py` | Turn a profile into a policy, a config dir and a command. |
| Process seam | `herdr_client.py` | The one module that shells out to `herdr`. Any layer may use it. |

The rule is that **the presentation layer never imports a backend**. It asks
`sessions` for a session, and `sessions` picks the backend that runs it. No
backend imports `tui`, `cli` or `sessions` either, so how a launch was chosen
cannot leak into how it is enforced. `log.py` is a leaf like `herdr_client.py`:
anything may log.

This is what makes each layer testable on its own, and it is why a failure has
one address. A wrong settings file is the backend's. A pane on the wrong session
is `sessions`'. A question asked in the wrong order is the TUI's. When every
layer can call every other one, every bug belongs to everybody.

`tests/test_architecture.py` parses the imports and fails on any edge that
breaks this, naming the file and the import it objected to. It also holds the
list of backend modules `sessions` may reach, so adding a backend is one line
there and not a search: `paddock.backends.microsandbox` was that line.
