# paddock roadmap

Where paddock goes after v1. These are decided directions, not promises with
dates. The design behind most of them is in [SPEC.md](SPEC.md); nothing here is
stubbed in the code until it is built.

## Stronger isolation

- **A `microsandbox` backend.** Each session runs in its own libkrun microVM
  instead of a filtered view of the host: hardware isolation, an OCI image per
  agent, and volume mounts where a session shares a host directory.
- **Workspace-scoped sessions.** One persistent microVM per workspace, with every
  tab exec'ing into the same guest, so tabs in a group share processes and not
  just files.
- **Patch-gated writes.** An optional alternative to sharing a directory: the
  sandbox gets a worktree copy to work in, and what comes back out is a git patch
  to read before it is applied. Nothing of yours is writable while it runs.
- **Pre-grant secret scan.** Warn before sharing a directory that holds keys or
  tokens, at the moment the grant is being made rather than after it.
- **Hard per-binary blocking.** An opt-in strict mode that puts unselected tools
  in `denyRead`, which the kernel enforces, instead of relying on the `PATH` shim
  dir that an absolute path can walk around.

## Network identity

- **Per-session egress: none, host, or `vpn:<name>`.** With the microVM backend
  each session gets its own interface and address, so one session can leave
  through its own VPN (WireGuard inside the guest, say) while sibling tabs use
  the host network. With `srt`, traffic already funnels through its local proxy;
  chaining that to an upstream proxy is the open question.
- **portless integration.** A collision-free `https://<session>.localhost` URL
  per sandbox, so a dev server inside one is reachable from the host browser with
  no port forwarding.

## Ecosystem

- **herdr plugin packaging.** A `herdr-plugin.toml` manifest, so paddock installs
  with `herdr plugin install desquaredp/paddock` and can be listed in the
  marketplace, instead of the keybinding `paddock init` writes.
- **A `backend` field on a session,** shown in the attach list, so it is clear
  what attaching to that session will mean before you do it.
- **Layer-2 permissions for Claude Code.** Generate the `permissions.allow` and
  `permissions.deny` block that goes with `--settings`, so the agent's own
  prompts match the sandbox instead of fighting it.
- **Releases and update notices.** Publish to PyPI, check for a newer version
  once a day, and `paddock update` to take it.
- **Standalone mode.** `paddock run` without herdr, behind a terminal-adapter
  seam, so the sandboxing is useful before the multiplexer is.
- **Cleanup.** Prune run directories that no session uses, and the config backups
  `paddock init` leaves behind.
