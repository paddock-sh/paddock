# Session control

A proposal. Nothing here is built. Epic slug `session_control`, cut from
`develop`.

The chooser decides what a sandbox gets before it starts. After that, paddock
has nothing to say: there is no way to see what a running session was granted,
no way to change it, and no way to make a whole workspace mean one session.
This adds those three, and the hard part of all three is the same question.
**A permission is enforced by something that was set when the sandbox started.
Changing the answer after the fact does not change the thing enforcing it.**

So the design is mostly about being honest per item and per backend: what
paddock can change on a live session, what needs the agent restarted under it,
and what cannot reach it at all. Nothing here relaxes
[CONTRIBUTING § Scope discipline](../../CONTRIBUTING.md#scope-discipline): no
permission gets a wider default, and no screen calls layer 2 a boundary.

Three features:

1. **A permissions panel** on `prefix+e`, for the session owning the focused
   pane (section 2).
2. **A keybinding scheme** that gives `prefix+c` back to herdr, and a migration
   for installs that already have paddock on it (section 3).
3. **Sandboxed workspaces**: a workspace bound to a session, so new tabs join it
   ([SPEC §3.3](../SPEC.md#33-workspace-default-binding) pulled forward)
   (section 4).

It builds on [the chooser redesign](chooser-redesign.md): the same form model,
the same keys, the same screen module. Every mockup here is drawn at 80 by 24,
which is the smallest terminal anyone has. The screens draw to
`min(columns, 110)` and centre the block in whatever is left, so a wider popup
gets margins, not longer lines.

---

## 1. What is there now

Shipped, and what each gives this design:

| Piece | What it already does | What this needs from it |
| --- | --- | --- |
| `sessions.py` | The registry: id, name, profile, agent, run dir, keep-alive, backend, pane ids | Which session owns a pane, and a place to record per pane what policy it started on |
| `backends/srt.py` | Settings JSON, shim dir, `launch.json`, `launch.sh` | Rewriting the settings for a live run, and a launch script that can restart |
| `backends/microsandbox.py` | `msb create` with net rules and mounts, one config copy into the guest, `msb exec` per tab | A second `msb exec` to change the guest's config dir while it runs |
| `synth_config.py` | The config dir: credentials, ticked skills, the MCP whitelist | Adding and removing a skill in an existing config dir |
| `screen.py` | `form`, `pick`, `tick`, `confirm`, `type_in`, the key map, the footer | The panel is a `form`. The restart warning is a `confirm` |
| `init.py` | One managed block between markers, backed up, TOML checked, herdr `config check`ed, reloaded | A different block, and a migration off the old one |
| `herdr_client.py` | `tab create`, `pane run` | `pane list` for agent status, `pane process-info` for pids, `workspace report-metadata` for the binding label |

Two facts from the shipped code shape everything below.

**On `srt` the boundary is per tab. On `msb` it is per session.** An srt session
is a settings file and a workdir, and every tab is its own `srt` process that
reads that file when it starts (SPEC §3.2). An msb session is one microVM, and
every tab is an `msb exec` into it, so network rules and mounts were fixed by
the one `msb create` that booted it. The same field is therefore restartable on
one backend and immovable on the other, and the panel has to say which.

**Claude Code's conversation lives inside the config dir paddock synthesizes.**
Transcripts are at `$CLAUDE_CONFIG_DIR/projects/<project>/<uuid>.jsonl`, proven
by pointing the variable at an empty directory and watching `claude --resume`
answer `No conversation found with session ID`. paddock gives each session its
own config dir under its run dir, and every tab of that session shares it, so
restarting an agent inside a session can resume what it was doing. Restarting it
in a *different* session cannot, and should not.

---

## 2. The permissions panel

`prefix+e` over any pane. It finds the session that owns that pane, shows what
that session actually got, and lets it be changed.

### 2.1 It reads the run directory, not the profile

The session record holds `profile_name`, not the profile. A profile is a file
the user can edit, and by the time the panel opens it may say something the
running sandbox never got. So the panel reads what is enforcing the policy:

| Shown | Read from, on `srt` | Read from, on `msb` |
| --- | --- | --- |
| Network, denied reads, extra writes | `<run_dir>/srt-settings.json` | `<run_dir>/launch.json`, which has to start recording them |
| Files | The settings file and `launch.json` | `launch.json`, which records the workdir already |
| Tools | The symlinks in `<run_dir>/bin` | Not shown. The image has what it has |
| Skills, MCP servers | `<run_dir>/config/skills/`, `<run_dir>/config/.mcp.json` | The same directory, mounted into the guest read-only |
| Agent, backend, keep-alive, tabs | The session record | The session record |

That answers SPEC §8's open question about how a pane shows what it actually
got, and it answers it better than a manifest would: a manifest is a copy that
can drift, and these files are the originals.

The one gap is msb. Its `launch.json` holds `vm_handle`, `workdir` and
`command`, so the domains the VM was created with are nowhere on disk once
`msb create` has returned, and `msb ls` does not report rules. So the msb
backend records the domains it passed, as one more key in the record it already
writes. That is a line of JSON, and without it the panel can only show an msb
session's network as `unknown`, which is worse than not showing it.

Reading the settings file also gives the panel the one thing the registry cannot:
a session's tabs can be running under **different** policies. Rewriting the
settings file changes what the next tab gets and nothing about the tabs already
running. So the session record gains one key, `pane_policy`, mapping a pane id to
a short hash of the settings that pane started under.

A new key, not a new shape for `pane_ids`. SPEC §3.4 already keeps a key it has
no field for and writes it back, so a new key costs nothing in either direction,
while changing `pane_ids` from a list of strings into a list of records would
make every existing registry a record of the wrong shape, and the same section
says such a record is dropped. A pane with no entry in `pane_policy`, which is
every pane in a registry written before this, shows as `policy unknown` rather
than being guessed at.

`paddock permissions` runs `sessions.reconcile()` first, like every other command
that looks a session up, so the panel is never drawn over a session whose tabs
have all closed.

### 2.2 Four labels, and what each promises

| Label | Promise |
| --- | --- |
| `live` | paddock changes it on the running session. Nothing restarts, and the focused pane is under the new answer as soon as the panel closes. |
| `restart` | paddock changes it, then the agent in this pane has to start again under it. The conversation is resumed where the agent supports that. |
| `new tabs` | The running tabs cannot take it. paddock writes it, and the next tab on this session gets it. |
| `fixed` | Not changeable on a live session at all. It goes in the profile, and the next session has it. |

`live` is about the sandbox, not about the agent. A file that appears in the
config dir is there the moment paddock puts it there, and whether the agent
notices is the agent's business: Claude Code lists its skills when it starts, so
a skill that arrives live is seen at the agent's next start. The panel says
that where it is true rather than implying the agent re-reads anything.

### 2.3 Every item, per backend

| Item | `srt` | `msb` | Why |
| --- | --- | --- | --- |
| Tools | `live` | `fixed` | srt's PATH is a directory of symlinks and lookup is by directory, so a symlink added now is found now. msb's tools are baked into the image (SPEC §2.2) |
| Network | `restart` | `fixed` | srt compiles the allowlist and starts its proxy per invocation. msb's rules are `msb create` flags and it offers no way to withdraw one, which the [roadmap](../ROADMAP.md) already names as the reason to want a prebuilt image, so only a new VM has new ones |
| Files (shared dir) | `restart` | `fixed` | srt: an `allowWrite` entry plus the workdir. msb: a `--mount-dir` at create |
| Skills, adding one | `restart` | `restart` | srt: the real config dir is denied, and only the linked targets are allowed back by name, so a new skill needs a new settings file. msb: one `msb exec` copies it from the read-only mount into the guest overlay, and the agent lists skills at start |
| Skills, removing one | `live` | `live` | Unlink it, or `msb exec rm` it. What is not in the config dir is not there to find |
| MCP servers | `restart` | `restart` | The whitelist is a file the agent reads at start, and `--mcp-config` is re-passed on every launch |
| Never readable | `restart` | `fixed` | srt: `denyRead` in the settings. msb: the host is not in the guest to deny |
| Also writable | `restart` | `fixed` | Same reason |
| Keep running | `live` | `live` | A field in the registry. Nothing enforces it until the last tab closes |
| Agent | `fixed` | `fixed` | A different agent is a different login, a different config dir and a different set of skills. That is a new session |
| Backend | `fixed` | `fixed` | The backend is what a session is |

On `msb`, `new tabs` is never offered, because a new tab execs into the same VM
and gets exactly what that VM has. The third button becomes
`[ Save to the profile ]` instead, which is the only honest thing left to do
with a change the running session cannot take.

### 2.4 The panel

```
+------------------------------------------------------------------------------+
| paddock: permissions   review              srt, 2 tabs, since 14:02          |
|                                                                              |
|    1 Agent         Claude Code (claude)                            fixed     |
|    2 Tools         git rg fd jq curl node npm npx uv python3 (10)  live      |
|  > 3 Network       anthropic, github, npm, pypi/uv  (12 domains)   restart   |
|    4 Files         shared: ~/dev/paddock                           restart   |
|    5 Skills        blog, dataviz  (2)                              restart   |
|    6 MCP servers   none                                            restart   |
|    7 Never read    ~/.ssh ~/.aws ~/.gnupg ~/.config/gh             restart   |
|    8 Also write    nothing beyond the workdir, /tmp and /dev/null  restart   |
|    9 Keep running  no, the session ends with its last tab          live      |
|                                                                              |
|  Only these domains are reachable. Everything else is refused by the OS. srt |
|  compiles the list when a sandbox starts, so this pane takes a change by     |
|  restarting the agent under it. Nothing else in the pane changes.            |
|                                                                              |
|  > [ Apply ]        [ Apply to new tabs only ]        [ Close ]              |
|                                                                              |
| enter edit   ^v move   1-9 jump   esc close   ? keys                         |
+------------------------------------------------------------------------------+
```

It is `screen.form` with one column added: the apply label, kept at the right
edge where the chooser keeps its counts. The editors are the chooser's own, so
`Network` opens the same checklist with the same groups and the same box for
extra domains. Nothing new is learned to use this.

Three things the header carries: the backend, because the labels depend on it;
the tab count, because a change reaches one tab and not the others; and, when
they disagree, how many tabs are behind.

```
| paddock: permissions   review    srt, 2 tabs, 1 on an older policy           |
```

With nothing changed, `Apply` is not a button. The row reads
`[ Close ]` alone, so the panel is also just a way to look.

An msb session, where most of it cannot move:

```
|    2 Tools         whatever node:22 ships                          fixed     |
|  > 3 Network       anthropic, github  (5 domains)                  fixed     |
|    4 Files         shared: ~/dev/paddock                           fixed     |
|    5 Skills        blog, dataviz  (2)                              restart   |
|                                                                              |
|  A microVM's network rules are set when it boots and there is no way to      |
|  change them from outside it. Every tab on this session shares that VM, so   |
|  a new tab would get the same rules. Save this to the profile and the next   |
|  session starts with it.                                                     |
|                                                                              |
|  > [ Restart the agent ]    [ Save to the profile ]    [ Close ]             |
```

Over a pane that is on no session:

```
|  This pane is a local tab. There is nothing sandboxing it, so there is       |
|  nothing to show and nothing to change: it can read and write anything you   |
|  can, and reach anything you can.                                            |
|                                                                              |
|  > [ Open the chooser ]        [ Close ]                                     |
```

### 2.5 The restart flow

The pane runs `exec /bin/sh <run_dir>/launch.sh`, and that script already runs
the agent in the foreground and survives it, which is how a failed launch keeps
its pane (SPEC §9). So a restart does not need a new tab and does not move
anything in the tab bar. The script gains one check after the command returns:

```sh
paddock_launch 2>>"$paddock_log"
paddock_exit=$?
if [ -f "$paddock_restart" ]; then
  rm -f "$paddock_restart"
  exec /bin/sh "$0"          # paddock may have rewritten the command
fi
```

`$paddock_restart` is per pane, `<run_dir>/restart.<pane id>`, because a session
has many tabs and only the focused one is being restarted. The pane id reaches
the script the same way the launch line does: `herdr pane run` is sent
`PADDOCK_PANE=<id> exec /bin/sh <script>`, which is still far short of the
1024-byte line limit.

The script runs on the host, outside the sandbox, on both backends: it is the
pane's own shell, and what it wraps is `srt ...` or `msb exec ...`. So the flag
is a host file the sandbox cannot reach. On srt the run directory is not
writable from inside; on msb it is not mounted at all beyond the workdir and the
read-only config dir. Either way a sandboxed agent cannot set its own restart
flag, which matters because the flag is what decides whether a policy the user
did not ask for gets picked up.

Steps, in order:

1. **Ask.** `herdr pane list` reports `agent_status` per pane: `idle`, `working`
   or `unknown`. A `working` pane is mid-turn and the panel refuses, saying so.
   `unknown` is not `idle`: herdr works the status out from the pane's processes
   and a sandboxed agent is three processes down from the shell, so the panel
   says it cannot tell and lets the user decide, rather than reading silence as
   safety.
2. **Write the new policy.** The settings file, the shim dir, the config dir,
   whatever changed. This is the point of no return for the *next* tab: the
   session's file has moved even if the restart then fails.
3. **Set the flag**, `<run_dir>/restart.<pane id>`.
4. **Stop the agent.** `herdr pane process-info --pane <id>` gives `shell_pid`
   and the foreground processes with their pids and argv. paddock checks the
   argv is what it expects, then sends `SIGTERM`. On srt that is the agent
   itself, under `srt`. On msb the agent is in the guest, so what paddock can
   see and signal is the host-side `msb exec` that is holding the terminal, and
   ending that ends the exec. Checking the argv first is what stops paddock
   signalling whatever else has taken that pane over.
5. **The script re-execs itself** with the resume argv in its environment, and
   the agent comes back under the new policy.
6. **Record it.** The pane's policy hash in the registry is updated, so the
   header stops saying the tab is behind.

The resume argv comes from the environment, passed by name the way the proxy
variables are (SPEC §2.1), so `launch.sh` stays the one script this paddock
would write and `ensure_launch_script` does not fight it.

```
+------------------------------------------------------------------------------+
| Restart the agent in this pane?                                              |
|                                                                              |
|   changing    Network  + crates.io, + go                                     |
|               12 domains becomes 17. Nothing is taken away.                  |
|   pane        wA:p5, the pane you opened this over                           |
|   agent       Claude Code, idle since 15:41                                  |
|                                                                              |
|   kept        The conversation. paddock restarts it as                       |
|               claude --resume 8f21c0de-4b1a-..., the id it started this      |
|               pane on, and re-passes the MCP whitelist, which resume does    |
|               not carry.                                                     |
|   lost        Anything the agent is holding in memory. Background tasks and  |
|               monitors do not come back.                                     |
|   other tabs  1 other tab keeps what it started under until it restarts too. |
|                                                                              |
|  > [ Restart ]      [ Apply to new tabs only ]      [ Cancel ]               |
|                                                                              |
| enter choose   <> move   esc back   ctrl-c cancel                            |
+------------------------------------------------------------------------------+
```

The change is spelled out both ways round. `12 domains becomes 17` and
`Nothing is taken away` are there because a permissions screen that only prints
a diff makes a widening and a narrowing look identical, and only one of them is
a security decision.

### 2.6 Which resume flag, per agent

Verified against the installed binaries for `claude` and `codex`, and against
current documentation and source for the rest.

| Agent | Resume | Where its conversation lives | Good enough? |
| --- | --- | --- | --- |
| Claude Code | `--resume <uuid>` | `$CLAUDE_CONFIG_DIR/projects/<project>/<uuid>.jsonl`, so inside the session's own run dir | Yes, and exactly this pane's |
| Codex CLI | `resume --last`, a subcommand before any flag | `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<uuid>.jsonl` | The most recent in this workdir, which is not certainly this pane's |
| OpenCode | `--continue` | `~/.local/share/opencode/project/<slug>/storage/`, with no known variable to move it | Same ambiguity, and the state is outside the sandbox's config dir |
| Gemini CLI | `-r latest` | `$GEMINI_CLI_HOME/.gemini/tmp/<hash>/chats/` | Same ambiguity |
| Aider | `--restore-chat-history` | `<git root>/.aider.chat.history.md`, in the workdir | No. It re-reads a rendered markdown transcript and summarizes it. Structured tool calls do not survive |
| shell | nothing | nothing | There is no conversation. A restart is a fresh shell |

So the registry gets two more data fields, alongside `command` and
`api_domains` (SPEC §5):

| Field | Meaning |
| --- | --- |
| `resume` | Argv appended to `command` to resume. Empty means the agent has none |
| `session_arg` | Argv that pins the conversation id at first launch, with `{id}` filled in. Empty means the agent cannot be pinned |

Only Claude Code has the second one today: `--session-id <uuid>`. paddock
generates a UUID per pane, passes it on the first launch, and resumes that exact
id on a restart. That is what turns "resume the most recent conversation in this
directory" into "resume the one this pane was having", and it matters because
two tabs of one session share a workdir, so `--continue` on either would be a
coin toss between them.

**When there is no resume**, the panel says so in the same place, and the button
changes so nobody presses it by accident:

```
|   kept        Nothing. Aider restores a chat history only from a rendered    |
|               markdown file, and what comes back is a summary, not the       |
|               conversation. paddock does not pretend otherwise.              |
|   lost        This conversation.                                             |
|                                                                              |
|  > [ Apply to new tabs only ]   [ Restart and lose it ]   [ Cancel ]         |
```

The order is deliberate. The safe option is the one the cursor opens on, and the
destructive one is spelled with its consequence in the label, which is the rule
[the chooser took from Claude Code's own permission prompt](chooser-redesign.md#34-where-the-model-comes-from).

### 2.7 What a restart costs, per backend

| | `srt` | `msb` |
| --- | --- | --- |
| What restarts | One `srt` process in one pane | One `msb exec` in one pane |
| The boundary | Rebuilt, because the srt process is the boundary | Untouched, because the VM is the boundary and it keeps running |
| Other tabs | Unaffected, and still on their own policy | Unaffected, and still on the same VM |
| Cost | A process start, milliseconds | An exec into a running VM, milliseconds |
| What it cannot fix | Nothing. Every settings change is reachable | Network and mounts. Those need a new VM, which is a new session |

That table is the whole reason the labels differ per backend. It is also why
this design does not offer "rebuild the VM": destroying a session's VM would
take every tab on it down at once, and the honest name for that is starting a
new session, which the chooser already does.

---

## 3. The keys

### 3.1 The scheme

| Key | What it does | Who runs it |
| --- | --- | --- |
| `prefix+c` | A plain new tab | herdr's own `new_tab`. paddock is not involved |
| `prefix+s` | The chooser | `paddock` |
| `prefix+shift+s` | The attach list, with no form in front of it | `paddock attach` |
| `prefix+e` | The permissions panel | `paddock permissions` |

Giving `prefix+c` back is the point. Today paddock takes it, so the cheapest and
most common thing anyone does in a terminal multiplexer goes through a Python
process, a popup frame and a form. A plain tab should cost one key and no
paddock at all, and then it also cannot be broken by paddock.

`prefix+shift+s` exists because attaching is the second most common thing and
does not need the form: the chooser's `Open` field already lists live sessions,
so this is that list on its own screen, and picking one launches.

### 3.2 Two of these keys are already herdr's

Read out of `herdr --default-config` on herdr 0.8.0:

```
# settings = "prefix+s"
# edit_scrollback = "prefix+e"
# new_tab = "prefix+c"
```

Two of those three are keys the scheme wants for paddock, and both are already
bound to something of herdr's. The third, `prefix+c`, is the one paddock is
giving back. Being taken is not a reason to pick different keys, because
`prefix+s` and `prefix+e` are the right keys, but it is a reason for `paddock
init` to move herdr's actions rather than leave two bindings fighting over one
chord. The precedent is already in the code: today's init moves `new_tab` off
`prefix+c` for exactly this reason.

| herdr action | Was | Becomes | Why that key |
| --- | --- | --- | --- |
| `new_tab` | `prefix+shift+c`, where paddock put it | `prefix+c`, herdr's own default | Removed from the managed block rather than pinned, so herdr's default applies and a future herdr can change it |
| `settings` | `prefix+s` | `prefix+comma` | The settings idiom everywhere else, and unmodified punctuation is the reliable kind. herdr's own config warns that punctuation with modifiers depends on the terminal |
| `edit_scrollback` | `prefix+e` | `prefix+shift+e` | A shifted letter, which herdr's config calls one of the reliable forms, and it keeps the mnemonic |

The managed block that results:

```toml
# --- paddock (managed) ---
[keys]
settings = "prefix+comma"
edit_scrollback = "prefix+shift+e"

[[keys.command]]
key = "prefix+s"
type = "popup"
command = "paddock"
width = "70%"
height = "70%"

[[keys.command]]
key = "prefix+shift+s"
type = "popup"
command = "paddock attach"
width = "70%"
height = "70%"

[[keys.command]]
key = "prefix+e"
type = "popup"
command = "paddock permissions"
width = "70%"
height = "70%"
# --- end paddock ---
```

No `new_tab` line. Its absence is the feature.

### 3.3 Migrating an install that already has the old block

The old block is recognisable without guessing, because paddock wrote it between
its own markers:

```toml
# --- paddock (managed) ---
[keys]
new_tab = "prefix+shift+c"

[[keys.command]]
key = "prefix+c"
type = "popup"
command = "paddock"
...
# --- end paddock ---
```

`init.py` already replaces the whole block between the markers on a second run,
so the mechanics are there. What is new is that this run **takes a key away from
the user**: anyone with the old block has been pressing `prefix+shift+c` for a
plain tab, and after this it does nothing. So the migration is not silent.

`paddock init` on a config holding the old block:

```
paddock: your keybindings have changed.

  prefix+c          a plain new tab            (was prefix+shift+c)
  prefix+s          the chooser                (was prefix+c)
  prefix+shift+s    attach to a session        (new)
  prefix+e          session permissions        (new)

  herdr's own settings screen moved to prefix+comma, and its
  scrollback editor to prefix+shift+e, because paddock took their keys.

  prefix+shift+c is no longer bound to anything.

  Backed up: ~/.config/herdr/config.toml.paddock-backup-20260822-011540
  Undo with: paddock init --undo
```

Every guard the current init has still applies and none of them are relaxed. The
result is parsed as TOML before anything is written, the old file is backed up
first, `herdr config check` runs on what was written and a config herdr refuses
is put straight back, and `herdr server reload-config` runs last.

Two cases the current init already handles, extended to the new keys:

- **A key the user has bound themselves.** Today init leaves a `new_tab` the
  user rebound alone and reports it. The same rule covers `settings` and
  `edit_scrollback`: a value that is not herdr's default is the user's, so it is
  left where it is and reported, and paddock takes the key anyway.
- **A `[[keys.command]]` the user already has on one of the four.** Refuse the
  whole splice and say which key, rather than silently replacing someone's
  lazygit binding. `--dry-run` prints the diff and touches nothing, which is how
  anyone checks first.

### 3.4 All four are rebindable

The four keys go in `~/.config/paddock/keys.json`, read by `paddock init` when
it composes the block:

```json
{
  "chooser": "prefix+s",
  "attach": "prefix+shift+s",
  "permissions": "prefix+e",
  "new_tab": ""
}
```

Those are the defaults, and a missing file means exactly them. `new_tab` is
`""`, meaning no `new_tab` line is written and herdr's own default stands. It is
in the list at all because someone who has learned `prefix+shift+c` should be
able to keep it by saying so.

Any of the other three set to `""` is not bound, and then the herdr action on
that key is left exactly where herdr had it. So someone who never wants the
permissions panel does not pay for it with a moved scrollback editor.

This is one config file with four string values, which is the smallest thing
that meets "all four rebindable". No key sequences, no modes, no per-workspace
overrides.

---

## 4. Sandboxed workspaces

SPEC §3.3 calls it a workspace default binding: *new tabs in workspace W attach
to session S*. It is a default answer to the chooser's first question, not a
mode, and removing it changes nothing about the session.

### 4.1 What a binding is

One line in `<state>/bindings.json`, keyed by herdr workspace id:

```json
{ "wA": "s7f2a", "wB": "s91cc" }
```

The value is a session id, never a name, because a name can be reused by a later
session and an id cannot. A binding whose session is gone is dropped by the same
reconcile that already collects sessions against `herdr pane list`, so a
workspace never points at nothing.

### 4.2 Two ways to make new tabs join it

**(a) A smart new tab.** `prefix+c` runs a paddock fast path. In an unbound
workspace it makes a plain tab and shows nothing. In a bound one it attaches to
the session. One key, one meaning, and the workspace decides what it means.

**(b) The chooser knows.** `prefix+c` stays herdr's plain new tab, untouched. In
a bound workspace the chooser opens with `Open` already set to `Attach: review`,
so a tab on the session is Enter, Enter.

(a) is the better idea and (b) is the better design. The measurement below was
run to find out whether (a) is even possible before arguing about whether it is
wise, because "too slow" would have settled it without an argument.

### 4.3 The measurement

Apple M5 Pro, macOS 26.5, Python 3.13.0, uv 0.12.5, herdr 0.8.0. Each command
run 25 times after 3 warm-up runs, whole process, fork to exit. Warm page cache,
so these are the good case, not the first case.

| Command | min | p50 | p90 |
| --- | ---: | ---: | ---: |
| `/bin/sh -c 'exit 0'` | 2.6 | 3.1 | 3.4 |
| A shell fast path: read `bindings.json`, decide | 3.8 | 4.3 | 5.2 |
| `herdr --help`, the herdr CLI's own floor | 3.0 | 3.4 | 3.8 |
| `herdr pane current`, one socket round trip | 3.1 | 3.4 | 3.7 |
| A bare interpreter, `python -c ""` | 13.3 | 13.7 | 15.5 |
| A python fast path: read `bindings.json`, decide | 14.1 | 14.6 | 15.0 |
| `import paddock.cli` | 71.6 | 72.9 | 74.3 |
| `paddock --help`, the installed console script | 72.1 | 73.8 | 79.2 |
| `uv run paddock --help` | 84.0 | 84.7 | 88.3 |

Milliseconds. What each import costs above a bare interpreter:

| Import | Cost |
| --- | ---: |
| `paddock.profiles` | +7 |
| `paddock.sessions` | +23 |
| `questionary`, and so prompt_toolkit | +50 |
| `paddock.cli`, which reaches `tui` and so questionary | +59 |

So the answer to "is python startup the risk" is: **python startup is 14ms and
is not the problem. paddock's own imports are 59ms and are.** Almost all of it
is one line, `import questionary` in `tui.py`, reached because `cli.py` imports
`tui` at module scope. `uv run` adds 11ms over the console script, which is less
than expected and not the deciding factor either.

Composite budgets for (a), unbound branch, which is the one with a 50ms budget
because it has to feel like a plain new tab:

| Fast path | Decide | Get the pane's cwd | `herdr tab create` | Total |
| --- | ---: | ---: | ---: | ---: |
| `/bin/sh` | 4.3 | 3.4 | 3.4 | **11** |
| python, after deferring every import in `cli.py` | 14.6 | 3.4 | 3.4 | **21** |
| python, as `paddock` stands today | 73.8 | 3.4 | 3.4 | **81** |

The cwd round trip is there because a `[[keys.command]]` does not inherit the
focused pane's working directory, and `herdr tab create` needs `--cwd` to put
the new tab where a plain new tab would have been. `herdr pane current` returns
it, along with `foreground_cwd`.

**So (a) fits the budget, twice over with a shell fast path and comfortably with
a Python one, but only after `cli.py` stops importing the screen library to
print its own help.** That restructure is worth doing regardless, and it is not
what decides this.

### 4.4 Recommendation: take (b)

Three reasons, and the timing is not one of them.

**It undoes feature 2.** Section 3 gives `prefix+c` back to herdr because a
plain tab should not go through paddock. Mechanism (a) takes it straight back,
with a faster paddock. The gain is one key press in bound workspaces; the price
is that the cheapest action in the multiplexer depends on a Python process
again.

**It cannot be a faithful plain tab.** `type = "shell"` runs the command instead
of `new_tab`, so paddock has to reconstruct what `new_tab` would have done:
the cwd, whatever label herdr gives a new tab, and wherever herdr decides to put
it. Every one of those is a herdr behaviour paddock would be copying by hand,
and every future change to `new_tab` is one paddock has to chase. `herdr pane
current` gives the cwd and nothing gives the rest.

**Its failure mode is invisible.** `type = "shell"` runs detached in the
background, which is what makes the no-UI pass-through possible in the first
place. A fast path that raises has nowhere to print. The user presses `prefix+c`
and no tab appears, with no message anywhere but paddock's log. Today's popup at
least draws its own error. Trading a visible failure for an invisible one, on
the most-pressed key, to save one keystroke, is a bad trade.

What (b) costs: in a bound workspace, a tab on the session is `prefix+s`, Enter,
Enter instead of `prefix+c`. The binding still does its job, which was never
really the keystroke. It was not having to remember which session, or pick it
out of a list, every single time.

### 4.5 Creating, showing and dissolving a binding

**Created** in two places, both of which already know the session:

- The chooser's confirm screen gets a `b` key, offered only when the session is
  about to exist and the popup knows its workspace:

  ```
  |   profile    claude-default, unchanged                                       |
  |   workspace  wA is not bound            press b to send new tabs here        |
  ```

- The permissions panel gets it as a row, because that screen is already about
  one session and already knows which:

  ```
  |   10 Workspace     wA "dev" opens the chooser on this session      live      |
  ```

Not a chooser *field*. A field is something you answer before launching, and a
binding is something you decide about a session that exists. Putting it on the
form would mean the field has no meaning until `Open` says `New sandbox`, and
greying out a tenth field is worse than a key on the screen where the answer is
already known.

**Shown** with `herdr workspace report-metadata --source=paddock --token
sandbox=<name> <workspace id>`, which herdr describes as display-only workspace
metadata and which takes a `--source` so it does not fight anything else writing
there. This is why paddock does not rename the workspace: a name is the user's,
and a tool that overwrites it to display its own state has taken something.
`--clear-token sandbox` puts it back.

Tabs keep their `sbx:<session>` labels (SPEC §3.5), so a bound workspace reads
the same as an unbound one whose tabs all happen to be on one session, which is
exactly what it is.

**Dissolved** three ways:

| How | What happens to the session |
| --- | --- |
| The same key on the panel, toggled off | Nothing. Its tabs stay attached |
| The session is collected, its last tab having closed without keep-alive | It is already gone. The binding goes with it, in the reconcile that collects it |
| `paddock init --undo`, or the state directory being removed | Nothing. A binding is a preference, not a permission |

A binding grants nothing. It is a default answer, so losing one costs a decision
and never an access, which is why it can be dropped automatically without
anybody being asked.

---

## 5. PR breakdown

Epic slug `session_control`, cut from `develop`. Tests first, per
[CONTRIBUTING § TDD](../../CONTRIBUTING.md#tdd), and every PR updates the
diagrams it touches.

1. **`[session_control] the keys and the migration`.** The new managed block,
   `keys.json`, the migration off the old block and its report, and the guards
   for a key the user already owns. No new UI. `init_flow.puml` redrawn.
   Independent of everything else, and the smallest PR, so it goes first and the
   user is pressing the right keys while the rest is built.

2. **`[session_control] reading a live session back`.** Pure functions: which
   session owns a pane, the settings file and shim dir and config dir read back
   into the same shape the chooser produces, the per-item apply label per
   backend, and the policy hash. `pane_policy` joins the session record, with an
   older registry reading back as `policy unknown`. No screen. This is the half
   that decides things, and it is testable with no sandbox and no herdr.

3. **`[session_control] the panel`.** `paddock permissions`, the form with its
   apply column, the editors reused from the chooser, and applying every `live`
   item: tools, skills removed, keep-alive. Nothing restarts yet. Headless tests
   through `create_pipe_input`, as the chooser's screens are tested.

4. **`[session_control] restart with resume`.** The launch script's restart
   check, `PADDOCK_PANE`, the `resume` and `session_arg` registry fields for the
   six built-in agents, the busy check off `agent_status`, the process check and
   `SIGTERM` off `pane process-info`, and the confirm screen with its lines for
   what is kept and what is lost. This is the PR that can lose someone's work if
   it is wrong, so it is the one to review hardest and the one to ping the user
   on. `launch_sequence.puml` gains the restart path.

5. **`[session_control] workspace bindings`.** `bindings.json`, the `b` key on
   the confirm and the row on the panel, the chooser opening on the bound
   session, the metadata token, and dropping a binding in the reconcile.
   `scoping_model.puml` gains a bound workspace.

PR 4 depends on 2 and 3. PR 5 depends on 3. PR 1 depends on nothing.

`cli.py` deferring its imports is worth doing in PR 1, because `paddock
permissions` and `paddock attach` are two more entry points paying 59ms to
print an error, and because section 4.3 measured it. It is a small change with a
test that asserts `paddock --help` imports no screen library, and it does not
commit the repo to mechanism (a).

---

## 6. Open questions

- **Should a `restart` item be applied to every tab at once?** The panel
  restarts the focused pane. A session with four tabs would need four visits,
  and a "restart all tabs" button is one press away from killing four
  conversations. Leaving it out for now, on the grounds that anyone who wants
  the whole session on a new policy can close the tabs and open new ones, which
  is the same outcome with the cost visible.
- **What happens when a tab is on an older policy and nobody notices?** The
  header says so, and nothing else does. A pane label marker (`sbx:review*`)
  would put it where it cannot be missed, at the price of a label that changes
  under the user.
- **Should the panel be able to narrow a permission without a restart?**
  Narrowing is the safe direction, and on srt some of it is genuinely live:
  removing a shim symlink or a skill takes effect immediately. Widening never
  is. The design does not treat them differently today, which means a narrowing
  is labelled `restart` when the change to the file was already the whole point.
- **Is `prefix+comma` a real key on every terminal herdr runs on?** herdr's own
  config says named punctuation is accepted and that punctuation with modifiers
  may not be. `prefix+comma` has no modifier beyond the prefix, so it should be
  the safe kind, but this is worth checking on Windows before the migration
  ships.
- **Does herdr report `agent_status` for a sandboxed pane?** It reports it for a
  plain `claude` pane. A sandboxed one is `sh` running `srt` running `bash`
  running the agent, and `herdr pane process-info` does list the whole
  foreground chain, so it probably does. The restart flow treats `unknown` as
  "ask", so a no here costs a confirmation rather than correctness, but it is
  worth measuring before PR 4 rather than after.
- **Does `type = "shell"` export `HERDR_ACTIVE_WORKSPACE_ID`?** SPEC §1.2 lists
  the three variables herdr exports to a popup. Nothing here needs the answer,
  because the recommendation is (b) and every paddock entry point stays a popup,
  but it is the one fact that would have to be established before anyone
  reopened mechanism (a).
- **Should `paddock permissions` work outside herdr?** `paddock permissions
  <session>` on the command line is a two-line addition and makes the whole
  panel testable by hand. Whether it should also be able to *change* anything
  without a pane to restart is less obvious.
