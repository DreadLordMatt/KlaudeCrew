# Ops Mission Control — Ideation

Status: ideation complete, spec in `spec.md`
Date: 2026-07-30

## 1. Where this comes from

A 5-person data-platform team ran an autonomous ops agent in production for two
weeks against a real oncall rotation. Measured over a 14-day before/after window
(n=152 before, n=74 after):

| Metric | Before | After | Change |
|---|---|---|---|
| Median TTR | 23.3h | 6.6h | **-72%** |
| Resolved ≤24h | 51% | 65% | +28% rel |
| Resolved ≤1h | 16% | 23% | +45% rel |
| Ops channel autonomy | — | 97.5% | — |

Caveats the source itself states, and which we carry forward honestly: some of
the volume drop came from upstream stabilization, the before-window contains
batch-close artifacts that inflate mean/p90, two-week samples are noisy, and the
numbers do not separate agent-resolved from human-assisted. The direction is
strong; the precision is not. **We will not put these numbers in the public
product's marketing copy** — they describe one team's internal pipeline, not a
promise to a stranger's infrastructure.

What matters for us is *why* it worked. Four mechanisms, none of which are
Amazon-specific:

1. **Rotation-aware cron tiering.** Automation is split into always-on,
   oncall-gated, and single-leader tiers. An hourly check reads the rotation and
   resumes/pauses the oncall tier automatically. Nobody remembers to turn
   anything on.
2. **Claim-based dispatch.** A silent 2-minute heartbeat polls for unowned work,
   claims it in a durable index, and spawns one visible, *replyable* thread per
   item. The heartbeat never speaks; only real work creates noise.
3. **Compounding institutional memory.** Fix patterns land in a shared,
   git-backed ledger with confidence and trust levels. The next responder starts
   where the last one finished. This is the mechanism the team's own quote
   singled out as the reason to templatize it.
4. **The channel as the dashboard.** Pinned threads in one ops channel *are* the
   live board: unclaimed / investigating / needs-human / resolved. Pins are
   reconciled against real state, so the board cannot drift into fiction.

Everything above is a *workflow* insight. None of it requires the specific
ticketing system, cloud account structure, or corporate tooling it was born in.
That is the thesis of this product.

## 2. Problem statement (public framing)

A small team running production infrastructure on AWS faces a structural
mismatch: alerts arrive 24/7, but attention is business-hours and single-threaded
through whoever is oncall. The consequences are consistent across teams:

- **Alerts accumulate instead of being worked.** Nothing claims an alarm the
  moment it fires, so items sit until someone triages a backlog.
- **Fix knowledge is tribal.** The remedy for a recurring failure lives in a
  closed ticket, a Slack thread, or one engineer's memory. Every rotation
  restarts from zero.
- **Signal is fragmented.** CloudWatch has the alarms, PagerDuty has the pages,
  Datadog has the monitors, and the ticket tracker has the work items. No surface
  answers "what is broken right now and who owns it?"
- **Existing tooling routes, it does not resolve.** Incident platforms are
  excellent at paging a human and terrible at doing the first twenty minutes of
  investigation that the human will inevitably do.

The gap is not *alerting*. It is the **first responder**: the entity that picks
up an alert within minutes, gathers context, matches it against what the team
already knows, fixes it or escalates with a real diagnosis attached, and writes
down what it learned.

## 3. Product concept

**Ops Mission Control** is a KiroCrew app that turns your KiroCrew instance into
an autonomous ops first responder for your own infrastructure.

It runs on the user's own machine (KiroCrew's model), holds the user's own
credentials, talks to the user's own providers, and reports into the user's own
Slack. There is no Ops Mission Control service, no hosted control plane, and no
telemetry — a deliberate positioning choice, since the alternative is asking
teams to hand production alert streams and cloud credentials to a third party.

### The mission-control surface

One page answers "what is broken, what is being done, and what do we know":

```
┌─ Ops Mission Control ─────────────────────── ● 3 active  ⏸ rotation: off-shift ─┐
│                                                                                  │
│  ┌ Signals ────────────┐ ┌ Board ──────────────────────────────────────────────┐│
│  │ CloudWatch    ● 2   │ │ 🔍 INV-114  RDS conn spike      investigating  12m  ││
│  │ PagerDuty     ● 1   │ │ 🧑 INV-112  cert expiry         needs human    2h   ││
│  │ Datadog       ○ 0   │ │ ⏳ INV-115  DLQ depth > 500      unclaimed     40s  ││
│  │ Webhook       ○ 0   │ │ ✅ INV-110  lambda throttle      resolved      1h   ││
│  └─────────────────────┘ └─────────────────────────────────────────────────────┘│
│                                                                                  │
│  ┌ Knowledge ledger ───────────────────────────────── 34 patterns ─────────────┐│
│  │ DLQ duplicate-PK backlog        high · verified    used 6×   last: 2d ago   ││
│  │ Lambda OOM on CSV ingest        high · verified    used 4×   last: 5d ago   ││
│  │ Cert rotation not propagated    medium · observed  used 1×   last: 3w ago   ││
│  └─────────────────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────────────┘
```

Each board row is backed by a real chat session (KiroCrew's `ChatEmbed`), so
"reply to the investigation" is the same interaction as replying in Slack — and
both mirror the same session.

### Feature set, mapped from the internal first pass

| Internal mechanism | Public equivalent | Notes |
|---|---|---|
| 3-tier cron model (always/oncall-gated/leader) | Same 3 tiers, SOP-driven | Tier names kept: `always`, `on_shift`, `primary` |
| `oncall-rotation-check` against corporate oncall | `RotationSource` seam: PagerDuty schedule, Opsgenie, static YAML rota, or always-on | The corporate oncall service becomes one adapter among several |
| `ticket-dispatch` claim heartbeat | `dispatch` heartbeat over normalized **Signals** | Signal = alarm, page, monitor, or webhook event |
| Ticket tracker as work source | `SignalSource` adapters: CloudWatch, PagerDuty, Datadog, generic webhook, GitHub Issues | No single tracker assumed |
| `remediation.md` + `common-pitfalls.md` | **Knowledge ledger** with confidence/trust/use-count | Same shape; machine-readable JSONL + rendered markdown |
| `shared-lessons.jsonl` team brain over git | Optional git-backed sync of the ledger | Off by default; a solo user needs no remote |
| Slack pin board | Slack pin board via KiroCrew Slack facilities | Direct reuse, not reimplementation |
| `knowledge-dream` (dedup/prune nightly) | Same, as a `primary`-tier SOP | Keeps the ledger from rotting |
| `harbinger` (pin/index reconciliation) | `reconcile` SOP | Prevents board drift |
| Operating modes `auto_resolve` / `propose_then_ask` | **Same two modes, but default flipped to propose** | See §5 |
| Internal ticketing/pipeline/wiki MCP tools | Companion package only | Not in the public component |

### What is deliberately *not* in the public component

Per `AGENTS.md`, this repo is a de-Amazoned public fork. The public app must not
reference or imply internal ticketing, pipeline, code-review, wiki, oncall, or
capability-management systems, or internal hostnames and package names. Those
belong to the companion (§6), developed separately.

## 4. Provider abstraction — the load-bearing design decision

The internal system hard-wired one ticketing system, one alarm source, and one
oncall service. Generalizing that into "support PagerDuty and Datadog too" by
branching would produce exactly the coupling this fork exists to avoid.

Instead, four narrow Protocols, each with a shipped default, following the
existing CPP pattern in `src/kiro_crew/platform/interfaces.py`:

| Protocol | Question it answers | Public adapters |
|---|---|---|
| `SignalSource` | What is firing right now? | CloudWatch alarms, PagerDuty incidents, Datadog monitors, generic inbound webhook, GitHub Issues |
| `RotationSource` | Who is on shift? | PagerDuty schedule, Opsgenie schedule, static YAML rota, always-on |
| `ActionSink` | How do I acknowledge / resolve / comment? | PagerDuty, Datadog, GitHub Issues, no-op (observe-only) |
| `EvidenceSource` | What context surrounds this signal? | CloudWatch Logs Insights, CloudWatch metrics, Datadog metrics, arbitrary read-only shell probe |

The registry is **additive**, mirroring `AppsLoader.registry_rows()`: a companion
can only ADD adapters, never repoint a core one. The core never imports a
companion and never branches on edition.

An important consequence: because `ActionSink` includes a no-op observe-only
adapter, the app is genuinely useful — and completely safe — before a user grants
a single write scope. That is the intended first-run state.

## 5. Safety posture (and where I diverge from the internal system)

The internal system classified work into `auto_resolve` for two known
machine-generated intake paths and `propose_then_ask` for everything else,
defaulting to propose when uncertain. Good design — and it was tuned by a team
that owned both the pipeline and the tickets.

For a public product I am **flipping the default**: `propose_then_ask` for
*everything*, with `auto_resolve` available strictly as per-rule opt-in matched
on a signal predicate the user writes. Reasoning: the internal team could reason
about which intakes were machine-generated because they built them. A stranger's
first install has no such basis, and an agent that auto-resolves a human's
production page on day one is a far worse failure than one that is slightly slow.
Autonomy is earned per-rule, by the user, after they have seen the agent's
proposals be right.

Concretely, three tiers of increasing autonomy the user opts into:

1. **Observe** (default) — reads signals, investigates, posts findings. Zero
   writes anywhere.
2. **Propose** — everything above, plus drafts the ack/comment/resolve action and
   asks in Slack or the dashboard. Nothing executes unapproved.
3. **Act** — executes matched actions for signal patterns the user has
   explicitly allowlisted, each SEL-audited.

Remediation *execution* (running a fix, not just resolving a ticket) is out of
scope for v1 in the public component. The app diagnoses and proposes; the human
runs the fix. Shipping an agent that executes remediation against a stranger's
production infrastructure is not a v1 decision.

### Credential handling

ARCC guidance on secrets (BSC5 Secrets Manager, `cnt_IGtzguLChi2MyB`) is
explicit: use IAM-based authentication wherever the target supports it, and for
targets that require credentials — third-party API keys and tokens, exactly our
PagerDuty/Datadog case — store them in a managed secret store rather than
plaintext, and never hardcode them or put them in plaintext environment
variables. The anti-patterns it names (hardcoded credentials, plaintext env
secrets, no rotation) are precisely the traps an app like this invites.

Mapping that onto a local-first app that has no AWS control plane of its own:

- **AWS access uses the ambient credential chain** — the user's existing profile,
  role, or instance role. The app never asks for, stores, or transmits an AWS
  access key. This follows the "IAM roles over keys" guidance directly.
- **Third-party tokens (PagerDuty, Datadog) are the BSC5 case.** They are stored
  in a keystone-protected file on the crew home, added to
  `security._CREW_SECRET_LEAVES` so the agent's own file and shell tools cannot
  read or write them (the same mechanism that protects `security_policy.json`).
  A token never enters `config.json`, never enters `data/config.json` (which is
  served unauthenticated to the UI), and never enters a model-visible string.
- **Optional AWS Secrets Manager backend.** For users who want rotation and are
  already on AWS, the token store is a seam with a Secrets Manager adapter, so
  the ARCC 90-day-rotation guidance is available rather than merely recommended.
- **Redaction on every egress.** All provider responses pass through
  `security.redact` before reaching a model, a transcript, or Slack. The existing
  AKIA/ASIA redaction is reused, extended with PagerDuty/Datadog token shapes.

This is the one area where I am consciously over-building relative to a v1
feature list, because a local agent holding production alert credentials is the
part that is expensive to retrofit.

## 6. Public core + Amazon companion split

```
┌─ PUBLIC (this repo) ─────────────────────────────────────────────┐
│  apps/builtins/ops_mission_control/                              │
│    • Signal/Rotation/Action/Evidence Protocols + registry        │
│    • Incident store, dispatch index, knowledge ledger            │
│    • SOP-driven cron tiers, reconciliation, ledger hygiene       │
│    • Slack board via KiroCrew Slack facilities                   │
│    • Adapters: CloudWatch, PagerDuty, Datadog, webhook, GitHub   │
│    • Dashboard page + embedded per-incident chat                 │
└──────────────────────────┬───────────────────────────────────────┘
                           │ registers adapters through the
                           │ additive OpsProviderRegistry seam
┌──────────────────────────┴───────────────────────────────────────┐
│  COMPANION (separate package, not in this repo)                  │
│    • Internal ticketing SignalSource + ActionSink                │
│    • Corporate oncall RotationSource                             │
│    • Internal pipeline/deployment EvidenceSource                 │
│    • Team-brain git sync against internal hosts                  │
└──────────────────────────────────────────────────────────────────┘
```

The seam rules, inherited from the existing CPP contract:

- The public core **never** imports the companion and never branches on edition.
- The companion may only **ADD** adapters. A name collision resolves in favor of
  the core.
- The companion is a separate package with its own lifecycle; this repo contains
  no reference to it beyond the neutral extension point.
- `scripts/scrub-lint.sh` gates the public tree, so a leaked internal marker
  fails CI rather than shipping.

A team that has both installed sees one board with internal and external signals
side by side. A public user sees the same board with the adapters they configured
and no evidence the other set exists.

## 7. Why this is a KiroCrew app rather than a standalone tool

Every hard part of this product is something KiroCrew already does:

| Need | KiroCrew facility |
|---|---|
| Scheduled autonomous work | cron service + `cron_add` MCP tools |
| Parallel investigations | `spawn_run` subagents |
| Slack as the interaction surface | Slack gateway, channels, threads, `send_message` |
| Per-incident conversation | chat slots + `ChatEmbed` |
| Learned patterns | lessons + vector memory |
| Human-in-the-loop approval | interactive approval layer |
| Audit trail | SEL audit log |
| Secret protection | keystone sensitive-path floor |
| Dashboard surface | App Kit builtin page |

Building this standalone would mean reimplementing all nine. As an app, the
product is the *ops-specific* part: provider adapters, the incident model, the
knowledge ledger, the board, and the SOPs. That is the right size for the value
it adds.

## 8. Success criteria for v1

1. A user with only an AWS profile and a Slack workspace gets a working board
   with real CloudWatch signals in under 10 minutes, without granting any write
   scope.
2. Adding PagerDuty or Datadog is entering one token in Settings; no restart, no
   config file editing.
3. A signal that fires while nobody is watching is claimed, investigated, and
   posted to Slack with a diagnosis attached, within one heartbeat interval.
4. The second occurrence of a known pattern is recognized from the ledger and
   resolves measurably faster than the first.
5. No token, no credential, and no raw provider payload ever appears in a
   transcript, a model prompt, or a Slack message.
6. `scripts/scrub-lint.sh` passes; the public component contains no internal
   marker.
7. The full backend gate is green: `black`, `isort`, `flake8`, `mypy`, `pytest`.

## 9. Open questions carried into the spec

- **Webhook ingress.** A generic inbound webhook needs a reachable endpoint. The
  gateway is local-first, so v1 accepts webhooks only on the existing
  authenticated gateway surface and documents the tunnel as the user's choice
  rather than shipping ingress.
- **Rotation for solo users.** A one-person team has no rotation. `always-on` is
  the default `RotationSource` so the oncall-gated tier degrades to always-on
  rather than never firing.
- **Ledger sync conflicts.** Git-backed team sync invites merge conflicts on a
  JSONL ledger. Append-only with content-addressed ids makes conflicts trivially
  resolvable; the internal system's append-only rule is adopted for the same
  reason.
- **Noise budget.** The internal heartbeat was silent by default and that is why
  the channel stayed usable at 207 messages/week. Silence-by-default is a hard
  requirement, not a preference.
