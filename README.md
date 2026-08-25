<p align="center"><img src="assets/logo.png" alt="paddock: four horses at a fence" width="360"></p>

# paddock

Run any coding agent, on a local model or a hosted one, in a sandbox whose tools,
network and files you pick per session. Works in any terminal.

MIT licensed · no telemetry · v0.2.0, beta

Permissions are enforced by the OS: Seatbelt on macOS, bubblewrap on Linux.
There has been no outside security review, so read the
[trust model](docs/GUIDE.md#trust-model) before you point it at anything valuable.

## Install

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/) and Node.js, plus
`bubblewrap`, `socat` and `ripgrep` on Linux
([details](docs/GUIDE.md#install)).

```sh
curl -fsSL https://raw.githubusercontent.com/desquaredp/paddock/main/install.sh | sh
# or, if you already have uv:
uv tool install git+https://github.com/desquaredp/paddock
```

Then start a sandbox, right where you are:

```sh
paddock run
```

Using [herdr](https://herdr.dev)? `paddock init` binds the chooser to `prefix+s`
and gives every sandbox its own tab. See
[herdr integration](docs/GUIDE.md#herdr-integration).

## The chooser

One screen, filled in from the profile you used last.

```
paddock                          claude-default            in ~/dev/paddock

   1 Open       New sandbox
 > 2 Profile    claude-default
   3 Backend    srt (instant, a policy sandbox)
   4 Agent      Claude Code (claude)
   5 Tools      git rg fd jq curl node npm npx uv python3               (10)
   6 Network    anthropic, github, npm, pypi/uv                (12 domains)
   7 Files      an isolated scratch directory
   8 Skills     none
   9 Advanced   name, save as profile, MCP

 Fills in everything below. Change anything and the title says "+ changes",
 because the session will then not be what the profile says.

   [ Launch ]        [ Cancel ]

enter edit   ^v move   1-9 jump   L launch   s save   esc cancel   ? keys
```

Change any row, or press `L`. Every launch ends on a confirm that spells out the
resolved policy, domain by domain, before anything starts.
[Field by field](docs/GUIDE.md#the-chooser).

## Commands

```sh
paddock run <profile>       # run a sandbox in this terminal
paddock launch <profile>    # start one in a herdr tab instead
paddock attach <session>    # put another tab on a running session
paddock collect <session>   # end one session now, sandbox and all
paddock profiles            # list saved profiles
paddock gc                  # collect sessions whose tabs are all closed
paddock logs                # where paddock logged what it did, and the end of it
paddock init                # bind the chooser into herdr's config
```

Full flags in [the command line reference](docs/GUIDE.md#command-line).

## Local models, contained

On the `msb` backend a session is a microVM, and naming a local port writes one
rule: reach this machine, on that port, and nothing else. Not another port, not
the internet, not even DNS. Declare the port in an agent file, and picking that
agent is what opens it:

```json
{
  "name": "Local inference",
  "command": "/bin/sh",
  "image": "alpine",
  "api_domains": ["localhost:11434"]
}
```

Same for ollama, LM Studio, llama.cpp and vLLM.
[The full recipe](docs/GUIDE.md#local-models-contained), verified live against a
27B model.

## Docs

- [docs/GUIDE.md](docs/GUIDE.md): the chooser, sessions, backends, local models,
  the command line, herdr, uninstall.
- [docs/SPEC.md](docs/SPEC.md): the enforcement layers and what each one does not
  stop, the agent registry, profiles, backends.
- [docs/ROADMAP.md](docs/ROADMAP.md): where this is going.
- [CONTRIBUTING.md](CONTRIBUTING.md): branching, TDD and design principles.
- [herdr](https://herdr.dev): the terminal multiplexer paddock adds tabs to.
