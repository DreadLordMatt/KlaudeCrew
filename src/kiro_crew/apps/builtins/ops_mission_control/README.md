# Ops Mission Control

An autonomous ops first responder, shipped as a built-in KiroCrew app. It polls your
signal providers, claims what is firing, investigates it in a chat session you can watch
and reply to, matches it against a knowledge ledger that gets better the longer you run
it, and proposes a fix.

**Read-only by default.** Nothing is written to any provider until you say so, per
signal pattern. See "Autonomy" below.

## Quick start

1. Enable the app (App Store → Discover → Ops Mission Control).
2. **Settings → Providers** → turn on a source. AWS CloudWatch needs no credential — it
   uses your ambient AWS profile chain and stores no key.
3. Wait one heartbeat, or press **Check now**.

If nothing happens, the board and the dispatch response both say why. "Quiet" and
"nothing is watching" are different states and the app never conflates them.

## The four tabs

| Tab | Answers |
|---|---|
| **Board** | What is being worked right now, its status, and the live investigation chat |
| **Signals** | Per-source health, what the *last poll actually returned* (including errors), and firing signals not yet claimed |
| **Handover** | What an incoming responder needs: what is waiting on a person, what stopped without a diagnosis, and what keeps recurring |
| **Settings** | Providers, credentials, autonomy, Slack, instance role |

## Autonomy

Three modes, and `act` is deliberately hard to reach:

| Mode | What it does |
|---|---|
| `observe` | Reads and investigates. Writes nothing anywhere. **Default.** |
| `propose` | Drafts the acknowledge/resolve/comment and asks first. |
| `act` | Executes — but only for signal patterns you allowlisted with a rule. |

`act` requires **both** the app-level mode *and* a rule matching that specific signal. A
rule must name a source plus a resource glob or label match, so "act on everything" is
not expressible. Every execution is audited.

This is a deliberate divergence from the internal workflow this app is modeled on, which
auto-resolved two known machine-generated intakes by default. That team could reason
about which intakes were safe because they had built them; your first install cannot.

## Credentials

- **AWS** uses your ambient credential chain. No key is ever stored.
- **Other providers'** tokens go to a keystone file the agent cannot read or overwrite
  (it is on KiroCrew's sensitive-path floor). The API never returns a stored secret —
  only whether a field is set.
- That file lives outside the app's folder, which is what makes it unreachable to the
  agent. It therefore **survives uninstalling the app** — use Revoke in Settings first
  if you want a credential gone.

## Slack

Mirrors incidents to a channel as a live board: one message per incident whose state
updates in place, diagnosis in the thread. It uses the Slack connection **KiroCrew
already has** and stores no token of its own — so if Slack is not set up for KiroCrew,
this channel is unavailable and Settings says so.

## The knowledge ledger

Each investigation that finds a reusable fix records it. A repeat failure matches by
*fingerprint* (a normalized signal shape that strips timestamps, ids, and bare numbers,
so a recurrence matches its ancestor) and the investigation starts from what you already
know instead of re-deriving it.

Entries carry `confidence` and `trust`. Only `verified` + `high` unlocks the fast path
where the agent proposes a remembered fix directly — anything weaker is presented as a
hypothesis to test. A knowledge base that overstates itself does harm.

## Extending it: the companion contract

Internal or bespoke adapters live in a **separate package** you install alongside
KiroCrew. The public core never imports it and never branches on which edition is
running.

Register an entry point:

```toml
[project.entry-points."kirocrew.ops_providers"]
my-company = "my_pkg.ops:register_adapters"
```

```python
def register_adapters(registry) -> None:
    registry.register_signal_source(MyTicketSource())
    registry.register_action_sink(MyTicketSink())
```

Implement one or more of four narrow Protocols (`backend/providers/base.py`). Each needs
`id` and `display_name` properties plus `configured() -> bool`:

| Protocol | Extra method |
|---|---|
| `SignalSource` | `async poll() -> list[Signal]` |
| `RotationSource` | `async on_shift() -> ShiftStatus` |
| `ActionSink` | `supported_actions() -> frozenset[str]`, `async execute(signal, action, payload) -> ActionResult` |
| `EvidenceSource` | `async gather(signal, budget) -> list[Evidence]` |

Build signals with `Signal.create(source=..., native_id=..., title=..., severity=...,
state=..., resource=..., url=..., labels=...)` so the fingerprint is computed the same
way as for every built-in adapter.

Rules that will not bend:

- **Registration is ADD-only.** An id that already exists is refused and the incumbent
  wins, so auditing what the public core does never requires auditing your package.
- **Every candidate passes the fleet admission policy before its code is imported.** A
  package the policy rejects never runs; each decision is audited.
- **Evidence is redacted for you** at a single chokepoint. Return raw text; the core
  scrubs credentials before anything reaches a model prompt or Slack.
- **Do not police your own authority.** The autonomy gate is resolved before `execute`
  is called.

## Layout

```
app.json                  manifest: crons, permissions, store listing
backend/
  models.py               Signal, Incident, LedgerEntry, the status grammar
  registry.py             ADD-only adapter registry + fan-out
  companion.py            entry-point discovery for out-of-tree packages
  dispatch.py             the cycle: poll -> claim -> ledger-match -> sweep
  store.py                atomic claim index (one incident per signal)
  ledger.py               append-only knowledge ledger + hygiene
  rotation.py             autonomy gate + rotation-driven tier arming
  slot_watch.py           derives "waiting on a person" from the live chat
  handover.py             the shift digest
  slack_out.py            the Slack pin board
  secrets.py              keystone credential store
  providers/              cloudwatch, pagerduty, datadog, github_issues, webhook, noop
tests/                    unit + contract tests
```

The agent-facing skill and its SOPs are **not** here — they ship in
`src/kiro_crew/builtin_skills/ops-mission-control/`, because a builtin app's own
directory is never copied into the data home. The dashboard UI is in
`website/src/apps/ops-mission-control/`.

Full design rationale and every durable contract:
`docs/system-specs/modules/ops-mission-control.md`.
