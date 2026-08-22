"""The `paddock` command: the popup chooser by default, and the same jobs without questions."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from paddock import init, sessions, tui
from paddock.profiles import Profile, load_profiles
from paddock.sessions import DEFAULT_BACKEND


@dataclass
class Command:
    """What argv asked for."""

    name: str
    profile: str = ""
    ref: str = ""
    cwd: str = ""
    backend: str = DEFAULT_BACKEND
    dry_run: bool = False
    undo: bool = False


def main(argv: list[str] | None = None) -> int:
    command = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return run(command)
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, ValueError) as error:
        # HerdrError and SrtNotFound are RuntimeErrors. The popup closes with the process,
        # so a traceback is never read by anyone: say what went wrong instead.
        return _fail(str(error))


def parse_args(argv: list[str]) -> Command:
    """argv to a Command. No subcommand means the chooser, which is how the popup runs it."""
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["choose", *argv]
    args = _parser().parse_args(argv)
    return Command(
        name=args.command,
        profile=getattr(args, "profile", ""),
        ref=getattr(args, "ref", ""),
        cwd=getattr(args, "cwd", ""),
        backend=getattr(args, "backend", "") or DEFAULT_BACKEND,
        dry_run=getattr(args, "dry_run", False),
        undo=getattr(args, "undo", False),
    )


def run(command: Command) -> int:
    """Work out the plan, then print it or carry it out."""
    if command.name == "profiles":
        for line in profile_lines(load_profiles()):
            print(line)
        return 0
    if command.name == "init":
        return init.run(dry_run=command.dry_run, undo=command.undo)

    cwd = Path(command.cwd) if command.cwd else Path.cwd()
    if command.name == "choose":
        plan = tui.choose(cwd)
        if plan is None:  # backed out: nothing chosen, nothing done
            return 0
    elif command.name == "attach":
        # Only an asked-for cwd, so an attached tab otherwise keeps the session's workdir.
        plan = tui.Attach(ref=command.ref, cwd=command.cwd)
    else:  # launch
        saved = load_profiles()
        if command.profile not in saved:
            return _fail(f"no profile named {command.profile!r}")
        profile = saved[command.profile]
        if command.cwd:
            profile = replace(profile, shared_dir=str(cwd))
        plan = tui.NewSession(profile=profile, backend=command.backend)

    if command.dry_run:
        print(describe(plan))
        return 0
    return perform(plan)


def perform(plan: tui.Plan) -> int:
    """Do it. Every call into sessions goes through here."""
    if isinstance(plan, tui.Local):
        print(sessions.launch_local(Path(plan.cwd)))
        return 0
    if isinstance(plan, tui.Attach):
        session = sessions.get_session(plan.ref)
        if session is None:
            return _fail(f"no session named {plan.ref!r}")
        print(sessions.attach(session, Path(plan.cwd) if plan.cwd else None))
        return 0

    profile = plan.profile
    if plan.agent_command:
        try:
            path = tui.remember_agent(profile.agent, plan.agent_command)
        except ValueError as error:
            return _fail(str(error))
        if path is not None:
            print(f"paddock: remembered agent in {path}", file=sys.stderr)
    if plan.save_as:
        profile, message = tui.save_answers(profile, plan.save_as)
        print(message, file=sys.stderr)
    _, pane_id = sessions.launch(profile, plan.name or None, plan.backend)
    print(pane_id)
    return 0


def describe(plan: tui.Plan) -> str:
    """What a plan would do, for --dry-run."""
    if isinstance(plan, tui.Local):
        return f"would open a local tab in {plan.cwd}"
    if isinstance(plan, tui.Attach):
        where = plan.cwd or "its own workdir"
        return f"would attach a tab to session {plan.ref!r} in {where}"
    parts = [
        f"would launch session {plan.name or '(generated name)'}",
        f"profile {plan.profile.name}",
        f"agent {plan.profile.agent}",
        plan.profile.shared_dir or "isolated workdir",
    ]
    if plan.backend != DEFAULT_BACKEND:
        parts.append(f"on the {plan.backend} backend")
    if plan.agent_command:
        parts.append(f"remembering the command {plan.agent_command!r}")
    if plan.save_as:
        parts.append(f"saving the profile as {plan.save_as}")
    return ", ".join(parts)


def profile_lines(saved: dict[str, Profile]) -> list[str]:
    """One line per profile: what it runs, where it works, and what it can reach."""
    lines = []
    for name, profile in sorted(saved.items()):
        network = ", ".join(profile.network_presets) or "no network"
        lines.append(
            f"{name:<16} {profile.agent:<10} "
            f"{profile.shared_dir or 'isolated workdir':<20} {network}"
        )
    return lines


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paddock", description=__doc__)
    dry = argparse.ArgumentParser(add_help=False)
    dry.add_argument("--dry-run", action="store_true", help="print what would happen, do nothing")
    where = argparse.ArgumentParser(add_help=False)
    where.add_argument("--cwd", default="", help="use this directory, not the current one")

    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser(
        "choose", parents=[dry, where], help="ask what to open (the default)"
    )
    launch = subcommands.add_parser(
        "launch", parents=[dry], help="start a session from a saved profile, no questions"
    )
    launch.add_argument("profile", help="profile name, as listed by `paddock profiles`")
    launch.add_argument(
        "--cwd",
        default="",
        help="share this host directory with the sandbox, read-write "
        "(overrides the profile's shared_dir)",
    )
    launch.add_argument(
        "--backend",
        default=DEFAULT_BACKEND,
        help=f"which sandbox runs it: srt, or msb for a microVM (default: {DEFAULT_BACKEND})",
    )
    attach = subcommands.add_parser(
        "attach", parents=[dry, where], help="put a new tab on a running session"
    )
    attach.add_argument("ref", metavar="session", help="session id or name")
    subcommands.add_parser("profiles", help="list saved profiles")
    setup = subcommands.add_parser(
        "init", parents=[dry], help="bind the chooser to prefix+c in herdr's config"
    )
    setup.add_argument(
        "--undo", action="store_true", help="put the newest backed-up herdr config back"
    )
    return parser


def _fail(message: str) -> int:
    print(f"paddock: {message}", file=sys.stderr)
    return 1
