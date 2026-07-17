<!-- Logo: pyfiglet "ANSI Shadow" font. Regenerate with:
     python3 -m pyfiglet -f ansi_shadow KiroCrew -->
<div align="center">
<pre>
██╗  ██╗██╗██████╗  ██████╗  ██████╗██████╗ ███████╗██╗    ██╗
██║ ██╔╝██║██╔══██╗██╔═══██╗██╔════╝██╔══██╗██╔════╝██║    ██║
█████╔╝ ██║██████╔╝██║   ██║██║     ██████╔╝█████╗  ██║ █╗ ██║
██╔═██╗ ██║██╔══██╗██║   ██║██║     ██╔══██╗██╔══╝  ██║███╗██║
██║  ██╗██║██║  ██║╚██████╔╝╚██████╗██║  ██║███████╗╚███╔███╔╝
╚═╝  ╚═╝╚═╝╚═╝  ╚═╝ ╚═════╝  ╚═════╝╚═╝  ╚═╝╚══════╝ ╚══╝╚══╝
</pre>
</div>

<p align="center">
  <b>A self-hosted AI agent that runs kiro-cli. Fully local, and locked down so it can't overstep.</b>
</p>

<p align="center">
  Chat with it from Slack, a web dashboard, or the command line. It runs
  multi-step tasks while you're away, schedules recurring jobs, spawns background
  workers, and remembers context between sessions. Nothing leaves your machine,
  and a security policy caps what it's allowed to touch.
</p>

<p align="center">
  <a href="https://github.com/kirodotdev/KiroCrew/actions/workflows/ci.yml?query=branch%3Amain"><img src="https://img.shields.io/github/actions/workflow/status/kirodotdev/KiroCrew/ci.yml?branch=main&style=for-the-badge" alt="CI status"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg?style=for-the-badge" alt="Apache 2.0 License"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue?style=for-the-badge" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/platforms-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey?style=for-the-badge" alt="Platforms">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#what-it-does">What it does</a> ·
  <a href="#talk-to-it-from-anywhere">Surfaces</a> ·
  <a href="#security">Security</a> ·
  <a href="#configuration">Configuration</a> ·
  <a href="#documentation">Docs</a>
</p>

---

<!-- TODO(maintainer): add a dashboard screenshot or short GIF here — the web
     dashboard is the headline surface and the README currently has no visual.
     e.g. <p align="center"><img src="docs/assets/dashboard.png" width="800"></p> -->

KiroCrew is an open-source personal AI agent you host yourself. It runs the
[`kiro-cli`](#2-install-the-agent-backend-kiro-cli) agent as its brain, wraps it
in a gateway that stays up, and lets you reach it wherever you already work:

```
CLI · Slack DM · Web Dashboard  →  KiroCrew gateway  →  kiro-cli + MCP tools
```

There's no hosted backend, no account to sign up for, and no per-seat pricing.
You run the gateway, and the data stays with you. (Under the hood, KiroCrew talks
to kiro-cli over the [Agent Client Protocol](https://github.com/zed-industries/agent-client-protocol),
so any MCP tool you add is available to the agent.)

## What it does

**Runs tasks while you're away.** Hand it a spec with `kirocrew run TASK.md` and
it works the job for hours, taking multiple steps on its own. You can watch the
progress from any surface, or ignore it until it's done.

**Schedules recurring work.** The built-in cron supports jitter, timeouts, and
timezones, so a nightly backup or a Monday-morning report is just a job you
describe once in plain language.

**Remembers, and learns from corrections.** It carries context across sessions
and picks up your preferences. When you correct it, the correction sticks as a
lesson that changes what it does next time.

**Searches your own docs.** Point the knowledge library at a folder of docs or
code and it builds a searchable index, using SQLite full-text search plus local
Ollama embeddings. All of it stays on your machine.

**Spawns subagents.** For work that splits cleanly, it launches isolated
background agents, runs them in parallel, and collects the results.

**Extends through MCP and apps.** It auto-discovers the MCP servers you add, and
ships an app store plus an SDK so you can install or build new capabilities.

**Built on kiro-cli.** KiroCrew targets one agent backend instead of a lowest
common denominator, so it uses kiro-cli's native tool execution, session
handling, and model access directly. You get whatever models your kiro-cli login
provides, without KiroCrew reimplementing a provider layer on top.

**Keeps you in control of what it can touch.** The `kiro-cli` subprocess runs in
an OS sandbox, credentials are redacted from output, destructive commands are
blocked by default, and an optional governance policy sets a ceiling the running
agent can't raise. More on this under [Security](#security).

**Speaks, if you want it to.** Optional speech-to-text and text-to-speech,
defaulting to a local [Piper](https://github.com/rhasspy/piper) voice with no
cloud dependency.

The complete list is in [docs/FEATURES.md](docs/FEATURES.md).

## Quick start

You need **Python 3.9+** and **Node** to build KiroCrew, and **`kiro-cli`** as
the agent it drives. [Ollama](https://ollama.com) is optional: it powers memory
and knowledge search, but you can skip it for a first run and add it later. On
Windows, use CPython 3.12 and see [docs/WINDOWS_INSTALL.md](docs/WINDOWS_INSTALL.md).

### 1. Install and build KiroCrew

```bash
git clone https://github.com/kirodotdev/KiroCrew.git
cd KiroCrew
make build      # builds the React dashboard, then installs the backend into a venv
```

`make build` is the one-command path: it runs the npm/Vite build, bundles the
dashboard into the package, and installs the `kirocrew` command. (If you'd rather
run the steps by hand, or want a wheel or the desktop app, see
[Installation and distribution](#installation-and-distribution).)

### 2. Install the agent backend (`kiro-cli`)

KiroCrew runs `kiro-cli` as its agent, so install it and log in:

```bash
curl -fsSL https://cli.kiro.dev/install | bash   # macOS/Linux; see below for other OSes
kiro-cli login
```

Full install options (Linux AppImage/zip, Ubuntu `.deb`, Windows) are in
[docs/kiro-cli/installation.md](docs/kiro-cli/installation.md) and at
[kiro.dev](https://kiro.dev/docs/cli/installation/).

### 3. Configure and run

```bash
kirocrew setup      # interactive wizard: data dir, agent, credentials
kirocrew doctor     # health check: kiro-cli, Ollama, config
kirocrew gateway    # start the server, then open http://localhost:5476
```

Open the dashboard and start talking to it. Slack is optional: skip the Slack
tokens during `kirocrew setup` and it runs dashboard-only.

### Adding memory (optional)

Memory and knowledge search use a local Ollama server. Install
[Ollama](https://ollama.com), pull the embedding model, and KiroCrew picks it up
automatically:

```bash
ollama pull qwen3-embedding:0.6b      # default (fallback: nomic-embed-text)
```

## Talk to it from anywhere

| Surface | What it's for |
|---------|---------------|
| **Web dashboard** | React app at `localhost:5476`, with multi-session chat, a memory explorer, the cron manager, and the app store. |
| **CLI** | `kirocrew chat` for interactive use, `kirocrew run TASK.md` for jobs you leave running, plus `cron`, `spawn`, and the rest. |
| **Slack DM** | Message the agent in Slack. Each thread is its own session with full tool access. See [SLACK_SETUP.md](SLACK_SETUP.md). |
| **Desktop app** | An Electron wrapper with multi-tab gateway connections and native macOS tabs. See [docs/DESKTOP_APP.md](docs/DESKTOP_APP.md). |

> Telegram and WeChat gateways also live in the codebase, but the four surfaces
> above are the ones that are documented and supported.

### Handy commands

```bash
kirocrew chat                 # interactive CLI conversation
kirocrew run TASK.md          # run a multi-step spec unattended
kirocrew gateway              # start dashboard + Slack server
kirocrew cron add ...         # schedule a recurring job
kirocrew spawn run ...        # launch a background subagent
kirocrew memory ...           # inspect / manage persistent memory
kirocrew security audit       # scan history for suspicious tool use
kirocrew doctor               # full health check
kirocrew update               # update to the latest version
```

### Running 24/7

For always-on operation as a Slack bot or cron runner, install it as a service
and it survives reboots and crashes:

```bash
kirocrew service install      # systemd (Linux) or launchd (macOS), auto-restart
kirocrew service status
```

To run it on a remote host, see
[docs/REMOTE_DESKTOP_SETUP.md](docs/REMOTE_DESKTOP_SETUP.md).

## Security

The agent has real tool access to your machine, so the controls run at
KiroCrew's own tool gate rather than trusting the agent to behave.

- **OS sandbox.** The `kiro-cli` subprocess runs inside Linux user/mount
  namespaces or a macOS seatbelt profile that hides `~/.aws`, `~/.ssh`, and other
  sensitive paths. Set with `"sandbox": "auto"`.
- **Always-on guards.** AWS credentials (`AKIA`/`ASIA`) are redacted from output,
  destructive commands are blocked by default, writes to sensitive paths are
  refused, and every tool call is written to a tamper-evident event log.
- **Governance policy (optional).** A two-level model: an enterprise ceiling the
  running app can't raise, narrowed further by per-surface, per-app, and per-task
  profiles. Inspect it with `kirocrew policy show`, `validate`, or `explain`.

For the full architecture see [docs/security-deep-dive.md](docs/security-deep-dive.md),
and report vulnerabilities through [SECURITY.md](SECURITY.md).

## Configuration

Config lives at `~/.kirocrew/config.json`. Manage it with
`kirocrew config get`, `set`, and `edit`:

```json
{
  "agent":     { "provider": "acp", "approval_mode": "interactive", "sandbox": "auto" },
  "session":   { "timeout_secs": 1800, "pool_size": 2 },
  "dashboard": { "bot_name": "KiroCrew" },
  "slack":     { "command": "kirocrew" }
}
```

The provider is fixed to `acp`, since KiroCrew always drives `kiro-cli` over ACP.
Embeddings come from your Ollama server, controlled by `memory.embedding_model`
(default `qwen3-embedding:0.6b`) and `memory.embedding_url` (default
`http://localhost:11434`). The dashboard port is the one setting that doesn't
live here: set it through `KIROCREW_PORT` or `kirocrew gateway --port <n>`, and
it defaults to `5476`. Credentials go in `~/.kirocrew/.env` instead:
`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, and `KIROCREW_OWNER_ID`.

## Installation and distribution

There are three ways to build and run KiroCrew, lightest to heaviest. All of them
go through the [`Makefile`](Makefile) using plain `pip`, `npm`/Vite, and
`pytest`, with no proprietary tooling. Full guide: [docs/INSTALL.md](docs/INSTALL.md).

| Method | Command | Result |
|--------|---------|--------|
| **From source** (dev) | `make build`, then `PYTHONPATH=src python -m kiro_crew gateway` | Editable install, run straight from `src/`. |
| **Self-contained wheel** | `make wheel`, then `pip install dist/*.whl` | A wheel with the dashboard bundled, so you can install it anywhere Python runs. |
| **Desktop app** | `make desktop` | A double-clickable DMG (macOS) or AppImage (Linux). End users need no Python or Node. See [docs/DESKTOP_APP.md](docs/DESKTOP_APP.md). |

Other Makefile targets: `make test` (build then run pytest) and `make clean`.

## Documentation

Start here, then follow the links inside each doc for the rest.

- **Install and run:** [docs/INSTALL.md](docs/INSTALL.md) · Windows: [docs/WINDOWS_INSTALL.md](docs/WINDOWS_INSTALL.md) · desktop app: [docs/DESKTOP_APP.md](docs/DESKTOP_APP.md) · remote/24-7: [docs/REMOTE_DESKTOP_SETUP.md](docs/REMOTE_DESKTOP_SETUP.md)
- **Features:** [docs/FEATURES.md](docs/FEATURES.md) · Slack: [SLACK_SETUP.md](SLACK_SETUP.md) · memory: [docs/memory-architecture.md](docs/memory-architecture.md) · MCP: [docs/mcp-architecture.md](docs/mcp-architecture.md) · apps: [docs/app-kit/getting-started.md](docs/app-kit/getting-started.md)
- **Security:** [docs/security-deep-dive.md](docs/security-deep-dive.md)
- **Reference:** [DEPENDENCIES.md](DEPENDENCIES.md) · [CHANGELOG.md](CHANGELOG.md) · [AGENTS.md](AGENTS.md) (conventions for AI assistants working in the repo)

## Troubleshooting

`AcpTimeoutError: ACP prompt timed out` means the agent backend never answered
the handshake. Check that `kiro-cli` is on your `PATH` and logged in
(`kiro-cli login`). The first launch loads MCP servers and can take over a
minute, so give it a moment before retrying, then run `kirocrew doctor`.

If memory or knowledge search isn't working, Ollama probably isn't up. Confirm
it with `curl http://localhost:11434/api/tags` and pull the embedding model
(`ollama pull qwen3-embedding:0.6b`). `kirocrew doctor` reports on embedding
health too.

If Slack won't connect, remember it's optional and dashboard-only mode works
fine without it. For the full Slack setup, see [SLACK_SETUP.md](SLACK_SETUP.md).

If an MCP server breaks after you uninstall it, run `kirocrew setup --agent-only`
to re-validate and drop the missing servers. Add `--clean` to rebuild the config
from scratch.

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[AGENTS.md](AGENTS.md) for the development workflow, conventions, and PR
guidelines.

```bash
# Backend
pip install -e ".[voice]"
pytest

# Frontend (in website/)
npm install
npm run check          # typecheck + lint + tests
npm run build          # production bundle → website/dist
```

## Community

Bugs and feature requests go in
[GitHub Issues](https://github.com/kirodotdev/KiroCrew/issues). For security
reports, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## License

KiroCrew is licensed under the Apache License 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).
