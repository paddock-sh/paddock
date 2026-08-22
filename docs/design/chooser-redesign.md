# Chooser redesign

A proposal. Nothing here is built. It replaces the linear questionnaire in
`paddock/tui.py` with one screen.

The verdict after real use: "far too clunky, does not work, not obvious what
some options are asking, back navigation not intuitive." That is a design
problem, not a list of bugs, so this starts from the flow rather than patching
questions.

## 1. What is wrong

Launching the most ordinary sandbox there is, `claude-default` with nothing
changed, costs **eleven screens and never fewer than eleven key presses**:

```
New window -> Start from -> Agent -> Tools -> Network -> Extra domains
-> Skills -> Share a host directory? -> Directory -> Session name
-> Save as profile -> Summary
```

Nine of those eleven are questions whose answer the profile already gave. The
user reads eleven screens to say "yes, that one".

Three things compound it.

**You cannot see the shape of the thing.** Each screen shows one question. You
never see the sandbox you are building until the summary, which is screen
eleven, and by then editing means going back through a second menu.

**Back means three different things.** On a list, back is the last entry, so you
arrow past eighteen tools to reach it. On a checklist, back is a *tick*, and
ticking it throws away the ticks you just made. On a text box there is no back
at all: questionary binds only ctrl-c and ctrl-q, so escape does nothing and
ctrl-c ends the popup and every answer with it. Three idioms, none of them the
one every other terminal app uses.

**Editing is two menus deep.** From the summary you choose "Edit a step", then
choose the step. The summary knows every answer and cannot take you to one.

## 2. Question by question

| # | Screen today | What it literally asks | What you must already know to answer | Verdict |
| --- | --- | --- | --- | --- |
| 1 | `New window:` | Local namespace (no sandbox) / New sandbox session / Attach to an existing session | That "namespace" is not a Linux namespace and means nothing here. That "local" means full access to your machine. That a session is a paddock thing, not a herdr tab | **Reword and merge.** Becomes the `Open` field, with live sessions listed on the same screen |
| 2 | `Start from:` | claude-default (claude) / offline-shell (shell) / Custom | That a profile pre-fills the next nine questions. That Custom is not blank, it is the built-in defaults | **Keep, reword, promote.** Becomes `Profile`, the one decision the common case needs |
| 3 | `Agent:` | Claude Code (claude) / Codex CLI (codex) / ... / Custom command | Little. This one works | **Keep, reword** the last entry to "Something else..." |
| 4 | `Command to run in the sandbox:` | Free text, only when Custom was picked | That it runs inside the sandbox, so a host-only path will not work | **Merge** into `Agent`. Picking "Something else..." asks for the command on the same screen |
| 5 | `Remember it as:` | Free text, pre-filled with a guessed key | That paddock keeps an agent registry in `~/.config/paddock/agents/`. That profiles name a key, not a command. Why the guess is `claude-custom` and not `claude` | **Drop.** Derive the key silently. Ask only on a collision, worded as a collision |
| 6 | `Tools on the sandbox PATH:` | Checklist of the binaries found on your PATH | What PATH is. That unticked tools are unreachable *by name*, not blocked: an absolute path still runs them | **Keep, reword, move behind the form.** One summary line, an editor on Enter |
| 7 | `Network:` | Checklist: anthropic, github, npm, pypi/uv, go, crates.io, homebrew | What each group expands to. That everything unticked is refused. That the agent's own domains are added whatever you tick | **Keep, reword, show the domains.** Merge with #8 |
| 8 | `Extra domains (space separated):` | Free text, asked on every launch | Nothing. The answer is almost always blank | **Merge** into the network screen as a text box under the groups |
| 9 | `Skills:` | Checklist of directory names under the agent's config dir | Whose skills these are. That they come from `~/.claude/skills`. That an unticked skill is *absent* inside the sandbox, not merely disabled | **Keep, reword, move behind the form.** Already hidden when the agent has none |
| 10 | `Share a host directory?` | y/n confirm | What happens when you say no. The question does not say, and "no" is the safe answer that most people want | **Merge** with #11 into one `Files` field whose value reads as the answer |
| 11 | `Directory:` | Free text, only when #10 was yes | That a relative path resolves against the popup's cwd, not your home | **Merged** |
| 12 | `Session name (blank to generate one):` | Free text, asked on every launch | That blank is fine and normal | **Move behind Advanced.** The generated name is shown on the confirm screen |
| 13 | `Save these answers as a profile (blank to skip):` | Free text, asked on every launch | That a name here writes a JSON file you will see again in question 2 | **Drop as a question.** Becomes the `s` key on the form, and an offer after a custom launch |
| 14 | `Ready to launch: ...` | Launch / Edit a step / Cancel | Nothing | **Keep the content, promote it.** The summary becomes the home screen |
| 15 | `Edit which step:` | The list of steps that were asked | That "step" means "question you already answered" | **Drop.** Every field is one arrow key away on the form |

Four profile fields have never been asked at all: `mcp`, `extra_allow_write`,
`deny_read` and `include_system_path`. They get a home under Advanced
(section 5.8).

## 3. The redesigned flow

### 3.1 The common case

The popup opens on the form, already filled in from the profile you used last.
That last part is not a convenience, it is the whole saving: NN/g's wizard
guidance ends on exactly this point, reuse the previous run's answers as this
run's defaults, and it is the one thing an eleven-question walk cannot do.

| You want | Interactions |
| --- | --- |
| The same sandbox as last time | **2**: Enter on Launch, Enter on the confirm |
| A different saved profile | **3**: pick the profile, Enter on Launch, Enter on the confirm |
| A plain local tab | **2**: pick Local tab on the `Open` field, Enter on Launch |
| A tab on a running session | **2**: pick the session on the `Open` field, Enter on Launch |

Everything else is one arrow key and Enter away, on the same screen, and coming
back from it puts you where you were.

The confirm screen stays even for a repeat launch. It is one key press and it is
the whole promise of the tool: every permission is an active choice, so the
thing that grants them says out loud what it is granting. It is also the only
screen that can show the resolved policy, since the form shows group names and
the confirm shows the domains they open.

### 3.2 The model: one form, editors on top

The screen is a list of fields. Each field is a label, its current value, and a
one-line explanation of what that value means, shown for the field you are on.
Up and down move between fields. Enter opens an editor for the field you are on.
Escape closes the editor, keeping what you did, and puts you back on the same
field. Enter on Launch goes to the confirm.

There is no wizard order, so there is no back-out-of-question-seven problem.
There is no separate summary, because the form is the summary. There is no
"Edit a step" menu, because every field is already on screen.

Fields, in order:

| Field | Value looks like | Replaces |
| --- | --- | --- |
| `Open` | New sandbox / Local tab / Attach: review | Questions 1 and the attach list |
| `Profile` | claude-default | Question 2 |
| `Agent` | Claude Code (claude) | Questions 3, 4, 5 |
| `Tools` | git rg fd jq curl node npm npx uv python3 (10) | Question 6 |
| `Network` | anthropic, github, npm, pypi/uv (12 domains) | Questions 7, 8 |
| `Files` | isolated scratch directory | Questions 10, 11 |
| `Skills` | none | Question 9 |
| `Advanced` | name, save as profile, keep running, MCP | Questions 12, 13, and four fields never asked |

Picking `Local tab` or an `Attach:` entry greys out everything below `Open`
except `Files`, because none of it applies. Nothing is hidden and nothing moves,
so the screen never rearranges under you.

### 3.3 Rules that survive from the questionnaire

Two of the current rules are good and stay, they just fire on a field change
instead of during a walk:

- Changing `Profile` hands over every value that profile carries, and says so in
  one line. Another profile means starting over from it, not keeping your old
  ticks against it.
- Changing `Agent` drops the skills, because skills come out of the agent's own
  config dir and another agent's do not carry over.

One rule is new: changing anything at all marks the answers custom, and the form
title shows `claude-default + changes` so it is obvious the session will not be
what the profile says.

### 3.4 Where the model comes from

None of this is invented. Each part is an idiom a terminal user already has, so
paddock should not teach a new one:

- **lazygit** opens a menu as a panel over the app, closes it with escape, and
  keeps a key line at the bottom of every context with `?` for the full set. Its
  footer truncates with an ellipsis rather than wrapping, so the layout can
  never be pushed around by the help text. That is our footer and our escape
  rule. lazygit also deliberately dropped `y` and `n` on confirmations in favour
  of enter and escape, which is why our confirm screen has buttons and no
  hotkeys.
- **`gh pr create`** ends on a small action menu, Submit or Continue in browser
  or Cancel, rather than a yes or no. Nothing on it is preselected, Cancel is
  always last, and the menu is rebuilt from state each time so an option that
  would now be a lie is removed rather than shown. That is the confirm screen.
- **charm's `huh`** puts a whole form on one screen, moves between fields with
  the arrows, carries a description under the field you are on, and ends with a
  confirm. Its default group width is 80 columns, which is the budget we are
  designing to. That is the form, and it is the part questionary cannot do.
- **fzf** narrows a list as you type instead of growing a second screen, and
  skips the prompt entirely when there is exactly one candidate. That is our
  `/` key and section 4.3.
- **Claude Code's own permission prompt** puts the consequence on the same line
  as the option instead of in a paragraph above it, teaches the key inside the
  label (`No, and tell Claude what to do differently (esc)`), and shortens a
  long label rather than dropping the option. Those are the rewriting rules in
  section 4.1.
- **`git add -i`** accepts a number, a single letter, or any unique prefix for
  the same menu entry. Different users reach for different ones and it costs
  nothing to take all three.

## 4. Words and keys

### 4.1 Every label rewritten

| Today | Proposed | Inline hint shown for the highlighted field |
| --- | --- | --- |
| `New window:` | `Open` | "New sandbox: an agent under the OS sandbox. Local tab: an ordinary herdr tab with full access to this machine." |
| `Local namespace (no sandbox)` | `Local tab` | "No sandbox. The agent can read and write anything you can." |
| `New sandbox session` | `New sandbox` | "Starts a sandbox with the permissions below." |
| `Attach to an existing session` | `Attach: <name>` | "A second tab on a sandbox already running. Same files and same policy, separate process tree." |
| `Start from:` | `Profile` | "Fills in everything below. Change anything and this session runs as claude-default + changes, not as claude-default." |
| `Custom` | `Custom (built-in defaults)` | "Starts from paddock's own defaults, not from nothing." |
| `Agent:` | `Agent` | "The command that runs inside the sandbox. It gets its own login and no other agent's." |
| `Custom command` | `Something else...` | "Type a command. paddock remembers it so a profile can name it later." |
| `Command to run in the sandbox:` | `Command` (on the Agent screen) | "Runs inside the sandbox, so it has to be a tool the sandbox can reach." |
| `Remember it as:` | gone | Asked only on a name clash: "codex already runs something else. Call this one:" |
| `Tools on the sandbox PATH:` | `Tools it can run` | "Ticked binaries are on the sandbox PATH. Unticked ones are not reachable by name. An absolute path still works, so this is convenience, not a boundary." |
| `Network:` | `Network access` | "Only these domains are reachable. Everything else is refused by the OS. Tick nothing for an offline sandbox." |
| `Extra domains (space separated):` | `Also allow` (on the Network screen) | "Space separated, for example example.com *.internal.dev" |
| `Skills:` | `Skills it can see` | "Unticked skills are not in the sandbox's config dir at all, so the agent cannot find them." |
| `Share a host directory?` + `Directory:` | `Files` | "Isolated: the sandbox gets a fresh scratch directory and no host path of yours is writable. Shared: that one directory is the only thing on your machine it can change." |
| `Session name (blank to generate one):` | `Name` (Advanced) | "Shown in the tab bar as sbx:<name> and used to attach later. Blank generates one." |
| `Save these answers as a profile (blank to skip):` | the `s` key | "Save these answers so they are one pick next time." |
| `Ready to launch:` | `Launch this sandbox?` | The resolved policy, in full |
| `Edit which step:` | gone | Every field is on the form |

The rule behind the rewrite: **say the consequence of the value that is
currently showing, not the topic of the question.** "Network access" with
"everything else is refused" tells you what happens. "Network:" over a list of
seven words does not.

### 4.2 Keys, matching herdr

Herdr's navigate mode uses arrows with `hjkl` beside them, `esc` to leave,
`enter` to take, and digits to jump. The chooser uses the same, so a herdr user
already knows it.

| Key | Everywhere in the chooser |
| --- | --- |
| `up` `down`, `k` `j` | Move between fields, or between items in an editor |
| `enter` | Open the field you are on, take the item you are on, or press the button you are on |
| `esc` | Back out one level, keeping every answer. On the form, close the popup with nothing done |
| `ctrl-c` | Cancel everything, at any depth |
| `1`..`8` | Jump straight to that field on the form |
| `space` | Toggle a tick, in a checklist only |
| `/` | Filter a long list. Typing on its own never filters, so the letter keys stay free |
| `a` `n` | Tick all, tick none, in a checklist only |
| `?` | The key list, over whatever is on screen |
| `L` | Launch, from anywhere on the form |
| `s` | Save these answers as a profile |

Two promises hold on every screen. **Escape never loses an answer.** It closes
what is open and leaves the value it was editing in place. **Ctrl-c always
cancels the whole popup**, and cancelling costs nothing, because the chooser
returns a plan and `cli.py` is the only thing that acts on one.

Every screen ends with a footer line showing the keys that work there, with
`esc` on all of them, which is lazygit's habit and the cheapest usability win in
a terminal. Where 80 columns will not hold every key, the footer keeps `enter`
and `esc` and `?` takes the rest, truncating rather than wrapping so the help
can never move the layout. Ctrl-c is not always in the footer because it works
everywhere in every terminal program, and the key list says so.

That footer line does one more job, borrowed from huh: **when something is
wrong, the error replaces it.** A typed domain that is not a domain, or a shared
directory that does not exist, is reported on the footer line, and the keys come
back when it is fixed. One row of chrome, two jobs, never both at once. At 24
rows there is no second row to spend.

### 4.3 One rule about lists

Every tool worth copying agrees on this and none of them hedges: **a digit or
letter hotkey and a type-to-filter box cannot live on the same list.** Charm
made it structural by shipping `gum choose` and `gum filter` as two commands.
gh made it a runtime switch. questionary's own validator refuses `j` and `k` as
navigation keys when its search filter is on, for the same reason. The moment
typing filters, every letter is filter text and no letter is a shortcut.

The chooser resolves it the way lazygit and `git add -p` do: **filtering is a
mode you enter with `/`, never something bare typing starts.** That one decision
keeps every letter free as a shortcut on every screen, and it is why the tools
checklist can offer `a` for all and `n` for none while still filtering.

What changes with list size is only how loudly filtering is advertised:

| Size | Model | Where |
| --- | --- | --- |
| 1 candidate | Do not ask at all | One saved profile, one live session |
| 0 candidates | Say so on the field, do not open an empty box | No skills, no sessions |
| Up to 5 | Arrows and Enter. `/` works, nobody needs it | `Open`, `Files` |
| 6 to 12 | Arrows and Enter, `/` in the footer | `Network`, `Agent`, `Profile` |
| Over 12 | `/` in the footer and a count in the header | `Tools`, `Skills` |

Digits are the one shortcut that stays off the lists. They live on the form
only, where `1` to `8` jump to a field, because the form is a fixed list of
eight that never reorders. A digit against a list that filters or sorts is a
lie the moment it does either.

### 4.4 When there is nothing to ask

Skipping a question nobody can answer is not politeness, it is fzf's
`--select-1` and gum's `--select-if-one`, and the current chooser half does it
already through `SKIP`. Made a rule:

- One saved profile and no others: the `Profile` field shows it and opening it
  is pointless, so Enter says so instead of drawing a one-row list.
- No live sessions: no `Attach:` entries on `Open`, as today.
- The agent has no skills directory: the `Skills` field reads "none for this
  agent" and does not open.
- No TTY: the chooser does not draw at all. `paddock` run without a terminal
  should fail with a message naming `paddock launch <profile>`, which is the
  flag that fixes it. That is the clig.dev rule, and paddock already has the
  non-interactive path, it just does not point at it.

Cancelling already exits 130, which is the convention fzf set. Keep it.

## 5. The screens

Drawn at 80 by 24. The popup is 70% by 70%, which is usually larger, so nothing
here may need more than 80 by 24. The outer border is herdr's, drawn here for
the frame.

### 5.1 The form (the home screen)

```
+------------------------------------------------------------------------------+
| paddock                          claude-default            in ~/dev/paddock  |
|                                                                              |
|    1 Open       New sandbox                                                  |
|  > 2 Profile    claude-default                                               |
|    3 Agent      Claude Code (claude)                                         |
|    4 Tools      git rg fd jq curl node npm npx uv python3               (10) |
|    5 Network    anthropic, github, npm, pypi/uv                (12 domains)  |
|    6 Files      isolated scratch directory                                   |
|    7 Skills     none                                                         |
|    8 Advanced   name, save as profile, keep running, MCP                     |
|                                                                              |
|  Fills in everything below. Change anything and this session runs as         |
|  "claude-default + changes", not as claude-default.                          |
|                                                                              |
|  > [ Launch ]        [ Cancel ]                                              |
|                                                                              |
|                                                                              |
|                                                                              |
|                                                                              |
| enter edit   ^v move   1-8 jump   L launch   s save   esc close   ? keys     |
+------------------------------------------------------------------------------+
```

After changing the tools, the title says so and the profile field says so:

```
| paddock                  claude-default + changes          in ~/dev/paddock  |
```

With `Open` set to a local tab, the sandbox fields grey out and say why:

```
|  > 1 Open       Local tab                                                    |
|    2 Profile    -            no sandbox, so there is nothing to permit       |
|    3 Agent      -                                                            |
|    4 Tools      -                                                            |
|    5 Network    -                                                            |
|    6 Files      ~/dev/paddock                                                |
|    7 Skills     -                                                            |
|    8 Advanced   -                                                            |
```

### 5.2 Open

Local, new, and every live session on one screen. This also answers SPEC §8's
open question about offering the last session as a zeroth option: the sessions
are simply here.

```
+------------------------------------------------------------------------------+
| Open                                                                         |
|                                                                              |
|  > New sandbox      an agent under the OS sandbox, with the permissions below|
|    Local tab        an ordinary herdr tab here. No sandbox, full access      |
|    ------------------------------------------------------------------------  |
|    review           Claude Code, claude-default, 2 tabs, since 14:02         |
|    docs             shell, offline-shell, 1 tab, since 09:31                 |
|                                                                              |
|                                                                              |
|  An agent under the OS sandbox. It can write only the directories you name   |
|  and reach only the domains you tick. Seatbelt on macOS, bubblewrap on Linux.|
|                                                                              |
|                                                                              |
|                                                                              |
|                                                                              |
|                                                                              |
|                                                                              |
|                                                                              |
| enter choose   ^v move   esc back   ctrl-c cancel                            |
+------------------------------------------------------------------------------+
```

Attaching says what attaching means before you do it, because with the v1
backend it does not mean what people assume:

```
|  Attach a second tab to "review". Same policy file and same working          |
|  directory. Separate process tree: the tabs cannot see each other's          |
|  processes. Shared files, never a shared runtime.                            |
```

### 5.3 Profile

The detail panel is the point. You pick on what a profile *is*, not on its name.

```
+------------------------------------------------------------------------------+
| Profile                                                        3 saved       |
|                                                                              |
|  > claude-default      Claude Code, 10 tools, 4 network groups               |
|    offline-shell       plain shell, 4 tools, no network                      |
|    review              Claude Code, 3 tools, github only, shares ~/dev       |
|    ------------------------------------------------------------------------  |
|    Custom              paddock's built-in defaults, not a blank slate        |
|                                                                              |
|  claude-default                                                              |
|    agent     Claude Code (claude)                                            |
|    tools     git rg fd jq curl node npm npx uv python3                       |
|    network   api.anthropic.com, *.anthropic.com, github.com, *.github.com,   |
|              *.githubusercontent.com, registry.npmjs.org, +6                 |
|    files     isolated scratch directory, no host path writable               |
|    skills    none                                                            |
|                                                                              |
|                                                                              |
|                                                                              |
| / filter   enter choose   ^v move   esc back   ctrl-c cancel                 |
+------------------------------------------------------------------------------+
```

### 5.4 Tools

```
+------------------------------------------------------------------------------+
| Tools it can run                                          10 of 18 ticked    |
|                                                                              |
| Ticked binaries are on the sandbox PATH. Unticked ones are not reachable by  |
| name. An absolute path still runs them, so this is convenience, not a        |
| boundary. Writes and network are the real limits.                            |
|                                                                              |
|    [x] git                          [ ] go                                   |
|  > [x] rg                           [ ] cargo                                |
|    [x] fd                           [ ] make                                 |
|    [x] jq                           [ ] cmake                                |
|    [x] curl                         [ ] gh                                   |
|    [x] node                         [ ] docker                               |
|    [x] npm                          [ ] psql                                 |
|    [x] npx                          [ ] sqlite3                              |
|    [x] uv                           [x] kubectl (not installed)              |
|    [x] python3                                                               |
|                                                                              |
| space toggle   a all   n none   / filter   enter done   esc back (keeps ticks|
+------------------------------------------------------------------------------+
```

Two columns because eighteen short names in one column do not fit next to a
header and a footer. Up and down walk the whole list in reading order, so there
is no second navigation idiom to learn. A tool the host lacks stays on the list
and says so, as it does today.

### 5.5 Network

Groups and extra domains on one screen, which kills a question.

```
+------------------------------------------------------------------------------+
| Network access                              4 groups ticked, 12 domains      |
|                                                                              |
| Only these domains are reachable. Everything else is refused by the OS.      |
| Tick nothing for an offline sandbox.                                         |
|                                                                              |
|    [x] anthropic    api.anthropic.com, *.anthropic.com                       |
|    [x] github       github.com, *.github.com, *.githubusercontent.com        |
|    [x] npm          registry.npmjs.org, *.npmjs.org, *.npmjs.com             |
|  > [x] pypi/uv      pypi.org, files.pythonhosted.org, astral.sh, +1          |
|    [ ] go           proxy.golang.org, sum.golang.org                         |
|    [ ] crates.io    crates.io, *.crates.io, static.crates.io                 |
|    [ ] homebrew     formulae.brew.sh, *.brew.sh, ghcr.io, +1                 |
|                                                                              |
|    Also allow  [                                                          ]  |
|                space separated, for example example.com *.internal.dev       |
|                                                                              |
| Claude Code adds api.anthropic.com whichever groups you tick.                |
| space toggle   tab to the box   enter done   esc back (keeps ticks)          |
+------------------------------------------------------------------------------+
```

### 5.6 Files

One screen for what is two questions today, and the default reads as an answer
rather than as a "no".

```
+------------------------------------------------------------------------------+
| Files                                                                        |
|                                                                              |
| The sandbox can always write its own working directory, /tmp and /dev/null.  |
| A shared directory is the only path on your machine it can change.           |
|                                                                              |
|  > (o) An isolated scratch directory                                         |
|        Nothing of yours is writable. Work done here lives under              |
|        ~/.local/state/paddock/runs/ and outlives the tab.                    |
|                                                                              |
|    ( ) Share a directory                                                     |
|        [ ~/dev/paddock                                                    ]  |
|        Read and write. Relative paths resolve against ~/dev/paddock.         |
|                                                                              |
|                                                                              |
| Reads are a separate matter: the sandbox can read most of your disk, except  |
| ~/.ssh ~/.aws ~/.gnupg ~/.config/gh. Change that under Advanced.             |
|                                                                              |
|                                                                              |
| ^v move   space choose   tab to the box   enter done   esc back              |
+------------------------------------------------------------------------------+
```

### 5.7 Confirm

The only screen that shows the resolved policy: presets expanded, paths real,
the agent's own domains folded in.

```
+------------------------------------------------------------------------------+
| Launch this sandbox?                                                         |
|                                                                              |
|   session    claude-default-7f2a                     rename under Advanced   |
|   agent      Claude Code (claude)                                            |
|   profile    claude-default, unchanged                                       |
|                                                                              |
|   can write  its own workdir, /tmp, /dev/null. No path of yours.             |
|   can read   your disk, except ~/.ssh ~/.aws ~/.gnupg ~/.config/gh           |
|   can reach  12 domains: api.anthropic.com, *.anthropic.com, github.com,     |
|              *.github.com, *.githubusercontent.com, registry.npmjs.org,      |
|              *.npmjs.org, *.npmjs.com, pypi.org, +3                          |
|   can run    git rg fd jq curl node npm npx uv python3, plus /usr/bin:/bin   |
|   can see    its own Claude Code login. No other agent's keys. No skills.    |
|                                                                              |
|                                                                              |
|  > [ Launch ]      [ Back to the form ]      [ Cancel ]                      |
|                                                                              |
|                                                                              |
| enter choose   <> move   esc back   ctrl-c cancel                            |
+------------------------------------------------------------------------------+
```

When the answers do not match any saved profile, the profile line says so and
the save offer is right there:

```
|   profile    claude-default + changes           press s to save these answers|
```

### 5.8 Advanced

Everything that should never be asked, in one place, including four profile
fields the chooser has never asked about at all.

```
+------------------------------------------------------------------------------+
| Advanced                                                                     |
|                                                                              |
|  > Name              (generated: claude-default-7f2a)                        |
|    Save as profile   not saved                                               |
|    Keep running      no, the session ends with its last tab                  |
|    MCP servers       none                                                    |
|    Also writable     nothing beyond the workdir, /tmp and /dev/null          |
|    Never readable    ~/.ssh ~/.aws ~/.gnupg ~/.config/gh                     |
|    System PATH       yes, /usr/bin:/bin is appended                          |
|                                                                              |
|  The session name shows in the tab bar as sbx:<name> and is how you attach   |
|  to it later. Blank generates one from the profile.                          |
|                                                                              |
|                                                                              |
|                                                                              |
|                                                                              |
|                                                                              |
| enter edit   ^v move   esc back   ctrl-c cancel                              |
+------------------------------------------------------------------------------+
```

## 6. Room for what is coming

The form takes new fields without a redesign, which the linear walk did not: a
new question there meant a new screen for everyone.

- **The msb backend.** The spike measured a 175ms boot and confirmed per-VM
  network rules and volume mounts. When it lands, `Backend` becomes a field on
  the form, and the `Open` screen's attach entries gain the backend name,
  because attaching means different things per backend (SPEC §3.2).
- **Keep-alive** (SPEC §3.4) lands under Advanced instead of becoming a
  twelfth question.
- **The workspace default binding** (SPEC §3.3) becomes a key on the form: set
  these answers as the default for this workspace. It is a default answer to the
  `Open` field, which is exactly what the SPEC says it is.
- **MCP servers** get asked about for the first time.
- **A plain numbered mode**, if anyone ever needs one. Both huh and gh ship one:
  not a degraded TUI but a different UI, questions printed as numbered lists and
  answered by typing a number, for screen readers and for terminals that cannot
  redraw. It would double as the no-TTY path. Worth knowing it exists; not worth
  building until someone asks.

## 7. How to build it

Three honest options. Questionary gives select, checkbox, text and confirm, and
`questionary.form()` is only those asked one after another, so none of them is a
form in the sense this design needs.

### (a) Stay with questionary, hub and spoke

The form is a `questionary.select` whose entries are `label  value` rows.
Enter on a row opens that field's editor, then returns to the hub. Launch and
Cancel are rows.

- **Gets:** two interactions for the common case, every field in one list,
  editing one hop deep, no new dependency. Questionary has more than the current
  chooser uses, and option (a) should use it: `use_search_filter` gives the `/`
  behaviour of section 4.3, `use_shortcuts` gives digit jumps,
  `Choice(description=...)` gives a per-row hint line, `Choice(disabled="why")`
  greys a row and says why, and `Separator` gives the rules in the mockups.
- **Does not get:** a real screen. Questionary prints each answered question
  above the next one, so in a 24-row popup the hub scrolls away under its own
  history. It has no escape binding at all, only ctrl-c and ctrl-q, so "escape
  backs out" needs reaching into `Question.application.key_bindings`, which is
  not public API. The per-row hint is a fixed `Description: ...` prefix it does
  not let you rename. There is no footer, no error line, no two-column
  checklist. And its own validator refuses `use_jk_keys` together with
  `use_search_filter`, while *permitting* `use_shortcuts` with
  `use_search_filter`, which is a collision it does not guard: the digits go to
  the filter. So option (a) has to give up either `j` and `k` or `/` on every
  long list, and cannot have digits and `/` on the same one at all.
- **Cost:** about 180 lines replacing the 140-line questionary shell. One to two
  days.

### (b) prompt_toolkit `Application`

A full-screen application: the form is a list of rows, the editors are the same
application swapping its body, and every key binding is ours.

- **Gets:** all of section 5, exactly as drawn. One screen with no scrollback
  under it, a persistent footer, real `esc` and `?` and digit bindings, a live
  hint line, two-column checklists, and filter-as-you-type.
- **Dependency:** prompt_toolkit 3.0 is already installed, as questionary's own
  dependency. This declares it directly and drops questionary, which nothing
  else in the repo uses. The runtime dependency count stays at one and one layer
  goes away.
- **Testable headless, which was the worry.** Verified on this machine with
  prompt_toolkit 3.0.53:

  ```python
  with create_pipe_input() as pipe:
      pipe.send_text("\x1b[B\x1b[B\r")          # down down enter
      with create_app_session(input=pipe, output=DummyOutput()):
          result = build_form().run()
  ```

  It returns the third row. `\x1b\x1b` returns from escape, `\x03` raises
  `KeyboardInterrupt`. No terminal, so Ubuntu CI runs it. This is a better test
  than today's: it presses real keys instead of faking the prompt library.
  One caveat: a lone `\x1b` waits for the escape-sequence timeout, so tests send
  it twice or set `Application(..., ttimeoutlen=0)`.
- **Cost:** about 400 new lines, roughly a form widget at 150, a list picker at
  90, a checklist at 90, a text field at 40 and the key map and footer at 50.
  Three to four days, four PRs.

### (c) A form library

There is no `huh` for Python. The candidates and why each fails:

| Library | Why not |
| --- | --- |
| `textual` | A real answer, and far too much of one: an async app framework with its own event loop, CSS and a large dependency tree, against a repo rule of few dependencies |
| `InquirerPy` | A questionary fork on the same prompt_toolkit base, with the same one-question-at-a-time model. Swaps the problem for the same problem |
| `urwid`, `npyscreen`, `pytermgui` | Older widget toolkits. Same work as (b), plus a dependency and a second idiom |
| `simple-term-menu`, `beaupy` | Menus only, no form |

### Recommendation

**Take (b).** It is the only option that delivers the design rather than an
approximation of it, it removes a dependency layer rather than adding one, and
its test story is stronger than what is there now. Budget four PRs and three to
four days. Take (a) only if that budget is not available, and expect to redo it,
because the scrolling popup and the missing escape key are the two complaints
that started this.

## 8. Migration

### 8.1 What survives in `tui.py`

Most of it. The module was built as a thin shell over pure functions, and the
pure functions are right.

**Survives unchanged.** `Local`, `Attach`, `NewSession`, `Plan`.
`session_label`, `session_choices`, `profile_choices`, `agent_choices`,
`tool_choices`, `network_choices`, `skill_choices`. `parse_domains`,
`resolve_shared_dir`, `suggested_key`. `base_profile`, `chosen_agent`,
`build_session`, `build_profile`, `save_answers`, `remember_agent`. That is
roughly 250 of 576 lines, and it is the half that decides things.

**Survives with a new shape.** `first_choices` becomes `open_choices`, taking
the live sessions so local, new and attach come back as one list. `DEPENDENTS`
and `_answer` become one `settle(answers, field)` function: same two rules,
fired on a field change instead of during a walk. `summary_lines` grows into the
confirm screen and starts expanding presets through `Profile.allowed_domains()`
instead of listing group names.

**Goes.** `STEPS` as an ordered walk, `collect`, `_walk`, `_edit`,
`edit_choices`, the `SUMMARY` / `EDIT` / `SEED` pseudo-steps, the `Asker` and
`Notify` protocols, and the whole questionary shell: `_choose`, `_new_session`,
`_asker`, `_ask`, `_pick`, `_tick`, `_type`, `_options`, `_ticks`. `SKIP`
disappears, because a field with nothing to offer is simply not on the form.
`BACK` disappears as a value, because back becomes a key.

### 8.2 What the tests keep pinning

`tests/test_tui.py` has 60 tests.

- **31 keep passing untouched:** everything from "the first question" through "a
  typed-in agent command". They test what the questions offer and what the
  answers build, which is exactly what does not change. One exception,
  `test_attach_is_offered_only_when_a_session_exists`, follows
  `first_choices` into `open_choices`.
- **15 are rewritten in spirit:** the "back navigation and the summary" block.
  The `Steps` harness and every BACK and SKIP walk test go, because the walk
  goes. Their intent is kept as new tests on `settle()`: changing the profile
  hands over its values, changing the agent drops the skills, changing Files
  settles the directory, and the confirm shows every answer.
- **14 are replaced:** the "questionary shell" block. Same behaviours, driven
  through `create_pipe_input` with real key sequences: escape from an editor
  keeps the ticks, escape on the form closes with no plan, ctrl-c anywhere
  returns `None`, Launch builds the plan the fields described.

Nothing outside `tests/test_tui.py` moves. `cli.py` still receives a `Plan` and
`tests/test_cli.py` is untouched, which is the point of the chooser returning a
plan instead of launching anything.

### 8.3 PR breakdown

Epic slug `chooser_redesign`, cut from `develop`.

1. **`[chooser_redesign] fields, labels and rules`.** The field table, the plain
   English labels and hints, the merges (agent plus command plus key, network
   plus domains, share plus directory), `open_choices`, `settle()`, and the
   resolved-policy lines for the confirm. Pure functions and tests only, no
   screen. Diagrams: `chooser_flow.puml` redrawn from a walk to a form.
2. **`[chooser_redesign] the screen layer`.** New module, the prompt_toolkit
   form, list picker, checklist, text field, footer and key map. Headless tests
   with `create_pipe_input`. Nothing wired up yet.
3. **`[chooser_redesign] wire the chooser to the form`.** `choose()` drives the
   form, the old shell and its tests are deleted, questionary comes out of
   `pyproject.toml` and prompt_toolkit goes in. SPEC §3.1 and the README's "The
   chooser" section are rewritten to match. This is the PR the user tests.
4. **`[chooser_redesign] advanced and confirm`.** The Advanced screen, so name,
   save as profile, keep-alive (SPEC §3.4), MCP, extra writable paths, denied
   reads and the system PATH all get a home, plus the confirm screen's resolved
   policy.

PR 3 is the one that changes what the user sees, so it is the one to ping them
on, per the usual rule.

### 8.4 What this does not change

- The chooser still returns a plan and launches nothing. Backing out still costs
  nothing.
- Profiles, the agent registry, sessions, the backends and the synthesized
  config dir are untouched. This is a UI change over the same data.
- No permission gets a wider default. The confirm screen makes the grant more
  visible, not less deliberate.
