# Spike: the microsandbox (`msb`) backend

Feasibility spike for the v1.1 `microsandbox` backend in
[SPEC.md §2.2](../SPEC.md#22-microsandbox-msb-registered-as-msb).
Everything below was measured on this machine. No number is estimated.

**Verdict: GO WITH CAVEATS.** `msb` does what §2.2 needs, and does it better than
the SPEC assumes on speed and on the server question. Two SPEC claims are wrong
and one design gap is open. See [Verdict](#verdict).

Nothing in `paddock/` was touched. This is docs and probes only.

## Environment

| | |
| --- | --- |
| Host | Apple Silicon (arm64, T6050), macOS 26.5, build 25F71 |
| `msb` | 0.6.13, released the day of the spike |
| Install | `~/.microsandbox` plus symlinks in `~/.local/bin`, no sudo |
| Images | `alpine` (4.0 MiB), `node:22-slim` (76.2 MiB) |

Times are wall clock from `/usr/bin/time -p` or a Python clock around the call.

---

## 1. Does `msb` run here, and how fast does a VM boot?

**Yes, with no privileged step, and there is no server.**

```
$ msb --version
msb 0.6.13

$ msb doctor
info Platform: macOS aarch64
info MSB_HOME: /Users/desquaredp/.microsandbox
   ✓ msb          /Users/desquaredp/.local/bin/msb
   ✓ libkrunfw    /Users/desquaredp/.microsandbox/lib/libkrunfw.5.dylib
   ✓ Root clone   reflink supported
   ✓ Architecture Apple silicon (arm64)
done Host setup is ready.
```

No sudo, no `/etc`, no kernel extension, no launch agent. `msb` reaches the
hypervisor because the shipped binary carries the entitlement:

```
$ codesign -d --entitlements - ~/.microsandbox/bin/msb
	[Key] com.apple.security.hypervisor
	[Value]
		[Bool] true
```

**There is no server component.** With no sandbox running there is no `msb`
process at all:

```
$ msb ls
No sandboxes found.
$ ps -eo pid,rss,comm | grep .local/bin/msb
(none: no daemon, no server)
```

Each running sandbox is one `msb` host process. Start the sandbox, get a
process; remove it, the process goes.

### Boot times

Full boot, run a command, tear down (`msb run alpine -- /bin/echo hello`):

```
COLD (first run after the image pull)
real 0.68

WARM (5 further identical runs)
real 0.20   real 0.19   real 0.20   real 0.18   real 0.20
```

Boot to a usable persistent VM, which is what a session actually costs
(`msb create` then the first `msb exec`):

```
create=156ms  first_exec=21ms  total_to_usable=178ms
create=154ms  first_exec=21ms  total_to_usable=176ms
create=153ms  first_exec=21ms  total_to_usable=174ms
create=170ms  first_exec=22ms  total_to_usable=192ms
```

A `node:22-slim` VM with 2G RAM and an 8G root disk boots just as fast:
`boot_to_usable=180ms`.

The image pull is the only slow part, and it happens once per image:
`alpine` 2.8s, `node:22-slim` 20.1s.

## 2. Exec a second shell into a running VM

**Yes. 20ms, flat.** This is the attach primitive.

```
$ for i in $(seq 8); do ...; done
exec=21ms  exec=20ms  exec=20ms  exec=20ms
exec=20ms  exec=20ms  exec=20ms  exec=20ms
```

Execs share one process namespace, which is the property SPEC §3.2 promises and
srt cannot give. Exec A starts a background process, exec B sees it:

```
-- exec A --
started pid 216
-- exec B (a separate exec) --
1            # count of matching "sleep 300" processes
PID   COMMAND
    1 /init.krun
```

A PTY attach works, which is what a herdr pane needs:

```
$ script -q /dev/null msb exec cfgvm --tty -- /bin/sh -c 'echo "tty=$(tty)"; ...'
tty=/dev/pts/0
TERM=xterm-256color
uid=0(root) gid=0(root) groups=0(root)
cfgvm
```

`msb exec <name>` with no command attaches to the default shell. `msb ssh` is a
second route.

## 3. Volume mounts

**Read-write works, read-only is enforced, and nothing else on the host is
visible.**

```
$ msb create --name mountvm --replace \
    --mount-dir $HOME/msbspike/shared:/work \
    --mount-dir $HOME/msbspike/readonly:/ro:ro \
    alpine

$ msb exec mountvm -- mount | grep -E "/work|/ro "
ro_f00f5414 on /ro type virtiofs (ro,relatime)
work_0c9a453f on /work type virtiofs (rw,relatime)
```

Guest writes land on the host:

```
[guest] echo "guest-wrote-this" > /work/from_guest.txt
guest write OK
[host]  cat ~/msbspike/shared/from_guest.txt
guest-wrote-this
```

The read-only mount reads and refuses writes:

```
read: ro-content
write refused (expected)
/bin/sh: can't create /ro/should_fail.txt: Read-only file system
```

Unmounted host paths do not exist in the guest. Not denied, absent:

```
ls: /Users/desquaredp/msbspike/secret: No such file or directory
ls: /Users/desquaredp: No such file or directory
ls: /Users: No such file or directory
```

**Gotcha: mount sources are not symlink-resolved.** A source under `/tmp` fails,
because `/tmp` is a symlink to `private/tmp` on macOS:

```
$ msb create --name mountvm --mount-dir /tmp/msbspike/shared:/work alpine
error: failed to start "mountvm"
  → mount: mount work_0c9a453f: Not a directory (os error 20)

$ msb create --name tmpvm --mount-dir /private/tmp/msbspike/shared:/work alpine
$ msb exec tmpvm -- cat /work/from_host.txt
host-wrote-this
```

The backend must pass a resolved path. `Path.resolve()` on every mount source.

## 4. Network policy

**Per-sandbox allow and deny on hosts, ports and protocols. It works.**

Each sandbox gets its own `/30` subnet and its own gateway:

```
nameserver 172.16.0.81
    inet 172.16.0.82/30 scope global eth0
default via 172.16.0.81 dev eth0
```

Deny by default, allow one host:

```
$ msb run --net-default deny \
    --net-rule "allow@example.com:tcp:443" --net-rule "allow@dns" alpine -- ...

[ALLOWED example.com]
<!doctype html><html lang="en"><head><title>Example Domain</title>...
[BLOCKED github.com]
wget: can't connect to remote host (172.182.252.133): Connection refused
[BLOCKED api.anthropic.com]
wget: can't connect to remote host (160.79.104.10): Connection refused
```

The rule grammar is `<action>[:<direction>]@<target>[:<proto>[:<ports>]]`, where
a target is an IP, a CIDR, a domain, a `*.example.com` suffix, or a group
(`public`, `private`, ...). This maps onto `network_presets` directly: one
`allow@<domain>:tcp:443` per allowed domain, plus `allow@dns`.

### Can the guest reach host services?

**Not by default, and that is the correct default. It needs an explicit rule.**

With a host server on `*:18080`, a default-network guest reaches nothing:

```
[guest 127.0.0.1:18080]          Connection refused   # the guest's own loopback
[gateway 172.16.0.81:18080]      Connection refused
[host LAN 100.110.158.155:18080] Connection refused
```

`--net host` alone does not change that. An explicit rule does:

```
$ msb run --net-rule "allow@100.110.158.155" alpine -- wget ...
HOST-SERVICE-REACHED
$ msb run --net private alpine -- wget ...
HOST-SERVICE-REACHED
```

Two limits worth knowing. There is no host alias to depend on:

```
host.docker.internal       NXDOMAIN
host.msb.internal          NXDOMAIN
host.internal              NXDOMAIN
gateway.internal           NXDOMAIN
```

And the gateway is a router, not a proxy to host services, so the host has to be
named by its real address. `--vsock HOST_PATH:PORT` is the stable host-IPC route
and is the better answer for talking to a host daemon. It was not exercised in
this spike.

## 5. Port forward guest to host

**Works.** `-p HOST:GUEST` binds the host port and forwards it in:

```
$ msb create --name portvm -p 18099:8000 --mount-dir $HOME/msbspike/shared:/srv alpine
[guest] a server listening on :::8000
[host]  $ curl -s http://127.0.0.1:18099/
GUEST-SERVER-REACHED
$ lsof -nP -iTCP:18099 -sTCP:LISTEN
msb  89955  desquaredp  32u  IPv4  TCP 127.0.0.1:18099 (LISTEN)
```

msb binds loopback only, which is the right default. This is the substitute for
the portless URL that §2.2 assumes and that does not exist (question 8).

## 6. Resource footprint

**Server: zero. Idle VM: about 110MB at boot, falling to about 65MB. A VM that
has done real work keeps what it used.**

Host RSS, one idle alpine VM with the default 1 CPU and 512 MiB:

```
pid=90760 RSS=109.6MB   # at boot
  CPUs:         1
  Memory:       512 MiB
[guest] free -m -> total 480, used 16, free 460
```

A second VM costs about the same, so there is no shared overhead to amortise:

```
pid=90760 RSS=109.8MB
pid=90777 RSS=106.1MB
TOTAL=215.9MB for 2 idle VMs
```

Guest memory is faulted in lazily, so RSS tracks real use rather than the
allocation. It does not come back down. After `npm install -g` inside two
`node:22-slim` VMs, with the alpine VMs now settled:

```
  pid=90760 RSS=74.3MB      # idle alpine
  pid=90777 RSS=63.9MB      # idle alpine
  pid=91509 RSS=787.2MB     # node VM, claude installed
  pid=92171 RSS=1330.8MB    # node VM, claude installed
  4 VMs, TOTAL RSS=2256.1MB
```

**This is the binding constraint, not boot time.** An agent VM costs roughly
0.8GB to 1.3GB resident, so concurrent sessions are limited by RAM.

Disk:

| Item | Size |
| --- | --- |
| Runtime, fixed (`bin` + `lib`) | 52 MB |
| `alpine` image in the layer cache | 4.0 MiB |
| `node:22-slim` image in the layer cache | 76.2 MiB |
| Layer cache total after both images | 259 MB |
| One alpine sandbox | 3.8 MB |
| One `node:22-slim` sandbox with `claude` installed | 431 MB |

Per-sandbox disk is cheap for a small image because the root clone uses reflink
(`msb doctor` reports `Root clone reflink supported`). After removing every
sandbox the `sandboxes` directory returns to `0B`.

## 7. Can `claude` install and run in a stock image?

**Yes, and the config-dir seam that layer 3 depends on works over a mount.**

```
$ msb create --name agentvm -m 2G --root-disk 8G node:22-slim
boot_to_usable=180ms
node: v22.23.2
npm: 10.9.8
os: PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"

$ msb exec agentvm -- npm install -g @anthropic-ai/claude-code
added 2 packages in 21s

$ msb exec agentvm -- claude --version
2.1.239 (Claude Code)
```

With no credentials it behaves exactly as SPEC §4.3 records for the host:

```
$ msb exec agentvm -- claude -p "say hi"
Not logged in · Please run /login
```

The important result is the next one. A host directory holding a **fake**
credentials file was mounted at `/cfg`, and `CLAUDE_CONFIG_DIR` pointed at it:

```
$ msb create --name cfgvm --mount-dir $HOME/msbspike/fakeconfig:/cfg node:22-slim

[A] no CLAUDE_CONFIG_DIR
Not logged in · Please run /login

[B] CLAUDE_CONFIG_DIR=/cfg
Failed to authenticate: OAuth session expired and could not be refreshed
```

The message changed, so `claude` read `.credentials.json` out of the mounted
host directory. **Layer 3 ports to `msb` as one volume mount plus one
environment variable**, with no change to how the config dir is built.

No real credential was read, copied or mounted at any point in this spike. The
token in `/cfg/.credentials.json` was the literal string `FAKE-NOT-A-REAL-TOKEN`.

What auth mounting would require in practice: mount `<run dir>/config` read-write
(Claude Code rewrites `.claude.json`), pass `CLAUDE_CONFIG_DIR` to the guest with
`-e`, and keep the mount source a resolved path (question 3).

## 8. Where this contradicts the SPEC

Three of the four assumptions hold. Two other claims in §2.2 do not.

| SPEC assumption | Result |
| --- | --- |
| OCI images | **Holds.** Pulls `alpine` and `node:22-slim` straight from Docker Hub. |
| Sub-second boots | **Holds, with room to spare.** 175ms to a usable VM, not merely under a second. |
| An `msb` server is needed | **Wrong.** There is no server and no daemon. Zero sandboxes means zero processes. |
| Per-VM isolation | **Holds.** Own kernel, own `/30` subnet, host filesystem absent unless mounted. |

Two more:

**§2.2's `<name>.localhost` URL does not exist.** The SPEC says msb "gives each
sandbox a `<name>.localhost` URL, so a dev server inside a sandbox is reachable
from the host browser with no port forwarding", and ROADMAP repeats it under
portless integration. Nothing in `msb --tree` (466 lines) offers it, `msb inspect`
exposes no URL, and a code search of the microsandbox repository for `portless`
and for `.localhost` returns zero hits. Port forwarding (question 5) works and is
what that feature would have to be built on. **The `image` field in the agent
registry is still justified; the URL sentence in §2.2 is not.**

**§4.3's "copied for microsandbox, which cannot follow them" is too strong.** A
relative symlink whose target is inside the mount resolves fine in the guest:

```
[link_to_inside.md]  -> ../real_inside.md
SKILL-BODY-INSIDE-MOUNT
```

What fails is an absolute symlink to a host path outside the mount, which fails
cleanly and correctly, because that path genuinely is not in the guest:

```
[link_to_outside.md] -> /Users/desquaredp/msbspike/skill_target_outside.md
cat: /cfg/skills/link_to_outside.md: No such file or directory
```

So the rule is not "msb cannot follow symlinks". It is **a symlink works when its
target is inside the same mount**. Copying skills is still the simplest correct
choice, because host skill paths are outside the run dir, but the reason in the
SPEC should be fixed.

**One open design gap, not a contradiction.** SPEC §3 describes an unsandboxed
tab orchestrating sandboxed ones "through the herdr CLI", and `herdr_client`
shells out to `herdr` on the host. An agent inside a microVM cannot reach a host
service by default (question 4), and there is no host alias to aim at. Talking to
herdr from inside a guest needs a deliberate mechanism, `--vsock` or an explicit
allow rule against a host-bound listener. It should be decided before the backend
is built, not during.

---

## Proposed SPEC addition

Not applied to SPEC.md. This is the contract sketch for review.

### Session record

§3.4 already reserves the fields. `backend` is `"srt"` or `"microsandbox"`.
`vm_handle` is the `msb` sandbox name, which is also what every `msb` subcommand
takes, so no other handle is needed.

The name must be unique among live sandboxes on the host, not just among paddock
sessions, since `msb create` fails on a name collision. Prefix it, for example
`paddock-<session_id>`.

### Lifecycle

| Session operation | `msb` |
| --- | --- |
| `create_session` | `msb create --name <vm_handle> [mounts] [net rules] [-e ...] <image>` |
| attach a tab | `msb exec <vm_handle> --tty` |
| `remove_pane` | Nothing. The VM is not tied to any pane. |
| collect the session | `msb rm -f <vm_handle>` |

`prepare` and `open_pane` split cleanly along that line: `prepare` boots the VM
and returns the handle, `open_pane` runs one `msb exec`. Both are one short
command, which §1.3 requires because the command is typed into a pane's shell.

`msb rm` without `-f` is a silent no-op on a running sandbox, so collection must
always pass `-f`.

`msb ls --format json` gives the live sandbox list machine-readably, so
`sessions.json` can be reconciled against what is actually running rather than
trusted on its own. Useful after a crash, where the registry may name a VM that
no longer exists.

VMs outlive the shell that made them, which is what §3.4's "sessions survive
Herdr detach and restart" needs. Every VM in this spike was created in one shell
and used from later ones. `--idle-timeout` and `--max-duration` exist if
`keep_alive` should ever have a backstop.

### Profile mapping

| Profile field | `microsandbox` |
| --- | --- |
| `agent` | the agent's `image` (§5), already in the registry |
| `tools` | baked into the image |
| `shared_dir` | `--mount-dir <resolved host path>:/work` |
| isolated workdir | the VM's own filesystem, no host dir needed |
| `network_presets` | `--net-default deny`, then `--net-rule allow@<domain>:tcp:443` per domain, plus `--net-rule allow@dns` |
| `skills` | copied into the run dir, then mounted with the config dir |

### Synthesized config dir in the guest

Layer 3 (§4.3) needs no new mechanism. The run dir's `config/` is mounted and the
variable points at it:

```
--mount-dir <run_dir>/config:/cfg  -e CLAUDE_CONFIG_DIR=/cfg
```

Read-write, because Claude Code rewrites `.claude.json`. Verified in question 7.

The host's real config dir needs no deny rule, unlike srt: it is simply not in
the guest. That makes layer 3 stronger under `msb` than under srt, and it is the
clearest single argument for the backend.

### What does not carry over

The PATH shim dir (§4.1) has no job here, since the guest only contains what the
image contains. That closes the absolute-path bypass §4.1 documents. Say so in
the SPEC when the backend lands, because it changes the honest trust description
in a good way.

---

## Verdict

**GO WITH CAVEATS.**

`msb` meets every functional requirement §2.2 asks for: OCI images, volume mounts
read-write and read-only, per-sandbox network policy, a real attach primitive
with a shared process namespace, port forwarding, and a 175ms boot. It installs
and runs entirely in user space with no privileged step, which was the main
unknown going in. Layer 3 works unchanged. Two SPEC sentences need correcting,
neither of them load-bearing for the decision.

The caveats are the reason this is not a plain GO.

### Top 3 risks

**1. Memory, not boot time, is the ceiling.** An agent VM that has installed a
toolchain holds 0.8GB to 1.3GB resident and does not give it back. Four sessions
is 3GB to 5GB. The whole point of sessions is running several at once, so this
caps the feature in a way the SPEC's "heavier, still sub-second" framing does not
convey. Decide a per-session memory budget and a session cap before building, and
measure a real agent workload rather than a fresh `npm install`.

**2. Host and guest integration is unsolved.** A guest cannot reach the host by
default, there is no host alias, and the gateway is not a proxy. Anything that
assumes an agent can call the `herdr` CLI, or reach a host dev server, needs a
mechanism chosen first (`--vsock` is the likely answer). The portless URL the
SPEC leans on does not exist, so that has to be built on port forwarding or
dropped.

**3. It is beta software moving fast.** The README says so. 0.6.13 shipped the
day of this spike and three releases landed in the preceding four days. The
binary is ad-hoc signed with no team identifier. One `msb exec` with no
`--timeout` hung indefinitely and could not be reproduced in six further attempts
under identical conditions, cause unknown. Mitigations: pin the version, always
pass `--timeout` on non-interactive exec, and treat an upgrade as a change that
needs a re-run of this spike.

A fourth, smaller one worth writing down: an image per agent is a real cost. The
`node:22-slim` pull was 20s and 76 MiB, and each sandbox from it costs 431MB on
disk once a toolchain is installed. The registry's `image` field makes this
possible, but somebody still has to build and maintain those images.

### Recommended next step

Build the backend against a pinned `msb` 0.6.13 behind the existing `Backend`
interface, starting with `create_session` and attach, and settle the host and
guest question (risk 2) in design before any code.

---

## Appendix: install and uninstall

Everything installed by this spike, and how to remove it.

### What was installed

```sh
curl -fsSL https://install.microsandbox.dev | sh
```

The script was read before running. It uses no sudo and touches nothing outside
`$HOME`. It installs:

| Path | What |
| --- | --- |
| `~/.microsandbox/bin/msb` | the binary, plus a `microsandbox` symlink to it |
| `~/.microsandbox/lib/libkrunfw.5.dylib` | the guest kernel |
| `~/.microsandbox/{cache,db,run,sandboxes}` | created on first use |
| `~/.local/bin/msb` | symlink to `~/.microsandbox/bin/msb` |
| `~/.local/bin/microsandbox` | symlink to `~/.microsandbox/bin/microsandbox` |

There is no Homebrew formula in `homebrew-core`. The project publishes its own
tap, `brew install superradcompany/tap/microsandbox`, which was not used here.

### Uninstall

```sh
msb rm -f $(msb ls -q)          # stop and remove every sandbox first
msb self uninstall -y           # removes msb, libkrunfw and the command links
rm -rf ~/.microsandbox          # only if anything is left behind
```

`msb self uninstall` removes the binary, the library and the `~/.local/bin`
links. It does not need sudo.

Images and sandbox state live under `~/.microsandbox` and go with that directory.

### Spike scratch files

```sh
rm -rf ~/msbspike               # test mounts, fake credentials, symlink probes
rm -f /tmp/msb_install.sh /tmp/hostsrv.log
rm -rf /tmp/msbdl /tmp/msbspike
```

### State of the machine after this spike

- Every sandbox created here was removed. `msb ls` reports none and no `msb`
  process is running.
- The host test server on port 18080 was stopped.
- `msb` 0.6.13 is **left installed**, at `~/.microsandbox` with links in
  `~/.local/bin`, because the verdict is GO WITH CAVEATS and the next feature
  needs it. 317MB total: 52MB runtime, 261MB image layer cache (`alpine` and
  `node:22-slim`), 4.5MB metadata.
- Nothing outside `$HOME` was created or changed. No sudo was used at any point.
