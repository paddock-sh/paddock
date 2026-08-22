# paddock roadmap

Where paddock goes after v1. These are decided directions, not promises with
dates. The design behind most of them is in [SPEC.md](SPEC.md); nothing here is
stubbed in the code until it is built.

## Stronger isolation

- **Agents inside the microVM.** The `msb` backend runs shell sessions today
  ([SPEC §2.2](SPEC.md#22-microsandbox-msb-registered-as-msb)): its own libkrun
  microVM per session, an OCI image, and a volume mount where a session shares a
  host directory. What is left is provisioning an agent in the guest, which needs
  an image per agent and the config dir mounted in.
- **Workspace-scoped sessions.** One persistent microVM per workspace, with every
  tab exec'ing into the same guest, so tabs in a group share processes and not
  just files.
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
  with `herdr plugin install desquaredp/paddock` and can be listed in the
  marketplace, instead of the keybinding `paddock init` writes.
- **Layer-2 permissions for Claude Code.** Generate the `permissions.allow` and
  `permissions.deny` block that goes with `--settings`, so the agent's own
  prompts match the sandbox instead of fighting it.
- **Cleanup.** Prune run directories that no session uses, and the config backups
  `paddock init` leaves behind.
