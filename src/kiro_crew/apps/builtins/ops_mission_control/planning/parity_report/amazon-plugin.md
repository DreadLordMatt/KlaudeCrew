# The Amazon-only companion plugin — capability surface, seam, security and packaging contract

Companion to [README.md](README.md), which summarises this document in §8. This file is the
full spec and stands alone. It answers two questions, in the order a reader wants them:

- **§1–§8 — what should be in it.** The capability surface: which internal writes are worth
  supporting and at what autonomy tier, which roster data answers `on_shift()`, which knowledge
  bridges are worth building, what must stay out of the plugin, and the staged build order.
- **§9–§17 — can it exist safely.** The seam, security and packaging contract: attachment,
  credentials, redaction, data boundaries, autonomy, seam gaps, testing, distribution.

Every claim in both halves is checked against code rather than docs.

Paths are relative to `src/kiro_crew/apps/builtins/ops_mission_control/` unless prefixed
`src/kiro_crew/` or `website/`. Paths prefixed `source:` are in the source context package and
`source-skillset:` in the source skillset package, matching [README.md](README.md) §1.

Naming: internal systems are referred to generically throughout (the internal ticketing
system, the internal build system, the internal wiki, the internal auth broker, the internal
on-call service, the internal code-review system, the internal metrics store, the internal
directory service). No internal hostname, account identifier, alias, ticket id or review id
appears in this document, deliberately — the constraint is the same one that makes the plugin
out-of-tree in the first place.

Line-number baseline, and **the two halves do not share one**. As in [sources.md](sources.md) §9,
the contract half (§9–§17, and G-1..G-8) is numbered against commit `e7a90677`. The capability
half (§1–§8, and G-9..G-16) was written later, against the live worktree, and its numbers are
**worktree numbers** — nine backend files grew between the two passes (`registry.py` +75,
`models.py` +75, `routes.py` +70, `ledger.py` +60, `slack_out.py` +52, `dispatch.py` +32,
`providers/pagerduty.py` +32, `store.py` +31, `providers/github_issues.py` +5), so a
capability-half citation into any of those is offset from the baseline by that much and will land
on unrelated text if diffed against `e7a90677`. The other nine backend files are byte-identical
at both points, so citations into them are unambiguous for either half: `rotation.py`,
`handover.py`, `providers/base.py`, `providers/schedule_file.py`, `providers/noop.py`,
`companion.py`, `secrets.py`, `ledger_sync.py`, `ledger_index.py`. Re-anchor on the named symbol,
not the number.

Separately, seven symbols the capability half cites did not exist at `e7a90677` at all
(`ACTION_SILENCE`, `EXPIRING_ACTIONS`, `resolve_silence_secs`, `Signal.provider_key`,
`LedgerEntry.provider_keys`, `registry.poll_health`, `ClaimedIncident.exact_match_ids`); each is
called out inline, because a reader diffing against the baseline will not find them at any line.

---

## 0. Verdict up front

**Yes, it can exist safely — but not as currently specced. Three of the required public-core
changes are security-load-bearing, not conveniences.**

**And on the capability question: five of the eight build stages in §8 are blocked on nothing,
including both of the owner's named sub-items and the entire read path.** The useful conclusion
is that the plugin's first release does not wait on the public core at all — it waits on
someone deciding that a read-only internal signal source with no write sink registered is a
release. §8.3 argues it is.

What is genuinely built is the *attachment* half: entry-point discovery, admission-before-load,
ADD-only registration, an audit trail on every decision, install order that makes core ids
un-shadowable, and a Settings surface that distinguishes "no companion" from "companion
rejected". That is real and covered by 14 tests in `tests/test_companion.py`.

What is **not** built is the half an internal plugin actually needs:

| Requirement | Status | Blocking? |
|---|---|---|
| Register adapters out of tree | Built | — |
| Admission gate before code import | Built | — |
| Store a static internal API token | Built (keystone) | — |
| Hold an **expiring, interactively-renewed** credential | **Nothing exists** | **Yes** |
| Contribute redaction patterns for internal identifiers | **Seam exists in core; this app bypasses it** | **Yes** |
| Keep internal content out of the git-synced ledger | **Nothing exists — the one unredacted egress** | **Yes** |
| Extend action vocabulary (create / severity) | `VALID_ACTIONS` is a closed core frozenset | Yes, for parity |
| Per-item human opt-out | Nothing at any layer | Yes, for internal use |
| Contract-test the plugin outside this repo | No published test kit | No, but costly |

**The single most important finding.** The app calls `security.redact` directly rather than
`platform.redact_via_context`, so the `CredentialPolicy` seam that exists *precisely* so a
loaded internal companion can add internal token regexes is not wired into either of this
app's two redaction chokepoints. Verified by grep over the whole backend — only three
platform/security imports exist:

```
backend/registry.py:255   from kiro_crew.security import redact as core_redact
backend/slack_out.py:58    from kiro_crew.security import redact
backend/companion.py:129   from kiro_crew.platform.admission import ...
```

and `redact_via_context` appears **nowhere** in the app. The docstring for that shim at
`src/kiro_crew/platform/context.py:554-557` calls it "the single, canonical credential-redaction
shim every egress site should import", and names the exact reason: "Routes through
`current_context().credentials.redact` so a loaded Amazon companion's extra credential/cookie
regexes apply." This app is a registered egress sink in `src/kiro_crew/security_posture.py`
(two rows: `slack_out.py` and `registry.py`) and does not use the shim. That is the difference
between the plugin adding redaction patterns declaratively and having to duplicate the whole
regex stack inside its own adapters.

**The second most important finding.** Redaction covers evidence → prompt and incident →
Slack. It does **not** cover ledger entry → `ledger.jsonl` → `git push`. `POST /ledger`
(`backend/routes.py:701-720`) passes `pattern`/`fix` straight to `LedgerEntry.create` with no
redaction, and `ledger_sync._stage_and_commit` (`backend/ledger_sync.py:407`) commits
`ledger.jsonl` verbatim. For a public install that is a latent risk; for an internal one where
a `fix` field naturally holds an internal command, a resolver-group name, or an employee
alias, it is the primary leak path — and recovery is a history rewrite across every teammate's
clone.

---

## 1. What the capability half assumes from the contract half

§9–§17 answer *can this exist safely*. §1–§8 answer *what should be in it*. This half does not
re-litigate security: every claim about redaction, credentials, provenance, admission or
packaging is referenced by section number and treated as settled.

Three facts from the seam bound everything below, and all three were read rather than assumed:

| Fact | Where | Consequence for the capability surface |
|---|---|---|
| Four Protocols, no fifth | `backend/providers/base.py:170-247` — `SignalSource.poll`, `RotationSource.on_shift`, `ActionSink.supported_actions`+`execute`, `EvidenceSource.gather` | Any capability that is not one of *what is firing / who is on shift / how do I write / what context surrounds this* is a seam gap by construction, and lands in §14 |
| `VALID_ACTIONS` is a closed core frozenset | `backend/models.py:171-173`; validated at `backend/routes.py:415` **and** `backend/rotation.py:220` | The plugin's write vocabulary is capped until G-1 lands |
| `supported_actions()` is unenforced | `grep -c supported_actions backend/routes.py` → 0; only `noop.py`, `pagerduty.py`, `datadog.py` and `github_issues.py` define it | Every narrowing this half specifies is advisory until §13.3's R-6 lands. The plugin must self-check in `execute`, as `providers/pagerduty.py:208` already does |

**One correction to the contract half, from re-reading the same file.** §14's G-1 text says
`VALID_ACTIONS = frozenset({ack, resolve, comment})` and calls a fourth verb unreachable. At
`e7a90677` that was exactly right (`models.py:157`). In the current worktree there are **four**:
`ACTION_SILENCE` was added with a mandatory bounded expiry, clamped at the authorization
boundary rather than in the adapter (`models.py:170-199`, `EXPIRING_ACTIONS`,
`MAX_SILENCE_SECS = 24h`, applied at `routes.py:437-442`). G-1's *argument* is unchanged — the
set is still closed and still validated in two places — but the plugin's usable vocabulary is
one verb wider than §14 says, and that verb happens to be the safest one. §4.2 A4 puts it to
work. G-1's entry in §14 now records this.

Two more corrections of the same kind, both in the plugin's favour and both post-baseline:
`Signal.provider_key` / `LedgerEntry.provider_keys` now give an exact provider-identity match
tier above the shape hash (`ledger.match`, `backend/ledger.py:203-215`), and
`registry.poll_health` (`backend/registry.py:273-281`) distinguishes "this source failed" from
"this source reported nothing". §8.3 depends on both.

A note on inference, stated once. The internal ticketing write tool's own parameter schema was
read, so the field names in §4 (`status`, `resolution`, `closureCode`, `rootCause`, `severity`,
`assignedGroup`, `assignee{namespace,value}`, `threadName`, `commentId`, `tagsToAdd`) are read,
not guessed, as are its two hard constraints: the top two severities cannot be set at all
(update additionally blocks the intermediate half-tier), and create is rate-limited to one per
minute. The on-call service's shape in §5 (`get-team-shifts` taking a team plus a start and end
date, returning shifts with `startDateTime` / `endDateTime` / `oncallMember[]`, and returning
only shifts *starting* inside the range) is likewise read. Where this half is inferring — the
wiki write path, the external shared-memory service's dedupe semantics — it says so inline.

---

## 2. Signal sources — what the plugin should watch

### 2.1 The owner's two named sub-items

`planning/features.md:449-450` names exactly two: **ticket resolver-group monitoring** (all
groups, or per alias) and **intake-folder monitoring** (all folders, or per alias). Both are
`SignalSource` implementations, both are unblocked today, and their field-by-field mapping onto
`Signal.create(...)` plus the poll shape and the `DEFAULT_POLL_LIMIT` cap is specified once in
§8.3 rather than repeated here — that mapping is the smallest-first-slice specification and it
belongs with the build order.

What this section adds is the rest of the surface: the six other signal classes the parity
research surfaced, the fingerprint hazard that applies to all of them, and the snapshot
discipline none of them can be correct without.

### 2.2 The fingerprint hazard, stated once because it applies to every class below

`compute_fingerprint` (`backend/models.py:269-283`) lowercases `source|resource|title`, replaces
every matched pattern with `#`, and — this is the hazard — `models.py:280-281` strips **bare
numbers** along with timestamps and ids. The report's §3.2 records the consequence for the public
adapters: `4xx` and `5xx` on one service collide, as do `p99`/`p50` and `shard-1`/`shard-47`.

For an internal plugin the exposure is worse, because internal work-item titles are *dense* with
significant numbers — a queue id, a folder number, a threshold value, a table generation. Two
rules follow, and every class below is specified to obey them:

1. **Never put a significant number in `title` where its magnitude is the meaning.** Put it in
   `labels` (not fingerprinted) and put the *class* in the title. `cpu above threshold` in the
   title, `{"observed": "91", "threshold": "85"}` in labels — not `CPU 91% > 85%`, which
   fingerprints identically to `CPU 34% > 85%`.
2. **Set `provider_key`.** `Signal.create` prefixes it with the source id
   (`models.py:350`) and `ledger.match` gives provider-identity an exact-match tier *above* the
   shape hash (`backend/ledger.py:203-215`). For a work item — which does not recur, so an exact
   match means "we have worked this very item before" — that tier is the correct primary key and
   it sidesteps the shape hash entirely. `providers/github_issues.py:183` already makes this
   choice for the same stated reason.

Where a class below cannot honour rule 1 — because the number *is* the identity rather than a
magnitude — it says so and names the label it uses instead.

### 2.3 The six other classes

Each row states the Protocol, the trigger the source uses, and the one design decision that is
not obvious. All six are `SignalSource`; none needs a public-core change to *emit*; three need
something the core lacks to be *handled*, and those are called out in §2.4 and §14.

| # | Class | Source SOP | Trigger the adapter uses | The non-obvious decision |
|---|---|---|---|---|
| **S-1** | **Data-freshness breach** — an expected write did not happen | `source: sops/table-freshness.md:23` (`staleness_hours > sla_hours`) | Scheduled query against the warehouse's own audit column, falling back to the system insert log (`:16-22`) | The adapter owns the threshold, which no public adapter does — see §2.4. Aggregate to **one signal per breach set**, not per table: the source posts one aggregated message (`:25`) precisely because per-table alerts flooded the channel |
| **S-2** | **Schema drift** — a column set changed | `source: sops/schema-drift.md:16-18` | Diff live `information_schema.columns` against a committed baseline | Severity is a **three-tier judgement**, not a threshold: expected/unexpected/breaking (`:18`). Only `unexpected` and `breaking` map to a firing signal; `expected` maps to `info`, which `run_cycle` will still claim — so the adapter must emit `expected` drift as evidence, not as a signal, or every known migration opens an incident |
| **S-3** | **Numeric threshold breach on infrastructure** | `source: sops/redshift-perf-monitor.md:19-24` | Metric read every 30 min; alert if any of CPU > 85% (**Average**, and `:29-31` explicitly forbids Maximum), connections > 50, disk > 80%, queue depth > 10 | The statistic is part of the threshold. An adapter that reads Maximum where the source reads Average is not "roughly equivalent" — it fires on every spike. Carry `statistic` in labels so an operator can see which was used |
| **S-4** | **Long-running / abnormal work item** | `source: sops/redshift-perf-monitor.md:107-132` | Per-principal duration thresholds, default 30 min, raised per known workload to 60/75/90 min | This is a **per-principal allowlist**, not one threshold — the source records 18+ false positives in under 7 days driving it (`:110`), and one 90-minute entry justified as "60m would leave <5m headroom" (`:113`). The plugin must ship the table as config, not constants, because it is tuned continuously. Also carries the victim/blocker rule — see §2.4 |
| **S-5** | **Credential / key expiry** | `source: sops/key-expiration-handler.md:23-44` | Poll the ticketing queue for the auto-cut alarm tickets, then check the alarm's own state | Not a ticket signal despite arriving as a ticket: the *authority* is the alarm state, and the item resolves when the alarm reads OK. So `provider_key` is the alarm name, not the ticket id — otherwise a re-cut ticket for the same key reads as a new problem |
| **S-6** | **Vendor-data arrival window** | `source: sops/s3-vendor-data-check.md:55-58` | One-shot on an arrival ticket, then re-check on a grace clock | The grace window is the whole mechanism, and it needs a clock the app does not have (§2.4). Note the source's own three-way inconsistency here (6h in the body, 12h in the template headings, 6h hardcoded in the heartbeat script, 12h defaulted in the checker) — the plugin should pick one value and make it config, not inherit the ambiguity |

Two further source jobs are deliberately **not** signal sources:

- **The auth-token watchdog** (`source: sops/midway-watchdog.md:15-21`) is a *suppressor*, not a
  detector: on expiry it pauses the agent crons, DMs once per 24h, and auto-resumes within 5
  minutes of renewal — and `:26` records that it "only resumes its own" paused jobs. Modelling it
  as a signal would put the app's own broken credential on the incident board, which is the one
  place an operator cannot act on it. It belongs in the app's cron/health layer, and the report's
  README §4.9 already ranks that as a public-core gap. The plugin's contribution is narrower: a
  `configured()` that returns False when the credential is dead, so `configured_signal_sources`
  drops the source cleanly (`backend/registry.py:109-125`) instead of every cycle recording a
  failure.
- **Security-review monitoring** (`source: sops/appsec-cr-monitor.md`) is shipped `enabled: false`
  at `:7` with a completion banner at `:12` — the source itself retired it and kept it as a
  reference playbook. It is a code-change-driving loop, which is §4.4's case and G-9/G-10's seam
  gap, not an ingestion class. Its transferable rule is worth keeping though: act on comments with
  `importance>0`, treat style comments as advisory (`:33-34`), and **never self-approve** (`:38`).

### 2.4 What these classes need that the core does not have

Four of the six cannot be fully handled by today's seam. Each is already a numbered seam gap or
maps onto one; this is the ingestion-side statement of them.

| Need | Which classes | Why the seam does not carry it | Where it is tracked |
|---|---|---|---|
| **An adapter that owns a threshold and reports the observed value against it** | S-1, S-3, S-4 | Every public adapter receives a boolean *firing* from a provider that already applied its own threshold. `Signal` has no field for "observed vs. expected", so the numbers survive only as labels and nothing in the core can render or reason about them | New — the generic half is a public-core capability, see §2.5 |
| **A clock that makes a signal louder with age** | S-6, and S-1 in its escalation form | The app's only age mechanism moves work *down*: the stale sweep (`backend/store.py:66`). A grace window that escalates at 6h and a freshness breach that becomes a ticket at 14h both need the opposite | README §4.4; this is the report's 4th-ranked gap |
| **A deterministic resolver — handling with no model turn** | S-5, S-6 | See §3.4. Both are fixed multi-step checks; both currently cost a full investigation | G-2 (strengthened), §3.4 |
| **A do-not-act annotation the core respects** | S-4 | The source's victim/blocker rule (`source: sops/redshift-perf-monitor.md:101-103`) says the *blocked* query is the victim and must not be killed; only an idle blocker may be terminated. There is no way to attach "this item is a victim, act on its blocker instead" to a `Signal` | G-2 |

### 2.5 The snapshot / last-known-good discipline — public core, not plugin

`source: sops/intake-refresh.md:33` states the rule in one line: on any unexpected exit code,
skip silently, because **"the previous-day snapshot remains usable."** The job documents an exit
code per failure class (0 success, 3 stale credential, 4 fetch failed, anything else skip) and
never overwrites a good snapshot with a bad fetch.

The app's ingestion has no snapshot layer: `run_cycle` polls live every cycle
(`backend/dispatch.py:336`) and a source returning `[]` is indistinguishable from a source with
nothing firing. The mitigation that exists is `registry.poll_health`
(`backend/registry.py:273-281`), which records `{ok, detail, at, signals}` per source. The core
already names the exact problem in `poll_all`'s own docstring (`registry.py:206-208`): absence of a
signal "only means 'it cleared' if the poll that would have reported it actually succeeded; callers
that resolve work on absence MUST consult this." That is the *reporting* half, and it is done; the
*behavioural* half is missing.

**This belongs in the public core, not the plugin,** and it is the source's most transferable
ingestion lesson. The discipline is provider-agnostic: a failed poll should leave the previous
cycle's view in place rather than implying the world went quiet. The plugin's obligation is
narrower and it is real — an internal adapter must map its failure classes onto raising rather
than returning `[]`, because `poll_all` records a raise as a failure (`registry.py:236-240`) while
an empty list is recorded as a successful poll of nothing. **Returning `[]` on a failed fetch is
the one adapter bug that silently disables the app**, and it is the easiest one to write.

---

## 3. Evidence sources — the context an investigator cannot get from the public core

### 3.1 Why this is the highest-value read surface

The public core already gathers metrics and logs well: the report's §3.1 records both CloudWatch
evidence branches verified live. What it cannot gather is everything that explains *why a system
is shaped the way it is* — and the source keeps that in committed markdown, which means it is
cheap to serve and needs no internal API at all.

Every entry below is `EvidenceSource.gather(signal, budget) -> list[Evidence]`
(`backend/providers/base.py:247`). Two constraints apply to all of them:

- **The budget may only be narrowed.** An adapter declares `evidence_budget_hint` and
  `EvidenceBudget.for_source` clamps every field with `min` (`base.py:74-105`), so an adapter
  cannot raise the operator's ceiling — deliberately, and for the same reason the autonomy gate
  is resolved outside the adapter. Defaults are 20s / 6 calls / 64 KB (`base.py:39-41`).
- **Return raw text.** Redaction happens centrally in the core (§11), so an adapter that
  pre-redacts is duplicating the regex stack and will drift from it. The corollary matters more:
  internal free text must go in `Evidence`, **never** in a `Signal` field, because §11.2's C-7
  establishes that `title`/`resource`/`url`/`labels` reach the model prompt without passing the
  chokepoint.

### 3.2 The catalogue

| # | Evidence | Where it lives in the source | `kind` | Budget hint | Why an investigator needs it |
|---|---|---|---|---|---|
| **E-1** | **Service map** — package/infra inventory: workspace→package→language→build, a Lambda handler registry, infrastructure by category, data models, cross-package dependencies | `source: knowledge/architecture/service-map.md` (579 lines) | `service-map` | Local file read; cheap | Answers "what *is* this thing that is failing, and what else touches it". Serve the sections matching the signal's `resource`, not the whole file — 579 lines against a 64 KB byte cap with a brief already measured at 7.5 KB would crowd out the actual diagnosis |
| **E-2** | **Data-flow lineage** — ingestion patterns, external providers with volumes and methods, processing layers, regional topology, active pipeline counts, monitoring coverage | `source: knowledge/architecture/data-flow.md` (203 lines) | `lineage` | Local file read | Answers "what is upstream of this, and who consumes it" — the question that decides whether a freshness breach is one table or a whole pipeline. This is the single most useful evidence type for S-1 and S-2 |
| **E-3** | **Runbooks** — 14 files, of which 10 are purely hand-written | `source: knowledge/runbooks/` | `runbook` | Local file read | The curated tier the app lacks. Detail and the authority rule in §6.2; the report's README §4.6 covers why this is distinct from the incident-derived ledger |
| **E-4** | **Prior item thread** — the correspondence/worklog history on a work item | Internal ticketing read path | `thread` | 2 calls, ~8 KB | The strongest single input for a repeat item, and the first evidence type that carries real internal free text — which is why §8.2 makes it stage 2 and ties the redaction matrix test to it. G-3 covers `kind="thread"` as convention |
| **E-5** | **Build / deploy state** — what shipped recently to the failing component | Internal build and deploy systems | `deploy` | 2 calls, 10s | "What changed" is the first question of most investigations and the app has no answer for it. **Inferred** — the API shape was not read |
| **E-6** | **Query / job history** — the recent expensive queries or job runs around the incident window, including the lock chain for S-4 | `source: sops/redshift-perf-monitor.md:119-132` (the running-query view) and `:101-103` (the lock chain) | `query-history` | 1 call, 15s, ≤16 KB | For S-3 and S-4 this *is* the diagnosis. The lock-chain read is what distinguishes victim from blocker, so without it the plugin cannot honour the source's do-not-kill rule |

### 3.3 One caution about E-1 and E-2

The two architecture documents exist in **both** source packages, and the copies have drifted:
the skillset copy of the service map still describes a serverless warehouse where the context
package describes provisioned clusters, and its data-flow copy is 8 lines shorter — missing a
whole pipeline row and a completed validation work-stream, and still describing a deployed
infrastructure fix as pending.

An evidence source that reads the stale copy will confidently supply a wrong architecture to an
investigation, which is worse than supplying none: the model has no way to know it is stale, and
the report's own §3.2 caveat about over-confident ledger matches applies with more force here.
**The plugin must name one of the two as authoritative in config and read only that one.** This
is a fact about the sources, not a design opinion — it was found by diffing them.

### 3.4 Deterministic (zero-model) handling — the class the seam cannot express

The parity research found the source's highest-volume and cheapest category is a signal whose
*entire* handling is a fixed multi-step check. Two clear instances:

- **S-5**: read the alarm state; if OK, comment and resolve; if still alarming, comment **only if
  this is the first time** and otherwise stay silent (`source: sops/key-expiration-handler.md:48-54`).
- **S-6**: query for the file; exit 0 → resolve, exit 1 → re-check after the grace window, exit 2
  → escalate (`source: sops/s3-vendor-data-check.md:55-58`).

Neither needs a model. In the app, both would take a full investigation turn — `run_cycle` claims
the signal and hands it to an agent session, and there is no path that resolves a claimed incident
deterministically.

**Can the four Protocols carry it? No.** This is the honest answer and it is the most valuable
line in this section. The decision step is not a `SignalSource` (the signal already exists), not an
`EvidenceSource` (it decides, it does not merely gather), and not cleanly an `ActionSink` (its
input is a *rule* plus a state read, not an operator-authorized verb). It is tracked as **G-16**,
and the source's own framing is the right shape for a public-core design: a fingerprint-keyed
deterministic resolver consulted *before* an agent session is created, which either resolves the
incident with a recorded reason or declines and lets the investigation proceed.

Two properties the source proves are non-negotiable if this is ever built, and both are cheap to
state now:

1. **First-observation-only commenting.** Without it a daily check comments daily on the same
   unchanged item. The source guards this explicitly by looking for its own prior comment.
2. **The resolver must be able to decline.** A deterministic handler that must always answer
   becomes a wrong answer generator; the source's exit-code 1 ("still missing, keep waiting") is a
   decline, and it is the most common outcome.

---

## 4. Action sinks — the internal writes worth supporting

### 4.1 The conservative frame

The source workflow's write vocabulary is much wider than four verbs
(`source: sops/ticket-investigation.md:126-167` runs status + resolution + closureCode +
rootCause + code-review creation; `source: sops/staleness-escalation.md:20-26` creates and
*bumps severity* through three tiers). The temptation is to port all of it. The reason not to is
recorded in `backend/rotation.py:13-17`: the public core deliberately diverged from the source's
auto-resolve-two-known-intakes default because "that team could reason about which intakes were
safe because they had built them." Internally the same asymmetry now runs the other way — the
plugin author *is* that team, which makes it easy to grant themselves what a stranger would not
get, on a system of record other teams read.

So the rule for this section: **a verb earns inclusion only if a wrong invocation is either
reversible or self-expiring.** That is the property `ACTION_SILENCE` was added for
(`models.py:166-168`: "a WRONG silence expires by itself. That makes 'let the agent act' a
bounded bet instead of an all-or-nothing one"), and it is the right test for every internal
write.

### 4.2 The sinks, with required tier and verdict

Autonomy tier = the *minimum* `effective_mode` (`models.py:513-529`) at which the plugin should
let the verb reach the provider, expressed as the tier the operator must have granted via an
act-rule.

| # | Write | Verb | Fits today's `VALID_ACTIONS`? | Required tier | Justification |
|---|---|---|---|---|---|
| A1 | Post a correspondence comment on an internal work item | `comment` | **Yes** | `act` + rule naming the queue via `label_match`, **plus** a mandatory disclosure prefix and a first-observation guard | Comments on internal work items are typically not deletable (§13.1), so this is irreversible-but-additive: the cheapest real write and the one the source used most |
| A2 | Acknowledge / take ownership | `ack` | **Yes** | `act` + rule | Reversible (hand it back) and the least consequential state change; safe to be the first verb an operator grants |
| A3 | Resolve / close with a one-line resolution | `resolve` | **Yes** (but see A6) | `act` + rule + `resolve` named **explicitly** in `AutonomyRule.actions` | `authorize_action` grants "any action this sink supports" on an empty `actions` set (`rotation.py:262-266`) — written when "any" meant three read-ish verbs. The plugin's sink must refuse `resolve` unless the rule names it, because closing someone else's work item asserts a fact about the world |
| A4 | Time-boxed mute of a recurring internal alert | `silence` | **Yes — and this is the verb §14's G-1 text misses** | `propose` is enough to *draft*; `act` + rule to apply | Core clamps the window at the boundary (`routes.py:437-442`, `MAX_SILENCE_SECS = 24h`), so the plugin cannot make an unbounded mute even by mistake. This is the correct home for the source's suppressed-row skip (`source: sops/staleness-escalation.md:26`) |
| A5 | **Severity change** (raise or lower) | none | **No — needs G-1** | `act` + rule + explicit verb + a hard floor | Two independent reasons to gate hard. (a) The write tool's own schema **blocks setting the top two severities**, so an agent "escalating" can silently no-op — an agent that believes it paged someone and did not is worse than one that never tried. (b) A *lowering* removes human attention, and the source permits it only "with justification in correspondence" (`source: knowledge/runbooks/oncall-sop.md:28`). Recommendation: ship **raise-only**, capped below the blocked tiers, never lowering |
| A6 | **Structured closure** — `closureCode` + `rootCause` alongside the resolution text | none | **No new verb needed** — see right | Same tier as A3 | The cheapest high-value item in the section, and it is *not* a new verb if done right: `execute(signal, action, payload)` already takes an arbitrary `payload` dict (`base.py:225`). The plugin can accept `payload["closure_code"]` / `payload["root_cause"]` under the existing `resolve` verb with **zero core change**. Prefer that over G-1. The gap that remains is that nothing in the core *asks* for them, so they arrive only when the SOP is told to send them |
| A7 | **Reassignment** to another group or person | none | **No — needs G-1** | `propose` **only**; never `act` in v1 | The one write whose blast radius is another team's queue, and the source gates it socially rather than mechanically: "Can reassign to external team with justification + Slack heads-up" (`source: knowledge/runbooks/oncall-sop.md:29`). A heads-up is not expressible in `ActionResult`. The shape also makes a typo silent: `assignee` is `{namespace: MIDWAY, value: <bare login>}` and `assignedGroup` is a free string, so a wrong-but-well-formed value routes work into a void |
| A8 | **Create** a new internal work item (the detection-sweep case) | none | **No — needs G-1** | `act` + rule + a per-cycle cap of 1 | The source's graduated escalation creates items (`source: sops/staleness-escalation.md:20-26`), and the write tool rate-limits create to **1/min** — so a loop hitting the limit gets errors, not duplicates, but a loop *under* the limit files 60 items an hour. Any plugin create path needs its own cap independent of the provider's |
| A9 | Worklog (internal-only) note instead of correspondence | `comment` + `payload["thread"]` | **Yes, via payload** | Same as A1 but **one tier lower in practice** | The write tool exposes a thread selector with `CORRESPONDENCE`, `WORKLOG` and `ANNOUNCEMENTS`. A worklog note is not visible to the requester, so it is a genuinely quieter write. The source mandates the opposite default — "Always post findings as a **correspondence** comment (NOT worklog)" (`source: sops/ticket-investigation.md:256`) — for *findings*. For an agent's own breadcrumbs, worklog is right, and the plugin should default breadcrumbs there |

### 4.3 Mandatory behaviours on every sink in this section

Not optional hardening; without them the sinks above are net-negative.

1. **Disclosure prefix, enforced in the adapter, not in the prompt.** The source treats this as
   MANDATORY on exactly the surface the plugin writes to, in three places independently:
   `source-skillset: context/workspace-conventions.md:22-32` (canonical),
   `source: sops/ticket-investigation.md:9-17`, `source: sops/key-expiration-handler.md:11-19`.
   The rule as written scopes to ticket systems only and explicitly exempts chat, code-review
   comments and wiki edits. §13.1 records that the app has **no** disclosure mechanism — grep
   across backend, `SKILL.md` and all six SOPs finds zero mention. So the plugin must prepend it
   inside `execute()`, before the provider call, unconditionally. A prompt-level instruction is
   not a mechanism.
2. **First-observation guard, keyed on a stable marker the plugin itself writes.**
   `POST /incident/action` posts a fresh comment on every call with no dedupe
   (`routes.py:400-460`), and C-15 already requires the plugin carry its own. The source's
   implementation is the model: "Only comment if FIRST time (no existing … comment)"
   (`source: sops/key-expiration-handler.md:53`) and the richer three-way re-page guard at
   `source: sops/ticket-investigation.md:52-55` (unchanged blocker → chat only; new evidence →
   post only the delta; prior comment wrong → prefix "Updating prior assessment:").
   Implementation: the plugin reads the item's comment thread — which it already must, for §6's
   evidence — and searches for its own marker line.
3. **Refuse-on-owned.** Both the public GitHub adapter (`providers/github_issues.py:157` —
   `if issue.get("assignees"): continue`) and the source (`source: sops/ticket-dispatch.md:35`,
   `source: sops/ticket-investigation.md:50`) drop items with an individual human assignee. This
   belongs in `poll()`, not `execute()` — but the sink should *also* refuse, because an item can
   be assigned in the gap between poll and write.
4. **`ActionResult.error` must never carry a raw provider body.** `routes.py:450,457` echoes it
   verbatim (§11.2). Return a normalized code plus a bounded, plugin-authored sentence.

### 4.4 The "a fix is a code change" case — precisely how far to go

The parity work found this dead-ends at `needs_human`. The source's counterpart is the
CR-created disposition (`source: sops/ticket-investigation.md:160-167,269`) plus a full
review-driving loop (`source: sops/appsec-cr-monitor.md`).

**Where the human gate belongs: at publish, not at merge, and not at push.** Reading the
source's own ordering, there are six distinct steps and the gate is not where a first-time
reader would put it:

| Step | Plugin may do it autonomously? | Why that line |
|---|---|---|
| 1. Check out the package into an **isolated throwaway clone** | **Yes** | The source is emphatic: an isolated throwaway clone, "NEVER the user's dev workspaces; delete the clone after" (`source: sops/appsec-cr-monitor.md:33`). Touching an operator's live workspace is the actual risk here, not the code change |
| 2. Make the minimal change, build, add a regression test | **Yes** | Entirely local. Nothing outside the clone observes it |
| 3. Push the branch to the backup remote | **Yes** | Required *before* review creation or the review has no readable diff (`source: sops/ticket-investigation.md:164`). A pushed branch nobody is subscribed to is not a notification |
| 4. Create the review as a **draft / unpublished** revision | **Yes** | A draft is visible to its author only. This is the last reversible step, and stopping *here* is what the source's propose-then-ask mode does: leave the review in draft "and link it in the operator handoff so the operator can publish after reviewing the diff" (`source: sops/ticket-investigation.md:165`) |
| 5. **Publish** the review to reviewers | **NO — human gate** | Publishing pages named reviewers and puts the change in a team's queue. The first irreversible, other-people-facing act |
| 6. Ship / merge | **NO — and this needs a second, different gate** | The source's rule is absolute: "NEVER self-approve; only ship after a human approval is recorded. NEVER `--no-verify`/force-push" (`source: sops/appsec-cr-monitor.md:38`). This is *not* the same gate as step 5: step 5 is the operator's consent, step 6 requires a **recorded reviewer approval by someone other than the agent's principal** |

So the plugin should go **as far as step 4 and stop**, surfacing the draft's identifier as
`Evidence` and letting the incident sit at `needs_human` with a `blocked_reason` — which is what
the field is for (`models.py:121-123` defines `BLOCKED_ON_APPROVAL` / `BLOCKED_ON_INPUT` /
`BLOCKED_ON_DIAGNOSIS`).

**Two honest problems with that plan, both worth stating rather than hiding:**

- There is no status that means *a change is in review*. `needs_human` with a draft link is a
  lossy encoding: `handover.open_work()` buckets by `blocked_reason` and diagnosis presence
  (`backend/handover.py:110-147`), so an incident awaiting a code review renders identically to
  one awaiting a decision. This is G-9.
- Steps 1–4 are not an `ActionSink` shape at all. `execute(signal, action, payload) ->
  ActionResult` is a single bounded RPC; a checkout-build-test-push cycle is a minutes-long
  supervised job. Forcing it through `execute` means it either blocks the request handler or
  lies about completion. This is G-10.

**What is NOT in scope even with G-1:** the source's autonomous implementation lane — a board
transition arming a supervised loop with PID/heartbeat and TERM→KILL→respawn
(`source: sops/auto-implement-watch.md`, `source: sops/auto-implement-supervisor.md`). That is a
second product living inside a plugin, and its supervision mechanism is generic enough that
building it privately is the wrong place. Excluded deliberately.

---

## 5. Rotation source — internal roster data answering `on_shift()`

### 5.1 The adapter

One `RotationSource`, id namespaced, backed by the internal on-call service. Its shape is read,
not inferred: the team-shifts call takes a team name plus a start and end date and returns
shifts carrying `startDateTime` / `endDateTime` / `oncallMember[]`, and the tool documents one
non-obvious constraint — it returns only shifts *starting* within the range, so a caller must
widen the window to find a shift that merely overlaps a date.

That constraint is the single most likely bug in this adapter, and the source already hit it:
`source: sops/oncall-rotation-check.md:15-19` queries `today - 14 days` to `today + 14 days` and
then finds the shift where `startDateTime <= now < endDateTime`. A naive same-day window returns
**empty** mid-shift, which under strict gating disarms a genuinely on-call instance.

| Concern | Value | Reason |
|---|---|---|
| Poll window | `now - 14d` → `now + 14d` | Matches the source; comfortably covers a weekly rotation whose shift started before the window |
| Cadence | Once per `rotation-check` tick (`app.json`, `every: 300`) | The tier gate is the only consumer; 5-minute staleness on "am I on call" is fine and the alternative is a paid call every 120s |
| Cache | In-process, TTL 300s, plus last-good on error | `on_shift()` is awaited inside `registry.resolve_shift()` from three routes (`routes.py:187,229,740`) and `dispatch.run_cycle` (`dispatch.py:316`); an uncached adapter multiplies calls by ~4 for one answer |
| Inner timeout | ≤ 10s | Core's `wait_for` is `DEFAULT_POLL_TIMEOUT_SECS = 15.0` (`base.py:50`, applied at `registry.py:310`) and kills the coroutine without cleaning up the plugin's sockets (§9.3) |
| `ShiftStatus.who` | The on-call login | Rendered raw in the UI and in the rotation payload — an alias, which §11.3 lists as a class the core does not redact. Acceptable *only* because it is the operator's own team roster; do not extend to arbitrary requester aliases |
| `is_fallback` | **Absent** (i.e. real) | `is_fallback` marks a *floor*, not an opinion (`providers/noop.py:86`). A real API must be heard |
| `unknown` on API failure | `on_shift=True, unknown=True` | See §5.3 |

### 5.2 Relationship to the public `schedule_file` source

They answer the same question from different authorities, and the app is built to hold both —
`resolve_shift` fans out across every registered non-fallback rotation source
(`registry.py:283-321`).

| | `schedule-file` (public) | the plugin's internal rotation source |
|---|---|---|
| Authority | A YAML file the team commits to the synced ledger repo (`providers/schedule_file.py:71,93`) | The org's system of record |
| Identity | GitHub login, from config or the local CLI (`schedule_file.resolve_login`, line 360) | Internal alias |
| Also supplies | `leader()`, `roster()`, `strict_gating()` | Nothing else — see §5.5 |
| Fails how | Under `strict_gating` (default on), indeterminate ⇒ `on_shift=False, unknown=True` (`schedule_file.py:278-290`) | Should fail the *other* way — see §5.3 |

### 5.3 The precedence rule, and the real bug it must honour

The app has a genuine, documented bug fix here and it constrains what the plugin may return. Two
mechanisms, and it matters that they are separate:

**(a) `resolve_shift` consults fallbacks only when no real source can answer**
(`registry.py:283-321`). The recorded reason: the always-on default rotation source is always
configured and always on-shift, `resolve_shift` returns the first on-shift status it finds, so a
real source correctly reporting "someone else is on call" was discarded — "the on-shift tier
armed permanently for everyone, which is exactly the failure a rotation exists to prevent.
Verified against the pre-fix code … before changing this."

**(b) `tier_states` reads `on_shift` alone; `unknown` is never an arming input**
(`rotation.py:336-360`). It used to compute `shift.on_shift or shift.unknown`, which "silently
defeated strict gating for exactly the case it was written for." The fail-open intent now lives
*per source*: "A rotation *API* returns `on_shift=True, unknown=True` (a network fault must not
disable response); the committed schedule returns `on_shift=False, unknown=True` under
`strict_gating`. Two sources, two policies, one gate that just reads the answer."

**So the precedence rule is not "internal wins". It is the existing OR, and the plugin's job is
to pick the right `unknown` polarity for its own kind of source:**

| Both configured, and… | `resolve_shift` returns | Correct? |
|---|---|---|
| Either says on-shift | on-shift (first hit wins) | Yes — a person on two rotations is on shift (`registry.py:286-287`) |
| API says someone else, file says you | on-shift | **Yes, and deliberately so.** A committed file a teammate reviewed is a legitimate override of a service the team may not have updated for a swap |
| API unreachable, file says someone else | off-shift | Yes — the file gave a definite answer |
| API unreachable, file absent | on-shift, `unknown=True` | Yes — this is why the plugin returns `on_shift=True` on fault. A network blip must not silently switch off incident response |

**C-14 restated for this adapter** (§13.2): the plugin MUST NOT register a rotation source that
reports permanently on-shift. If it cannot resolve the operator's identity it returns `unknown`
with the fault polarity above — it does not guess yes. That is the same trap `is_fallback` exists
to keep out.

**The gap the plugin cannot close from here:** `authorize_action`'s off-shift refusal reads
`schedule_file.resolve_now()` **directly and synchronously** — not `resolve_shift`
(`rotation.py:187-210`, and the docstring says why: "an await here would make every action
authorization depend on a provider round trip"). Confirmed by grep: `schedule_file` is imported
in `rotation.py` at four sites, `registry.resolve_shift` at none. So an instance whose *only*
rotation source is the plugin's gets `_definitely_off_shift() → False` on the no-file path, and
the write gate's off-shift refusal — the one added after reproducing a real leak
(`rotation.py:225-239`) — never fires for it. This is G-11.

### 5.4 The leader/worker split, and what it maps onto

The source skillset package models this as **two agent specs**, and they were diffed rather than
described. The delta is exactly four things:

| Delta | Leader | Worker |
|---|---|---|
| `allowedTools` | includes `write` | omits `write` |
| auto-approved version-control ops | add, commit, checkout and branch-create on top of the read-only set | read-only set only (status, diff, log, show, branch) |
| auto-approved review creation | the review-revision creator is allowed | absent |
| Everything else — the MCP tool *capability* set, the shared-knowledge loader hook, the data-store MCP with its read-only profile | **identical** | **identical** |

(`source-skillset: agents/inscope-leader.agent-spec.json`,
`source-skillset: agents/inscope-worker.agent-spec.json`.) The important reading: capability is
the same on both; only *auto-approval* differs. Worker can do everything leader can — it just has
to ask. That is the same algebra as `observe < propose < act`.

**Where it lands in the app: the `leader:` key in the committed schedule, and nowhere else.**
`rotation.is_primary()` (`rotation.py:281-307`) resolves `schedule_file.leader()` and compares it
case-insensitively to this instance's login; the committed file wins whenever it names anyone,
and local `primary_instance` decides only when the file is silent. The recorded reason is worth
quoting because it is the same reason the split exists at all: `primary_instance` defaults to
`True` and lives in each instance's own config, so "on a team where nobody opted out, EVERY
instance claimed the primary tier — verified with three default installs … Concurrent prunes are
worse than concurrent claims: a claim wastes a turn, a prune deletes knowledge."

The mapping is lossy in a specific way:

| Source concept | App equivalent | Fidelity |
|---|---|---|
| Leader-role job registration (`source: sops/wiki-sync.md`, `source: sops/package-scan.md` both register with a leader role) | `TIER_PRIMARY: ("ops-mission-control/ledger-hygiene",)` (`rotation.py:77`) | Good — one tier, one job, gated by `is_primary()` |
| All-role + on-call-gated | `TIER_ON_SHIFT: (dispatch, reconcile)` (`rotation.py:76`) | Good |
| Leader gets *write authority* the worker must ask for | **Nothing.** `is_primary()` gates *which crons run*, not what mode they run in | **Lossy.** `app_mode()` and `load_rules()` are per-instance local config (`rotation.py:153-169`) with no shared-file override |

That last row is the substance of the "a team cannot centrally set differential autonomy" gap. It
is G-12, and note it is **not** a plugin capability: it is a ceiling-shaped key in the file
everyone already reads, exactly as `leader:` was. The plugin must not attempt it, because C-14
forbids the plugin reaching into `data/config.json` to add rules.

### 5.5 One thing the rotation adapter should NOT do

An internal directory group can enumerate team membership (`source: scripts/setup.sh:307-341`
resolves members from the POSIX group by directory lookup and merges with manual entries;
`source-skillset: context/doc-sync-schema.yaml:8,21-23` documents the group as the source of
truth with hand-merge allowed). It is tempting to have the plugin populate `roster()` from it.

Don't. `roster()` reads the committed file (`schedule_file.py:402`, surfaced through
`rotation._roster_safely()` at line 400), and a roster that appears without anyone committing it
is a roster nobody reviewed. Keep membership operator-authored; contribute only "who holds the
pager right now", which is the one fact a service genuinely owns.

---

## 6. Knowledge bridges — runbook import, shared-memory sync, wiki publication

### 6.1 What the public ledger actually is, so the mapping is honest

`LedgerEntry` (`backend/models.py:428-510`) is `{entry_id, pattern, fix, fingerprints[],
provider_keys[], confidence, trust, use_count, first_seen, last_used, source}`. Four properties
bound every bridge:

- **Content-addressed id** over `lower(pattern)|lower(fix)` (`compute_id`, line 449). Two people
  who learn the same lesson produce the same id, so a git merge is a dedupe. `read_entries`
  merges duplicate ids on read because "git resolves that as *both lines present*"
  (`ledger.py:86-95`).
- **Capped and pruned.** `MAX_LEDGER_ENTRIES = 500`; `hygiene()` dedupes, decays confidence after
  `DECAY_AFTER_DAYS = 90`, then **deletes** the tail (`ledger.py:57,64,367-425`).
- **Matched only by fingerprint or provider key** (`ledger.match`, line 203), capped at
  `MAX_MATCHES_PER_SIGNAL = 3`. There is no topic, tag, category or author dimension.
- **`source` is a write-only field.** `LedgerEntry.source` defaults `"agent"` (`models.py:446`),
  accepts a caller-supplied value at `routes.py:786`, and grep finds exactly one other assignment
  (`ledger_index.py:156`) and **no reader** — no ranking, no filter, no UI. So "just set
  `source="runbook"`" does not create a tier; it creates an unread string.

### 6.2 Bridge 1 — runbook import

| | |
|---|---|
| **Protocol** | `EvidenceSource` — **not** the ledger |
| **Maps onto** | `Evidence(source=…, kind="runbook", title=<section heading>, body=<section text>, url=<permalink>)`, rendered into `investigation_brief` (`dispatch.py:566`) |
| **Direction of authority** | Runbook → agent, read-only. Nothing flows back |
| **Caps** | `evidence_budget_hint` (must be a `Mapping`, ideally a `MappingProxyType` — `base.py:88-91` checks `isinstance(hint, Mapping)`, and the earlier `dict` check silently ignored every correctly-written hint). Suggest `{timeout_secs: 5, max_calls: 2, max_bytes: 16384}` — a local-ish read, so narrow the default hard |
| **Selection** | Match on `signal.resource` and `signal.labels`, never full text: an unranked grep across a dozen runbooks returns a dozen hits and the budget truncates to whichever sorted first |

**Why `EvidenceSource` and not the ledger.** Same reasoning §14's G-3 applies, and it is the right
answer here too. A runbook section is 1–3 KB of prose. Forced into `pattern`/`fix` it (a) blows
through the 500-row cap, (b) gets its confidence decayed after 90 days of non-use even though a
runbook does not go stale by not being read, (c) becomes eligible for `hygiene()`'s prune — the
app would delete the team's documentation to stay under a cap — and (d) lands in `ledger.jsonl`
and gets pushed, which §12.4's C-11 forbids for internal content. Evidence, by contrast, passes
the single redaction chokepoint (`registry.gather_evidence`, `registry.py:323-388`) and is never
persisted.

**The one thing the plugin should carry across from the source's runbook format:** the
machine-owned vs human-owned marker convention. It is real and consistently applied — HTML
comments marking auto-updated regions, manual regions, and mixed ones
(`source: knowledge/runbooks/inscope-data-platform.md:8,428,476,482,496,503`, and the same markers
in three other runbooks; exactly four of the fourteen runbook files carry them). The plugin's
importer should **prefer human-authored regions** when the budget forces a choice — the
auto-generated half is a commit log, and dated commit summaries are the least useful thing to put
in a diagnostic prompt.

### 6.3 Bridge 2 — external shared-memory service sync

The source mirrors its lesson stream into a semantic/graph memory service
(`source: sops/mindcraft-sync.md`). Its model is stated in one line worth reproducing:
**"Mirror, not migrate."** The git file stays the source of truth; the service is a searchable
index on top; if the cron fails or the service is down, "nothing is lost — the next tick retries,
and git is intact" (lines 19-21).

| | |
|---|---|
| **Protocol** | `EvidenceSource` for the **read** direction. The **write** direction has no Protocol — see G-13 |
| **Read maps onto** | `Evidence(kind="prior-lesson", …)` — semantically similar lessons the local fingerprint match could not find, since `ledger.match` is exact-key only |
| **Direction of authority** | git → service, one way. The service is derived and disposable |
| **Cadence** | The source runs at :15 past each weekday hour, deliberately after the append job and after the pull (line 11). Any plugin equivalent must keep that ordering or it mirrors a pre-pull view |
| **Idempotency** | `sha1(author + normalized-text)` in a local state file, "safe across re-runs and multi-machine races" (lines 26-28) |
| **Content gate** | Skip records containing secret markers; redact every external-party email before mirroring (lines 29-31) |

The app already has a **local** semantic layer that does this job with no external service:
`ledger_index.py` projects `ledger.jsonl` into the core vector store, with a deferred-embedding
batch pass and a local, deliberately-unsynced import cursor (`ledger_index.py:17-27,52-56`). Its
reasoning is the same as the source's: text is derivable-to-vectors, vectors are not committable.

**So the honest recommendation: the plugin should not build this bridge in v1.** The local index
already answers "find me something similar", and an external mirror adds a second copy of
internal operational text living outside the machine — which §12.4's C-11 enumerates as the thing
that must not leave. The one case that justifies it is cross-team retrieval (a lesson a
*different* team learned), and that is a different product decision, not a plugin feature. Note
also that the source's own mirror carries an explicit external-PII redaction step, which tells
you the corpus does contain third-party contact data — so this bridge cannot be built without the
redaction work in §11 landing first.

### 6.4 Bridge 3 — wiki publication, and the authority rule reproduced exactly

The source's rule was checked rather than paraphrased. It appears twice, consistently:

> "Merge new content into the runbook, **preserving operational notes / agent-only comments** that
> do not exist on the wiki. **Wiki is upstream for authoritative docs; runbook is upstream for
> operational annotations.**" — `source: sops/wiki-sync.md:20`

> "Wiki is the 'official' source but the runbook is the operational truth. Never overwrite
> incident notes, workarounds, or lessons with wiki content." —
> `source-skillset: agent-sops/wiki-sync.sop.md:23-24`

Four operational rules travel with it, all from `source: sops/wiki-sync.md:27-30`: never delete
operational notes absent from the wiki; never overwrite a runbook wholesale — always merge; silent
when nothing changed or nothing is watched; a fetch failure skips **that entry only** and does not
block the rest.

And the direction is worth being precise about: **the bridge as built is inbound only.** The
watch list is empty in the live config (`source: doc-sync.yaml:93`), the SOP's step 2 says an
empty list finishes silently as "the expected no-op path" (`source: sops/wiki-sync.md:16`), and
the state file shows no watched wikis (`source: doc-sync-state.json:57`). There *is* one
hand-authored outbound artifact — a wiki-markup runbook under
`source: knowledge/runbooks/` with a companion wiki URL noted at
`source: sops/smartsheet-ingestion.md:224` — but no SOP publishes it. **This is an inference:**
no automated write path was found, so publication appears to be manual.

| | |
|---|---|
| **Protocol (inbound)** | `EvidenceSource`, same as §6.2 — a wiki page is a runbook with a different fetch |
| **Protocol (outbound)** | **None.** Publishing a document is not ack/resolve/comment/silence on a `Signal`, and its target is a page, not a work item. G-14 |
| **Direction of authority** | Wiki authoritative for facts; local knowledge authoritative for operational annotation. Merge, never replace |
| **What must NOT be bridged** | Outbound, essentially everything the plugin holds: ledger `fix` text (§12.4 — pasted commands, hostnames, aliases), per-incident dossiers, evidence bodies, requester or assignee identity, anything from the vector index. Inbound: page *history* and author metadata — the plugin needs the current text, and importing revision authors imports personnel data for no diagnostic gain |

**Recommendation: v1 is inbound-only, and say so out loud.** Outbound publication has no Protocol,
no redaction boundary, and no idempotency story — and its natural payload is precisely the material
§12.4's C-11 forbids leaving the machine.

### 6.5 The curated-runbook vs incident-derived-lesson distinction — who adds the second tier?

[README.md](README.md) §4.6 is right that the app lacks this. The source has two genuinely
different shapes, and both were verified:

| | Lesson stream | Curated pattern files |
|---|---|---|
| Artifact | `source: lessons/shared-lessons.jsonl`, 2168 rows | `source: knowledge/patterns/remediation.md` (180 lines; table columns Pattern / Confidence / Trust Level / Fix / Last Used) + `common-pitfalls.md` (388 lines) |
| Record shape | `{ts, author, category, lesson, tags[]}` — verified by parsing line 1 | Human-editable markdown, topic-organised |
| Written by | An hourly append job, machine | Promotion, or a human editing directly |
| Retrieval | Grep, or the mirror's semantic search | A hand-maintained keyword→file index across 10 domains, with literal grep instructions (`source: knowledge/patterns/README.md:17-19`) |
| Promotion rule | — | **"Promote recurring lessons (≥3 across authors on same topic)"**, then remove the individuals (`source: sops/knowledge-dream.md:23`) |

Note what the curated file's columns are: Confidence and **Trust Level** (`High` / `Verified`).
Those are the app's `confidence` and `trust` fields (`models.py:141-152`) — the app took the
*curated* tier's vocabulary and applied it to the *lesson* tier, which is exactly why the two
collapse into one.

**Answer: the public core needs the tier first; the plugin must not add it.** Three reasons, each
checkable:

1. **The promotion rule is unimplementable in the app today, and the missing piece is public.**
   "≥3 across authors" needs an author on each entry. `LedgerEntry` has no author field, and
   `use_count` counts *matches*, not *people* — so "three teammates independently hit this" and
   "one flaky alarm fired three times" are the same number. The identity needed already exists in
   the public core: `schedule_file.resolve_login()` (`schedule_file.py:360`) is public, already
   used by `rotation.is_primary()`, and is the same git identity the shared ledger is keyed on.
2. **A plugin-side tier would be invisible to everything that reads knowledge.** `ledger.match`
   (fingerprint / provider-key only), `handover.recurring_patterns` (`use_count >=
   MIN_USES_TO_RECUR = 2`, top `MAX_PATTERNS = 8`), `investigation_brief`, `hygiene`, and the
   vector projection are all core. A second store the plugin keeps privately is a second store
   nothing consults.
3. **`hygiene()` would delete it.** The prune is unconditional past 500 rows and orders by
   `(-use_count, trust, confidence, last_used)` (`ledger.py:411-423`). A curated entry nobody has
   needed yet sorts to the bottom. Any tier that must survive pruning has to be *known* to the
   pruner — and the pruner is public.

**The smallest public-core change that unblocks the plugin** is not a whole second tier. It is two
fields the pruner and matcher already have room for: an `author` on `LedgerEntry` (set from
`resolve_login()`) so corroboration is countable, and a `curated: bool` (or `source` finally being
*read*) that `hygiene()` exempts from prune and decay. Roughly 20 lines. With those, the plugin's
contribution becomes what it should be: an importer that files internal curated content as
`Evidence` per §6.2, and nothing that needs its own storage at all. This is G-15.

---

## 7. What must stay OUT of the plugin

The test: **would a stranger's public install want this, and would putting it in the plugin make
the public core's behaviour unauditable on its own?** The second clause is the registry's own
stated reason for ADD-only: "if a companion could shadow `cloudwatch`, then auditing what the
public core does would require auditing every companion too" (`registry.py:8-11`).

| A plugin author will be tempted to put this in the plugin | It belongs in the public core because | Where |
|---|---|---|
| **A per-item human opt-out** (the source's plain-language phrase, `source: sops/ticket-dispatch.md:38`, `source: sops/ticket-investigation.md:70`) | "Leave this one thing alone" is universal. Half of it *must* be core anyway: `run_cycle` filters on `state == firing` and the owned-set and nothing else (`dispatch.py:336-355`), so with no core filter hook the plugin's only lever is dropping the item inside its own `poll()` — invisible to the operator, unauditable, and it silently loses coverage | §14 G-2 |
| **Provenance on a claim** (`claimed_by`) | One string field on `Incident`, set by the two claim paths. Confirmed absent (grep returns nothing outside the planning docs). The mesh work the backlog already scopes needs it, and every public multi-instance install needs it | §12.2 C-9 |
| **A credential-expiry watchdog** | The *trigger* is provider-agnostic (an adapter reporting repeated auth failure) and the *mechanism* already exists: `cron_pause`/`cron_resume` are declared in `app.json` and `rotation-check` already pauses and resumes its own tier. A public install with an expired cloud profile has the identical failure. Only the internal renewal *command string* is plugin-side | §10.3 C-5 |
| **Cron self-health** ("is the thing that polls providers still alive?") | The app can tell you a provider is broken but not that its own dispatch cron died — the one conflation it is otherwise rigorous about refusing (`registry.poll_health`'s whole docstring draws exactly this distinction, `registry.py:273-281`). `cron_list` is already in `app.json`'s tool list | §14 G-4 |
| **A verbatim propose/approve backend** | `Incident.proposed_action` is declared (`models.py:390`) and serialized, and grep finds **no writer and no reader** — only the model definition and the TypeScript type. The property that matters, *what you draft is what you post*, is the source's own hard rule ("The drafted comment text is the *contract*… RE-ASK with a fresh draft", `source: sops/ticket-investigation.md:213`). A plugin-side approval flow would be a second approval concept the core's audit trail knows nothing about | §13.4 R-7 |
| **Last-known-good snapshot semantics on poll** | Provider-agnostic and half-built: `poll_health` already distinguishes "failed" from "quiet" but nothing keeps the prior snapshot, so a rate-limit response still reads as "everything cleared". A plugin that snapshots privately fixes it for one source and leaves the shared conflation in place | Public |
| **Age / staleness as a dimension** | `_incident_row` already carries `age_from` and `updated_at` (`handover.py:96-108`); nothing buckets on them. Age is org-neutral and the data is already owned | [README.md](README.md) §4.10 |
| **Redaction patterns for internal identifiers** | Genuinely internal *content* — but the delivery seam is `CredentialPolicy` on the **platform** group, not the ops group. Putting the regexes in an ops adapter means duplicating the stack per adapter and drifting | §11.3 R-1/R-2, and §17 open question 1 |
| **A per-fingerprint deterministic resolver** (the source's formulaic-handler class, `source: sops/key-expiration-handler.md`) | The *mechanism* — "this fingerprint has a four-step deterministic check, run it instead of a model turn" — is the highest-leverage generic thing in the whole parity set and is org-neutral. Only the *specific* checks (which metrics store, which metadata table) are internal. Build the hook publicly, the handlers privately | G-16 |
| **Idempotent-write rate limiting** | Tempting to keep private since only the plugin writes to a system of record — but `routes.py:400-460` posts a fresh comment per call for *every* sink including the public ones. A public GitHub-issue spam bug is the same bug. C-15 asks the plugin to self-guard, which is right as a *floor*, not as the only guard | §13.2 C-15 |

**Two things that genuinely belong in the plugin and should not be pushed upstream in a fit of
generosity:** the specific queue/folder identifiers and category-shaped routing tuples
(`source: knowledge/runbooks/maxis-intake-folders.md` is a table of opaque UUIDs — meaningless
publicly), and the internal auth broker invocation. §10.3's C-3 is explicit that the plugin must
invoke the broker's own client and never copy its cache.

---

## 8. Staged build order

### 8.1 Why this order

Three ordering constraints, in decreasing strength:

1. **Read before write.** Every write capability depends on reading the item correctly first — the
   source's own gates are all pre-write reads (re-page guard, self-resolution check, link
   validation, assignee check, opt-out phrase; `source: sops/ticket-investigation.md:48-73`). A
   write sink built before the read path has nothing to check against.
2. **A stage blocked on a public-core change must not be started.** §14's cost table says
   everything except R-7 is small — but small is not shipped. Building against an unlanded hook
   produces a plugin that fails at runtime per-adapter, reported as a poll error, which
   `companion.py:85` calls "the worst kind of bug, because everything appears to work."
3. **Prove the seam with the cheapest possible adapter first.** The seam was verified once with a
   throwaway package (`planning/features.md:440-448`) but never with a real credential, a real
   expiry, or a real internal payload.

### 8.2 The stages

| # | Stage | Protocols | Blocked on public core? | Why here |
|---|---|---|---|---|
| **1** | **Resolver-group monitoring, read-only.** One `SignalSource` polling the internal queue for a group (all, or per alias — the owner's first named sub-item at `features.md:449-450`). No sink, no evidence, no rotation | `SignalSource` | **No** | The smallest first slice that stands alone — see §8.3 |
| **2** | **Item-thread evidence.** One `EvidenceSource` returning the correspondence / worklog history as ordered `Evidence` items | `EvidenceSource` | **No** — G-3's `kind="thread"` convention is a near-zero-change convention, not a code dependency | Unblocks every pre-write gate in stage 5. Also the first stage that exercises the redaction chokepoint with real internal free text, which is where §15.3's "redaction matrix over real identifier shapes" test actually gets written |
| **3** | **Rotation source.** §5's adapter, with the ±14-day window and the fault polarity | `RotationSource` | **No** for arming (`resolve_shift` → `tier_states` works today). **Yes** for the *write* gate — G-11 means `authorize_action` cannot see this source | Must precede any write stage: an off-shift instance writing to a system of record is the failure `rotation.py:225-239` was fixed for, and this stage is where G-11 stops being theoretical |
| **4** | **Intake-folder monitoring.** A second `SignalSource` over the intake folders (all, or per alias — the owner's second sub-item) | `SignalSource` | **No** | Same shape as stage 1, different query, so it costs little once stage 1 is real. Second because the folder→ops-vs-dev routing rule (`source: knowledge/runbooks/maxis-intake-folders.md:27-32`) is a genuine classification decision that benefits from stage 1's normalization being settled first |
| **5** | **Comment + ack sink.** A1, A2, A9 — with disclosure prefix, first-observation guard, and refuse-on-owned all in the adapter | `ActionSink` | **Partially.** Functions today; but R-6 (§13.3) is what makes narrowing load-bearing rather than advisory, and R-4/R-5 (§12.4 — ledger-write redaction, pre-push veto) must land before any internal text can reach a synced ledger | First write stage, and the two safest verbs only. Register as a **separate sink id** from stage 7's so a rule naming one does not grant the other (§13.2) |
| **6** | **Runbook import.** §6.2's `EvidenceSource`, human-authored regions preferred | `EvidenceSource` | **No** | Pure read, no new risk, and it makes stage 5's comments better-grounded. Cheap enough to slot here rather than compete with the sink work |
| **7** | **Resolve + structured closure.** A3 with `payload["closure_code"]` / `payload["root_cause"]` under the existing `resolve` verb; explicit-verb-in-rule required | `ActionSink` | **No for the mechanism** (payload is already free-form, `base.py:225`); **yes for the vocabulary** if closure must be its own verb rather than a payload key — that is G-1 | Deliberately after stage 5 has run in `propose` for a while. The first verb that asserts something false about the world if wrong |
| **8** | **Draft-only code-change lane.** Steps 1–4 of §4.4, stopping at unpublished draft, surfacing the identifier as evidence | Neither — G-9 and G-10 | **Yes, hard.** Needs a status meaning *a change is in review* and a long-running-job shape `execute()` does not have | Last, and correctly so. Highest complexity, highest blast radius, and the only stage whose value the operator can get by hand in the meantime |
| **—** | Severity change (A5), reassignment (A7), create (A8) | `ActionSink` | **Yes — G-1** | Explicitly *not* numbered. Each needs a vocabulary the core does not have, and each has a failure mode (silent no-op on blocked severities; well-formed-but-wrong assignee; 60-items-an-hour) that argues for waiting even after G-1 lands |

### 8.3 The smallest first slice that delivers standalone value

**Stage 1 alone: one `SignalSource` over the internal resolver-group queue, read-only, no sink
registered at all.**

What the operator gets on day one, with nothing else built and no public-core change:

- Internal work items appear on the board and on the Signals tab with per-source poll health
  (`registry.poll_health`).
- The dispatch heartbeat claims them and runs a real investigation with the existing
  metrics/log evidence path — the metrics-store side of most internal ops work is already covered
  by the public CloudWatch adapter.
- Every claim gets a fingerprint (`compute_fingerprint`, `models.py:269`) and a `provider_key`
  (the item id — the same choice `providers/github_issues.py:183` makes, and for the same stated
  reason: a work item does not recur, so an exact match means "we have worked this very item
  before"). So the ledger starts compounding from the first incident.
- Autonomy is `observe` by default (`rotation.DEFAULT_APP_MODE`, line 81) and the default sink is
  `noop`, which records what *would* have happened and performs none of it
  (`providers/noop.py:34,51-60`). With **no sink registered**, the plugin cannot write anywhere —
  which is also the strongest possible answer to §17's open question 6: the right first release
  ships with no write sink at all.

The design constraint that makes this slice small and also makes it correct: §11.2's C-7.
`Signal.title`, `resource`, `url` and `labels` are unredacted and durably stored, and reach the
model prompt via `investigation_brief` without passing the evidence chokepoint. So the adapter's
`title` is a **plugin-constructed normalized shape** — item id plus a bounded structured summary —
never the human-authored subject line. Concretely:

| Field | Value | Why |
|---|---|---|
| `native_id` | the item id | Becomes `Signal.id` as `<source>:<native_id>` |
| `title` | `"<item-id> · <bounded normalized summary>"` | C-7: never the raw subject. Also feeds `compute_fingerprint` |
| `severity` | the item's severity, through `normalize_severity` | Handles `p1` / `sev1` / `SEV_3` vocabularies already (`models.py:234-250`) |
| `state` | `firing` while open | `run_cycle` filters `state == firing` (`dispatch.py:336`) |
| `resource` | the resolver group or folder label | Stable, non-free-text, and what an `AutonomyRule.resource_glob` will match on |
| `labels` | `{queue, category, folder_label, mode_hint}` — structured, low-cardinality, **no aliases, no free text** | These are what an operator pins an act-rule to via `label_match`, which is how a rule becomes expressible without a resource glob (§13.2) |
| `provider_key` | the item id | Exact-match tier in `ledger.match` outranks shape match |
| `fingerprint` | derived | Free via `Signal.create` |

Poll shape: a search filtered to the group, open statuses, rows capped at
`DEFAULT_POLL_LIMIT = 100` (`base.py:45`); metadata-only (no comment threads) on the list pass so
the cheap path stays cheap; inner timeout ≤10s under the core's 15s; cadence is the existing
`dispatch` cron at `every: 120`, no new cron. Drop items with an individual human assignee,
matching both the public adapter and the source. `configured()` must never raise —
`configured_signal_sources` treats a raising `configured()` as not configured
(`registry.py:109-125`), which silently drops the source.

### 8.4 Which stages are blocked, stated plainly

**Not blocked on anything:** 1, 2, 3 (for arming), 4, 6. That is five of eight stages, including
both of the owner's named sub-items and the whole read path. This is the useful conclusion — the
owner's actual ask is unblocked today.

**Blocked:** stage 5 needs R-4 + R-5 before internal text can reach a synced ledger and wants R-6
to make narrowing real; stage 3's *write*-gate half needs G-11; stage 7's closure-as-a-verb wants
G-1 (though the payload route needs nothing); stage 8 needs G-9 + G-10; A5/A7/A8 need G-1.
R-1/R-2 (redaction) gate any stage that returns internal free text, which is stage 2 onward — so in
practice R-1 is the first thing to land, and it is two lines.

---

## 9. Attachment — the seam as it exists today

### 9.1 The verbatim contract

Entry-point group, from `backend/companion.py:75`:

```toml
# the private package's pyproject.toml
[project.entry-points."kirocrew.ops_providers"]
inscope-ops = "inscope_kirocrew_ops.register:register_adapters"
```

```python
# inscope_kirocrew_ops/register.py
def register_adapters(registry) -> None:          # returns None; return value ignored
    registry.register_signal_source(TicketQueueSource())
    registry.register_action_sink(TicketSink())
    registry.register_evidence_source(TicketThreadEvidence())
    registry.register_rotation_source(RotationApiSource())
```

The group is `kirocrew.ops_providers`, deliberately distinct from `kirocrew.plugins`
(`src/kiro_crew/platform/discovery.py:34`) so contributing an ops adapter does not require or
imply authority over the platform edition seam. Pinned by
`test_ops_group_is_not_the_platform_plugin_group`.

**Adapter shape** — each object needs `id`, `display_name`, `configured() -> bool`, plus its
role method. Optional attributes the core reads by `getattr`, so they are opt-in: `detail`,
`config_fields`, `secret_fields`, `evidence_budget_hint` (must be a `Mapping`, ideally a
`MappingProxyType`), and for a `RotationSource`, `is_fallback`.

### 9.2 Admission policy — what runs before `ep.load()`

Order in `install_companion_adapters` (`companion.py:147-204`), and the order is the whole
point:

1. `provider_entry_points()` enumerates the group (with the 3.9 dict-vs-`select` API split
   handled).
2. `_admitted(ep)` → `seed_default_policy()`, `load_admission_policy()`,
   `evaluate_admission(ep, policy)`. **No plugin code has run yet.** `evaluate_admission`
   reads the plugin manifest from the *installed distribution's files*
   (`src/kiro_crew/platform/admission.py:572`), never by import.
3. Only on `allowed` does `ep.load()` execute.
4. `callable()` check, then `register(registry)` inside a `try`.
5. Every outcome — `denied`, `failure`, `success` — is audited as `ops_provider.admission`.

The checks `evaluate_admission` actually applies (`admission.py:654-718`), in order:

| Check | Behaviour | Relevance to an internal plugin |
|---|---|---|
| `banned` kill-switch | Always wins, any mode; NFKC + casefold normalized so a Unicode-lookalike name cannot evade | This is the remote-disable lever a fleet gets |
| Fast path | If mode is `open`, no `approved`, no `require_signature`, no `capability_ceiling` → **admit** | A default install admits the plugin with no review |
| `approved` allowlist | Both manifest name AND `ep.name` must appear | The realistic internal posture |
| `require_signature` | HMAC over canonical manifest bytes, key held by the *policy* | Available; symmetric-only today |
| `capability_ceiling` | Manifest-declared capabilities must fall inside fleet globs (`fnmatchcase`) | The right place to bound the plugin's egress |

Two asymmetries to know:

- **Fail-OPEN for the plugin, fail-CLOSED for the gate.** A rejected / unimportable / throwing
  companion is logged and skipped — `companion.py`'s module docstring gives the reasoning: it
  only ADDS signal sources, and aborting boot would take down chat and every other app. But if
  the *evaluator itself* raises, `_admitted` returns `(False, ...)` — "the gate broke" never
  reads as "the gate said yes" (`companion.py:117-144`).
- **A missing policy file fails closed globally.** `load_admission_policy` returns
  `_fail_closed_policy()` = `MODE_ENFORCE, require_signature=True, approved=[]`, which admits
  nothing (`admission.py:320,479-509`). So deleting the policy file silently disables the
  companion. That file is on the keystone floor (`_CREW_SECRET_LEAVES` includes
  `admission_policy.json`), so the agent cannot delete it — but an operator can, and the
  failure mode is "my internal adapters vanished", diagnosable only from the gateway log plus
  the Settings companion row.

### 9.3 Already built vs. the plugin author's own work

| Already built in public core | Plugin author must build |
|---|---|
| Entry-point discovery + 3.9/3.10 split | The plugin manifest (name, publisher, version, capabilities) if the fleet enforces anything |
| Admission before load, audited | Every adapter class, including a `configured()` that never raises |
| ADD-only registration; core wins on id collision | Unique ids that do not collide with `cloudwatch`, `pagerduty`, `datadog`, `github-issues`, `webhook`, `noop`, `schedule-file` |
| `poll_all` fan-out with a 15s per-source timeout + error map | Its own inner timeouts (the core timeout kills the coroutine; it does not clean up the plugin's sockets) |
| `gather_evidence` redaction chokepoint + budget clamping | Return raw text and *nothing else* — see §11 |
| Autonomy gate resolved before `execute` | Do **not** re-police authority; do gate on `supported_actions()` yourself (§13.3) |
| Keystone secret storage + write-only API | Its own token-refresh story (§10) |
| `Signal.create` normalization + fingerprinting | Always use `Signal.create`, never the raw constructor, or fingerprints diverge from every built-in |
| Companion row in `/state` and Settings | Nothing |

---

## 10. Credentials and auth — the hard case is the expiring token

### 10.1 What the keystone store gives you, exactly

`backend/secrets.py` is a real credential store and its properties bound what the plugin can
and cannot do.

- File is `<crew_home>/ops_mission_control_secrets.json`, mode `0o600`, plus an owner-only
  DACL on Windows with **fail-loud lockdown** (`_write` unlinks and re-raises if
  `restrict_to_owner` fails — `secrets.py:184-198`).
- The filename is registered in `security._CREW_SECRET_LEAVES`, which puts it on the shared
  read+write sensitive-path floor. Verified the agent genuinely cannot reach it:

```
is_sensitive_path(<crew_home>/ops_mission_control_secrets.json)        -> True
is_sensitive_bash_command("cat <crew_home>/ops_mission_control_secrets.json")
    -> "Blocked: command accesses sensitive credential path"
```

- **Write-only over the API.** `describe_secrets` returns a bullet placeholder or empty string
  per field, never a value (`secrets.py:272-279`). `PUT /providers/<id>/secret` refuses a
  field the adapter did not declare in `secret_fields`, so the file cannot become generic
  agent-inaccessible storage (`routes.py:653`).
- `PUT /providers/<id>/config` **refuses** any key that appears in `secret_fields` and audits
  the refusal (`routes.py:497-514`). That guard exists because `data/config.json` is served
  over `GET /api/apps/{name}/config` (`src/kiro_crew/apps/routes.py:1406`).

**Contract clause C-1.** Every internal credential the plugin needs MUST be declared in
`secret_fields` and MUST reach disk only through `put_secret`. The plugin MUST NOT read a
credential from an environment variable, from a file inside the app's own data dir, or from
anywhere `config.json` can name. Governance guidance on secrets names plaintext environment
variables as an anti-pattern, and the app's own config file is served unauthenticated.

**Contract clause C-2.** Where the internal system supports role-based auth against ambient
credentials, the plugin MUST use the ambient chain and store nothing — the posture
`backend/providers/cloudwatch.py` already takes. Governance guidance prefers roles over
long-lived keys, and the app already advertises "AWS uses your ambient credential chain, no
key is ever stored" as a product property.

### 10.2 The hard case: the internal auth broker

This is the part the current design has no answer for, and it is not a small gap.

Internal auth material is not a static API token. It is a short-lived credential obtained by
an **interactive** ceremony (browser redirect, hardware-token touch) that expires on the order
of hours and whose renewal cannot be automated from inside a daemon. Everything in
`secrets.py` assumes the opposite: a value an operator types once into a Settings field that
stays valid until they rotate it.

Four concrete failures follow, none hypothetical given how the app is wired:

1. **Silent, self-repeating failure.** `poll_all` catches the per-source failure and returns it
   in `errors` (`backend/registry.py:193-195`); `/signals` surfaces it verbatim. But the
   Signals tab polls only behind an explicit "Poll now" button (by design — polls cost money).
   So an expired credential produces a source-level error every 120 seconds that nobody sees.
   `run_cycle` returns a `skipped_reason` only for the *unconfigured* case
   (`backend/dispatch.py:310-316`), not the expired one. A watched board becomes an unwatched
   board that looks identical to a quiet one — the exact conflation this app is otherwise
   rigorous about refusing.
2. **`configured()` lies.** The Protocol's contract is "True when this adapter has everything
   it needs to poll." With an expiring credential, `has_secrets(...)` returning True means "a
   token is stored", not "a token is valid". The Providers list renders `ready` for an adapter
   that cannot make a single call.
3. **No renewal channel exists.** The daemon cannot open a browser. The agent must not be
   handed the credential. The only actor who can renew is the operator at their terminal, and
   the app has no mechanism to ask them.
4. **Nothing pauses the fleet.** The app declares `cron_pause` / `cron_resume` in
   `app.json:43-44` and uses them **only** for rotation tier arming. A credential-expiry
   watchdog is the second obvious consumer and does not exist.

### 10.3 The contract for expiring credentials

**C-3 — never copy the broker's cache.** The plugin MUST NOT read, copy, or re-serialize the
internal auth broker's own credential cache into the keystone store. A copy has a different
lifetime from the original, is invisible to whatever revokes the original, and creates a
second thing to leak. Invoke the broker's own client library or CLI and let the broker own the
cache.

**C-4 — auth state is a first-class adapter state, distinct from configured.** The plugin MUST
distinguish three states and the core MUST be able to render all three:

| State | `configured()` | What the operator must do |
|---|---|---|
| Not set up | `False` | Fill in config |
| Set up, credential valid | `True` | Nothing |
| Set up, credential **expired** | `True`, but every call fails | Run the renewal command |

The third row has no representation today. The minimal public-core change is to let an adapter
declare a health reason: an optional `auth_status() -> tuple[str, str]` returning
`(state, remediation_text)`, read by `getattr` in `registry.catalog()` so no existing adapter
changes, surfaced on the Providers card. `ProviderInfo` already carries an **unused**
`labels: dict[str, str]` field (`backend/providers/base.py:166`) that could carry it without a
new field at all — the cheapest version, one `getattr` plus one line of UI.

**C-5 — expiry MUST pause the on-shift tier, not just report.** Detection is only half. When
the plugin's `auth_status()` reports expired for N consecutive cycles, the app SHOULD pause
`tier_crons.on_shift` and notify **once per 24h** naming the renewal command, then resume
**only what it itself paused** when auth returns. Single-authority-per-state matters:
`rotation-check` already owns the on-shift tier, so a watchdog that resumes a cron the
rotation checker paused would re-arm an off-shift instance. Hand-back must be explicit — the
watchdog resumes to "not blocked by me" and lets `rotation-check` make the arming decision on
its next tick.

**C-6 — the agent gets brokered text, never a credential, and this already holds.**
`investigation_brief` (`dispatch.py:497-513`) states unconditionally that the agent has no
credentials and must not run a provider CLI. That paragraph is unconditional deliberately — it
used to sit inside `if claimed.evidence:`, so the one case that most needed it never got it.
The internal plugin inherits this for free and MUST NOT weaken it: gateway gathers
(credentialed, bounded, redacted) → brief carries text → agent reasons. An agent with its own
internal credential would be a second credential holder whose reads nothing redacts.

**On `register_secret_backend`.** The seam at `secrets.py:226` has no production caller
anywhere in the repo (only `tests/test_security.py:310`). It is available for a broker-backed
backend, but it is process-global and last-writer-wins, and swapping it changes storage for
*every* provider including the public ones. If the plugin uses it, it MUST preserve keystone
semantics for fields it does not own — otherwise installing the internal plugin silently
relocates the operator's PagerDuty token.

---

## 11. Redaction — a plugin returning raw text is the DESIGNED behaviour

### 11.1 Where the chokepoints actually are

There is exactly one chokepoint for provider payloads, and the test suite pins it by source
inspection:

```python
# tests/test_providers.py:506
adapter_calls = source.count("src.gather(")
self.assertEqual(adapter_calls, 1,
    "an adapter's gather() must be invoked only from gather_evidence, "
    "which is where redaction happens")
```

Inside it (`registry.py:284-311`) the order is load-bearing and was fixed after being wrong:
**pre-trim to `max_bytes * 2` → redact → truncate to `max_bytes`**. Truncating first let a
redaction marker (longer than what it replaces, ~1.09× measured on an all-credential body)
push the final body over budget; the pre-trim bounds the regex work so a misbehaving adapter
cannot hand over unbounded text to scan.

The second boundary is Slack: `slack_out.py` redacts `signal.title` (line 168),
`signal.resource` (177), and thread detail (258).

**The unredacted paths, enumerated honestly:**

| Path | Redacted? | Consequence for an internal plugin |
|---|---|---|
| `EvidenceSource.gather()` → brief | **Yes**, centrally | Safe by construction |
| Incident → Slack board / thread | **Yes** | Safe |
| `Signal.title` / `resource` / `url` / `labels` → `/state`, `/signals`, `/incident` JSON | **No** | Dashboard shows raw provider text |
| `Signal.title` → `investigation_brief` line 445 | **No** — only *evidence* passes the chokepoint | **A ticket title reaches the model prompt unredacted** |
| `diagnosis` / `resolution` free text → index | **No** (`routes.py:258-262` just `str()`s it) | Model-authored text stored raw |
| `pattern` / `fix` → `ledger.jsonl` → **git push** | **No** | §12.4 — the worst one |
| `HttpError` message | `redact_tokens` only | Tokens yes, aliases no |

The `Signal.title` gap matters more internally than publicly. A CloudWatch alarm name is a
resource string. An internal ticket title is free text a human wrote, and it routinely
contains an alias, an employee identifier, or a customer reference. It reaches
`investigation_brief` without passing `gather_evidence`.

### 11.2 What "return raw text" implies for review

The `Evidence` docstring promises an adapter "cannot forget" to redact, and the README tells
plugin authors "**Evidence is redacted for you** at a single chokepoint. Return raw text".
That is deliberate and correct: centralizing redaction means one reviewable implementation
instead of N adapters each getting it slightly wrong.

Three review consequences, which invert the reviewer's usual instinct:

1. **A plugin adapter returning an unredacted internal ticket body is NOT a finding.** A
   reviewer who flags it is asking the author to duplicate the chokepoint, which is worse — a
   second implementation drifts. The review question is not "did you redact?" but "does every
   byte you return exit through `gather()`?"
2. **The real finding is any egress the plugin creates itself.** A plugin that logs a payload,
   writes a debug file, posts to a channel of its own, or raises an exception whose message
   embeds the body has escaped the chokepoint. Concretely: an `ActionSink.execute` returning
   `ActionResult(error=<raw provider body>)` puts unredacted text on the `/incident/action`
   response, since `routes.py` echoes `result.error` verbatim (line 425).
3. **Anything the plugin routes into a `Signal` field or a ledger entry bypasses redaction
   entirely.** This is the sharpest edge. `Signal.title` is not redacted anywhere, so a plugin
   author's instinct — "put the ticket summary in the title, the requester in a label" — is
   exactly the wrong move.

**C-7.** A companion `SignalSource` MUST treat `Signal.title`, `resource`, `url` and `labels`
as **operator-visible, unredacted, and durably stored**. Internal free text belongs in an
`Evidence` body (redacted and budgeted), never in a `Signal` field. Titles MUST be a
normalized shape the plugin constructs — item id plus a bounded structured summary — not the
raw human-authored subject line.

### 11.3 Which patterns the plugin must contribute, and where

The core credential stack (`_CREDENTIAL_PATTERNS`, `src/kiro_crew/security.py:3445`) covers AWS
keys, PEM blocks, Slack/Telegram/GitHub/GitLab/Stripe/SendGrid/OpenAI shapes, JWTs, plus an
entropy-gated bare-40-char-secret detector. The app adds provider-token shapes in
`secrets._TOKEN_PATTERNS` (PagerDuty prefixes, Datadog bare and prefixed keys, and a generic
`bearer|token|api_key|app_key|application_key` carrier whose separator is optional — that last
fix landed because an `Authorization: Bearer` header with a *space* separator was reaching the
model prompt in clear text).

None of that covers internal identifiers, which are not credentials but are the thing that
must not leave the machine:

| Class | Why the core misses it | Where it turns up |
|---|---|---|
| Employee aliases / logins | Ordinary lowercase words; no distinctive shape | Ticket assignee, correspondence, host logs, commit authors |
| Internal ticket / work-item ids | Structured but org-specific | Titles, comment bodies, deep links |
| Internal hostnames and service endpoints | Not a credential; the exfil heuristics may even *exempt* them | Host log lines, stack traces |
| Internal auth broker cookies / session blobs | Opaque, no prefix; the bare-secret heuristic needs 40 base64 chars | HTTP debug output, reproduced request traces |
| Employee identifiers in query results | Bare integers | Data-warehouse evidence bodies |

**The delivery mechanism already exists in core and this app does not use it.**
`platform.interfaces.CredentialPolicy` (`src/kiro_crew/platform/interfaces.py:173-201`) is
documented as "Public default = the AKIA/ASIA credential patterns and exfil URL patterns in
`security.py`. **The companion adds internal token/cookie regexes.**" It reaches egress sites
through `platform.redact_via_context`, which is fail-closed on `PlatformCompositionError` (a
non-standalone host that could not compose its companion does **not** silently downgrade to
the OSS baseline) and degrades to bare `security.redact` on any transient adapter fault.

**Required public-core change R-1 (security-load-bearing).** Both ops chokepoints MUST switch
from `security.redact` to `platform.redact_via_context`:

- `registry.py:255` — `from kiro_crew.security import redact as core_redact` → the context shim
- `slack_out.py:58` — same

Two lines, three properties worth the cost: (a) internal patterns become declarative in the
companion's `CredentialPolicy` rather than duplicated per adapter, (b) an internal host that
fails to compose its platform companion now *fails closed* on redaction instead of quietly
falling back to public patterns, (c) `src/kiro_crew/security_posture.py` already lists
`platform/context.py` under the redact-via-context helpers, so the drift guard stays satisfied.

What R-1 does *not* fix: the `CredentialPolicy` seam is on `kirocrew.plugins`, the platform
group — not `kirocrew.ops_providers`. An ops-only companion cannot supply patterns; the
internal *platform* companion must.

**Required public-core change R-2.** Either (a) document that the internal ops plugin requires
the internal platform companion to be installed too (cheapest, no code), or (b) add an
ADD-only `register_redaction_patterns(patterns)` to `OpsProviderRegistry`, applied inside
`gather_evidence` after `core_redact`, same ADD-only rule — patterns can only be added, and
there is no key to disable them (matching the `redact_tokens` precedent, which deliberately
has no policy toggle because "there is no legitimate reason to disable it, so exposing a
toggle would only create a way to get it wrong"). Option (b) is ~15 lines and is the right
answer if the ops plugin must stand alone.

**C-8.** Contributed patterns MUST be additive and MUST NOT be sourced from `config.json`. The
`exempt_exact_hosts` docstring states the principle for the analogous case: "The set is NEVER
sourced from `config.json` — an agent-writable exemption would be a hole in the redaction
ceiling, so the companion adapter is the only supplier." Same rule here.

---

## 12. Multi-tenancy and data boundaries

### 12.1 The stated scope, and why internal deployment violates it

The public core's scope is a single-user machine. That is not a disclaimer — it is
load-bearing in four places:

- Auth is "is the caller the same UID" plus a loopback token. There is no per-user
  authorization anywhere.
- The keystone secret file is `0o600` owner-only. It protects the credential from *the agent*,
  not from a second human.
- Every autonomy decision reads local `data/config.json`.
- The claim index is per-instance with a local file lock (`backend/store.py`).

Internal deployment means teammates sharing a ledger. What breaks, by severity:

### 12.2 Shared credential = shared identity, and the audit trail lies

The backend is a black box holding one credential. If the plugin authenticates as one operator
and serves an incident a teammate then acts on, every write in the internal system is
attributed to the credential holder. `Incident` records `claimed_at` but **not who or what
claimed it** — grep for `claimed_by` across the app returns nothing. The audit trail says
`caller="core:ops-mission-control"` for every action, so correlating "was this me or the cron
or my teammate?" is timestamp archaeology.

**C-9.** The plugin MUST stamp provenance on every write. Minimally: an `instance_id` (the
resolved login `schedule_file.resolve_login()` already computes) plus the trigger
(`cron:dispatch` / `manual:api` / `manual:ui`) on the incident, and the same identity in the
outbound payload where the internal system accepts an on-behalf-of field. One string field,
which also de-risks the mesh work the backlog already scopes.

**C-10.** Tool personalization and any per-user learning in the internal backend MUST be
force-disabled where the plugin controls spawn env, and MUST be rejected (adapter reports
unconfigured with a stated reason) where it does not. A shared backend that learns one user's
patterns and serves them to another is a data-boundary violation no amount of redaction
catches.

### 12.3 Autonomy config is locally writable — verified

`planning/user_manual.md:205` claims autonomy rules are "deliberately *not* settable over the
API, so a single API call can never widen the app's own authority". The app's own
`PUT /settings` honours that — it accepts only `mode`, `primary_instance`, slack keys, cycle
tuning, and the ledger-sync keys.

The **generic** app-config route does not. `src/kiro_crew/apps/routes.py:1406` mounts
`GET`/`PUT /api/apps/{name}/config`, and the PUT **replaces the whole config file** with the
request body. And the file is on no protection floor — verified:

```
is_sensitive_path(<crew_home>/apps/ops-mission-control/data/config.json)       -> False
is_sensitive_write_path(...)                                                   -> False
is_sensitive_bash_command("echo x > .../data/config.json")                     -> None
```

So `autonomy_rules` and `mode` are writable by (a) any authenticated caller of the generic
config route, and (b) any auto-approved agent shell — the second is the same hole
`_WRITE_PROTECTED_HOME_PATHS` was created to close for the *main* `config.json` and which was
never extended to app configs. On a single-user machine with an `observe` default this is
modest. On a shared internal ledger where one teammate's instance is in `act` mode against
production tooling, it is the highest-value target in the app.

**Required public-core change R-3 (security-load-bearing).** Add the ops app's
`data/config.json` to `_WRITE_PROTECTED_HOME_PATHS` (read stays allowed — the UI bootstraps
from it), or move `mode` + `autonomy_rules` alone onto the keystone floor in a small
`ops_mission_control_autonomy.json` written only by the app's own `PUT /settings`. The second
is cleaner: the unauthenticated GET keeps working for everything the UI needs while the
authority ceiling sits where neither the agent nor the generic route can reach it. This
benefits the public core independently of the internal plugin.

### 12.4 The shared-ledger-in-git question

What the sync tracks is already tight and the mechanism is correct. `_ensure_repo` writes a
generated `.gitignore` of exactly:

```
*
!.gitignore
!ledger.jsonl
!rotation.yaml
```

so `incidents/index.json`, the per-incident markdown, `config.json`, and the vector index
**physically cannot** be pushed (`ledger_sync.py:178-196`, `TRACKED_FILES` at 404). The
reasoning is recorded: the index is last-writer-wins on a shared key, so syncing it would let
two instances each believe they own an incident.

But `ledger.jsonl` is the one artifact leaving the machine that **nothing sanitizes**.
`POST /ledger` does no redaction; `_stage_and_commit` commits verbatim. And a `fix` field is
the single most likely place for a pasted command line, an internal hostname, a resolver-group
name, or an alias — because that is literally what a fix looks like.

**C-11 — what must never reach a git remote.** Enumerated, not gestured at:

| Must never be committed | Why |
|---|---|
| Internal ticket / work-item bodies and correspondence | Free human text; unbounded content |
| Employee aliases and identifiers | Personnel data; irrecoverable once in N clones |
| Internal hostnames, service endpoints, account identifiers | Infrastructure disclosure |
| Query results from internal data stores | May contain customer or personnel records |
| Host log excerpts | Where credentials turn up by accident — logging guidance is explicit that logs must not contain secrets, which is precisely why they do |
| Any auth material, expired or not | An expired credential still discloses shape, issuer and holder |

**Required public-core change R-4 (security-load-bearing).** Redact on the ledger *write*
path, not just at the sync boundary. Write-path beats sync-path for two reasons: the entry is
already on local disk and in the vector index by the time sync runs, and an operator who later
enables sync retroactively publishes everything written before. Concretely: run
`redact_via_context` (post R-1) plus `redact_tokens` over `pattern` and `fix` inside
`_handle_post_ledger` before `LedgerEntry.create`. This changes the content-addressed id
(`sha256(lower(pattern)|lower(fix))`), which is *correct* — two entries differing only in a
redacted secret should dedupe to one — but it must land before any ledger has entries in the
wild or ids shift under existing installs.

**Required public-core change R-5.** Add a pre-push content scan that **refuses** the push.
There is precedent: `push()` already refuses when `rotation.yaml` holds conflict markers, and
that refusal was added after a real three-teammate incident where a conflicted schedule
reached the remote and left every teammate unable to parse who was on call.
`security.get_credential_patterns()` exists as a public accessor specifically so downstream
scans can reuse the canonical regexes without coupling to the private name — the web-deploy
pre-publish scan already does this.

**C-12.** For an internal deployment the sync remote MUST be an internal git host, and the
plugin MUST refuse to sync to a remote it cannot classify as internal. Fail-closed: an
unrecognized remote is external. The check belongs in the plugin (it knows what internal looks
like) but needs a core hook — a `pre_push_veto()` the plugin can register, which is the
smallest version of R-5.

### 12.5 Co-primary shifts reintroduce the race the design claims to have removed

A data-boundary problem, not just duplication. `rotation.yaml` supports `who: [bob, carol]` and
`tests/test_schedule_file.py:430` pins that both are on call. The mesh-arbitration spec is
marked SUPERSEDED on the grounds that "a shared `rotation.yaml` makes exactly ONE instance
eligible, so there is no race to arbitrate" — which holds only for a scalar `who`. With a
list, two instances both claim, both mint `INV-1` (the counter is local), two Slack threads
appear, and each ledger entry under-counts the recurrence. Nothing warns the operator that
writing a list disables the guarantee.

**C-13.** An internal deployment MUST use scalar `who` until cross-instance claim arbitration
ships. The minimal core change is a warning (or refusal) on a multi-login window, surfaced on
the rotation card — small, and it stops an operator silently opting out of the only thing
standing between them and double-dispatch.

---

## 13. Autonomy — a stricter default, expressed without weakening the gate

### 13.1 Why internal writes need a stricter default than the public core's

The public core's argument for `observe`-by-default is recorded in `backend/rotation.py:11-19`:
the source team "could reason about which intakes were safe because they had built them; a
stranger's first install has no such basis." Every reason applies more forcefully internally,
plus three that are new:

1. **Blast radius is other people's work queues.** A public install's worst case is
   acknowledging its own alarm. An internal install's worst case is commenting on,
   reassigning, or resolving a work item a human owns, in a system of record other teams read.
2. **Writes are attributable to a person, and misattributable.** With no `claimed_by` (§12.2)
   and a shared credential, an agent write reads as a human write from the credential holder.
   The source workflow treated bot-identity disclosure as MANDATORY on exactly the surface the
   app writes to — and the app has no disclosure mechanism at all (grep across backend,
   `SKILL.md` and all six SOPs finds zero mention). That absence is a genuine finding,
   recorded nowhere.
3. **Irreversibility.** Comments on internal work items are typically not deletable. "Undo" is
   unavailable, so the mode ladder is the only control.

### 13.2 How to express it in the existing gate without weakening it

The gate's algebra is `effective = min(app_mode, matching_rule_mode)` over
`observe < propose < act`, with three enforcement points verified in `authorize_action`
(`rotation.py:213-267`), in order:

1. `action not in VALID_ACTIONS` → refuse.
2. `_definitely_off_shift()` → refuse. Deliberately narrow: refuses only when the schedule
   *positively* says someone else owns the shift, never when it merely cannot tell, so a solo
   install and a broken schedule both stay usable.
3. `mode < act` → refuse. Then no matching act-rule → refuse. Then the rule's `actions` set not
   granting this action → refuse.

Every outcome, allow and deny, is audited. And `AutonomyRule.from_dict` **refuses** an act-rule
with neither `resource_glob` nor `label_match` (line 120), so "act on everything from source X"
is not expressible.

**The correct move is to narrow using what is already there, adding no new authority concept:**

| Mechanism | How the plugin uses it | Weakens the gate? |
|---|---|---|
| `supported_actions()` returning `frozenset()` | The plugin's sink advertises **no** actions until the operator supplies an on-behalf-of identity. Exactly the `providers/pagerduty.py:184-189` precedent: "Writes need the From header; without it PagerDuty rejects them, so we advertise no actions rather than failing at execute time." | No — narrowing only |
| A distinct sink id per authority level | Register a comment-only sink separately from a write sink. A rule naming one does not grant the other | No — rules are per-source |
| `execute()` self-check on `supported_actions()` | Refuse at the sink, as pagerduty already does at line 194 | No — a second floor |
| Narrow `label_match` on the plugin's own labels | The plugin emits labels the operator can pin a rule to (e.g. a queue name), making rules expressible without a resource glob | No |

**C-14 — the plugin narrows, never widens.** The plugin MUST NOT: call `authorize_action`
itself and act on a permissive answer; read `mode` and branch on it; register a
`RotationSource` that reports permanently on-shift (that is what `is_fallback` is for — and
note `resolve_shift` was fixed after the always-on default masked every real rotation,
`registry.py:216-246`); or reach into `data/config.json` to add rules.

**C-15 — the plugin's `execute` MUST be idempotent or refuse.** A real gap: nothing
rate-limits the outbound comment path. `POST /incident/action` with `action: comment` posts a
fresh comment every call, with no check for an identical prior note (`routes.py:349-400`). The
source workflow's key-expiration handler exists precisely because that happened. Since the
plugin writes to a system of record, it MUST carry its own first-observation guard — a stable
marker in the comment body it checks for before posting.

### 13.3 The enforcement gap worth naming

`supported_actions()` is **not enforced by the core**. `grep -c supported_actions
backend/routes.py` returns `0`. `_handle_action` resolves a sink and calls `execute` after the
autonomy gate without ever consulting what the sink says it supports. `pagerduty.py`
self-checks; `noop` advertises the whole of `VALID_ACTIONS` (`providers/noop.py:48-49` — all
four verbs since `ACTION_SILENCE` landed, per §1) and performs none; a plugin that advertises
`frozenset()` and does not self-check would still be invoked. So `supported_actions()` is
today a UI hint, not a gate.

**Required public-core change R-6.** Check `action in sink.supported_actions()` in
`_handle_action` before `execute`, returning 409 with the sink's reason. Cheap, closes the gap,
and makes the C-14 narrowing mechanism load-bearing rather than advisory.

### 13.4 `propose` mode has no backend — and this is where an internal plugin needs it most

`Incident.proposed_action` is declared at `backend/models.py:325` and serialized by
`to_dict`/`from_dict`, and grep confirms **nothing ever writes or reads it**. So `propose`
mode's contract — draft the verbatim outbound text, post one structured draft, wait for a typed
approval, re-ask on edit, bump once at 24h and never auto-act — exists only as prose
instruction to the model. There is no draft storage, no approval endpoint, no timeout, and no
re-ask rule. The `awaiting_approval` blocked reason comes from a chat-slot *tool* approval
(`backend/slot_watch.py:60-71`), which gates one tool call, not the whole outbound package.

For a public install where the worst write is an alarm ack, that is a gap. For an internal
plugin writing to a system of record, `propose` is the mode the operator will run in for
months, and the property that matters most — **what you draft is what you post** — is
unenforced. An agent can silently alter the text between drafting and posting.

**Required public-core change R-7.** Give `propose` a backend: persist `proposed_action`
(verbatim text + target + action + created_at), an approve endpoint that executes **exactly
the stored text** and refuses if the text changed, and a bump-then-stop timeout. The largest of
the required changes and the one an internal plugin has the strongest claim on.

---

## 14. Seam gaps — where the four Protocols are insufficient, with minimal core change each

G-1 through G-8 came out of the contract analysis (*can this attach safely*). G-9 through G-16
came out of the capability analysis (§1–§8: *what should be in it*) and are numbered after them
so existing references stay valid. Ordered by cost within each block. "Minimal change" means the
smallest thing that keeps the ADD-only and redaction guarantees intact — not the nicest design.

### G-1. `VALID_ACTIONS` is a closed core frozenset — the plugin cannot extend its own vocabulary

`models.py:157` defined `VALID_ACTIONS = frozenset({ack, resolve, comment})` at the `e7a90677`
baseline. Both `routes.py:395` and `rotation.py:220` validate against it. An `ActionSink` declares
a *subset* via `supported_actions()`; there is no path to a further verb even for the plugin's own
sink. So the internal plugin cannot create a work item (the detection-sweep case), change
severity, or record a structured closure code as its own verb. It can only annotate and close
things that already exist.

**Updated by the capability pass (§1):** the set is now **four** —
`ACTION_SILENCE` landed after the baseline, with a mandatory bounded expiry clamped at the
authorization boundary rather than in the adapter (`models.py:170-199`, `EXPIRING_ACTIONS`,
`MAX_SILENCE_SECS = 24h`, applied at `routes.py:437-442`). Three consequences, all in the
plugin's favour and none weakening the gap:

- The gap's *argument* is unchanged: the set is still closed and still validated in two places.
- The plugin's usable vocabulary is one verb wider than this section originally said, and it
  happens to be the safest one — a wrong `silence` expires by itself, which is the exact property
  §4.1 uses as the inclusion test for every internal write. §4.2 A4 puts it to work.
- **Structured closure no longer needs G-1 at all.** `execute(signal, action, payload)` already
  takes an arbitrary `payload` dict (`base.py:225`), so the plugin can carry `closure_code` and
  `root_cause` under the existing `resolve` verb with zero core change (§4.2 A6). What still
  needs G-1 is severity change, reassignment, and create — §4.2 A5/A7/A8, each of which §8.2
  argues for deferring even *after* G-1 lands.

This undercuts the ADD-only seam specifically for the action role: three of four Protocols are
genuinely extensible, and one is not.

**Minimal core change.** Let a sink declare `extra_actions() -> frozenset[str]` (read by
`getattr`, default empty), and validate against `VALID_ACTIONS | sink.extra_actions()`
**after** the sink is resolved — which means moving sink resolution above the action check in
`_handle_action`. Namespace plugin verbs so they can never collide with a future core verb, and
require `AutonomyRule.actions` to name them explicitly (no empty-set "any action" grant for a
namespaced verb, since "any" was written when "any" meant three read-ish verbs). ~25 lines.
Without this, parity with the source workflow's write vocabulary is unreachable.

### G-2. No exclusion / human opt-out layer at any level

`run_cycle` filters on `state == firing` and the owned-set, and nothing else
(`dispatch.py:318-338`). Exclusion is only expressible as *inclusion narrowing* inside one
adapter's config, which cannot express "watch this whole queue EXCEPT these three". There is no
per-signal mute, no per-fingerprint suppression, and — the one that matters internally — **no
way for a human to write something on a work item that makes the agent leave it alone.**

The source workflow's plain-language opt-out in the latest correspondence is the mechanism a
human uses to take an item back. Internally that is not a nicety: it is how a person stops an
agent from commenting on a ticket they are hand-working.

**Minimal core change.** A `signal_filters` config block read by `run_cycle` after the firing
filter: `deny_fingerprints`, `deny_resource_globs`, `deny_title_regex`, plus a board-level mute
button writing into it. Config-file-only for the same reason as autonomy rules. ~40 lines plus
a UI button. The *phrase*-based opt-out then becomes plugin-side: the plugin's `poll()` drops
an item whose latest correspondence carries the opt-out marker — which requires G-3.

**Strengthened by the capability pass (§7).** The reason this cannot be pushed entirely into the
plugin is sharper than "not a nicety": with no core filter hook, the plugin's *only* lever is
dropping the item inside its own `poll()`, which is invisible to the operator, unauditable, and
silently loses coverage — the board shows nothing and nothing explains why. `run_cycle` filters on
`state == firing` and the owned-set and nothing else (`dispatch.py:336-355`), so half of this
capability must be core regardless of who wants it. The source's own version is a plain-language
phrase checked in two independent places (`source: sops/ticket-dispatch.md:38`,
`source: sops/ticket-investigation.md:70`), and it is a *pre-write* gate — which is why §8.2
sequences stage 2 (thread evidence) before stage 5 (the first write sink). This hook is also the
natural home for G-16's deterministic resolver.

### G-3. `Signal` cannot represent a work item with a discussion thread

`Signal` carries id/source/title/severity/state/fired_at/resource/url/labels/fingerprint and
nothing else — no body, no description, no comment history (grep for `description` in
`models.py` returns nothing). So on any source with a discussion, the investigator diagnoses
from the title and the resource alone. It cannot notice a human already answered, cannot detect
that its own previous comment covered the same ground, and cannot honour an opt-out phrase
written in a comment. All three are pre-comment gates the source workflow treats as mandatory.

**Minimal core change — and the answer is *not* to add a body field to `Signal`.** Adding one
would put unredacted internal free text on `/state`, `/signals` and the brief (§11.2). Instead:
the plugin returns the thread as `Evidence` items from its own `EvidenceSource`, which is
redacted, budgeted, and already rendered into the brief. What the core is missing is only
*ordering and labelling* so the agent knows it is reading a chronological discussion rather
than unrelated context — one `kind` convention (`kind="thread"`, gathered oldest-first) plus a
brief line that says so. Near-zero core change; the honest note is that the opt-out check must
then happen inside the plugin's `poll()`, because `poll` runs before evidence is gathered.

### G-4. No health/liveness channel from an adapter

`configured()` is a boolean and `poll_all` reports an exception string. There is no way for an
adapter to say "configured, reachable, but my credential expired 40 minutes ago and here is the
command to fix it." §10.3 covers why that matters most for internal auth. The same gap means
nothing can detect that the app's own dispatch cron has stopped — the app can tell you a
*provider* is broken but not that the thing which polls providers has died, which is the one
invariant it is otherwise rigorous about.

**Minimal core change.** Optional `auth_status()` / `health()` read by `getattr` in
`catalog()`, surfaced through the existing unused `ProviderInfo.labels`, rendered on the
Providers card. ~20 lines.

### G-5. No pre-push veto on the shared ledger

Covered as R-5/C-12. `push()` already has the shape — it refuses on a conflicted
`rotation.yaml`. The plugin needs to register into that refusal, since only the plugin knows
which remotes and which content are acceptable internally.

**Minimal core change.** An ADD-only list of `pre_push_veto(paths) -> str | None` callables
consulted in `push()` before `_stage_and_commit`; any non-empty return refuses and audits.
~20 lines.

### G-6. The redaction seam is on the platform group, not the ops group

Covered as R-1/R-2. Stated here because it is a seam gap, not just a wiring bug: an ops-only
companion has no way to contribute a redaction pattern even after R-1 lands.

### G-7. `supported_actions()` is unenforced

Covered as R-6. A seam gap because it makes the plugin's primary narrowing mechanism advisory.

### G-8. `store.write_log` has no caller, so `/incident`'s `log` is structurally always empty

Not a Protocol gap, but it lands on the plugin author's desk the moment they ask "where does
the per-incident record live?". `write_log` (`store.py:385-423` at the baseline; `416-454` in the
worktree the capability half was written against) renders exactly the durable
per-incident dossier the source workflow relies on as citable precedent, and grep across
`--include=*.py` and `tests/` finds only its own definition and body. So `incidents/<id>.md` is
never written and the API advertises a field that is always empty. For an internal deployment
where an investigation may need reconstructing after the fact, that is the audit artifact — and
it is an unfinished wire, not a scope decision (nothing in `features.md` records it, though
`planning/emitters-comms-gap-research.md:613-638` does).

**Minimal core change.** Call `write_log` from `_handle_transition` when a diagnosis or
resolution is present. ~5 lines. The file is gitignored by the sync's generated `.gitignore`,
so it stays local — which is correct. Note `write_log` today renders **unredacted** provider
text and an unredacted model diagnosis, so an HTTP download route would need redaction at that
boundary and a `security_posture.py` entry.

### G-9. No status meaning "a change is in review"

An incident whose fix is a code change has nowhere to sit but `needs_human`. None of the four
Protocols carries incident lifecycle state; only the core's status grammar does.
`LEGAL_TRANSITIONS` (`models.py:66-102`) has seven statuses and no edge meaning "work is
progressing elsewhere, outside this app, awaiting a third party". `needs_human` is the only
landing spot, and it is semantically wrong in a way that is visible downstream:
`handover.open_work()` buckets by `blocked_reason` and diagnosis presence
(`handover.py:110-147`), so an incident awaiting a code review renders identically to one
awaiting an operator decision — and `slack_out.py:172-173` renders the same glyph for both.
`TERMINAL_STATUSES` is *derived* from the grammar (`models.py:114-116`), so this cannot be faked
with a label.

**Minimal core change.** One status, `STATUS_IN_REVIEW`, reachable from `investigating` and
`needs_human`, with outgoing edges to `resolved` / `needs_human` / `stale` — never directly to
`resolved` from a plugin write, because the merge is the human's. Plus a fourth `BLOCKED_ON_*`
constant, `BLOCKED_ON_REVIEW`, so the board can say "waiting on a reviewer" — the same
distinction `models.py:121-123` already draws for approval vs input vs diagnosis. ~10 lines in
`models.py` plus one bucket in `handover.open_work()`. Blocks §8.2 stage 8.

### G-10. No long-running, supervised action shape

Checkout, build, test, push a branch is minutes of work, not one RPC. `ActionSink.execute` is a
single awaited call returning `ActionResult` (`base.py:225`), invoked inline from a request
handler (`routes.py:444`) and bounded by the caller's patience with no timeout of its own. A
multi-minute job either blocks the handler or has to return `ok=True` before it has done
anything, which makes `ActionResult` a lie. There is no job, no progress, no cancel and no
supervision anywhere in the four Protocols — and the source's own version of this needed a
dedicated supervisor with PID + heartbeat and TERM/KILL/respawn
(`source: sops/auto-implement-supervisor.md`), which is a strong signal about the true shape.

**Minimal core change.** Do NOT widen `ActionSink`. The app already declares `cron_add` in
`app.json:41` with no current caller, so the cheapest correct shape is a one-shot, self-retiring
per-incident job: `execute()` returns immediately having registered the work, the job reports back
through the existing transition route, and it removes itself when done. That also delivers
"recheck this incident in N hours" from the same mechanism — see
[parity-matrix.md](parity-matrix.md) 1C-5, which independently reaches the same conclusion about
the unused `cron_add`. The alternative — an optional async-sink convention where `ActionResult`
carries an `accepted` state — duplicates machinery that already exists. Blocks §8.2 stage 8.

### G-11. A companion `RotationSource` cannot gate provider WRITES, only cron arming

`rotation.authorize_action`'s off-shift refusal calls `_definitely_off_shift()`, which reads
`providers.schedule_file.resolve_now()` directly and synchronously (`rotation.py:187-210`),
deliberately: "an await here would make every action authorization depend on a provider round
trip." It never consults `registry.resolve_shift()`, so **no registered `RotationSource`
participates**. Confirmed by grep: `schedule_file` is imported at four sites in `rotation.py`;
`registry.resolve_shift` at none.

So an instance whose only rotation source is the plugin's takes the no-schedule-file path, gets
`False`, and the write gate's off-shift refusal never fires — the exact leak `rotation.py:225-239`
records as having been reproduced before fixing ("bob off shift, `dispatch` tier disarmed, and
`authorize_action` still returned granted by rule"). It is worse than a missing feature: arming
works, so the operator sees a correctly-disarmed instance and reasonably concludes writes are
gated too.

**Minimal core change.** Have `registry.resolve_shift()` persist its last resolved `ShiftStatus`
(with a timestamp) into a small local file on each `rotation-check` tick, and have
`_definitely_off_shift()` consult it as a second definite source alongside the schedule file —
refusing only on a fresh, definite off-shift answer, preserving the existing "never refuse when it
merely cannot tell" narrowness and staying synchronous. ~20 lines. The alternative, letting an
adapter expose a synchronous resolver, duplicates the same fan-out policy in two places. Blocks
the write half of §8.2 stage 3.

### G-12. No team-wide differential autonomy

The leader/worker authority split the source models with two agent specs has no representation.
Autonomy is per-instance local state: `app_mode()` and `load_rules()` both read the instance's own
`data/config.json` (`rotation.py:153-169`), and the only shared-file authority key is `leader:`,
which gates WHICH crons run (`is_primary`, `rotation.py:281-307`) and not what mode they run in.
No Protocol touches authority — C-14 (§13.2) explicitly forbids the plugin reading `mode`, calling
`authorize_action` itself, or reaching into `config.json` to add rules, which is correct and which
also means there is no legitimate plugin-side implementation. The source expresses the split as
auto-approval deltas across two otherwise-identical agent specs
(`source-skillset: agents/inscope-leader.agent-spec.json`,
`source-skillset: agents/inscope-worker.agent-spec.json` — identical capability sets, four
auto-approval differences; see §5.4).

**Minimal core change.** An optional `autonomy_ceiling:` mapping in the committed schedule file —
`{login: observe|propose|act}` — folded into `app_mode()` as a further `min()` alongside the local
value, so it can only ever NARROW. That preserves the existing tightest-wins algebra
(`effective_mode`, `models.py:513-529`) and the "a rule cannot escalate an instance the operator
pinned to `observe`" property, while putting the fact in the file everyone already reads — exactly
the argument that made `leader:` right (`rotation.py:288-298`). ~15 lines. **It must narrow only:**
a shared file that could RAISE an instance's ceiling would be a remote privilege escalation over a
git push.

### G-13. No shape for publishing knowledge OUT of the app

All four Protocols are inbound or item-scoped: `SignalSource` and `EvidenceSource` read,
`RotationSource` answers a question, `ActionSink` writes to a work item identified by a `Signal`.
Publishing a corpus has no `Signal`, so there is nothing to pass to `execute()`. The only existing
outbound path is `ledger_sync`'s git push, whose tracked set is deliberately closed to three files
(`TRACKED_FILES` at `ledger_sync.py:404`, enforced by a generated `.gitignore` that makes
everything else physically unpushable, `ledger_sync.py:178-196`). A plugin that publishes anyway is
creating its own egress, which §11.2 names as the real review finding — the plugin would own an
unredacted exit the core knows nothing about.

**Minimal core change: leave it out.** If it must exist, the honest shape is not a new Protocol but
a registered exporter with a mandatory redaction pass and a pre-push-style veto, sharing the G-5
hook. §6.3's recommendation is that v1 does not build this bridge at all — the local vector
projection (`backend/ledger_index.py`) already answers the retrieval question without a second copy
of internal operational text leaving the machine.

### G-14. No shape for document-shaped write targets (wiki publication)

Same root cause as G-13 with a sharper edge: the target is a page, not a work item, so no `Signal`
identifies it, and the natural payload is precisely the material §12.4's C-11 enumerates as
never-committable (ledger `fix` text, per-incident dossiers, aliases, evidence bodies). Worth
recording that the source's own bridge is inbound-only in practice — the watch list is `[]` in the
live config (`source: doc-sync.yaml:93`), the SOP treats an empty list as "the expected no-op path"
(`source: sops/wiki-sync.md:16`), and the state file shows no watched wikis
(`source: doc-sync-state.json:57`). The one outbound wiki-markup artifact found under
`source: knowledge/runbooks/` has no SOP that publishes it; **this is an inference** — publication
appears to be manual.

**Minimal core change: none, deliberately.** The inbound half needs nothing new — a wiki page is a
runbook and lands as `Evidence` per §6.2, redacted and budgeted for free. For the outbound half the
minimal change is to NOT add a Protocol and instead keep the authority rule the source states
(wiki upstream for authoritative docs, local knowledge upstream for operational annotations, merge
and never replace) as an inbound-only invariant.

### G-15. `LedgerEntry` has one shape, so a curated tier is uncomputable

Not a Protocol gap but a type gap that lands on the plugin author's desk the moment they ask where
an imported runbook lives. `LedgerEntry` (`models.py:428-446`) has no author, so the source's
promotion rule — "≥3 across authors on same topic" (`source: sops/knowledge-dream.md:23`) — is
uncomputable: `use_count` counts matches, not people, so three teammates independently hitting a
failure and one flaky alarm firing three times are the same integer. And `hygiene()` prunes
unconditionally past `MAX_LEDGER_ENTRIES = 500` ordered by
`(-use_count, trust, confidence, last_used)` (`ledger.py:411-423`), so a curated entry nobody has
needed yet sorts to the bottom and gets deleted. The app also borrowed the curated tier's
vocabulary for the raw tier: the source's curated file's columns are literally Confidence and
Trust Level (`source: knowledge/patterns/remediation.md:3`), which is why the two collapse. A
plugin-side tier is not an option — `ledger.match`, `handover.recurring_patterns`,
`investigation_brief`, `hygiene` and the vector projection are all core, so a private store is a
store nothing reads.

**Minimal core change.** Two fields, not a second store: `author` on `LedgerEntry`, set from the
already-public `schedule_file.resolve_login()` (`schedule_file.py:360`, the same git identity the
shared ledger is keyed on), making cross-author corroboration countable; and `curated: bool` that
`hygiene()` exempts from both prune and decay. ~20 lines. Also worth noting `LedgerEntry.source`
already exists and has exactly one writer and zero readers (grep finds `ledger_index.py:156` and
`routes.py:786` only) — so "just set `source='runbook'`" creates an unread string, not a tier. With
`author` and `curated` in place the plugin needs no storage of its own: it files internal curated
content as `Evidence` and stays inside the existing redaction chokepoint (§6.5). This is the type
half of [README.md](README.md) §4.6's knowledge-tier gap.

### G-16. No deterministic per-fingerprint handling

The source's highest-volume, cheapest category (`source: sops/key-expiration-handler.md`: parse an
identifier from a structured title, check the alarm state, verify a notification actually went out
from a log, read an expiry from a metadata table, then resolve on OK or post a first-time-only
status) has no Protocol seat. It is not a `SignalSource` (the signal already exists), not an
`EvidenceSource` (it decides, it does not just gather), and not cleanly an `ActionSink` (its input
is a fingerprint plus a decision procedure, not an operator-authorized verb on a `Signal`).
`run_cycle` unconditionally claims and dispatches a model investigation
(`dispatch.py:299-411`), so every occurrence of a signal whose handling is four deterministic
steps costs a full turn.

**Minimal core change.** An ADD-only registry of fingerprint- or provider-key-keyed resolvers
consulted in `run_cycle` after the firing filter and before the claim:
`resolver(signal) -> tuple[str, str] | None` returning `(proposed_action, rationale)`, or `None` to
fall through to the model. It MUST route its outcome through the existing autonomy gate rather than
acting — a deterministic resolver that bypassed `authorize_action` would be a second write path
with no audit. G-2's `signal_filters` hook is the natural place for it to sit, and the source's
auto-resolve-by-default POLICY stays correctly declined (`rotation.py:13-17`) — the mechanism is
public, the handlers are the plugin's (§7).

### Cost summary

| Change | Lines | Security-load-bearing | Blocks the plugin? | Which §8.2 stage |
|---|---|---|---|---|
| R-1 redact via context | 2 | Yes | Yes | 2 onward |
| R-2 ops-group pattern hook | ~15 | Yes | Yes, if standalone | 2 onward |
| R-3 protect autonomy config | ~10 | Yes | No, but shared-ledger risk | — |
| R-4 redact ledger writes | ~10 | Yes | Yes | 5 |
| R-5 / G-5 pre-push veto | ~20 | Yes | Yes | 5 |
| R-6 / G-7 enforce supported_actions | ~10 | Yes | No | 5 (wants) |
| G-1 extra_actions | ~25 | No | Yes, for parity | A5/A7/A8; 7 only if closure must be a verb |
| G-2 signal_filters + mute | ~40 | No | Yes, internally | — (day-two, all stages) |
| G-4 auth_status | ~20 | No | Yes | — |
| G-8 call write_log | ~5 | No | No | — |
| G-9 `in_review` status + `BLOCKED_ON_REVIEW` | ~10 | No | Yes, for stage 8 | 8 |
| G-10 one-shot self-retiring job | ~30 | No | Yes, for stage 8 | 8 |
| G-11 sync last-good shift for the write gate | ~20 | **Yes** | Yes, for the write half of stage 3 | 3 (write) |
| G-12 `autonomy_ceiling:` in the shared schedule | ~15 | **Yes** (narrow-only) | No | — |
| G-13 knowledge export | — | Yes | No — declined in §6.3 | — |
| G-14 document-shaped writes | 0 | Yes | No — declined in §6.4 | — |
| G-15 `author` + `curated` on `LedgerEntry` | ~20 | No | Yes, for a curated tier | 6 (to file as knowledge rather than evidence) |
| G-16 deterministic fingerprint resolvers | ~30 | No | No | — (cost reduction) |
| R-7 propose backend | large | Yes | No, but the mode is hollow | 5, 7 (quality of) |

Everything except R-7 is small. The honest read: the *seam* is close, and the gap is not
architectural — six small guarantees were specced and not wired, and the capability pass added
eight more of the same size. Note the shape of the second block: G-13 and G-14 are gaps whose
recommended resolution is *not to close them*, and G-9 through G-12 and G-16 are all under ~30
lines. Only G-11 and G-12 are security-load-bearing, and G-11 is the one that currently misleads —
arming works, so an operator reasonably concludes writes are gated when they are not.

---

## 15. Testing and verification for a plugin that cannot be built or CI-tested here

### 15.1 The constraint, stated precisely

The public tree is gated by `scripts/scrub-lint.sh`, whose working-tree scan fails on internal
domains, hostnames, account numbers, ticket ids and internal tool invocations, and which
self-tests by planting one probe per pattern family. So the plugin's code, its fixtures, and
even its test names cannot live here. `planning/features.md` states it directly: "The internal
adapters themselves are out-of-tree by design and cannot be built here."

The verification story therefore splits three ways, and the split has to be written down or it
will be assumed away.

### 15.2 Layer 1 — contract tests the public core owns and must publish

The core already has the right tests for the *seam* (14 in `tests/test_companion.py`, ordered
by what would hurt most if broken: rejected code never runs; a broken evaluator denies;
ADD-only holds; a bad companion is never fatal). What it does **not** have is a reusable kit a
plugin author runs against their own adapters.

**Deliverable T-1: publish a contract-test kit in the public core**, as a small importable
module (not test files — the plugin's CI must be able to import it). It must assert, for any
adapter object:

| Assertion | Why it is the right thing to pin |
|---|---|
| `configured()` never raises, in any state | `configured_signal_sources` treats a raising `configured()` as NOT configured (`registry.py:104`), which silently drops the source |
| `id` is non-empty and not a core id | `_add` refuses an empty id and refuses a collision; both are warnings, not errors |
| `poll()` returns only `Signal`s built via `Signal.create` | Fingerprints must be computed identically or the ledger never matches |
| `poll()` respects its own inner timeout under 15s | The core's `wait_for` kills the coroutine; it does not clean up the plugin's sockets |
| `gather()` honours the passed `budget` | `for_source` clamps a hint but cannot make an adapter obey it |
| `evidence_budget_hint` is a `Mapping` and can only narrow | The `isinstance(hint, dict)` bug silently ignored every correctly-written `MappingProxyType` hint |
| `execute()` refuses an action outside `supported_actions()` | Until R-6 lands, this is the only enforcement |
| `execute()` never puts a raw provider body in `ActionResult.error` | `routes.py` echoes it verbatim — an egress the plugin creates |
| No `Signal` field carries unredacted free text | C-7; the one rule a plugin author will get wrong |

**Deliverable T-2: a public reference companion**, already effectively proven —
`planning/features.md` records the seam was verified end-to-end "with a real installed
throwaway package: admitted under the open policy, refused under an enforcing one, core
adapters intact either way." Promote that throwaway into a checked-in, buildable example
(public-safe: a fake ticket source over a local JSON file). It becomes the thing the kit is
tested against and the thing a plugin author copies.

### 15.3 Layer 2 — what the private package's own CI must run

| Test class | What it proves | Notes |
|---|---|---|
| Contract kit (T-1) against every adapter | Seam conformance | Must be a blocking gate |
| Admission matrix: open / enforcing-allowlisted / enforcing-not-listed / banned / no-policy | The plugin is reachable when intended and unreachable when not | The `no-policy` case is the surprising one — fails closed globally |
| Redaction matrix over **real** internal identifier shapes | Every contributed pattern actually fires | The test that cannot exist publicly, and the most important one. Precedent: the Datadog prefixed-key and application-key misses were found only with a live tenant's credentials, because every synthetic fixture used bare hex |
| Expired-credential path | `auth_status()` reports expired, the cron pauses, the notification fires once per 24h, resume only un-pauses what it paused | Needs a genuinely expired credential, not a mock |
| Ledger content scan | No contributed content survives the write-path redactor | Assert on the committed bytes, not on the API response |
| Two-instance git roundtrip against a real remote | Sync, conflict reconcile, schedule-conflict refusal | The public core found **4 bugs this way that mocked tests missed** (`features.md:526-546`). Mocks are not sufficient here and the record says so |
| Idempotent write | Calling `comment` twice produces one comment | C-15; there is no core guard |

### 15.4 Layer 3 — what only a human can verify, and the discipline that makes it honest

Some properties are not automatable and must be a signed pre-release checklist rather than a
hope:

- Provenance: an action performed by the plugin is attributable to the right person in the
  internal system.
- Disclosure: an outbound comment identifies itself as agent-authored.
- Off-shift: a teammate's instance cannot write while the schedule says someone else owns the
  shift (the public core verified this by *reproducing the leak first* — "bob off shift,
  `dispatch` tier disarmed, and `authorize_action` still returned granted by rule on
  cloudwatch").

**The discipline that carries across the boundary.** The most valuable thing in this app's
history is not any test — it is the habit of *reproducing the failure before fixing it*,
recorded at each fix site. The `tier_states` `or unknown` bug, the bare `omc-*` cron names that
made the whole tier mechanism inert, the three-default-installs-all-primary bug, the
space-separated bearer token leak reaching the model prompt, the 408 unit tests that passed
while `run_cycle`'s pre-filter discarded every recurrence. Each was found by driving the real
thing, and each is documented as "verified before fixing". The private package's review
standard MUST be the same, because it is the only standard that survives having no shared CI: a
claim in the private package's changelog carries weight only if it names what was observed.

### 15.5 What must be pinned in the *public* repo to protect the plugin

The plugin cannot test the core, so the core must pin the properties the plugin depends on.
Three tests to add here:

1. The ops chokepoints route through `redact_via_context` (source-inspection, same style as
   `test_redaction_is_the_only_path_out_of_an_adapter`).
2. `PROVIDER_GROUP` and the `register_adapters(registry) -> None` signature are stable — a
   rename silently orphans every installed companion.
3. `TRACKED_FILES` and the generated `.gitignore` content are exactly what they are today — a
   widening here silently starts publishing local state.

---

## 16. Distribution — installed alongside a public core it must not fork

### 16.1 Shape

A separate, independently-versioned Python distribution installed into the **same interpreter**
as `kirocrew`, contributing one entry point. Not a fork, not a patch, not a vendored copy.
`companion.py`'s reasoning for entry points over a config path is the constraint that fixes the
shape: "A filesystem path to import would be a new, unaudited code-loading channel in an app
whose whole security story is that the agent cannot reach its own configuration.
`importlib.metadata` entry points mean the only way to contribute an adapter is to *install a
package* — an action outside the agent's reach and visible to `pip list`."

That last clause is the distribution requirement in one line: **installation must be an
operator action, visible in the package list, outside the agent's reach.**

### 16.2 Build and vending

Built and vended through the internal build system as an internal package, consumed through the
internal package channels. Third-party-package governance is the governing constraint in the
*other* direction: the public `kirocrew` core is 3P from the internal perspective, so it must
arrive through the approved import path and be kept current there — not pinned to a private
snapshot, because a private pin is a fork with extra steps.

The plugin's own dependency on the core must be a **range**, not a pin:
`kirocrew >= X.Y, < X+1.0`. A `==` pin means every core release breaks the internal install and
the pressure to fork becomes irresistible.

### 16.3 Versioning and the compatibility contract

The core is at `0.1.2` (`pyproject.toml:7`, `src/kiro_crew/__init__.py:7`). The platform seam
has an explicit `CONTRACT_VERSION = 1` (`src/kiro_crew/platform/context.py:65`) and
`bootstrap_context` **refuses to compose** on mismatch with the message "rebuild the companion
against this core" (`src/kiro_crew/platform/bootstrap.py:154`). The ops seam has **no
equivalent**. So a companion built against today's `SignalSource` and loaded into a core that
changed `Signal.create`'s signature fails at runtime, per-adapter, reported as a poll error —
the failure mode the module docstring elsewhere calls "the worst kind of bug, because
everything appears to work."

**Required public-core change R-8.** Add `OPS_PROVIDER_CONTRACT_VERSION` to
`backend/providers/base.py`, have the plugin declare its target in its manifest capabilities,
and check it in `_admitted` **before** `ep.load()` — so an incompatible companion is refused by
the same audited gate rather than half-working. The single cheapest thing that keeps the plugin
from ever needing to fork, at ~15 lines.

Versioning rules once that exists:

| Core change | Contract version | Plugin action |
|---|---|---|
| New optional `getattr`-read attribute (`auth_status`, `extra_actions`, `evidence_budget_hint`) | unchanged | Adopt when convenient |
| New required Protocol method | **bump** | Rebuild |
| Signature change to `Signal.create`, `Evidence`, `ActionResult`, `ShiftStatus` | **bump** | Rebuild |
| New core adapter id | unchanged, but ADD-only means the core wins | The plugin must not have squatted a plausible future core id — namespace yours |
| Redaction chokepoint moves | unchanged | Nothing (that is the point of centralizing it) |

### 16.4 The three ways this becomes a fork, and the guard for each

| Fork pressure | Guard |
|---|---|
| "I need a fourth action verb" | Ship G-1 (`extra_actions`). Without it, the plugin's only path is patching `models.VALID_ACTIONS` — a fork of the security-relevant frozenset both the route and the gate validate against |
| "I need internal redaction patterns" | Ship R-1 + R-2. Without them the plugin either duplicates the whole regex stack inside each adapter (drifts immediately) or monkey-patches `security.redact` — a fork of the redaction ceiling, which would also defeat the posture drift guard |
| "I need the ledger not to publish internal content" | Ship R-4 + R-5. Without them the plugin's only option is disabling sync entirely, which removes the app's headline team feature internally |

All three are small. All three are cheaper than one fork.

### 16.5 Install, upgrade, uninstall — operator-visible behaviour

**Install.** Install the private distribution into the same environment. On next gateway boot,
`get_registry()` installs public adapters first, then companions (`registry.py:329-341`) —
order pinned by `test_public_adapters_are_installed_before_companions`, and load-bearing
because ADD-only means the incumbent wins. The Settings Providers card then shows "Adapter
package installed: <name>" via `companion_summary()`, which deliberately does **not** load
plugin code — it reports what is *installed*, a different question from what was *admitted*.
That distinction exists because "no companion installed" and "companion installed but rejected"
look identical in the provider list and need completely different fixes.

**Diagnosing a rejection.** Two places: the gateway log (`companion %r rejected by admission
policy: %s`) and the audit trail (`ops_provider.admission`, outcome `denied`, with the reason).
The UI says to check the log. For an internal deployment, surfacing the reason string in the
Settings row would save every operator the same log dive — a small, worthwhile addition.

**Upgrade.** Independent of the core. Adapter changes take effect at next gateway boot (the
registry is a process-global built on first use with no reload path). No manifest rewrite, no
cron re-registration — the plugin contributes adapters, not crons.

**Uninstall.** Uninstalling the distribution removes the entry point and the adapters disappear
at next boot. **Credentials do not disappear.** The keystone file lives outside the app folder
— that is what makes it unreachable to the agent — and the README already warns: "It therefore
**survives uninstalling the app** — use Revoke in Settings first if you want a credential
gone." Doubly important for a credential against internal production tooling.

**C-16.** The private package's uninstall documentation MUST instruct Revoke-then-uninstall,
and MUST state that a synced ledger's history is not affected by uninstall — anything already
pushed stays pushed in every clone.

### 16.6 Kill switch

Worth stating because it is the property that makes the whole arrangement acceptable: a fleet
can disable the plugin **without touching the operator's machine's packages**, by adding its
name to `banned` in the admission policy. The ban always wins in any mode, is checked before
`ep.load()`, and is name-normalized (NFKC + casefold + strip) so a Unicode-lookalike or case
variant cannot evade it. The policy file sits on the keystone floor, so the agent cannot edit
it. That is a real remote-disable control and it is already built.

---

## 17. Open questions

Each of these changes the work, so they are worth answering before committing to a plan.

1. **Does the internal deployment ship the internal PLATFORM companion (`kirocrew.plugins`) as
   well as the ops companion?** If yes, R-1 alone delivers internal redaction patterns via the
   existing `CredentialPolicy` seam and R-2 is unnecessary. If the ops plugin must stand alone,
   R-2 (an ADD-only pattern hook on `OpsProviderRegistry`) becomes required. This single answer
   changes the redaction work from 2 lines to ~15.
2. **What is the actual admission posture on internal machines** — is a policy shipped with
   `mode=enforce` and an approved allowlist, or is the seeded permissive default left in place?
   Under the default the plugin is admitted with no review and the manifest/signature/capability
   machinery is inert. Under enforce the plugin MUST ship a manifest or be refused, and someone
   must own the allowlist.
3. **Can the internal auth broker be invoked non-interactively at all** once an initial ceremony
   has completed (a cached-credential refresh), or does every expiry require a human? If refresh
   is possible the plugin can self-heal and C-5's cron-pause watchdog is a fallback. If not,
   pause-and-notify is the primary mechanism and needs building first.
4. **Does the internal system of record accept an on-behalf-of / actor field on a write?**
   C-9's provenance clause is only fully satisfiable if it does; otherwise every agent write is
   attributable solely to the credential holder and the honest mitigation is a mandatory
   disclosure prefix in the comment body — which the app has no mechanism for today, and that
   disclosure gap is recorded nowhere.
   **Narrowed by the capability pass.** The internal ticketing write tool's schema was read: it
   exposes `assignee` and `requester` as `{namespace, value}` but nothing that distinguishes "an
   agent acting for this person" from "this person". If there is genuinely no actor field, §4.3's
   disclosure prefix is not a nicety — it is the only provenance mechanism, which raises it from
   convention to a hard requirement enforced inside the adapter's `execute()`.
5. **Is the shared ledger's git remote internal-only, and who owns that decision?** C-12's
   fail-closed remote classification needs a definition of internal the plugin can test — a
   stable URL shape to match on, or an operator-maintained allowlist.
6. **Should the propose-mode backend (R-7) block the internal plugin's first release, or ship
   after?** It is the largest change and the internal write path has the strongest claim on it —
   but the plugin can ship useful in observe mode with no write sink registered at all, which is
   arguably the right first release regardless. **§8.3 now argues that position directly:** stage
   1 is a read-only `SignalSource` with no sink registered at all, so the plugin cannot write
   anywhere and R-7 does not gate it.
7. **Is co-primary rotation (`who: [a, b]`) actually used internally?** If yes, C-13 is blocking
   and cross-instance claim arbitration must be built rather than warned about, because the
   double-claim it reintroduces produces two investigations and two outbound comments on one
   internal work item. **Sharpened by §5:** this changes stage 3 materially. A list-valued `who`
   reintroduces double-claim, and on an internal work item that means two investigations and two
   outbound comments — a visible, embarrassing failure rather than a wasted turn. If the answer is
   yes, the plugin's rotation source should arguably refuse a multi-login window outright rather
   than warn.
8. **Which severities can the plugin actually set?** The write tool blocks the top two on create
   and the top two plus the intermediate half-tier on update. So an agent "escalating" a genuine
   outage can silently no-op. Does the internal setup rely on an auto-upgrade policy to reach
   those tiers instead — and if so, is a raise-only severity sink (§4.2 A5) even useful, or is the
   honest answer that severity escalation is always a human act?
9. **Is the intake-folder set stable enough to be plain config, or does it need discovery?** The
   source hardcodes folder identifiers in a script and documents an add-a-folder procedure with
   four manual steps (`source: knowledge/runbooks/maxis-intake-folders.md:43-50`). If identifiers
   churn, §8.2 stage 4 needs a name-based resolver; if not, they are plain operator config.
10. **Should stage 5's comment sink require an explicit on-behalf-of identity before advertising
    any action at all** — the PagerDuty precedent (`providers/pagerduty.py:193-197`: "Writes need
    the From header; without it PagerDuty rejects them, so we advertise no actions rather than
    failing at execute time")? That is the strongest available narrowing, but it only bites once
    R-6 makes `supported_actions()` enforced.
11. **For the code-change lane: does the internal code-review system distinguish an unpublished
    draft from a published revision in a way the plugin can read reliably?** §4.4's whole gate
    placement depends on step 4 (draft) being genuinely invisible to reviewers. If a draft
    notifies anyone, the gate has to move one step earlier and stage 8 loses most of its value.
12. **Who owns the promotion decision once the curated tier exists (G-15)?** The source runs
    promotion nightly on the leader instance with a ≥3-author threshold
    (`source: sops/knowledge-dream.md:23`). In the app the equivalent would be the primary tier's
    `ledger-hygiene` job — but that job currently only dedupes, decays and prunes. Is promotion a
    deterministic pass or a model judgement, and does the plugin get any say for
    internally-sourced entries?
