"""The chooser: what the popup asks, and the plan it hands back.

One screen. `screen.py` draws the form and every editor over it; everything that decides
something (which sessions to offer, which tools the host has, what a value means, the answers
as a `Profile`) is a plain function here, so it is tested without a terminal. One answers dict
holds the lot, `settle()` keeps it consistent when a field changes, and `form_rows()` and
`confirm_lines()` say what those answers mean.

Nothing in this module launches anything: `choose()` returns a plan and `cli.py` carries it
out, which is why backing out at any depth costs nothing.
"""

from __future__ import annotations

import json
import shlex
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from paddock import recent, screen, sessions
from paddock.agents import AgentSpec, agent_dir, load_agents
from paddock.profiles import (
    DEFAULT_DENY_READ,
    EVERYTHING,
    LOCAL_SERVICES_CONSEQUENCE,
    NETWORK_ALL,
    NETWORK_PRESETS,
    TOOL_CANDIDATES,
    Profile,
    load_profiles,
    loopback_port,
    names_loopback,
    save_profile,
)

# The "none of the saved ones" entry in the profile and agent lists. It is not a
# name anyone would give a file, so it cannot collide with a real key.
CUSTOM = "+custom"
# The two answers to the Open field that are not a session to attach to.
NEW, LOCAL = "new", "local"

# The sandbox a session runs under (SPEC 3.2). srt is v1 and always here; the microVM backend
# needs a binary of its own, and the field says so when this machine has not got one.
SRT, MSB = "srt", "msb"

# The agent that means "the guest's own shell". msb gives it the default image rather than
# refusing it for having none of its own (SPEC §2.2), so the agent list must know it too.
SHELL_AGENT = "shell"
BACKEND_LABELS = {SRT: "srt (instant, a policy sandbox)", MSB: "msb (a microVM, full isolation)"}
BACKEND_HINTS = {
    SRT: "Instant. A policy sandbox around the process itself: Seatbelt on macOS, bubblewrap "
    "on Linux. It shares this machine's kernel and filesystem, minus what the policy refuses.",
    MSB: "A microVM: about 20 seconds to its first start, and full isolation, with a kernel "
    "and a filesystem of its own.",
}

# What each allow-all row is called, and what ticking it grants in the words of the screen
# that says what was granted.
ALL_ROWS = {
    NETWORK_ALL: "everything (any domain, no restriction)",
    "tools": "Everything on the host PATH",
    "skills": "All installed skills",
}
ALL_GRANTED = {
    "network": "ANY domain (unrestricted)",
    "tools": "the full host PATH",
    "skills": "all skills",
}

# Why the network's allow-all cannot be ticked on srt. Measured against srt 0.0.73:
# `network.allowedDomains` is a required key and a bare `*` is refused as too broad, so
# there is no settings file that means unrestricted egress (SPEC §2.1).
NO_ALLOW_ALL_ON_SRT = (
    "srt cannot run without a domain allowlist; use the msb backend or list domains"
)

# The two rows of the Files field, which were two questions before this design.
FILES_CHOICES = (
    (
        "An isolated scratch directory",
        "Nothing of yours is writable. What is done here lives under the run dir and "
        "outlives the tab.",
    ),
    (
        "Share a directory",
        "That one directory is the only thing on this machine the sandbox can change.",
    ),
)

# The fields of the form, in the order they are shown. Fixed, because a digit jumps to each of
# them and a list that reorders would make a digit a lie.
FIELDS = (
    "open",
    "profile",
    "backend",
    "agent",
    "tools",
    "network",
    "files",
    "skills",
    "advanced",
)

# What each field is called on the form.
FIELD_LABELS = {
    "open": "Open",
    "profile": "Profile",
    "backend": "Backend",
    "agent": "Agent",
    "tools": "Tools",
    "network": "Network",
    "files": "Files",
    "skills": "Skills",
    "advanced": "Advanced",
}

# The heading of the editor that opens on a field.
FIELD_TITLES = {
    "open": "Open",
    "profile": "Profile",
    "backend": "Backend",
    "agent": "Agent",
    "tools": "Tools it can run",
    "network": "Network access",
    "files": "Files",
    "skills": "Skills it can see",
    "advanced": "Advanced",
}

# One line under the field the cursor is on. Each says what the value showing means, not what
# the question is: "everything else is refused" tells you what happens, "Network:" does not.
FIELD_HINTS = {
    "open": "New sandbox: an agent under the OS sandbox. Local tab: an ordinary herdr tab "
    "with full access to this machine.",
    "profile": "Fills in everything below. Change anything and the title says \"+ changes\", "
    "because the session will then not be what the profile says.",
    "backend": "What runs the sandbox. srt wraps the process in the OS policy and starts at "
    "once. msb boots a microVM, which is slower to start and shares less with this machine.",
    "agent": "The command that runs inside the sandbox. It gets its own login and no other "
    "agent's.",
    "tools": "Ticked binaries are on the sandbox PATH. Unticked ones are not reachable by "
    "name. An absolute path still runs them, so this is convenience, not a boundary.",
    "network": "Only these domains are reachable. Everything else is refused by the OS. Tick "
    "nothing and only the agent's own API stays reachable; pick the Shell agent for a fully "
    "offline sandbox.",
    "files": "Isolated: the sandbox gets a fresh scratch directory and no host path of yours "
    "is writable. Shared: that one directory is the only thing on your machine it can change. "
    "Want no filesystem fence at all? Use a Local tab.",
    "skills": "Unticked skills are not in the sandbox's config dir at all, so the agent "
    "cannot find them.",
    "advanced": "The session name, saving these answers as a profile, keeping the session "
    "running, MCP servers, extra writable paths, denied reads and the system PATH.",
}

# One line per box inside an editor. Merging questions into a field cost them their screen,
# not their explanation.
EDITOR_HINTS = {
    "command": "Runs inside the sandbox, so it has to be a tool the sandbox can reach.",
    "directory": "Read and write, and the only path of yours the sandbox can change. A "
    "relative one is next to the directory the popup was opened in.",
    "also_allow": "Space separated, for example example.com *.internal.dev",
    "name": "Shown in the tab bar as sbx:<name> and used to attach later. Blank generates one.",
    "save_as": "Save these answers so they are one pick next time.",
    "keep_alive": "A session normally ends with its last tab. Kept running, it waits with its "
    "files and its policy until you end it yourself.",
    "mcp": "Space separated. Only the servers named here are in the sandbox's config dir, so "
    "the agent cannot reach any other one.",
    "extra_allow_write": "Space separated paths, beyond the workdir, /tmp and /dev/null. Every "
    "one of them is a path on this machine the sandbox can change.",
    "deny_read": "Space separated paths the sandbox may not read at all, whatever else it can. "
    "Emptying this hands over the credential directories it names.",
    "include_system_path": "Whether /usr/bin:/bin is appended to the sandbox PATH, which is "
    "what gives it a shell and the ordinary commands.",
}

# The Advanced rows that are a yes or a no, with what each answer means and what it holds.
ADVANCED_FLAGS = {
    "keep_alive": (
        (False, "No, the session ends with its last tab"),
        (True, "Yes, it waits until you end it yourself"),
    ),
    "include_system_path": (
        (True, "Yes, /usr/bin:/bin is appended"),
        (False, "No, only the tools ticked above"),
    ),
}

# The Advanced rows that are a list of words in a box.
ADVANCED_LISTS = {
    "mcp": "none",
    "extra_allow_write": "nothing beyond the workdir, /tmp and /dev/null",
    "deny_read": "nothing, so the sandbox can read whatever you can",
}

# The heading of the last screen, which is a question and not an announcement.
CONFIRM_TITLE = "Launch this sandbox?"

# What an in-guest install downloads from. msb runs the install inside the guest, where the
# profile's domains are the only way out, so an npm install needs the npm preset (SPEC §2.2).
INSTALL_TOOLS = ("npm", "npx")
INSTALL_PRESET = "npm"
INSTALL_WARNING = "the in-guest install needs npm; add the npm preset or the first start will fail"

# How long an msb launch sits there before its first tab. Measured with the image already
# pulled: a first pull is on top of it.
FIRST_START_SECONDS = 40

# The line under a field a local or attached tab greys out.
NO_SANDBOX = "No sandbox, so there is nothing to permit."

# The second question the Open field asks about a live session: what goes in the new tab.
ATTACH_TITLE = "What goes in the tab?"
ATTACH_CHOICES = (
    ("Attach the agent", "The agent again, under the same policy and on the same files."),
    (
        "Open a shell inside it",
        "A plain shell in the same sandbox: the same files, the same policy, no second agent.",
    ),
)

# One line per entry on the Open list.
OPEN_HINTS = {
    NEW: "An agent under the OS sandbox, with the permissions below. Seatbelt on macOS, "
    "bubblewrap on Linux.",
    LOCAL: "No sandbox. The agent can read and write anything you can.",
    "attach": "A second tab on a sandbox already running. Same files and same policy, "
    "separate process tree.",
}

# What a base profile carries, so picking another one hands over all of it.
PROFILE_CARRIES = (
    "agent",
    "command",
    "remember_as",
    "tools",
    "network",
    "domains",
    "skills",
    "share",
    "directory",
)

@dataclass
class Local:
    """Open an ordinary tab. No session, no sandbox."""

    cwd: str


@dataclass
class Attach:
    """Put a new tab on a session that is already running."""

    ref: str
    # Blank leaves the session its own workdir, which is what attaching usually means.
    cwd: str = ""
    # A plain shell inside the sandbox rather than the agent again (SPEC §3.2).
    shell: bool = False


@dataclass
class NewSession:
    """Start a session from these answers."""

    profile: Profile
    # Session name. Blank lets sessions pick one.
    name: str = ""
    # Save the answers as a profile under this name. Blank saves nothing.
    save_as: str = ""
    # A command the user typed instead of picking an agent, remembered as `profile.agent`.
    agent_command: str = ""
    # Which sandbox runs it (SPEC 3.2). srt wraps the process; msb boots a microVM.
    backend: str = SRT
    # Whether the session outlives its last tab (SPEC 3.4).
    keep_alive: bool = False
    # The saved profile the answers stand on, which is what the chooser opens on next time.
    started_from: str = ""


Plan = Local | Attach | NewSession


def choose(cwd: Path, answers: dict | None = None, attach: bool = False) -> Plan | None:
    """Ask what to open. None means the popup was closed with nothing done.

    Ctrl-c raises KeyboardInterrupt from wherever it was pressed, which `cli.py` turns into
    the exit code 130 that fzf made the convention.

    `answers` starts the form on answers already given, which is how a launch that failed
    comes back to the form it was made on instead of to a blank one.

    `attach` opens the Open list first, which is the whole of the second key binding: a
    session picked there is launched without the form, and backing out of it leaves the
    form exactly where it would have been. With nothing running there is no list to open,
    so it is the ordinary chooser.
    """
    saved, registry = load_profiles(), load_agents()
    live = sessions.list_sessions()
    answers = dict(answers) if answers else opening_answers(saved)
    if attach and live:
        # On the first session, not on New sandbox: the key that opens this one means
        # attaching, and the list is two rows of other answers before it gets there.
        answers = _edit_open(answers, live, cursor=len(open_choices([])))
        picked = plan_from(answers, base_profile(saved, answers), cwd)
        if isinstance(picked, Attach):
            return picked
    cursor = 0
    while True:
        base = base_profile(saved, answers)
        rows = form_rows(answers, base, registry, live, str(cwd))
        chosen = screen.form(form_title(answers, base), f"in {cwd}", rows, cursor)
        if chosen is None:  # escape or Cancel: nothing chosen and nothing done
            return None
        what, cursor = chosen
        if what == screen.LAUNCH:
            plan = plan_from(answers, base, cwd)
            if not isinstance(plan, NewSession):
                return plan  # a local or an attached tab permits nothing, so there is no policy
            while True:
                said = screen.confirm(CONFIRM_TITLE, confirm_lines(answers, base, registry))
                if said != screen.SAVE:
                    break
                answers = _edit_save_as(answers)  # the offer section 5.7 puts on this screen
                plan = plan_from(answers, base, cwd)
            if said == screen.CANCEL:
                return None
            if said == screen.LAUNCH:
                return plan
            continue  # back to the form, with every answer where it was
        if what == screen.SAVE:
            # A local or an attached tab permits nothing, so it has nothing to save either.
            if editable(answers, "advanced"):
                answers = _edit_save_as(answers)
        elif editable(answers, FIELDS[cursor]):
            answers = _edit(FIELDS[cursor], answers, base, saved, registry, live, cwd)


def opening_answers(saved: dict[str, Profile]) -> dict:
    """What the form opens on: the profile this workspace launched last, if it still exists.

    Reusing the last run's answers is the whole saving of a form over a walk, and a profile
    that has since been deleted is no answer, so paddock's own defaults stand instead.
    """
    remembered = recent.last_profile()
    return {"profile": remembered} if remembered in saved else {}


def plan_from(answers: dict, base: Profile, cwd: Path) -> Plan:
    """The answers as the one thing the popup hands back."""
    opened = str(answers.get("open", NEW))
    if opened == LOCAL:
        return Local(cwd=str(cwd))
    if opened != NEW:  # anything else on the Open list is a session to attach to
        return Attach(ref=opened, shell=bool(answers.get("shell")))
    return build_session(base, answers)


def editable(answers: dict, field: str) -> bool:
    """A local or an attached tab permits nothing, so only the Open field opens on one."""
    return field == "open" or str(answers.get("open", NEW)) == NEW


# --- one editor per field ---------------------------------------------------


def _edit(
    field: str,
    answers: dict,
    base: Profile,
    saved: dict[str, Profile],
    registry: dict[str, AgentSpec],
    live: list[sessions.Session],
    cwd: Path,
) -> dict:
    """Open the editor for one field, and give back the answers it leaves behind.

    Escape closes an editor keeping what was done, so every one of these can come back with
    the answers it was given and nothing lost.
    """
    if field == "open":
        return _edit_open(answers, live)
    if field == "profile":
        return _edit_profile(answers, saved, registry)
    if field == "backend":
        return _edit_backend(answers, base)
    if field == "agent":
        return _edit_agent(answers, base, registry)
    if field == "tools":
        return _edit_tools(answers, base)
    if field == "network":
        return _edit_network(answers, base)
    if field == "files":
        return _edit_files(answers, base, cwd)
    if field == "skills":
        return _edit_skills(answers, base, registry)
    return _edit_advanced(answers, base)


def _edit_open(answers: dict, live: list[sessions.Session], cursor: int | None = None) -> dict:
    """The Open list, and for a live session the second question of what to put in the tab.

    Two screens for one field, so escape from the second backs out to the list, not to the
    form, exactly as the Files field's directory box does.

    `cursor` overrides where the list opens, which is what the attach key uses: a key that
    means "attach to something" should land on something to attach to.
    """
    choices = open_choices(live)
    rows = [(title, open_hint(value)) for title, value in choices]
    where = _at(choices, str(answers.get("open", NEW))) if cursor is None else cursor
    while True:
        index = screen.pick(FIELD_TITLES["open"], rows, cursor=where, rule_after=open_rule(live))
        if index is None:
            return answers
        opened = choices[index][1]
        if opened in (NEW, LOCAL):
            return dict(answers, open=opened, shell=False)
        was = int(bool(answers.get("shell")))
        how = screen.pick(ATTACH_TITLE, list(ATTACH_CHOICES), cursor=was)
        if how is not None:
            return dict(answers, open=opened, shell=how == 1)
        where = index  # backed out of the second question, so the list opens where it was


def _edit_profile(answers: dict, saved: dict[str, Profile], registry: dict[str, AgentSpec]) -> dict:
    choices = profile_choices(saved)
    rows = [(title, profile_hint(value, saved, registry)) for title, value in choices]
    note = f"{len(saved)} saved"
    where = _at(choices, str(answers.get("profile", CUSTOM)))
    index = screen.pick(FIELD_TITLES["profile"], rows, note, where, rule_after=len(choices) - 2)
    if index is None:
        return answers
    return settle(dict(answers, profile=choices[index][1]), "profile")


def _edit_backend(answers: dict, base: Profile) -> dict:
    rows = backend_choices(build_session(base, answers).profile.opens_every_domain())
    refused = {index: why for index, (_, _, why) in enumerate(rows) if why}
    choices = [(key, hint) for key, hint, _ in rows]
    where = _at([(key, key) for key, _, _ in rows], str(answers.get("backend", SRT)))
    index = screen.pick(FIELD_TITLES["backend"], choices, cursor=where, refused=refused)
    return answers if index is None else dict(answers, backend=rows[index][0])


def _edit_agent(answers: dict, base: Profile, registry: dict[str, AgentSpec]) -> dict:
    choices = agent_choices(registry, str(answers.get("backend", SRT)))
    rows = [(title, agent_hint(value, registry)) for title, value, _ in choices]
    refused = {index: why for index, (_, _, why) in enumerate(choices) if why}
    listed = [(title, value) for title, value, _ in choices]
    where = _at(listed, str(answers.get("agent", base.agent)))
    index = screen.pick(FIELD_TITLES["agent"], rows, cursor=where, refused=refused)
    if index is None:
        return answers
    key = choices[index][1]
    if key != CUSTOM:
        return settle(dict(answers, agent=key), "agent")
    # The command and the key it is saved under are on this field too, not two more questions.
    command = screen.type_in("Command", str(answers.get("command", "")), EDITOR_HINTS["command"])
    if not command:
        return answers
    settled = settle(dict(answers, agent=CUSTOM, command=command), "agent")
    return dict(settled, remember_as=remembered_key(command, registry))


def _edit_tools(answers: dict, base: Profile) -> dict:
    rows = tool_choices(base, answers.get("tools"))
    if not rows:  # nothing on this host to offer, so there is nothing to ask
        return answers
    before = [value for _, value, on in rows if on]
    ticked = screen.tick(
        FIELD_TITLES["tools"],
        _boxes(rows),
        FIELD_HINTS["tools"],
        never_all=_apart(rows, EVERYTHING),
    )
    return dict(answers, tools=exclusive(before, [rows[index][1] for index in ticked], EVERYTHING))


def _edit_network(answers: dict, base: Profile) -> dict:
    rows = network_choices(base, answers.get("network"))
    typed = str(answers.get("domains", " ".join(base.extra_domains)))
    before = [value for _, value, on in rows if on]
    ticked, extra = screen.tick(
        FIELD_TITLES["network"],
        _boxes(rows),
        FIELD_HINTS["network"],
        box=("Also allow", typed, EDITOR_HINTS["also_allow"]),
        refused=network_refusals(rows, str(answers.get("backend", SRT))),
        never_all=_apart(rows, NETWORK_ALL),
    )
    picked = exclusive(before, [rows[index][1] for index in ticked], NETWORK_ALL)
    return dict(answers, network=picked, domains=extra)


def exclusive(before: list[str], after: list[str], all_of_it: str) -> list[str]:
    """Allow-all and a list of exceptions cannot both be the answer, so one of them wins.

    Whichever was just ticked is the one meant: ticking allow-all clears the rest, and
    ticking anything else while allow-all is on takes allow-all off. Neither is a screen
    telling the user their last key press did nothing.
    """
    if all_of_it not in after:
        return after
    if all_of_it not in before:
        return [all_of_it]
    return [name for name in after if name != all_of_it] or [all_of_it]


def network_refusals(rows: list[tuple[str, str, bool]], backend: str) -> dict[int, str]:
    """Which network rows this backend cannot enforce, and why (SPEC §2.1).

    srt has no allow-all: every settings file it takes names the domains. The row stays on
    the list and says so, the way an agent this machine has not got stays on the agent list,
    because hiding it would leave the user wondering where it went.
    """
    if backend != SRT:
        return {}
    return {index: NO_ALLOW_ALL_ON_SRT for index, (_, value, _) in enumerate(rows)
            if value == NETWORK_ALL}


def _edit_files(answers: dict, base: Profile, cwd: Path) -> dict:
    """Two rows and a box, and escape from the box backs out to the rows, not to the form."""
    shares = shares_a_directory(answers, base)
    while True:
        index = screen.pick(FIELD_TITLES["files"], list(FILES_CHOICES), cursor=int(shares))
        if index is None:
            return answers
        if index == 0:
            return settle(dict(answers, share=False), "files")
        typed, backed_out = screen.typed_in(
            "Directory",
            str(answers.get("directory") or base.shared_dir or cwd),
            EDITOR_HINTS["directory"],
            check=lambda text: missing_directory(text, cwd),
        )
        shared = resolve_shared_dir(typed, cwd)
        answers = settle(dict(answers, share=bool(shared), directory=shared), "files")
        if not backed_out:
            return answers
        shares = bool(shared)


def _edit_skills(answers: dict, base: Profile, registry: dict[str, AgentSpec]) -> dict:
    agent = chosen_agent(answers, base)
    carried = base.skills if agent == base.agent else []
    rows = skill_choices(registry.get(agent, AgentSpec()), list(answers.get("skills", carried)))
    if not rows:  # this agent has no skills directory, so there is nothing to ask
        return answers
    before = [value for _, value, on in rows if on]
    ticked = screen.tick(
        FIELD_TITLES["skills"],
        _boxes(rows),
        FIELD_HINTS["skills"],
        never_all=_apart(rows, EVERYTHING),
    )
    return dict(answers, skills=exclusive(before, [rows[index][1] for index in ticked], EVERYTHING))


def _edit_advanced(answers: dict, base: Profile) -> dict:
    """The list of rows, and the editor for one of them. Escape backs out one at a time."""
    cursor = 0
    while True:
        rows = advanced_choices(answers, base)
        shown = [(label, hint) for label, hint, _ in rows]
        index = screen.pick(FIELD_TITLES["advanced"], shown, cursor=cursor)
        if index is None:
            return answers
        label, _, step = rows[index]
        cursor = index  # the list comes back where it was left
        if step in ADVANCED_FLAGS:
            answers = _edit_flag(answers, base, label, step)
            continue
        hint = EDITOR_HINTS[step]
        if step in ADVANCED_LISTS:
            typed = screen.type_in(label, " ".join(advanced_list(step, answers, base)), hint)
            answers = dict(answers, **{step: parse_paths(typed)})
            continue
        answers = dict(answers, **{step: screen.type_in(label, str(answers.get(step, "")), hint)})


def _edit_flag(answers: dict, base: Profile, label: str, step: str) -> dict:
    """A yes or a no, as two rows that say what each one means."""
    rows = ADVANCED_FLAGS[step]
    standing = advanced_flag(step, answers, base)
    where = next(place for place, (value, _) in enumerate(rows) if value == standing)
    choices = [(said, EDITOR_HINTS[step]) for _, said in rows]
    index = screen.pick(label, choices, cursor=where)
    return answers if index is None else dict(answers, **{step: rows[index][0]})


def _edit_save_as(answers: dict) -> dict:
    """The `s` key: the same box the Advanced screen opens, one press from the form."""
    saved = str(answers.get("save_as", ""))
    return dict(answers, save_as=screen.type_in("Save as profile", saved, EDITOR_HINTS["save_as"]))


def _apart(rows: list[tuple[str, str, bool]], value: str) -> set[int]:
    """Where the allow-all row is, so the `a` key can leave it alone.

    "All of them" and "no list at all" are different answers, and the key for the first
    must not hand out the second.
    """
    return {index for index, (_, name, _) in enumerate(rows) if name == value}


def _boxes(rows: list[tuple[str, str, bool]]) -> list[tuple[str, bool]]:
    """A checklist takes what to show and whether it is ticked. The values stay here."""
    return [(title, on) for title, _, on in rows]


def remembered_key(command: str, registry: dict[str, AgentSpec]) -> str:
    """The key a typed command is saved under, asked for only when it would take another's.

    The box opens on a name nothing answers to, so taking it as it stands cannot fail. The
    colliding one is what the question is about, not what it offers.
    """
    key = suggested_key(command, registry)
    known = registry.get(key)
    if known is not None and known.command != command:
        key = screen.type_in(key_clash(key), free_key(key, registry)).strip()
    return key


def free_key(key: str, registry: dict[str, AgentSpec]) -> str:
    """`key` if the registry has no such agent, else the same with a number after it."""
    candidate, count = key, 2
    while candidate in registry:
        candidate, count = f"{key}-{count}", count + 1
    return candidate


def missing_directory(typed: str, cwd: Path) -> str:
    """What is wrong with a shared directory, for the line the key list gives up (section 4.2)."""
    if not typed.strip():
        return ""
    where = resolve_shared_dir(typed, cwd)
    return "" if Path(where).is_dir() else f"no directory there: {where}"


# --- the rules -------------------------------------------------------------


def settle(answers: dict, field: str, base: Profile | None = None) -> dict:
    """The answers after `field` changed, with whatever that field decides forgotten.

    The rules the questionnaire had, fired on a field change instead of during a walk.
    Another base profile means starting over from it, not keeping the old ticks against it.
    Skills and a typed command come out of the agent, so another agent's do not carry over.
    Sharing nothing leaves no directory behind. A forgotten answer is not lost work: the
    profile or the agent that now stands answers it.

    `base` is the profile the answers stand on. It reaches only the Files rule, and only
    when nothing has answered that field yet.
    """
    settled = dict(answers)
    if field == "profile":
        for carried in PROFILE_CARRIES:
            settled.pop(carried, None)
    elif field == "agent":
        settled.pop("skills", None)
        if settled.get("agent") != CUSTOM:  # a registered agent brings its own command
            settled.pop("command", None)
            settled.pop("remember_as", None)
    elif field == "files" and not shares_a_directory(settled, base or Profile()):
        settled.pop("directory", None)
    return settled


def shares_a_directory(answers: dict, base: Profile) -> bool:
    """Whether a host directory is shared: the answer, else a typed path, else the profile.

    One answer for two old questions, so a typed directory is never dropped for want of a
    yes beside it.
    """
    if "share" in answers:
        return bool(answers["share"])
    return bool(answers.get("directory") or base.shared_dir)


def profile_label(key: str) -> str:
    """What a profile answer is called on screen. The blank start is Custom, not a key."""
    return "Custom" if key == CUSTOM else key


# --- the form and the confirm ----------------------------------------------


def form_title(answers: dict, base: Profile) -> str:
    """What the form calls the answers: the profile they stand on, and whether they match it.

    A session that says it runs `claude-default` has to be the permissions that profile
    describes, so anything changed is said out loud in the title.
    """
    name = profile_label(str(answers.get("profile", CUSTOM)))
    built = build_session(base, answers).profile
    return name if replace(built, name=base.name) == base else f"{name} + changes"


def form_rows(
    answers: dict,
    base: Profile,
    registry: dict[str, AgentSpec],
    live: list[sessions.Session],
    cwd: str = "",
) -> list[tuple[str, str, str, str]]:
    """The form: a label, the value showing, the hint for it, and a count kept at the edge.

    A local or an attached tab greys out everything the sandbox fields decide, because none
    of it applies. Nothing is hidden and nothing moves, so the screen never rearranges.

    `live` is the session list the Open answer is read against, so it is required: without
    it every attach would read as a new sandbox, which is the opposite of what it does.
    """
    opened = str(answers.get("open", NEW))
    values = _field_values(answers, base, registry, live, cwd, bool(answers.get("shell")))
    notes = _field_notes(answers, base)
    rows = []
    for field in FIELDS:
        if opened != NEW and field not in ("open", "files"):
            rows.append((FIELD_LABELS[field], "-", NO_SANDBOX, ""))
        else:
            rows.append((FIELD_LABELS[field], values[field], FIELD_HINTS[field], notes[field]))
    return rows


def confirm_lines(
    answers: dict, base: Profile, registry: dict[str, AgentSpec]
) -> list[tuple[str, str]]:
    """The resolved policy, for the last screen before anything happens.

    The form shows group names; this is the only screen that shows the domains they open,
    the agent's own domains folded in and the paths as they will be.
    """
    plan = build_session(base, answers)
    profile = plan.profile
    agent = agent_title(profile.agent, registry)
    if plan.agent_command:  # the key is derived, so it can still be blank here
        running = f"running {plan.agent_command}"
        agent = f"{agent}, {running}" if agent else running
    lines = [
        ("session", plan.name or "generated at launch"),
        ("backend", BACKEND_LABELS.get(plan.backend, plan.backend)),
        ("agent", agent),
        ("profile", _profile_line(answers, base)),
        ("can write", _writable(profile, plan.backend)),
        ("can read", _readable(profile, plan.backend)),
        ("can reach", _reachable(profile, plan.backend)),
        ("can run", _runnable(profile, registry, plan.backend)),
        ("can see", _visible(profile, registry)),
    ]
    warned = [warning for warning in (policy_warning(plan), install_warning(plan, registry))
              if warning]
    return lines + [("warning", warning) for warning in warned]


def policy_warning(plan: NewSession) -> str:
    """Why this backend will refuse this policy, or nothing when it will not.

    The Backend and Network fields refuse each other's answer already, so reaching here
    takes a saved profile that names both. This screen is the one that says what is being
    granted, and it may not assert a grant the backend is about to reject (SPEC §2.1).
    """
    if plan.backend == SRT and plan.profile.opens_every_domain():
        return NO_ALLOW_ALL_ON_SRT
    return ""


def install_warning(plan: NewSession, registry: dict[str, AgentSpec]) -> str:
    """Why this msb launch is about to fail on its install, or nothing when it is not.

    The install runs inside the guest, where the profile's domains are the whole of the
    network, so an agent installed from npm needs the npm preset. Said before the wait
    rather than after it, and the profile is never changed to suit: what a sandbox may
    reach is an answer the user gives (SPEC §6).
    """
    if plan.backend != MSB:
        return ""
    spec = registry.get(plan.profile.agent)
    try:
        words = shlex.split(spec.install) if spec and spec.install else []
    except ValueError:
        return ""  # an install nobody can read is one this cannot say anything about
    if not words or words[0] not in INSTALL_TOOLS:
        return ""
    if plan.profile.opens_every_domain():
        return ""  # no allowlist at all reaches the registry along with everything else
    needed = set(NETWORK_PRESETS[INSTALL_PRESET])
    return "" if needed <= set(plan.profile.allowed_domains()) else INSTALL_WARNING


def starting_lines(plan: NewSession, registry: dict[str, AgentSpec]) -> list[str]:
    """The steps a launch is about to take, for the screen drawn before it blocks on them.

    Only the slow ones earn a line. An msb launch pulls an image and installs the agent in
    the guest, which is the minute the popup used to sit through with nothing on it.
    """
    if plan.backend != MSB:
        return ["preparing the sandbox"]
    spec = registry.get(plan.profile.agent, AgentSpec())
    steps = [f"pulling the {spec.image or 'guest'} image"]
    if spec.install:
        steps.append(f"installing {plan.profile.agent} in the guest")
        steps.append(f"the first start takes about {FIRST_START_SECONDS} seconds")
    warning = install_warning(plan, registry)
    return steps + [warning] if warning else steps


def starting(plan: NewSession) -> None:
    """Say what the launch is doing, before the call that blocks until it is done."""
    screen.progress(f"Starting {plan.name or 'a new sandbox'}", starting_lines(plan, load_agents()))


def launch_failed(message: str, log_path: str = "") -> bool:
    """Show a launch that never opened a pane. True means back to the form, answers and all."""
    return screen.failed(message, log_path)


def _field_values(
    answers: dict,
    base: Profile,
    registry: dict[str, AgentSpec],
    live: list[sessions.Session],
    cwd: str,
    shell: bool = False,
) -> dict[str, str]:
    """What each field reads as. Every value says the answer, not the question."""
    plan = build_session(base, answers)
    profile = plan.profile
    opened = str(answers.get("open", NEW))
    return {
        "open": _open_value(opened, live, shell),
        "profile": profile_label(str(answers.get("profile", CUSTOM))),
        "backend": _backend_value(answers),
        "agent": agent_title(profile.agent, registry),
        "tools": _tools_value(profile),
        "network": _network_value(profile, registry),
        "files": _files_value(opened, profile, cwd),
        "skills": _skills_value(profile, registry),
        "advanced": _advanced_value(plan),
    }


def _field_notes(answers: dict, base: Profile) -> dict[str, str]:
    """What a field keeps at the right edge: how much of a thing its value names."""
    profile = build_session(base, answers).profile
    domains = profile.allowed_domains()
    counted = _counted(domains) if domains else "offline"
    if profile.opens_every_domain():
        counted = "unrestricted"
    return {
        field: "" for field in FIELDS
    } | {
        "tools": "(all)" if EVERYTHING in profile.tools else (
            f"({len(profile.tools)})" if profile.tools else ""
        ),
        "network": f"({counted})",
    }


def _tools_value(profile: Profile) -> str:
    if EVERYTHING in profile.tools:
        return ALL_ROWS["tools"].lower()
    return " ".join(profile.tools) or "none"


def _network_value(profile: Profile, registry: dict[str, AgentSpec]) -> str:
    """What the Network row reads as.

    Nothing ticked is not no network: the chosen agent's own API is open whatever the
    profile says (SPEC §5), so a row reading "none" beside a note counting two domains was
    the form contradicting itself.
    """
    if profile.opens_every_domain():
        return "everything (any domain)"
    if profile.network_presets:
        return ", ".join(profile.network_presets)
    if profile.extra_domains:
        return "the domains you typed"
    spec = registry.get(profile.agent)
    if spec and spec.api_domains:
        return f"only {spec.name or profile.agent}'s own API"
    return "none"


def _skills_value(profile: Profile, registry: dict[str, AgentSpec]) -> str:
    """An agent with no skills directory says so, rather than opening an empty list."""
    if EVERYTHING in profile.skills:
        return ALL_ROWS["skills"].lower()
    if profile.skills:
        return " ".join(profile.skills)
    offered = skill_choices(registry.get(profile.agent, AgentSpec()), [])
    return "none" if offered else "none for this agent"


def _at(choices: list[tuple[str, str]], value: str) -> int:
    """Where an answer already given sits on its list, so opening it changes nothing."""
    values = [item[1] for item in choices]
    return values.index(value) if value in values else 0


def _backend_value(answers: dict) -> str:
    backend = str(answers.get("backend", SRT))
    return BACKEND_LABELS.get(backend, backend)


def _open_value(opened: str, live: list[sessions.Session], shell: bool = False) -> str:
    """A session that ended between the list and the form says so, rather than reading as new."""
    if opened == NEW:
        return "New sandbox"
    if opened == LOCAL:
        return "Local tab"
    for session in live:
        if session.session_id == opened:
            what = "a shell in" if shell else "the agent on"
            return f"Attach {what} {session.name}"
    return f"session is gone: {opened}"


def _counted(domains: list[str]) -> str:
    return f"{len(domains)} domain" + ("" if len(domains) == 1 else "s")


def _files_value(opened: str, profile: Profile, cwd: str) -> str:
    if opened == LOCAL:
        return cwd or "this directory"
    if opened != NEW:
        return "the session's own workdir"
    return profile.shared_dir or "an isolated scratch directory"


def _advanced_value(plan: NewSession) -> str:
    """What has been set under Advanced, or what lives there when nothing has.

    The two that hand out access say so first: a row reading as untouched while it holds new
    write grants or fewer denied reads would be the one lie this form may not tell.
    """
    profile = plan.profile
    writable = len(profile.extra_allow_write)
    set_here = [
        f"{writable} writable path" + ("" if writable == 1 else "s") if writable else "",
        "denied reads changed" if profile.deny_read != DEFAULT_DENY_READ else "",
        "no system PATH" if not profile.include_system_path else "",
        f"{len(profile.mcp)} MCP" if profile.mcp else "",
        plan.name,
        f"saved as {plan.save_as}" if plan.save_as else "",
        "keeps running" if plan.keep_alive else "",
    ]
    said = ", ".join(part for part in set_here if part)
    return said or "name, save as profile, keep running, MCP"


def _profile_line(answers: dict, base: Profile) -> str:
    title = form_title(answers, base)
    if not title.endswith("+ changes"):
        return f"{title}, unchanged"
    if answers.get("save_as"):
        return f"{title}, saving as {answers['save_as']}"
    return f"{title}. Press s to save these answers"


def _writable(profile: Profile, backend: str = SRT) -> str:
    """What the sandbox may change. A microVM and a policy sandbox mean different things.

    srt wraps a process on this machine, so the answer is a list of host paths. msb boots a
    guest with a filesystem of its own, so the answer is that guest, and the only host path
    in the sentence is the one directory mounted into it (SPEC §2.2).
    """
    if backend == MSB:
        shared = profile.shared_dir or "an isolated scratch directory"
        return f"everything in the guest, which goes when it does, and {shared}, mounted at /work"
    paths = ([profile.shared_dir] if profile.shared_dir else []) + profile.extra_allow_write
    if paths:
        return f"its own workdir, /tmp and /dev/null, plus {', '.join(paths)}"
    return "its own workdir, /tmp and /dev/null. No path of yours."


def _readable(profile: Profile, backend: str = SRT) -> str:
    """What the sandbox may read. On msb your disk is absent rather than denied (SPEC §4.1)."""
    if backend == MSB:
        return (
            "the guest's own filesystem. Your disk is not in there at all, "
            "so none of it has to be denied"
        )
    if not profile.deny_read:
        return "your disk, and nothing is denied"
    return f"your disk, except {' '.join(profile.deny_read)}"


def _reachable(profile: Profile, backend: str = SRT) -> str:
    """The count and every domain it names. The screen elides only when the popup makes it.

    The local grant is the one line that differs by backend, and by more than wording.
    srt's loopback rule takes no port, so the grant is every service listening here
    whatever the profile named. msb writes the port into the rule, so a profile that
    named one reaches that port and no other (SPEC §2.1, §2.2). Saying "whatever port"
    on an msb session would overstate what was granted, which is the one direction this
    screen must never be wrong in.
    """
    if profile.opens_every_domain():
        # No list to print, and no count that would mean anything: this is the whole grant.
        return ALL_GRANTED["network"]
    domains = profile.allowed_domains()
    if not domains:
        return "nothing, this sandbox is offline"
    line = f"{_counted(domains)}: {', '.join(domains)}"
    if not profile.opens_local_services():
        return line
    return f"{line}. Plus {_local_grant(profile, backend)}"


def _local_grant(profile: Profile, backend: str) -> str:
    """What ticking loopback actually opens on this backend, in the confirm's own voice."""
    domains = profile.allowed_domains()
    ports = sorted({port for port in map(loopback_port, domains) if port is not None})
    portless = any(names_loopback(domain) and loopback_port(domain) is None for domain in domains)
    if backend != MSB or portless or not ports:
        # Either the backend cannot scope the grant, or an entry named no port, which msb
        # writes as the whole machine. Both are wide, and the wide wording is the true one.
        return LOCAL_SERVICES_CONSEQUENCE
    named = [str(port) for port in ports]
    listed = " and ".join(named) if len(named) < 3 else f"{', '.join(named[:-1])} and {named[-1]}"
    counted = "port" if len(named) == 1 else "ports"
    return f"{counted} {listed} on this machine, and nothing else on it"


def _runnable(profile: Profile, registry: dict[str, AgentSpec], backend: str = SRT) -> str:
    """What the sandbox PATH holds: the ticked tools, and what the agent cannot start without.

    A required tool is not a ticked one. It is on the PATH because the agent was chosen, so
    it is named with the agent that asked for it rather than folded in among the ticks.

    An msb session has no shim dir at all: the guest holds what its image holds, so the
    ticked tools decide nothing there and saying they do would be a lie (SPEC §4.1).
    """
    if backend == MSB:
        spec = registry.get(profile.agent, AgentSpec())
        image = spec.image or "the guest"
        return f"whatever the {image} image ships, plus {profile.agent} itself"
    if EVERYTHING in profile.tools:
        return ALL_GRANTED["tools"]
    named = profile.tools + [
        f"{tool} (needed by {profile.agent})" for tool in required_tools(profile, registry)
    ]
    tools = " ".join(named) or "nothing by name"
    return f"{tools}, plus /usr/bin:/bin" if profile.include_system_path else tools


def required_tools(profile: Profile, registry: dict[str, AgentSpec]) -> list[str]:
    """What the chosen agent needs on the sandbox PATH beyond what the profile ticked."""
    spec = registry.get(profile.agent)
    needed = spec.required_tools if spec else []
    return [tool for tool in needed if tool not in profile.tools]


def required_note(plan: NewSession) -> str:
    """The same tools as a line for `--dry-run`, so no CLI launch adds one silently."""
    extra = required_tools(plan.profile, load_agents())
    return f"plus {', '.join(extra)}, needed by {plan.profile.agent}" if extra else ""


def _visible(profile: Profile, registry: dict[str, AgentSpec]) -> str:
    spec = registry.get(profile.agent)
    name = spec.name if spec and spec.name else profile.agent
    parts = [f"its own {name} login", "No other agent's keys"]
    if EVERYTHING in profile.skills:
        parts.append(f"Skills: {ALL_GRANTED['skills']}")
    else:
        parts.append(f"Skills: {', '.join(profile.skills)}" if profile.skills else "No skills")
    if profile.mcp:
        parts.append(f"MCP servers: {', '.join(profile.mcp)}")
    return ". ".join(parts) + "."


# --- what each field offers ------------------------------------------------


def open_choices(live: list[sessions.Session]) -> list[tuple[str, str]]:
    """The Open field: a new sandbox, a plain local tab, and every live session on one list."""
    return [("New sandbox", NEW), ("Local tab", LOCAL), *session_choices(live)]


def backend_choices(everything: bool = False) -> list[tuple[str, str, str]]:
    """Each backend, what it costs and gives, and why it cannot be chosen when it cannot.

    A backend this machine has no binary for stays on the list and says so, the way a tool
    the host lacks does: hiding it would leave the user wondering where the microVM went.

    `everything` is the network's allow-all row, which srt has no way to enforce (SPEC
    §2.1). Refusing it here is the other half of refusing the row on srt: whichever of the
    two is picked first, the second one says the two cannot go together, and no plan is
    built that a backend would only reject later.
    """
    absent = "msb is not installed, so this machine cannot run a microVM session"
    return [
        (SRT, BACKEND_HINTS[SRT], NO_ALLOW_ALL_ON_SRT if everything else ""),
        (MSB, BACKEND_HINTS[MSB], "" if shutil.which(MSB) else absent),
    ]


ADVANCED_ROWS = (
    ("Name", "name"),
    ("Save as profile", "save_as"),
    ("Keep running", "keep_alive"),
    ("MCP servers", "mcp"),
    ("Also writable", "extra_allow_write"),
    ("Never readable", "deny_read"),
    ("System PATH", "include_system_path"),
)


def advanced_choices(answers: dict, base: Profile) -> list[tuple[str, str, str]]:
    """What Advanced holds: a label, what it says now, and the answer it edits.

    Everything here is either something that should never be asked on the way to a sandbox, or
    something a profile carries that the form has no room to show.
    """
    return [
        (label, f"{advanced_value(step, answers, base)}. {EDITOR_HINTS[step]}", step)
        for label, step in ADVANCED_ROWS
    ]


def advanced_value(step: str, answers: dict, base: Profile) -> str:
    """What one Advanced row says as it stands."""
    if step in ADVANCED_FLAGS:
        standing = advanced_flag(step, answers, base)
        return next(label for value, label in ADVANCED_FLAGS[step] if value == standing)
    if step in ADVANCED_LISTS:
        return " ".join(advanced_list(step, answers, base)) or ADVANCED_LISTS[step]
    return str(answers.get(step, "")) or ("generated at launch" if step == "name" else "not saved")


def advanced_flag(step: str, answers: dict, base: Profile) -> bool:
    """A yes or a no: the answer if there is one, else the profile's, else no."""
    return bool(answers.get(step, getattr(base, step, False)))


def advanced_list(step: str, answers: dict, base: Profile) -> list[str]:
    """A list of words: the answer if there is one, else the profile's."""
    return list(answers.get(step, getattr(base, step, [])))


def open_rule(live: list[sessions.Session]) -> int:
    """Where the line under the two ways to start a tab goes, when there is anything under it."""
    return 1 if live else -1


def open_hint(value: str) -> str:
    """The line under the Open list. Anything that is not new or local is a live session."""
    return OPEN_HINTS.get(value, OPEN_HINTS["attach"])


def session_label(session: sessions.Session) -> str:
    """A session by what it is (backend, agent, permissions, size), not by its name (SPEC §3.1).

    The backend is there because attaching means a different thing on each one (SPEC §3.2).
    """
    panes = len(session.pane_ids)
    return (
        f"{session.name} [{session.backend}]: {session.agent} / {session.profile_name}, "
        f"{panes} tab{'' if panes == 1 else 's'}"
    )


def session_choices(live: list[sessions.Session]) -> list[tuple[str, str]]:
    return [(session_label(session), session.session_id) for session in live]


def profile_choices(saved: dict[str, Profile]) -> list[tuple[str, str]]:
    """Saved profiles, plus a start on paddock's own defaults, which is not a blank slate."""
    entries = [(f"{name} ({profile.agent})", name) for name, profile in sorted(saved.items())]
    return entries + [("Custom (built-in defaults)", CUSTOM)]


def profile_hint(key: str, saved: dict[str, Profile], registry: dict[str, AgentSpec]) -> str:
    """The line under a profile on the list: what it is, so the pick is not made on a name."""
    if key not in saved:
        return "Starts from paddock's own defaults, not from nothing."
    profile = saved[key]
    spec = registry.get(profile.agent)
    groups = profile.network_presets
    if not groups:
        network = "no network"
    elif len(groups) == 1:
        network = f"{groups[0]} only"
    else:
        network = f"{len(groups)} network groups"
    tools = f"{len(profile.tools)} tool" + ("" if len(profile.tools) == 1 else "s")
    parts = [spec.name if spec and spec.name else profile.agent, tools, network]
    if profile.shared_dir:
        parts.append(f"shares {profile.shared_dir}")
    return ", ".join(parts)


def agent_hint(key: str, registry: dict[str, AgentSpec]) -> str:
    """The line under an agent on the list, including the domains it opens whatever is ticked."""
    spec = registry.get(key)
    if spec is None:
        return "Type a command. paddock remembers it so a profile can name it later."
    domains = ", ".join(spec.api_domains) or "nothing of its own"
    hint = f"Runs {spec.command} in the sandbox. Reaches {domains} whatever you tick."
    if spec.required_tools:
        # Choosing the agent is what puts these on the PATH, so the list that chooses says so.
        hint += f" Cannot start without {', '.join(spec.required_tools)}, which it gets."
    return hint


def key_clash(key: str) -> str:
    """Asked only when a typed command wants a key another agent already answers to."""
    return f"{key} already runs something else. Call this one:"


def agent_title(key: str, registry: dict[str, AgentSpec]) -> str:
    """What an agent is called on screen: its name and command, or the key it is saved under."""
    spec = registry.get(key)
    return f"{spec.name} ({spec.command})" if spec else key


def agent_choices(registry: dict[str, AgentSpec], backend: str = SRT) -> list[tuple[str, str, str]]:
    """Registered agents, plus a command typed in by hand, and why one cannot be chosen.

    An agent this machine has no binary for stays on the list and says so, the way a backend
    without its binary does: hiding it would leave the user wondering where it went, and
    choosing it would open a tab that dies on `No such file or directory`.
    """
    entries = []
    for key in sorted(registry):
        why = agent_refusal(key, registry, backend)
        title = agent_title(key, registry)
        entries.append((f"{title} (not installed)" if why else title, key, why))
    return entries + [("Something else...", CUSTOM, "")]


def agent_refusal(key: str, registry: dict[str, AgentSpec], backend: str = SRT) -> str:
    """Why this agent cannot be chosen on this backend, or nothing when it can.

    What stops an agent is not the same on the two. srt runs the host's own binary, so an
    agent this machine has not got cannot run, and neither can one whose `required_tools`
    are missing: `codex` without `node` is a script with nothing to run it. A command
    written as a path is the user's own answer to where it lives, which is what the shell
    agent's `$SHELL` is, so it is left alone.

    msb runs whatever its image holds and installs the rest in the guest, so the host PATH
    says nothing at all there. What stops an agent on msb is having no image to boot, which
    is what the backend itself refuses on (SPEC §2.2).

    A command paddock cannot parse is treated as one it cannot run, and says so. Nothing
    here may raise: this is drawn for every agent on the list, before anything is chosen.
    """
    spec = registry.get(key)
    if spec is None or not spec.command:
        return ""
    try:
        words = shlex.split(spec.command)
    except ValueError as error:
        return f"{key} has a command paddock cannot read: {error}"
    if not words:
        return ""
    if backend == MSB:
        if spec.image or key == SHELL_AGENT:
            return ""
        return f"{key} has no image, so a microVM has nothing to run it in"
    if "/" not in words[0] and not shutil.which(words[0]):
        return f"{words[0]} is not installed, so this machine cannot run it"
    missing = [tool for tool in spec.required_tools if not shutil.which(tool)]
    if missing:
        return f"{key} needs {', '.join(missing)}, which this machine has not got"
    return ""


def tool_choices(base: Profile, selected: list[str] | None = None) -> list[tuple[str, str, bool]]:
    """Tools to offer, as title, name and whether it is ticked.

    Candidates the host does not have are left out: nobody needs a checklist of tools
    they never installed. The base profile's own tools stay on the list either way,
    marked when they are missing, so editing a profile on another machine cannot
    quietly drop what that machine cannot see. `selected` is the answer already given,
    which is what a question asked a second time offers back.
    """
    ticked = base.tools if selected is None else selected
    rows = [(ALL_ROWS["tools"], EVERYTHING, EVERYTHING in ticked)]
    for name in dict.fromkeys(TOOL_CANDIDATES + base.tools + list(ticked)):
        if name == EVERYTHING:
            continue  # already the first row, and it is not a binary to look for
        if shutil.which(name):
            rows.append((name, name, name in ticked))
        elif name in base.tools or name in ticked:
            rows.append((f"{name} (not installed)", name, name in ticked))
    return rows


def network_choices(
    base: Profile, selected: list[str] | None = None
) -> list[tuple[str, str, bool]]:
    """The domain groups, with the one that is not a group at the top of the list."""
    ticked = base.network_presets if selected is None else selected
    groups = [name for name in NETWORK_PRESETS if name != NETWORK_ALL]
    return [
        (ALL_ROWS[NETWORK_ALL], NETWORK_ALL, NETWORK_ALL in ticked),
        *((name, name, name in ticked) for name in groups),
    ]


def skill_choices(agent: AgentSpec, selected: list[str]) -> list[tuple[str, str, bool]]:
    """Skills under the agent's own config dirs, plus any already chosen, which stay ticked.

    An agent with no skills directory offers none, and the question is skipped. The
    allow-all row is only there when there is something for it to mean.
    """
    names: list[str] = []
    for path in agent.config_write_paths:
        directory = Path(path).expanduser() / "skills"
        if directory.is_dir():
            names += sorted(entry.name for entry in directory.iterdir() if entry.is_dir())
    found = [name for name in dict.fromkeys(names + list(selected)) if name != EVERYTHING]
    if not found and EVERYTHING not in selected:
        return []
    return [
        (ALL_ROWS["skills"], EVERYTHING, EVERYTHING in selected),
        *((name, name, name in selected) for name in found),
    ]


def parse_domains(text: str) -> list[str]:
    """Typed-in domains: commas or spaces, blanks dropped, no repeats."""
    return list(dict.fromkeys(text.replace(",", " ").split()))


def parse_paths(text: str) -> list[str]:
    """Typed-in paths: whitespace only, because a comma can be part of a path."""
    return list(dict.fromkeys(text.split()))


def resolve_shared_dir(answer: str, cwd: Path) -> str:
    """A typed directory as an absolute path. Blank means share nothing, not share here."""
    answer = answer.strip()
    if not answer:
        return ""
    # An absolute or ~ answer wins the join, so relative answers mean "next to the popup".
    return str((cwd / Path(answer).expanduser()).resolve())


def suggested_key(command: str, registry: dict[str, AgentSpec]) -> str:
    """A registry key for a typed-in command that does not stand on a registered one."""
    words = command.split()
    first = Path(words[0]).name if words else ""
    return f"{first}-custom" if first in registry else first


# --- the answers -----------------------------------------------------------


def base_profile(saved: dict[str, Profile], answers: dict) -> Profile:
    """The profile the answers start from. The blank start is a default Profile."""
    key = answers.get("profile", CUSTOM)
    return Profile() if key == CUSTOM else saved[str(key)]


def chosen_agent(answers: dict, base: Profile) -> str:
    """The agent the answers name: a registry key, or the key a typed-in command is saved under."""
    agent = answers.get("agent", base.agent)
    if agent == CUSTOM:
        return str(answers.get("remember_as", ""))
    return str(agent)


def build_session(base: Profile, answers: dict) -> NewSession:
    """The answers as a plan. A step that was never asked keeps the base profile's value."""
    agent = chosen_agent(answers, base)
    carried = base.skills if agent == base.agent else []
    profile = build_profile(
        base,
        agent,
        list(answers.get("tools", base.tools)),
        list(answers.get("network", base.network_presets)),
        parse_domains(str(answers.get("domains", " ".join(base.extra_domains)))),
        list(answers.get("skills", carried)),
        _shared_dir(base, answers),
        advanced=advanced_fields(base, answers),
    )
    return NewSession(
        profile=profile,
        name=str(answers.get("name", "")),
        save_as=str(answers.get("save_as", "")),
        agent_command=str(answers.get("command", "")),
        backend=str(answers.get("backend", SRT)),
        keep_alive=advanced_flag("keep_alive", answers, base),
        started_from=str(answers.get("profile", CUSTOM)),
    )


def answers_from(plan: Plan, saved: dict[str, Profile]) -> dict:
    """A plan as the answers that made it, so a failed launch comes back to its own form.

    A local or an attached tab permits nothing, so the one answer it has is all it gives
    back. `started_from` is the profile key the answers stood on, so the form reopens on
    that profile and not on a guess made from the built profile's name.
    """
    if isinstance(plan, Local):
        return {"open": LOCAL}
    if isinstance(plan, Attach):
        return {"open": plan.ref, "shell": plan.shell}
    profile = plan.profile
    started_from = plan.started_from or CUSTOM
    return {
        "open": NEW,
        "profile": started_from if started_from in saved else CUSTOM,
        "backend": plan.backend,
        "agent": profile.agent,
        "command": plan.agent_command,
        "tools": list(profile.tools),
        "network": list(profile.network_presets),
        "domains": " ".join(profile.extra_domains),
        "skills": list(profile.skills),
        "share": bool(profile.shared_dir),
        "directory": profile.shared_dir,
        "name": plan.name,
        "save_as": plan.save_as,
        "keep_alive": plan.keep_alive,
        "mcp": list(profile.mcp),
        "extra_allow_write": list(profile.extra_allow_write),
        "deny_read": list(profile.deny_read),
        "include_system_path": profile.include_system_path,
    }


def advanced_fields(base: Profile, answers: dict) -> dict:
    """The profile fields Advanced asks about, as they now stand."""
    return {
        "mcp": advanced_list("mcp", answers, base),
        "extra_allow_write": advanced_list("extra_allow_write", answers, base),
        "deny_read": advanced_list("deny_read", answers, base),
        "include_system_path": advanced_flag("include_system_path", answers, base),
    }


def _shared_dir(base: Profile, answers: dict) -> str:
    """The Files answer: nothing when nothing is shared, else the typed or the profile's path."""
    if not shares_a_directory(answers, base):
        return ""
    return str(answers.get("directory", base.shared_dir))


def build_profile(
    base: Profile,
    agent: str,
    tools: list[str],
    presets: list[str],
    extra_domains: list[str],
    skills: list[str],
    shared_dir: str,
    advanced: dict | None = None,
) -> Profile:
    """The answers as a Profile. Fields the chooser never asks about keep the base's values.

    Changed answers get a changed name: a session that says it runs `claude-default` has
    to be the permissions that profile describes. The blank start is already custom.
    """
    built = replace(
        base,
        agent=agent,
        tools=list(tools),
        network_presets=list(presets),
        extra_domains=list(extra_domains),
        skills=list(skills),
        shared_dir=shared_dir,
        **(advanced or {}),
    )
    if built == base or base.name == Profile().name:
        return built
    return replace(built, name=f"{base.name}+custom")


def save_answers(profile: Profile, name: str) -> tuple[Profile, str]:
    """Save the answers under `name`. Returns the profile to launch and a line for the user.

    A name the profile rules refuse costs the save, never the sandbox just described.
    """
    renamed = replace(profile, name=name)
    try:
        path = save_profile(renamed)
    except ValueError as error:
        return profile, f"paddock: profile not saved: {error}"
    return renamed, f"paddock: saved {path}"


def remember_agent(key: str, command: str) -> Path | None:
    """Write a typed-in command to the agent registry: a profile names a key, not a command.

    None means the key already runs that command and nothing was written. A key that runs
    something else is refused: a user file replaces a registry entry whole, so overwriting
    one would drop its domains and credential paths for every profile that names it.
    """
    if not key or "/" in key or key.startswith("."):
        raise ValueError(f"agent key must be a plain filename, got {key!r}")
    known = load_agents().get(key)
    if known is not None:
        if known.command == command:
            return None
        raise ValueError(f"agent {key!r} already runs {known.command!r}, so choose another name")
    agent_dir().mkdir(parents=True, exist_ok=True)
    path = agent_dir() / f"{key}.json"
    path.write_text(json.dumps({"name": key, "command": command}, indent=2) + "\n")
    return path
