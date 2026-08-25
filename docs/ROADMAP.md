# paddock roadmap

Where paddock goes after v1. These are decided directions, not promises with
dates. The design behind most of them is in [SPEC.md](SPEC.md); nothing here is
stubbed in the code until it is built.

## Stronger isolation

- **A prebuilt agent image.** The `msb` backend runs agents in the guest today
  ([SPEC §2.2](SPEC.md#22-microsandbox-msb-registered-as-msb)), installing the
  agent on every session because a new sandbox is a fresh clone of the image. The
  reason to stop doing that is the network: the install needs the package
  registry, `msb` fixes network rules at create and offers no way to withdraw
  one, so a session that installed its agent can reach npm for as long as it
  lives. An image with the agent already in it drops that allowance entirely, and
  saves about 21s per session as well. The cost is building and publishing one
  image per agent, and keeping the pinned versions in it current.
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
  per sandbox, so a dev server inside one is reachable from the host browser by
  name. This is paddock's to build on top of a forwarded port; `msb` has no such
  URL of its own (see the [microVM spike](spikes/microvm.md)).

## Ecosystem

- **herdr plugin packaging.** A `herdr-plugin.toml` manifest, so paddock installs
  with `herdr plugin install paddock-sh/paddock` and can be listed in the
  marketplace, instead of the keybinding `paddock init` writes.
- **Layer-2 permissions for Claude Code.** Generate the `permissions.allow` and
  `permissions.deny` block that goes with `--settings`, so the agent's own
  prompts match the sandbox instead of fighting it.
- **Releases and update notices.** Publish to PyPI, check for a newer version
  once a day, and `paddock update` to take it.
- **Standalone mode.** `paddock run` without herdr, behind a terminal-adapter
  seam, so the sandboxing is useful before the multiplexer is.
- **Cleanup.** Prune run directories that no session uses, and the config backups
  `paddock init` leaves behind.
