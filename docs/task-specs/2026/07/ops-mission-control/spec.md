# Ops Mission Control — Technical Specification

Status: **implemented** — see `docs/system-specs/modules/ops-mission-control.md` for
the shipped behavior (that file is the living spec; this one is the archived design)
Date: 2026-07-30
Ideation: `ideation.md`

## Deltas discovered during implementation

Recorded here because each one was a silent-failure mode the design did not
anticipate:

1. **A builtin's app directory is not copied into the data home.**
   `register_builtin_apps` writes only `app.json` + `installed.json`, so
   `manifest.skills` pointing at an app-local `skills/` dir registers nothing and
   reports no error. The skill and its SOPs therefore live in
   `src/kiro_crew/builtin_skills/ops-mission-control/`, which IS packaged and is
   copied to every install by `_ensure_builtin_skills`. (`code_review_sage` and
   `dev_fleet` have the same latent gap — out of scope to fix here, but worth
   knowing.)
2. **Crons must be declared in the manifest or the app is inert.** §7 described the
   SOPs but nothing registered them. All four are now manifest crons shipping
   `enabled: false` (registered but paused) so they cannot fire before a provider
   is configured.
3. **A dispatch engine was missing.** The spec described the cycle but no module
   owned it, which left `Incident.ledger_matches` permanently empty — the
   compounding-memory mechanism present in structure and dead in fact.
   `backend/dispatch.py` now owns it.
4. **The config route needed a secret guard.** `data/config.json` is served
   unauthenticated, so `PUT /providers/<id>/config` refuses any key matching the
   adapter's `secret_fields` rather than trusting the caller to use the right route.

Ops Mission Control is a **builtin KiroCrew app** (`origin: builtin`) that turns a
KiroCrew instance into an autonomous ops first responder: it polls signal
providers, claims unowned work, investigates it in a real chat session mirrored to
Slack, matches it against a compounding knowledge ledger, and proposes or (on
explicit opt-in) executes an action.

Public core lives in this repo. Amazon-internal adapters live in a separate
companion package and reach the core only through the additive provider registry
(§4.6).

---

## 1. Layout

Builtin contract per `AGENTS.md` and the `issue_radar` template:

```
src/kiro_crew/apps/builtins/ops_mission_control/
├── app.json                     # manifest (defaultEnabled: false)
├── __init__.py
├── backend/
│   ├── __init__.py
│   ├── routes.py                # register_routes(app) -> None, FULL paths
│   ├── models.py                # Signal, Incident, LedgerEntry dataclasses
│   ├── store.py                 # incident store + dispatch index (atomic writes)
│   ├── ledger.py                # knowledge ledger (append-only JSONL)
│   ├── secrets.py               # keystone-protected token store
│   ├── registry.py              # OpsProviderRegistry (additive)
│   ├── rotation.py              # RotationSource resolution + tier gating
│   └── providers/
│       ├── __init__.py
│       ├── base.py              # the four Protocols + result types
│       ├── cloudwatch.py        # SignalSource + EvidenceSource (AWS)
│       ├── pagerduty.py         # SignalSource + RotationSource + ActionSink
│       ├── datadog.py           # SignalSource + EvidenceSource + ActionSink
│       ├── github_issues.py     # SignalSource + ActionSink
│       ├── webhook.py           # SignalSource (inbound, authed gateway surface)
│       └── noop.py              # ActionSink (observe-only) + always-on rotation
├── skills/
│   └── ops-mission-control/SKILL.md
├── sops/                        # SOP markdown with YAML frontmatter
│   ├── dispatch.md
│   ├── investigate.md
│   ├── reconcile.md
│   ├── rotation-check.md
│   └── ledger-hygiene.md
└── tests/
```

Frontend:

```
website/src/apps/ops-mission-control/
├── OpsMissionControlPage.tsx    # board + signals + ledger
├── IncidentPanel.tsx            # per-incident detail + ChatEmbed
├── SettingsPanel.tsx            # provider config + autonomy tier
└── api.ts
website/public/app-assets/ops-mission-control/{icon,hero-light,hero-dark}.svg
```

Wiring:
- `BUILTIN_NAMES` in `apps/builtins/__init__.py` gains `"ops_mission_control"`
  (required — that startup loop is what calls `register_routes`).
- `website/src/apps/builtinRegistry.ts` gains
  `'/ops-mission-control': lazy(() => import('./ops-mission-control/OpsMissionControlPage'))`.

---

## 2. Manifest (`app.json`)

```json
{
  "name": "ops-mission-control",
  "version": "0.1.0",
  "displayName": "Ops Mission Control",
  "description": "An autonomous ops first responder. Watches your alarms, pages, and monitors; claims what fires; investigates it with context; and proposes a fix — with everything it learns kept in a local ledger so the second occurrence is faster than the first.",
  "author": "kirocrew",
  "tags": ["ops", "oncall", "incidents", "aws", "monitoring", "sre"],
  "highlights": [
    "Claims a firing alarm within one heartbeat and investigates before anyone is paged",
    "Knowledge ledger with confidence and trust levels — the second occurrence resolves faster",
    "Live board in the dashboard and as pinned Slack threads you can reply to in context",
    "Connects CloudWatch, PagerDuty, Datadog, GitHub Issues, or any inbound webhook",
    "Read-only by default — write actions are opt-in per signal pattern, each audited",
    "Rotation-aware: the on-shift automation tier arms and disarms itself"
  ],
  "defaultEnabled": false,
  "permissions": {
    "api": [
      "/api/apps/ops-mission-control",
      "/api/apps/ops-mission-control/*",
      "/api/chat", "/api/chat/*",
      "/api/approvals", "/api/approvals/*"
    ],
    "network": true,
    "storage": true,
    "cron": true,
    "mcpTools": ["cron_add", "cron_list", "cron_pause", "cron_resume", "send_message", "spawn_run", "learn_add"]
  },
  "dependencies": { "optionalCommands": ["aws", "gh"] },
  "backend": { "routes": "backend.routes:register_routes" },
  "ui": {
    "pages": [
      { "route": "/ops-mission-control", "label": "Mission Control", "icon": "Radio" }
    ]
  },
  "iconUrl": "/app-assets/ops-mission-control/icon.svg",
  "heroImage": "/app-assets/ops-mission-control/hero-light.svg",
  "heroImageDark": "/app-assets/ops-mission-control/hero-dark.svg"
}
```

`permissions.api` includes `/api/chat*` and `/api/approvals*` because the incident
panel embeds `ChatEmbed` and renders approval cards — omitting either yields a
silent 403 with no visible error (documented trap in `kirocrew-app-dev`).

---

## 3. Data model

### 3.1 `Signal` — normalized work item

Every provider maps its native object onto one shape. This normalization is what
makes the board provider-agnostic.

```python
@dataclass(frozen=True)
class Signal:
    id: str            # provider-scoped stable id, e.g. "cloudwatch:alarm/rds-conn-high"
    source: str        # adapter id: "cloudwatch" | "pagerduty" | "datadog" | ...
    title: str
    severity: str      # "critical" | "warning" | "info"  (normalized)
    state: str         # "firing" | "ok" | "unknown"       (normalized)
    fired_at: str      # ISO 8601 UTC
    resource: str = ""   # the thing that is broken, if the provider names one
    url: str = ""        # deep link back to the provider console
    labels: dict[str, str] = field(default_factory=dict)
    fingerprint: str = ""  # sha256 over (source,resource,title-shape) for ledger matching
```

`fingerprint` deliberately excludes timestamps and instance ids so a recurrence
matches its ancestor.

### 3.2 `Incident` — a claimed signal being worked

```python
Status = Literal["unclaimed", "dispatched", "investigating",
                 "needs_human", "resolved", "escalated", "stale"]

@dataclass
class Incident:
    incident_id: str        # "INV-<seq>"
    signal: Signal
    status: Status
    operating_mode: str     # "observe" | "propose" | "act"
    claimed_at: str
    updated_at: str
    slot_key: str = ""      # KiroCrew chat slot backing the investigation
    slack_thread_ts: str = ""
    ledger_matches: list[str] = field(default_factory=list)  # LedgerEntry ids
    diagnosis: str = ""
    proposed_action: dict | None = None   # awaiting approval, if any
    resolution: str = ""
```

Status transitions (only these are legal; enforced in `store.transition`):

```
unclaimed → dispatched → investigating → {needs_human, resolved, escalated}
investigating → stale        (no activity > stale_after, released for re-pickup)
stale → dispatched           (re-claim)
needs_human → investigating  (human replied in thread)
```

### 3.3 `LedgerEntry` — compounding knowledge

Directly modeled on the internal `remediation.md` table, made machine-readable.

```python
@dataclass
class LedgerEntry:
    entry_id: str        # content-addressed: sha256(pattern + fix)[:16]
    pattern: str         # human-readable description of the failure
    fingerprints: list[str]   # Signal fingerprints this entry has matched
    fix: str             # what resolved it
    confidence: str      # "high" | "medium" | "low"
    trust: str           # "verified" | "observed"
    use_count: int
    first_seen: str
    last_used: str
    source: str          # "agent" | "human" | "imported"
```

Content-addressed `entry_id` makes the append-only JSONL trivially mergeable
across git-synced team members — the resolution of the "ledger sync conflicts"
open question from ideation §9.

### 3.4 On-disk layout

```
<crew_home>/apps/ops-mission-control/data/
├── config.json           # NON-SECRET only: enabled adapters, tiers, thresholds
├── incidents/index.json  # {incident_id: Incident}  — the dispatch index
├── incidents/<id>.md     # investigation log (human-readable, git-friendly)
└── ledger.jsonl          # append-only LedgerEntry stream

<crew_home>/ops_mission_control_secrets.json   # KEYSTONE — see §5
```

`config.json` is served unauthenticated via `/api/apps/<name>/config` (a
documented KiroCrew behavior), so it holds **no secrets, ever**. That is the whole
reason the token store is a separate keystone file rather than a config key.

All writes use `kiro_crew.atomic_write.atomic_write`.

---

## 4. Provider seam

### 4.1 The four Protocols (`providers/base.py`)

```python
class SignalSource(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def display_name(self) -> str: ...
    def configured(self) -> bool: ...
    async def poll(self) -> list[Signal]: ...

class RotationSource(Protocol):
    @property
    def id(self) -> str: ...
    def configured(self) -> bool: ...
    async def on_shift(self) -> ShiftStatus: ...   # (on_shift: bool, until: str, who: str)

class ActionSink(Protocol):
    @property
    def id(self) -> str: ...
    def configured(self) -> bool: ...
    def supported_actions(self) -> frozenset[str]:  # {"ack","resolve","comment"}
        ...
    async def execute(self, signal: Signal, action: str, payload: dict) -> ActionResult: ...

class EvidenceSource(Protocol):
    @property
    def id(self) -> str: ...
    def configured(self) -> bool: ...
    async def gather(self, signal: Signal, budget: EvidenceBudget) -> list[Evidence]: ...
```

`EvidenceBudget` caps wall-clock, response bytes, and call count so an
investigation cannot run away against a paid API.

### 4.2 Public adapters

| Adapter | Roles | Auth | Notes |
|---|---|---|---|
| `cloudwatch` | Signal, Evidence | ambient AWS chain | `describe_alarms` (state ALARM) + Logs Insights / metric statistics for evidence |
| `pagerduty` | Signal, Rotation, Action | REST token | incidents, on-call schedules, ack/resolve/note |
| `datadog` | Signal, Evidence, Action | API + app key | monitor state, metric queries, mute/resolve |
| `github_issues` | Signal, Action | `gh` CLI (reuses issue-radar's precedent) | label-filtered issues as work items; comment/close |
| `webhook` | Signal | gateway token | inbound POST on the authenticated gateway surface |
| `noop` | Action, Rotation | none | **observe-only default** + always-on rotation |

`boto3` is an **optional lazy import**, matching the existing STT precedent — the
app must import and run with `boto3` absent (CloudWatch adapter reports
unconfigured rather than raising).

### 4.3 AWS access

Ambient credential chain only. The app never accepts, stores, or transmits an AWS
access key — direct application of ARCC BSC5 "IAM roles over keys". Required
read-only permissions, documented for the user to attach to their own
role/profile:

```
cloudwatch:DescribeAlarms, cloudwatch:GetMetricStatistics, cloudwatch:GetMetricData
logs:StartQuery, logs:GetQueryResults, logs:DescribeLogGroups
```

No write permission is requested. Resolving a CloudWatch alarm is not a thing the
app does; it resolves *work items* in trackers, via `ActionSink`.

### 4.4 Rotation and tiers

Three SOP tiers, from the internal three-tier model:

| Tier | Gate | Default SOPs |
|---|---|---|
| `always` | always active | `rotation-check` (5m), `reconcile` (15m) |
| `on_shift` | armed only when `RotationSource.on_shift()` is true | `dispatch` (2m) |
| `primary` | active only on the instance flagged primary | `ledger-hygiene` (daily) |

`rotation-check` arms/disarms the `on_shift` tier by pausing/resuming its crons
via the `cron_pause`/`cron_resume` MCP tools. With the default `noop` rotation
source, `on_shift` is permanently true — so a solo user gets always-on behavior
rather than a tier that never fires (ideation §9).

`primary` defaults to **on** for a single-instance install; a team sets exactly
one instance primary so ledger hygiene does not run N times.

### 4.5 Heartbeat: claim-based dispatch

Adopted from the internal design, silence-by-default preserved:

```
dispatch (every 2 min, silent):
  1. poll every configured SignalSource            (concurrent, per-source timeout)
  2. normalize → Signal[]; drop state != "firing"
  3. diff against incidents/index.json by Signal.id
  4. for each unclaimed, up to max_claims_per_run (default 3):
       a. atomically claim → status=dispatched   (index lock; see §4.5.1)
       b. match Signal.fingerprint against ledger → ledger_matches
       c. create chat slot, title "🚨 <incident_id> — <title>"
       d. link slot to Slack channel → replyable thread
       e. POST /api/chat with the investigate SOP prompt
  5. release incidents idle > stale_after (default 2h) → status=stale
  6. if nothing changed: exit silently, emit nothing
```

Step 6 is a hard requirement, not an optimization. The internal channel stayed
usable at 207 messages/week precisely because the heartbeat never spoke.

#### 4.5.1 Claim atomicity

Two KiroCrew instances sharing a git-synced index could double-claim. The index
write takes an exclusive file lock via `platform_compat.file_lock` (never raw
`fcntl` — Windows support), and the claim is compare-and-set on
`(incident_id, status)`. A losing claimant skips the signal. Cross-machine git
races resolve on merge because claims are keyed by content-addressed signal id
and the later timestamp loses.

### 4.6 Registry (additive)

```python
class OpsProviderRegistry:
    def register_signal_source(self, src: SignalSource) -> None: ...
    def register_rotation_source(self, src: RotationSource) -> None: ...
    def register_action_sink(self, sink: ActionSink) -> None: ...
    def register_evidence_source(self, src: EvidenceSource) -> None: ...
```

Rules, mirroring `AppsLoader.registry_rows()`:
- **ADD-only.** Registering an id that already exists is refused with a warning;
  a core adapter always wins. A companion can extend the catalog, never repoint it.
- The core never imports a companion and never branches on edition.
- Registration happens at app-enable time. Because kiro-cli caches `tools/list`
  per session, changing the adapter set requires no session reset (adapters are
  not MCP tools) — but toggling the app does re-register routes on restart.

---

## 5. Security

### 5.1 Token storage (keystone)

Third-party API tokens are the ARCC BSC5 case: targets that cannot use IAM auth.
Per that guidance they must not be hardcoded, must not sit in plaintext env vars,
and should live in a managed store with rotation.

Design:

- Tokens live in `<crew_home>/ops_mission_control_secrets.json`, mode `0600` via
  `platform_compat.restrict_to_owner` (fail-loud — not a silent no-op on Windows).
- The filename is added to `security._CREW_SECRET_LEAVES`, which places it on the
  read+write keystone floor. Consequence: **the agent's own file tools and shell
  cannot read or write it** — the same mechanism that makes the governance ceiling
  un-disableable. Covered on both the tool path (`is_sensitive_path`) and the
  shell forms (`cat`, `>`, `tee`, `tar -C`/`unzip -d` extraction).
- The only writer is the authenticated dashboard PUT handler, which opens the path
  directly and does not route through the gate — so the Settings UI still works.
- Optional `SecretBackend` seam with an AWS Secrets Manager adapter, so users
  already on AWS get the ARCC-recommended ≤90-day rotation rather than a
  recommendation they cannot act on.

### 5.2 Redaction

Every provider response passes `security.redact` before it reaches a model
prompt, a transcript, Slack, or the UI. `_ENV_CRED_PATTERNS` is extended with
PagerDuty (`u+[A-Za-z0-9_-]{20,}`) and Datadog (32-hex API / 40-hex app key)
shapes. Redaction is an **always-on floor with no policy key** — matching the
existing secure-field precedent.

### 5.3 Autonomy gate

`operating_mode` resolves per incident, tightest-wins:

```
effective = min(app_default_mode, rule_mode_for_this_signal)
            over the ordering observe < propose < act
```

- Default `app_default_mode` is `observe`.
- `act` requires **both** a user-authored rule whose predicate matches the signal
  AND `app_default_mode >= act`. There is no wildcard rule; a predicate must name
  a source and either a resource glob or a label match.
- Every `act` execution is SEL-audited (`ops-mission-control.action`) with
  adapter, action, signal id, and outcome — on denial, success, and failure.
- Remediation *execution* (running a fix) is out of scope for v1. The app
  diagnoses and proposes; the human fixes.

### 5.4 Route auth

All routes are same-origin authenticated (the standard builtin `/api/apps/<name>/*`
surface) and wrapped in the deny-by-default enabled gate:

```python
def _require_enabled(handler):
    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response({"error": "ops-mission-control is disabled"}, status=403)
        return await handler(request)
    return _wrapped
```

Builtin routes exist at startup even while the app is disabled, so this gate is
required, not decorative.

### 5.5 Webhook ingress

The inbound `webhook` SignalSource accepts POSTs **only** on the authenticated
gateway surface (`/api/apps/ops-mission-control/webhook`), requiring a
per-endpoint shared secret held in the keystone store and compared with
`hmac.compare_digest`. The gateway is local-first; exposing it publicly is the
user's decision and is documented as such. We ship no ingress or tunnel.

---

## 6. Backend routes

All under `/api/apps/ops-mission-control/`, full paths registered directly on the
router (builtin contract), each `_require_enabled`-wrapped.

| Method | Path | Purpose |
|---|---|---|
| GET | `/state` | board snapshot: incidents + per-source health + rotation + tier states |
| GET | `/incidents` | index, filterable by `status` |
| GET | `/incident?id=` | one incident + its markdown log |
| POST | `/incident/transition` | `{id, status}` — legal transitions only |
| POST | `/incident/claim` | manual claim of an unclaimed signal |
| POST | `/incident/action` | execute/propose an ActionSink action (autonomy-gated) |
| GET | `/signals?refresh=1` | live poll across sources (cache-first otherwise) |
| GET | `/providers` | adapter catalog + configured/unconfigured state |
| PUT | `/providers/<id>/config` | non-secret adapter config |
| PUT | `/providers/<id>/secret` | token write → keystone store (never echoed back) |
| DELETE | `/providers/<id>/secret` | revoke |
| GET | `/ledger` | ledger entries, sorted by use_count |
| POST | `/ledger` | add/promote an entry |
| DELETE | `/ledger?id=` | retire an entry |
| GET | `/rotation` | current shift status + which tiers are armed |
| POST | `/webhook` | inbound signal ingress (HMAC-verified, §5.5) |

`GET /providers` returns `configured: bool` and **never** the token value —
write-only secret semantics.

---

## 7. SOPs and crons

SOP markdown with YAML frontmatter, adopting the internal pattern (a file in
`sops/` is the single source of truth for both the schedule and the logic):

```yaml
---
cron: omc-dispatch
schedule: "*/2 * * * *"
tier: on_shift
silent: true
---
```

| SOP | Tier | Schedule | Behavior |
|---|---|---|---|
| `dispatch` | on_shift | 2m | claim + spawn investigations (silent) |
| `investigate` | — | on demand | per-incident: gather evidence, match ledger, diagnose, propose |
| `reconcile` | always | 15m | index ↔ provider truth, unpin resolved, release stale (silent unless changed) |
| `rotation-check` | always | 5m | arm/disarm the `on_shift` tier |
| `ledger-hygiene` | primary | daily 03:00 | dedupe, decay unused confidence, prune |

Crons register at app-enable via `cron_add`, and are removed on disable (KiroCrew
deletes an app's crons on disable and re-registers from the manifest on enable —
a documented caveat; `enabled: false` shipped crons register **paused**).

`investigate` phases, adopted from the internal investigation SOP:
1. **Gather** — evidence sources, within budget.
2. **Match** — fingerprint against ledger; a high-confidence verified match is the
   fast path.
3. **Decide** — resolve / propose action / need human / escalate.
4. **Document** — write `incidents/<id>.md`, update index, post to Slack thread,
   append a ledger entry if the pattern is new.

Phase 4 is what makes the second occurrence cheaper — the compounding mechanism,
not a nice-to-have.

---

## 8. Slack integration

Reuses KiroCrew's Slack facilities rather than reimplementing them:

- Each incident's chat slot is Slack-linked → a bidirectional thread. Replying in
  Slack talks to the investigating agent in context.
- Board state maps to thread reactions/pins: ⏳ unclaimed, 🔍 investigating,
  🧑 needs human, ✅ resolved (auto-unpins).
- `reconcile` owns the entire pin lifecycle so the pin board cannot drift from
  real state.
- Silent SOPs post via the ops channel, never DM — the internal system's
  documented pitfall (`send_message` defaults to DM).

---

## 9. Frontend

`OpsMissionControlPage.tsx` — three regions per the ideation mock: Signals health
rail, Board, Knowledge ledger. `IncidentPanel.tsx` embeds `ChatEmbed` keyed
`ops-mission-control-<incident_id>`.

Conventions per `website/AGENTS.md`: theme tokens (`var(--accent)`, `var(--card)`,
`var(--border)`, `var(--danger)`), `lucide-react` icons with
`className="lucide-inline"`, **no emojis in the UI** (the status glyphs above are
Slack-side text, not dashboard icons), shared `@kirocrew/app-sdk/ui` components.
Builtin pages import directly — no module-map feature detection, and
`ChatEmbed` needs an `AppApiProvider` ancestor the page mounts itself.

Polling: 5s while any incident is `investigating`, 30s idle. No SSE
(`/api/file-watch` overwrites React state on connect).

---

## 10. Tests

| Area | Test |
|---|---|
| Signal normalization | each adapter's fixture payload → expected `Signal`, incl. fingerprint stability |
| Fingerprint | timestamp/instance-id variation yields the SAME fingerprint; different failure yields a different one |
| Claim atomicity | concurrent claims of one signal → exactly one winner |
| Transitions | every illegal transition rejected |
| Autonomy gate | `act` refused without both app mode and a matching rule; wildcard rule rejected |
| Redaction | PagerDuty/Datadog token shapes redacted from provider payloads before egress |
| Keystone | secrets filename is on `_CREW_SECRET_LEAVES`; `is_sensitive_path` blocks read+write; bash forms blocked |
| Secret write-only | `GET /providers` never returns a token value |
| Enabled gate | every route 403s when the app is disabled |
| Rotation tiers | `noop` rotation ⇒ `on_shift` permanently armed; a disarming source pauses exactly the on_shift crons |
| Ledger | append-only; content-addressed ids dedupe; hygiene decays unused entries |
| Lazy imports | module imports and adapters report unconfigured with `boto3` absent |
| Webhook | HMAC mismatch rejected; missing secret rejected |

Async tests carry `@pytest.mark.asyncio` (`asyncio: mode=strict`). External
processes and HTTP are mocked — no real provider calls, no spawned processes.

---

## 11. Non-goals for v1

- Executing remediation against user infrastructure (diagnose + propose only).
- Hosting webhook ingress or any tunnel.
- A hosted control plane, cross-user sync, or telemetry of any kind.
- Reproducing the internal first pass's published metrics as public claims.
- Any internal ticketing, pipeline, wiki, oncall, or capability-management
  integration — those are companion-only, and `scripts/scrub-lint.sh` gates it.

---

## 12. Spec maintenance

Per `AGENTS.md`: this task spec is archival. When the app lands, its durable
behavior is documented in `docs/system-specs/modules/ops-mission-control.md`, and
any change to the provider Protocols, the on-disk schema, the autonomy gate, or
the keystone secret path updates that module spec **in the same commit**.
