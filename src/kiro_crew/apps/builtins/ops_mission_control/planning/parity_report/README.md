# Ops Mission Control — feature-parity report

**Date: 2026-08-01.** Author: single-author synthesis over four domain research passes plus
independent re-verification of every load-bearing negative claim (see
[sources.md](sources.md) for what was read, what was re-run, and what could not be read).

## 1. What was compared against what

**The app:** the `ops-mission-control` builtin in this repo —
`src/kiro_crew/apps/builtins/ops_mission_control/` (manifest, 15 backend modules, 10 provider
files carrying 7 registerable adapter ids, 21 registered routes), its skill and 6 SOPs at
`src/kiro_crew/builtin_skills/ops-mission-control/`, and its 6 dashboard files at
`website/src/apps/ops-mission-control/`.

**The source:** the internal ops workflow it was modelled on — two packages, referred to
throughout as the **source context package** (33 SOPs, 15 legacy mission
files, a 2168-record lesson log, 121 per-incident dossiers, 14 runbook files, 49 Python/shell
scripts among 84 committed files under `scripts/`) and
the **source skillset package** (3 skills, 7 agent SOPs, 2
agent specs, 6 context docs). Paths into them are prefixed `source:` and
`source-skillset:` respectively. The internal ops workflow site (URL in the task brief,
deliberately not repeated here) was also read; it documents 22 SOPs where the live tree has
33, so the site is a simplified snapshot and the packages are authoritative.

Four domains: signal→dispatch→investigation; knowledge and the ledger; rotation, team and
handover; proactive automation and reporting.

## 2. Headline verdict

**The app is a faithful and in several places mechanically stronger port of the source's
alarm-response spine, and a deliberately narrower one on autonomy. It is *not* at parity on
three axes the source treats as core: proactive detection, the written record, and anything
with a clock on it.**

Actionable form: an operator moving from the source workflow to this app keeps the
2-minute claim heartbeat, atomic claiming, the status grammar, the pin board, rotation
gating and a better knowledge ledger. They lose the ability to (a) detect that an expected
thing *did not happen*, (b) read a durable per-incident writeup, (c) have anything get
louder with age, and (d) tell one thing to leave one alarm alone.

| Domain | Implemented | Partial | Missing | Deliberate |
|---|---:|---:|---:|---:|
| Signal ingestion, claiming, dispatch, investigation | 20 | 12 | 6 | 7 |
| Knowledge, memory, learning, the ledger | 10 | 7 | 4 | 6 |
| Rotation, team model, handover, multi-instance | 11 | 8 | 7 | 12 |
| Proactive automation, reporting, digests | 10 | 7 | 9 | 14 |
| **Total** | **51** | **34** | **26** | **39** |

Read the counts as coverage of *source capabilities*, not effort. The "deliberate" column is
large because the app declined a lot on purpose and argued most of it in code; §5 covers
where that argument is real and §4 covers where it is missing. Every cell equals the rows
actually enumerated in the matrix section it names — verified by counting them.

Two corrections to the app's own self-assessment are load-bearing and appear below:
`planning/features.md:19-22` claims the knowledge tier is covered, and
`planning/features.md:975-987` claims absence-of-activity detection is covered. Neither
holds as stated (§4.1, §4.6).

## 3. What is implemented

### 3.1 The dispatch spine — the strongest area

The heartbeat is a direct port with the cadence preserved and the cost profile improved: the
source spends an agent turn every 2 minutes, the app spends a deterministic Python cycle.

| Source capability | App equivalent | Note |
|---|---|---|
| 2-min claim heartbeat, max 3/run, silent when idle (`source: sops/ticket-dispatch.md`) | `backend/dispatch.py:282-389`, `DEFAULT_MAX_CLAIMS_PER_CYCLE` (`dispatch.py:61`), `app.json:57-64` | Exact cadence match; `CycleResult.changed` (`dispatch.py:132-141`) is the silence signal |
| Atomic claim against a shared index | `store.claim()` under `_IndexLock` (`backend/store.py:120-143,183-243`) | **Stronger.** The source's `index.json.lock` / `.index.lock` files exist but no SOP or script references them — grep for `flock` across the source's `sops/` and `scripts/` hits only `install-crons.sh`. The app actually takes the lock |
| Never claim a human-assigned item | GitHub adapter skips issues with `assignees` (`backend/providers/github_issues.py:155-157`); PagerDuty keeps `triggered`+`acknowledged` (`providers/pagerduty.py:56-59`) | `github_issues.py:156` cites the source in-comment. Verified live: 100 open − 10 assigned = 90 signals (`planning/features.md:903`) |
| Status grammar (`source: README.md`) | `models.LEGAL_TRANSITIONS`, 7 statuses, `TERMINAL_STATUSES` **derived** (`backend/models.py:45-128`); `store.transition` the only door (`store.py:246-278`) | **Stronger.** The source's live index carries 34 distinct field names, `severity` as both `'3'` and `'SEV_3'`, both `closureCode` and `closure_code`, and a `filtered` status appearing in no SOP. The app enforces mechanically |
| Stale release | `store.sweep_stale` every cycle (`store.py:289-322`), `stale` a first-class status | App re-dispatches **in place** (one timeline per signal); the source has no stale-reclaim code path, only a snapshot report. But see §4.4 — `needs_human` is not sweepable |
| Per-item operating mode carried as data | `Incident.operating_mode` (`models.py:318`) set at claim by `rotation.resolve_mode` (`backend/rotation.py:172-186`), rendered into the brief (`dispatch.py:458`) | Three values (observe/propose/act) vs the source's two, and `effective_mode = min(app, rule)` so a rule can only narrow (`models.py:130-139`) |
| Slack pin board, glyph tracks state | `slack_out.publish()` — one message per incident, `chat_update` in place (`backend/slack_out.py:198-271`) | Full glyph parity plus `blocked_reason` in place of a bare status. The app stores **no Slack token** of its own; the source reads one from a dotfile |
| Bounded evidence handed to the investigator | `registry.gather_evidence()` as the single chokepoint (`backend/registry.py:248-312`) | **Architecturally different and better:** the app *brokers* evidence (credentialed gateway gathers, redacts, hands text) instead of giving the agent credentials |

Two app-only additions worth naming. `slot_watch.derive_status()`
(`backend/slot_watch.py:75-101`) *derives* `(status, blocked_reason)` from the live chat slot
on every `/state` read, so approving from the embedded chat self-clears with nobody resetting
a flag — the source stores intent in `index.json`, which can go stale. And
`providers/webhook.py:50-176` (HMAC-SHA256 over the raw body, check order
enabled→secret→size→signature→parse) has no source counterpart at all.

### 3.2 The ledger — the app's clearest win

| Source | App | Why the app is stronger |
|---|---|---|
| `lessons/shared-lessons.jsonl`, 2168 records, 7 authors, identity *derived* | `ledger.jsonl`, content-addressed ids (`models.py:377-412`) | Merge becomes a dedupe. `read_entries` reconciles duplicate ids **on read** (`backend/ledger.py:76-143`), so the app stays correct mid-merge |
| `knowledge/patterns/remediation.md` — 178 rows, Confidence × Trust as prose in a table cell | `LedgerEntry.confidence`/`.trust`/`.use_count`, `is_fast_path()` requiring **both** verified and high (`ledger.py:208-215`) | A Python predicate reads a field instead of a model judging prose. `handover.recurring_patterns` reuses the *same* constants (`handover.py:89`) so the digest cannot disagree with the engine |
| `knowledge-dream` step 4: "resolve contradictions" with no tooling | `ledger.find_contradictions()` (`ledger.py:252-304`), `GET /ledger/contradictions` | Deterministic pair detection replacing an O(n²) model eye-scan. It **detects and refuses to decide**; `ledger.py:285` skips identical fixes so real conflicts are not buried under dedupe noise |
| 180-day deletion of stale lessons | 90-day one-step confidence decay then prune at 500 (`ledger.py:307-377`) | Degrades before deleting. "Never delete an entry that has been used" |

Semantic recall is a genuine port of the source's stance: `backend/ledger_index.py:1-34`
reproduces "mirror, not migrate" — text in git, vectors local and never committed — and
`dispatch.attach_similar_lessons` (`dispatch.py:199-254`) deliberately does **not** call
`record_use` — stated in its own docstring at `dispatch.py:208` — because inflating the count
would corrupt the number `is_fast_path` reads.

**One caveat that undercuts this section, and it is mine, not the research's.** I ran
`compute_fingerprint` (`models.py:226-240`) directly this session, with `source="cloudwatch"`:

```
58538b8e259f59c9  4xx error rate high      svc/api
58538b8e259f59c9  5xx error rate high      svc/api
c4dbf4e759b19ceb  p99 latency above 500ms  svc/api
c4dbf4e759b19ceb  p50 latency above 100ms  svc/api
fbf3afe769949bba  replication lag          shard-1
fbf3afe769949bba  replication lag          shard-47
```

The fingerprint strips **all bare numbers** (`models.py:180-182`), so a 4xx and a 5xx alarm
on one service are one pattern, and `shard-1` and `shard-47` are one pattern. Every
fast-path match today is therefore suspect: the ledger can hand a responder a fix learned
from a different failure. This is recorded in
`planning/emitters-comms-gap-research.md:481-507` (§5.3), whose `58538b8e259f59c9` for the
4xx/5xx pair matches mine exactly — so the collisions and the digests both reproduce and the
finding is confirmed twice over. Note that `source` is part of the hash basis
(`models.py:239`), so quoting a digest without naming the source is meaningless; that is what
made an earlier run of mine appear to disagree with the sibling doc. It does not appear in
`planning/features.md`.

### 3.3 Rotation and the team model

`rotation.yaml` in the git-synced ledger repo, identity = GitHub login
(`backend/providers/schedule_file.py:93-134,251-357`). The app reached the source's own
conclusion independently — do not call an internal on-call API — and the source only cached to a
state file *because* the API was unreachable from its hosts
(`source: scripts/install-crons.sh:133-207`).

The design decision worth porting anywhere: **the two rotation sources fail in opposite
directions on purpose.** A rotation API returns `on_shift=True, unknown=True` (a network
fault must not disable incident response); the committed file returns
`on_shift=False, unknown=True` under `strict_gating` (`schedule_file.py:267-291`), because
with a file every instance reads, "cannot tell" means the schedule is wrong and arming makes
the whole team claim the same alarm. `tier_states` reads `on_shift` **alone**
(`rotation.py:337-360`) — `unknown` is a UI explanation, never an arming input.
`planning/features.md:203-219` records that `tier_states` previously computed
`on_shift or unknown`, silently defeating strict gating for exactly the case it was written
for.

Off-shift writes are blocked at **three** independent layers, more than the source has: the
cron tier, `run_cycle`'s own check (`dispatch.py:296-302`), and
`authorize_action`'s `_definitely_off_shift()` (`rotation.py:196-260`) which guards
`/incident/action` and an in-flight investigation — paths that bypass both others. The
source's only gate is cron pausing, so a manual off-shift invocation there would mutate.

Autonomy is a two-key lock and the second key cannot be a wildcard:
`AutonomyRule.from_dict` **refuses** an act-rule with neither `resource_glob` nor
`label_match` (`rotation.py:89-150`), so "act on everything from CloudWatch" is not
expressible. I verified this is enforced in code, not just documented.

### 3.4 Proactive tier and hygiene

The tier model is a direct port including the source's cold-start asymmetry — exactly one
cron ships enabled. `TIER_CRONS` (`rotation.py:56-78`) ships **both** `armed_crons` (flat
union) and `tier_crons` (per-tier) specifically because off shift the union still contains
`rotation-check` itself, and an agent told to "pause the armed crons" would pause the only
job that can re-arm the instance. `sops/rotation-check.md:24-71` forbids acting on the
union. That hazard is not called out in the source.

Silence-by-default is enforced rather than restated: all four crons are
`silent: true, persistent_session: false` (`app.json:56-88`), each cron message says produce
no output, and `sops/dispatch.md:75-77` carries the source's exact lesson about the default
message target being a DM. The source repeats that rule in ~12 SOPs and its own
`AGENTS.md`; the app pins it with a test that iterates the manifest
(`tests/test_config_routes.py:364`).

The hygiene pass (`routes.py:722-770`) runs pull → dedupe/decay/prune → vector index →
prune closed → push, in an order pinned by test. `planning/features.md:518-524` records that
`ledger_sync` and `import_pending` were previously wired to **nothing** — semantic recall
queried an index nothing populated, while every unit test passed. Two individually-correct
modules, collectively dead.

## 4. What is missing — ranked across domains by operator cost

Ranked by what it actually costs an operator, not by domain. Each entry states placement:
**public core**, **companion plugin**, or **nowhere**.

### 4.1 `propose` mode has no backend — the mode most operators will live in is prose only

*Source:* a six-part enforced protocol. The agent drafts the **verbatim** outbound text plus
the exact mutations, posts one structured draft, and waits on a typed grammar — approve →
execute exactly as drafted, edits → revise and re-ask, cancel → abandon, 24h → one bump and
never auto-act. "The drafted comment text is the contract."
(`source: sops/ticket-investigation.md:169-215`.)

*App:* `propose` exists in `MODE_ORDER` and `authorize_action` refuses writes below `act`
(`rotation.py:187-278`). `Incident.proposed_action` is declared at `models.py:325`. **I
verified by grep across `src/` and `website/src/` that nothing ever writes or reads it** —
the only hits are the dataclass field, `from_dict` (`models.py:341,353`), and a TypeScript
type (`api.ts:48`). There is no draft store, no approval endpoint, no re-ask rule, no
timeout. The `awaiting_approval` blocked reason comes from a chat-slot *tool* approval
(`slot_watch.py:60-71`), which gates one tool call, not the whole outbound package.

*Impact:* an operator cannot see a queue of pending proposals or approve one from the board
(`opsApi.action` in `api.ts` has **no** `.tsx` caller). Nothing stops an agent altering the
text between drafting and posting. The source's most load-bearing safety mechanism is
unenforced, and a proposal ignored for a week looks identical to one made a minute ago.

*Placement:* **public core.** This is the safety property that makes "let the agent act"
defensible for everyone, not just internal users.

### 4.2 Nothing is ever written to the per-incident log — an advertised field is structurally always empty

*Source:* 121 committed `oncall-context/dispatch/tickets/<ID>.md` dossiers, each with
`## Root Cause` / `## Investigation` (a bullet trail of every command run and its literal
output) / `## Resolution:`, explicitly re-read by later investigations as precedent
(`source: sops/ticket-investigation.md:218-234`).

*App:* `store.write_log()` (`store.py:385-423`) renders exactly that shape. **I verified it
has zero callers** — grep across `src/` and `website/src/` returns the definition plus
planning-doc references only. So `incidents/<id>.md` is never created and `/incident`'s
`log` field (`routes.py:277`) is always the empty string. The module spec at
`docs/system-specs/modules/ops-mission-control.md:886` documents an on-disk artifact that
cannot exist.

*Impact:* the record of what happened lives only in a chat transcript and two free-text
fields on an index row that `prune_closed` deletes after 500 closed incidents
(`store.py:341-377`). A later investigation of a recurrence cannot cite the previous one —
the app tells you the *lesson* (ledger) but never the *case*. An API advertising a
permanently-empty field is worse than omitting it.

*Placement:* **public core**, and it is the cheapest item in this report — the renderer is
already written. **Documented**, in
`planning/emitters-comms-gap-research.md:613-638` (§5.8), which also correctly notes
`write_log` renders unredacted provider text and an unredacted model diagnosis, so an HTTP
download route would need redaction and a `security_posture.py` entry. Not in
`planning/features.md`.

### 4.3 No proactive detection — the app cannot ask "did the thing that should have happened, happen?"

*Source:* a whole job class distinct from alarm response. `table-freshness.md` reads a
per-resource freshness expectation, computes staleness, posts **one aggregated** report and
must never create tickets; `schema-drift.md` queries live column metadata and diffs against
a committed baseline, classifying each change info/warning/critical.

*App:* every signal must be pushed by a provider that already decided something is wrong.
The only absence-check is CloudWatch `include_insufficient_data`, off by default
(`providers/cloudwatch.py:51-56`).

*This directly contradicts the app's own record.* `planning/features.md:975-987` says
"Absence-of-activity detection is already covered… The public equivalent is CloudWatch
`INSUFFICIENT_DATA`". That covers one narrow instance — a CloudWatch metric stopped
reporting — of a much broader capability. A pipeline that silently stopped, a table that
stopped being written, or a schema that changed are invisible unless the operator happened
to have an alarm for exactly that. The reasoning in that entry is sound about the *SLA
schema* being org-specific (§5) but the entry conflates the schema with the job class.

*Placement:* **public core** for the shape — a scheduled check comparing observed state
against a declared expectation needs no internal system, and drift-against-a-committed-
baseline maps onto the app's existing git-synced-file transport. The specific SLA table is
correctly declined.

### 4.4 Escalation has no clock — and the one clock that exists moves work down, not up

*Source:* `staleness-escalation.md:14-22` is a graduated matrix that acts over time: over
SLA no ticket → log; over threshold → create SEV-5; >2× with an open ticket → bump to SEV-4
and comment; >4× → SEV-3 and notify lead; suppressed → skip.

*App:* `escalated` is a one-shot terminal label an agent applies (`models.py:50,86,91,101`).
No age-based escalation, no severity bump, no suppression. `Signal.severity` is set once at
normalization and never changed by anything.

Three findings compound here, and this is where I depart from the research input most:

1. `sweep_stale` is the **only** clock-driven transition and it moves work *down* to `stale`.
2. **`needs_human` is not sweepable at all.** I verified `_SWEEPABLE_STATUSES` at
   `store.py:66` is `{dispatched, investigating}` — while `models.py:88-89` legalises
   `needs_human → stale` precisely so an incident nobody answers cannot pin a signal
   forever. `dispatch.py:332-337` counts every non-stale non-terminal incident as owning its
   signal, so that alarm is never re-claimed. This is the app's quietest failure.
3. The handover digest emits `age_from` per row (`handover.py:104`) but I confirmed nothing
   sorts, buckets or thresholds on it — a 165-day-old carry-over and a 5-minute-old incident
   render identically.

*Impact:* an incident parked in `needs_human` presents the same at five minutes and five
days, *and* it silently holds its signal hostage. An operator who steps away has no
mechanism that raises priority on their behalf.

*Placement:* **public core.** Item 2 is small and is documented at
`planning/emitters-comms-gap-research.md:529-553`, which also argues persuasively *against*
the nudge-ladder half on the grounds that it collides with the shipped noise rule at
`SKILL.md:133-141` — a nudge belongs on a notification bus with a `group_key`, not on Slack.
I agree with that split. Items 1 and 3 are undocumented.

### 4.5 No human opt-out, and no suppression at any layer

*Source:* a plain-language phrase a person writes on the item — case-insensitive, in the
latest correspondence or worklog — that makes the agent skip it entirely: no claim, no
investigation, no comment (`source: sops/ticket-dispatch.md:38`,
`ticket-investigation.md:70`). Separately, `table-freshness.md:31-38` skips rows marked
`suppressed: true`.

*App:* no app-level exclusion layer. `run_cycle` filters only on `state == firing` and the
owned-set (`dispatch.py:318-338`). Exclusion is expressible only as *inclusion narrowing*
inside one adapter's config (`cloudwatch.py:130-137`, `datadog.py:93`,
`github_issues.py:105`), which cannot express "watch this whole prefix EXCEPT these three".

*Impact:* an operator who wants the agent to stop touching one alarm must narrow the whole
adapter (losing unrelated coverage) or disable the provider. There is no per-signal mute and
no way for a human to take an item back mid-flight other than resolving or escalating it. On
a noisy account the only lever is all-or-nothing, and a known-flappy alarm mints a fresh
incident per flap while consuming a claim slot a real failure needs.

*Placement:* **public core**, and small — a per-fingerprint mute list in app config with a
board button, the same shape as the existing `autonomy_rules` key. A stranger installing
this app hits it on day two. Partly documented:
`planning/emitters-comms-gap-research.md:680-703` (§5.11) covers reading *provider-side*
suppression (Alertmanager `status.state=suppressed`, Zabbix `suppressed=1`) but not an
operator-authored mute. Undocumented as a human opt-out.

### 4.6 The knowledge tier has one shape where the source has two

*Source:* two tiers with a promotion event between them — raw incident-derived lessons
(2168 rows, machine-appended) and **curated** files: `remediation.md` (178 rows) and
`common-pitfalls.md` (361 bullets), human-editable and topic-organised, plus 14 runbook files
of which **10 are purely hand-written procedure docs** with no dependency on any internal
system. (Counted: only 4 carry the `<!-- Auto-updated … -->` ownership markers, and only those
4 appear as a `doc_target` in the source's `doc-sync.yaml` — so the machine-maintained set is
4, not 6, and the hand-written set is correspondingly larger than an earlier draft said.)

*App:* one graded tier. `POST /ledger` **requires both `pattern` and `fix`**
(`routes.py:708-710`, 400 otherwise), so there is no home for:

- a **procedure** ("here is how you clean up the DLQ, in seven steps") — not a symptom→fix
  pair bound to a fingerprint;
- knowledge organised by **subject** rather than by failure — no per-service page;
- a **trap with no remedy** ("Glue validation is all-or-nothing, so 95.6% pass = 0 rows
  ingested") — the class the source's own patterns README tells responders to scan *first*.

*This contradicts the app's own record.* `planning/features.md:19-22` marks
`knowledge-preserve` as "Covered" and declines `package-scan`/`wiki-sync` as "internal build
+ wiki systems". That correctly disposes of the **auto-population mechanism** and silently
takes the **runbook artifact** with it. The 10 hand-written runbooks need no internal system
and no decision anywhere records declining them.

Two adjacent findings: there is **no architectural context** for the investigator (grep for
service-map / data-flow / topology across the app, skill, UI and spec returns nothing), even
though core already ships a document knowledge library with a local-folder connector at
`src/kiro_crew/knowledge/connectors/local_folder.py` that the app does not import; and there
is **no operator-editable knowledge surface at all** — `addLedgerEntry` and
`removeLedgerEntry` exist in `api.ts` with zero `.tsx` callers, so all human knowledge entry
is curl-only.

*Placement:* **public core** for a curated procedure/topic tier and for the inbound half of
doc ingestion (the connector already exists). **Companion** for publishing back to a
specific internal wiki.

### 4.7 Nothing redacts the one artifact that leaves the machine

*Source:* the lesson mirror skips records containing secret markers and redacts every
external email before mirroring, enforcing a confidentiality ceiling
(the source's knowledge-mirror cron, `source: scripts/crons/`).

*App:* redaction is diligent at two chokepoints — `registry.gather_evidence`
(`registry.py:284-302`) and `slack_out` (`slack_out.py:168,177,258`). **Neither covers the
ledger.** `POST /ledger` (`routes.py:701-719`) passes `pattern`/`fix` straight to
`LedgerEntry.create` with no redaction, and `ledger_sync._stage_and_commit`
(`ledger_sync.py:407`) commits `ledger.jsonl` verbatim. I confirmed the app imports
`kiro_crew.security.redact` at exactly two sites (`registry.py:255`, `slack_out.py:58`) and
`redact_via_context` **nowhere**.

*Impact:* once team sync is on, the corpus is the one artifact leaving the machine that
nothing sanitises — and it is the artifact most likely to contain a pasted credential,
because a `fix` field naturally holds command shapes. Recovery is a git history rewrite
across every teammate's clone.

*Placement:* **public core.** Redact on the **write** path, not the sync boundary: the entry
is already on local disk and in the vector index by the time sync runs, and an operator who
enables sync later retroactively publishes everything written before. Undocumented; no test
covers it. Full detail in [amazon-plugin.md](amazon-plugin.md) §12.4 (R-4), because it is the
plugin's hardest blocker too.

### 4.8 The multi-instance story breaks on the two paths the app itself flagged as most dangerous

Two findings, both undocumented, both contradicting shipped claims:

**`POST /ledger/hygiene` has no `is_primary()` gate.** I verified `is_primary` is referenced
only from `tier_states` and `describe` (`rotation.py:359,389`) — never from `routes.py`. The
route runs pull → dedupe/decay/**prune** → index → `prune_closed` → push with no ownership
check, so any instance that calls it prunes a shared ledger.
`sops/rotation-check.md:34-35` claims hygiene "self-gates on `is_primary()` at runtime";
that is not true of any code. `planning/features.md:226-237` records concurrent pruning as
*worse* than concurrent claiming ("a duplicate claim wastes a turn, a duplicate prune deletes
knowledge") — the fix landed in `is_primary()` and the enforcement point was never wired.

**Co-primary shifts reinstate the double-claim the single-owner model is credited with
superseding.** `rotation.yaml` accepts `who: [bob, carol]` and
`tests/test_schedule_file.py:430-434` pins that both are on call. But
`docs/system-specs/modules/ops-mission-control.md:1074-1080` states the claim index is
per-instance and does **not** stop two instances claiming one signal, and
`docs/task-specs/2026/07/ops-mission-control/mesh-claim-arbitration.md:1-9` is marked
SUPERSEDED on the grounds that "a shared `rotation.yaml` makes exactly ONE instance
eligible". That holds only for a scalar `who`. Nothing warns an operator that writing a list
disables the guarantee.

Compounding both: `ledger_sync.sync_safely`'s docstring (`ledger_sync.py:504`) claims "the
dispatch cycle and the daily hygiene pass call this" — grep of `dispatch.py` for
`ledger_sync` returns **zero** hits. Its only caller is the daily primary-tier hygiene route
(`routes.py:750-759`), so a **non-leader instance has no code path that ever pulls
`rotation.yaml`**. A shift swap pushed by a teammate can be a day stale, or on a non-leader
box stale indefinitely — the transport quietly undoing the single-owner decision.

*Placement:* **public core**, all three small. Either build arbitration or refuse/warn on a
multi-login window.

### 4.9 No credential-expiry handling, and nothing watches the watchers

*Source:* a 5-minute zero-token watchdog pauses every enabled agent cron on auth expiry,
sends **one** message per 24h naming the blocked jobs plus the fix command, and on re-auth
resumes **only** the jobs it itself paused — with an explicit hand-back so it never re-arms
an on-call-gated cron while off shift (the source's auth-watchdog SOP and
its zero-token cron script). Separately its snapshot cron cross-checks the
cron fleet against the work index, because a monitor can die silently.

*App:* `poll_all` catches per-source failure and reports it in `errors`
(`registry.py:193-195`), and the Signals tab prints the verbatim text — but only behind an
explicit "Poll now" button (correctly, since polls cost money). Nothing pauses, notifies, or
counts consecutive failures. `run_cycle` returns a `skipped_reason` for the *unconfigured*
case only (`dispatch.py:310-316`), not the expired one. And nothing checks whether the app's
**own** crons are alive.

*Impact:* expired credentials make a watched board an unwatched board that looks identical
to a quiet one — the exact conflation the app is otherwise rigorous about refusing
(`dispatch.py:304-315`, `handover.py` headline ordering). Dispatch keeps firing every 120s
producing an error nobody sees.

*Placement:* **public core.** The mechanism already exists: the app declares
`cron_pause`/`cron_resume`/`cron_list` in `app.json:42-45` and uses them **only** for tier
arming. Undocumented.

### 4.10 The handover digest drops released work entirely, and two sections it already has data for

`open_work` (`handover.py:110-147`) buckets into waiting / escalated / stalled / progressing,
all four derived from `store.open_incidents()`. `stale` is **not** in `OPEN_STATUSES`
(`models.py:126-128`, which is `{unclaimed, dispatched, investigating, needs_human}`), so a
stale incident appears in **no bucket and no count** — `total_open` excludes it too. Corrected
from an earlier reading of mine that had it landing in `progressing`: the mechanism is
omission, not mis-bucketing, and the operator cost is the same shape but quieter. A signal the
app released hours ago is invisible in the one artifact written to survive a shift change. And
`CycleResult.unclaimed_remaining` is computed (`dispatch.py:393`) but reaches only the
`/dispatch` response; nothing tells a human "N firing signals are queued behind the
3-per-cycle cap".

Also absent with no recorded decision: a **resolved-this-shift count** (the source reports
resolved grouped by root cause; `store.counts_by_status()` already computes it at
`store.py:334-338` and `/state` already returns it), an **aged** bucket (§4.4), and
**escalation targets** — when the agent decides `escalated` it has nowhere to look up the
next hop, and `sops/handover.md:60-62` rightly forbids inventing one, so escalation is a dead
end by construction.

*Impact:* an incoming responder never hears about work the app released as stale at all, is
never told a backlog exists, and cannot answer the most common shift-change question — what
did the last shift finish?

*Placement:* **public core**, all small, all from already-owned data. Undocumented. The
*org-specific* half (rosters, ticket ids, runbook links) is correctly declined (§5).

### 4.11 Lower-cost gaps, grouped

| Gap | Source | App | Placement |
|---|---|---|---|
| No `create` / severity-mutate / structured-close verb; `VALID_ACTIONS` is a closed core frozenset (`models.py:157`, validated at `routes.py:395`) | Detection sweeps file SEV-5s and bump severity | Can only annotate and close what exists — and a companion cannot add a fourth verb without a core change | **public core** (`extra_actions` hook) |
| `Signal` has no body/description/comment-thread field (`models.py:243-289`) | "Read the ticket FIRST": 50 correspondences + worklogs, and the re-page guard depends on it | Agent diagnoses from title and resource alone; cannot see a human already answered | **public core** for the shape — but as ordered `Evidence`, *not* a `Signal` field (see amazon-plugin.md §G-3) |
| No incident slot is ever dismissed | Three-script lifecycle: create → set_status → `close --reconcile`, with a self-guard so an agent never deletes the slot it runs in | Creation only (`sops/dispatch.md:41-52`); no backend code touches `/api/chat/slots` | **public core**, small |
| No run provenance on a claim | `claimed_by` embeds the cron tick or manual invocation (126 of 130 live entries) | I confirmed `claimed_by` appears nowhere in the app | **public core**, one string field |
| No self-improvement loop; no observation history/delta; no self-measurement | A 3-hourly scan scores impact×effort and auto-implements only impact≥3 **and** effort≤2, with a hard do-not-touch list; every alarm tick writes a snapshot with an explicit `## Delta vs last snapshot` | None. Grep for scorecard/workflow-improvement/morning/weekly/daily-queue returns zero hits across the app, its 1109-line spec and its 1008-line backlog. The store listing advertises a 72% TTR reduction while the app computes no durations at all | **public core** for self-measurement (derivable from the index it already keeps); **nowhere** for the source's roadmap/audio pipelines |
| No aggregation | One aggregated message per detection sweep | Every signal becomes its own incident and its own Slack message | **public core** |

### 4.12 Genuinely missing vs deliberately declined — and the undocumented set

The distinction that matters for planning. **Undocumented omissions are the more useful
finding**: a declined item has an argument you can disagree with, an undocumented one means
nobody has decided yet.

| Item | Status |
|---|---|
| `write_log` has no caller (§4.2); `needs_human` unsweepable (§4.4); provider-side suppression (§4.5) | **Documented** — `planning/emitters-comms-gap-research.md` §5.8, §5.5, §5.11 (2026-08-01) |
| Fingerprint over-merge (§3.2) | **Documented** in the same doc §5.3, whose hash values reproduce exactly; **absent from `features.md`** |
| Auto-resolve default, remediation execution, SLA schema, mesh, scheduled handover, hosted telemetry | **Declined with reasons** — §5 |
| `propose` has no backend (§4.1) | **Undocumented** |
| Absence-of-activity as a job class (§4.3) | **Contradicted** — `features.md:975-987` claims covered |
| Runbook artifact / curated tier (§4.6) | **Contradicted** — `features.md:19-22` declines the mechanism, takes the artifact with it |
| Human opt-out (§4.5); ledger-write redaction (§4.7); no `is_primary()` gate on hygiene, co-primary hole, non-leader never pulls (§4.8); credential expiry + cron self-health (§4.9); handover stale/unclaimed/resolved/aged/escalation-target (§4.10); `claimed_by`; slot dismissal; aggregation; self-measurement; bot-identity disclosure | **Undocumented** |
| Age-based escalation ladder (§4.4) | **Undocumented** — though the *nudge* half is argued against in the emitters doc |

One entry deserves separate naming because it is the only item where I could find neither an
implementation nor a recorded decline. The source treats **bot-identity disclosure** as
MANDATORY — the literal first line of every outbound ticket comment — restated in 4+ SOPs
and its workspace-conventions doc, and explicitly scoped to ticket systems only (not chat,
not code review, not wiki). The app **does** write to third-party systems via
`ActionSink.execute` with `comment` (`github_issues.py` posts `--body` verbatim,
`pagerduty.py` POSTs to `/notes`) with no disclosure prefix and no rule requiring one. Grep
across the backend, `SKILL.md` and all 6 SOPs finds zero mention. It may be a reasonable
omission for a public app with no org identity convention — but the source treats it as
mandatory on exactly the surface the app writes to, so the silence is itself the finding.

## 5. Deliberate non-gaps

Brief, with the reason and where recorded. These are the decisions I checked and agree are
argued, not the ones I am flagging above.

| Declined | Reason | Recorded |
|---|---|---|
| Auto-resolve by default | "That team could reason about which intakes were safe because they had built them; a stranger's first install cannot. Autonomy is earned per-rule." **Verified enforced**, not just claimed: `AutonomyRule.from_dict` refuses a wildcard act-rule | `planning/features.md:996-1005`; `backend/rotation.py:11-19,89-150` |
| Remediation execution | "The app diagnoses and proposes; a human applies the fix." Enforced in three places, including a three-verb vocabulary that cannot touch infrastructure | `features.md:997-1000`; `dispatch.py:542-547`; `SKILL.md:28-29` |
| SLA table **schema** | Per-provider by nature — a CloudWatch alarm already encodes its own threshold, and a generic schema for a stranger's warehouse would be guessing. **Note:** covers the schema only, not the job class (§4.3) or a suppression flag (§4.5) | `features.md:989-994` |
| Internal ticket + intake-folder signal sources | Out of tree by design; the public core ships only the ADD-only entry-point seam, verified with a real installed package | `features.md:364-379`; `backend/companion.py:75-218` |
| Scheduled handover | "A handover is read by a person at a moment they choose, and a scheduled one that nobody reads is exactly the noise this app exists to avoid." Ships `cron: null`, pinned by test. Covers the **schedule** only — the two missing content buckets are §4.10 | `sops/handover.md:1-11`; spec:836-839 |
| Caching the handover | "A stale handover is worse than none" — computed per request. Does not cover *archiving* a shift's digest as history | `handover.py:24-27`; spec:820-821 |
| Cross-instance claim arbitration | Superseded by the single-owner model: "no arbitration to get wrong." **Sound only for a scalar `who`** — see §4.8 | `mesh-claim-arbitration.md:1-9`; `features.md:269-271` |
| The shared file granting write **authority** | "This adapter never decides authority, only tier arming… wired to the cheap decision (when to look) and not the expensive one (what to do)." A teammate pushing a schedule cannot escalate what any instance may do | `providers/schedule_file.py:33-38`; spec:585-588 |
| Syncing the dispatch index | "Last-writer-wins on a shared key, so syncing it would silently let two instances believe they each own an incident." Enforced by a generated `.gitignore` | `ledger_sync.py:19-23,182-196` |
| `package-scan` / `wiki-sync` auto-population | Depend on internal build and wiki systems. Correct for the mechanism; see §4.6 for the artifact | `features.md:22,256-260` |
| Hosted control plane / telemetry | "Local-first is the point: the user's credentials and alert streams never leave their machine" | `features.md:999-1001` |
| Stale-marker annotations, history compaction | File-format conventions for a hand-maintained markdown repo; the app's equivalents are structural. **Holds only because the app declined the markdown tier** (§4.6) | `features.md:44-48` |
| Publishing the source's TTR numbers | "They describe one team's pipeline, with caveats that team states itself." Covers not *reusing* their numbers — not letting an operator measure their own (§4.11) | `features.md:996-1000` |

## 6. The Amazon-only companion plugin

Full spec in **[amazon-plugin.md](amazon-plugin.md)**, which covers two halves: the **capability
surface** (its §1–§8 — what should be in it) and the **seam, security and packaging contract**
(its §9–§17 — can it exist safely). Summary of both, standing alone:

### 6.1 Verdict

**Can it exist safely: yes — but not as currently specced. Three of the required public-core
changes are security-load-bearing, not conveniences.**

**What should be in it: five of the eight build stages are blocked on nothing** — including both
of the owner's named sub-items (resolver-group monitoring and intake-folder monitoring) and the
entire read path. The plugin's first release does not wait on the public core; it waits on someone
deciding that a read-only internal signal source with no write sink registered is a release. It
is.

### 6.2 The capability half — what the plugin should have

**Signal sources (its §2).** Eight classes. The owner's two named sub-items — resolver-group
monitoring and intake-folder monitoring (`planning/features.md:449-450`) — are ordinary
`SignalSource` implementations needing no core change, and their field-by-field mapping onto
`Signal.create` is specified with the build order (amazon-plugin.md §8.3). The other six are
data-freshness breach, schema drift, infrastructure threshold breach, long-running work item,
credential/key expiry, and vendor-data arrival window. Two source jobs are deliberately **not**
signal sources: the auth-token watchdog is a *suppressor* (modelling it as a signal would put the
app's own dead credential on the board, the one place an operator cannot act on it), and
security-review monitoring is a code-change loop the source itself retired.

One hazard governs all eight and it is the report's §3.2 finding applied to internal data:
`compute_fingerprint` strips **bare numbers** (`models.py:280-281`), and internal work-item titles
are dense with significant ones. So a threshold's magnitude must live in `labels`, never in
`title` — `CPU 91% > 85%` fingerprints identically to `CPU 34% > 85%` — and every work-item source
must set `provider_key`, which buys an exact-identity match tier above the shape hash
(`ledger.py:203-215`). Three of the eight also need something the core lacks to be *handled*
rather than merely emitted: an adapter that owns a threshold and can report observed-vs-expected,
a clock that escalates with age (§4.4), and a do-not-act annotation — the source's victim/blocker
rule says the *blocked* query must not be killed, and there is no way to say that to this app.

**The snapshot discipline belongs in the public core, not the plugin.** The source's rule is one
line — on any unexpected failure, skip silently, because the previous snapshot remains usable. The
app polls live every cycle (`dispatch.py:336`) and cannot distinguish a source that returned
nothing from a source that failed; `registry.poll_health` reports that distinction but nothing acts
on it. The plugin's own obligation is narrow and easy to get wrong: **an internal adapter must
raise, not return `[]`, on a failed fetch**, because `poll_all` records a raise as a failure and an
empty list as a successful poll of nothing. Returning `[]` is the one adapter bug that silently
disables the app.

**Evidence sources (its §3)** are the highest-value read surface, and mostly free: the source keeps
its architecture in committed markdown, so a service map, data-flow lineage and 14 runbooks need no
internal API at all. Six types: service map, lineage, runbooks, prior item thread, build/deploy
state, and query/job history — the last including the lock chain, without which the plugin cannot
honour the do-not-kill rule. Two cautions. The two architecture documents exist in **both** source
packages and have **drifted** (one describes a serverless warehouse where the other describes
provisioned clusters; one is missing a pipeline row and a completed work-stream), so the plugin must
name one authoritative in config — an evidence source that confidently supplies a stale architecture
is worse than one that supplies none. And internal free text must go in `Evidence`, never in a
`Signal` field, because `title`/`resource`/`url`/`labels` reach the model prompt without passing the
redaction chokepoint (§11.2's C-7).

**The class the seam cannot express** is the source's highest-volume, cheapest category: a signal
whose entire handling is a fixed multi-step check with no model turn — read the alarm state, resolve
if OK, comment only on first observation. In this app both instances cost a full investigation. It
is not a `SignalSource` (the signal exists), not an `EvidenceSource` (it decides rather than
gathers), and not cleanly an `ActionSink` (its input is a rule plus a state read, not an authorized
verb). Tracked as **G-16**. Two properties the source proves are non-negotiable: first-observation-
only commenting, and the resolver must be able to *decline* — its most common outcome is "still
waiting".

**Action sinks.** The inclusion test is one line: *a verb earns inclusion only if a wrong
invocation is either reversible or self-expiring* — the property `ACTION_SILENCE` was added for
(`models.py:166-168`). By that test, four writes are available today: comment (`comment`),
acknowledge (`ack`), resolve (`resolve`, but only when a rule names it explicitly, because
`authorize_action` grants "any action this sink supports" on an empty `actions` set), and a
time-boxed mute (`silence`, whose window core clamps at the boundary). A fifth — structured closure
with a closure code and root cause — needs **no core change at all**: `execute()` already takes a
free-form `payload` dict, so it rides under the existing `resolve` verb. Three more (severity
change, reassignment, create) need G-1 *and* are argued for deferring even after it lands, each
for its own reason: the write tool's schema blocks the top two severities so an "escalation" can
silently no-op; a well-formed-but-wrong assignee routes work into a void; and create is
rate-limited to 1/min, so a loop under the limit files 60 items an hour.

Three behaviours are mandatory on every internal sink or the sinks are net-negative: a **disclosure
prefix enforced inside `execute()`** (the source treats this as MANDATORY in three independent
places; the app has no mechanism at all, and a prompt instruction is not a mechanism), a
**first-observation guard** keyed on a marker the plugin itself writes, and **refuse-on-owned**.

**The code-change lane** stops at step 4 of six: isolated throwaway clone → minimal change + test →
push branch → **draft review**, then stop. Publishing is the human gate; merging needs a *second*,
different gate (a recorded approval by someone other than the agent's principal). This is the
source's own ordering, and the gate is not where a first-time reader would put it.

**Rotation.** One `RotationSource` over the internal on-call service, window widened to ±14 days
because the service returns only shifts *starting* inside the range — a same-day window returns
empty mid-shift and disarms a genuinely on-call instance. The precedence rule against the public
`schedule-file` source is not "internal wins": it is the existing OR in `resolve_shift`, and the
plugin's only real choice is its `unknown` polarity. A rotation *API* must fail **open**
(`on_shift=True, unknown=True` — a network blip must not switch off incident response); the
committed file fails closed. That split is already recorded in `tier_states`' docstring.

**Knowledge bridges.** Runbook import is an `EvidenceSource`, **not** ledger writes — forcing 1–3 KB
of prose into `pattern`/`fix` blows the 500-row cap, decays for non-use, becomes prune-eligible
(the app would delete the team's documentation to stay under a cap), and lands in the pushed
`ledger.jsonl`. Evidence passes the single redaction chokepoint and is never persisted. The
external shared-memory mirror and outbound wiki publication are **both declined for v1**: the local
vector projection already answers "find me something similar", and outbound publication has no
Protocol, no redaction boundary and no idempotency story. Inbound wiki sync is fine, and carries
the source's authority rule verbatim — wiki upstream for authoritative docs, local knowledge
upstream for operational annotations, merge and never replace.

**What must stay OUT of the plugin** — the test is "would a stranger's public install want this,
and would putting it in the plugin make the public core unauditable on its own?" Ten items fail
it, including the per-item human opt-out (§4.5 here), `claimed_by` provenance (§4.11), the
credential-expiry watchdog and cron self-health (§4.9), age as a dimension (§4.10), and the
per-fingerprint deterministic resolver whose *mechanism* is the highest-leverage generic thing in
the whole parity set. Two things genuinely belong in the plugin: the opaque queue/folder
identifiers, and the internal auth broker invocation.

**Build order.** Eight stages, read before write throughout. Stages 1–4 and 6 are blocked on
nothing. Stage 5 (the first write sink) needs ledger-write redaction and the pre-push veto first.
Stage 8 (the draft-only code lane) needs two new seam changes. The smallest first slice is stage 1
alone: one read-only `SignalSource`, no sink registered, so the plugin cannot write anywhere. The
operator still gets internal items on the board, per-source poll health, a real investigation with
the existing metrics/log evidence path, and a ledger that compounds from the first incident.

### 6.3 The contract half — can it attach safely

**What is genuinely built** is the *attachment* half, and it is good: `kirocrew.ops_providers`
entry-point discovery deliberately distinct from the platform plugin group; admission
evaluated **before** `ep.load()` so rejected code never executes; ADD-only registration
where the incumbent always wins; public adapters installed before companions
(`registry.py:329-341`, pinned by test); a name-normalised `banned` kill-switch that works in
any mode; and a Settings row that distinguishes "no companion" from "companion rejected".
14 tests at `tests/test_companion.py`. A fleet can disable the plugin without touching the
operator's packages — a real remote-disable control, already shipped.

**What is not built** is the half an internal plugin needs:

| Requirement | Status | Blocking |
|---|---|---|
| Register adapters out of tree; admission before import; static token storage | Built | — |
| Hold an **expiring, interactively-renewed** credential | Nothing exists | **Yes** |
| Contribute redaction patterns for internal identifiers | Seam exists in core; **this app bypasses it** | **Yes** |
| Keep internal content out of the git-synced ledger | Nothing — the one unredacted egress | **Yes** |
| Extend action vocabulary (`create`, severity, reassign) | `VALID_ACTIONS` is a closed core frozenset — now four verbs, still closed | Yes, for parity |
| Per-item human opt-out | Nothing at any layer | Yes, internally |
| Gate provider **writes** on a companion rotation source | `authorize_action` reads `schedule_file` synchronously and never consults `resolve_shift` | **Yes**, and it misleads: arming works, so writes look gated |
| A status meaning "a change is in review" | Seven statuses, no such edge; `needs_human` is the only landing spot | Yes, for the code-change lane |
| An author on a ledger entry, so corroboration is countable | `use_count` counts matches, not people | Yes, for a curated tier |

**The single most important finding.** The app calls `security.redact` directly rather than
`platform.redact_via_context`, so the `CredentialPolicy` seam that exists *precisely* so a
loaded internal companion can add internal token regexes is not wired into either chokepoint.
I verified the app imports exactly two redaction sites (`registry.py:255`, `slack_out.py:58`)
and `redact_via_context` appears nowhere in the app, while
`src/kiro_crew/platform/context.py:554-557` documents that shim as "the single, canonical
credential-redaction shim every egress site should import". Two lines, three properties: the
companion contributes patterns declaratively instead of duplicating the regex stack, an
internal host that fails to compose its companion **fails closed** on redaction instead of
quietly falling back to public patterns, and the posture drift guard stays satisfied.

**The second.** Redaction covers evidence→prompt and incident→Slack. It does not cover ledger
entry→`ledger.jsonl`→`git push` (§4.7). For a public install that is latent; for an internal
one where a `fix` field naturally holds an internal command, a resolver-group name or an
employee alias, it is the primary leak path.

Three further items the plugin work surfaces that also matter to the public core: the app's
`data/config.json` is on **no** protection floor, so `mode` and `autonomy_rules` are writable
by the generic `PUT /api/apps/{name}/config` route (`src/kiro_crew/apps/routes.py:1406`) and by
any auto-approved agent shell — contradicting `planning/user_manual.md:205`, which claims
autonomy rules are "deliberately *not* settable over the API". `supported_actions()` is
**unenforced** (`grep -c supported_actions backend/routes.py` = 0), so it is a UI hint rather
than a gate. And a `Signal.title` reaches the model prompt unredacted, because only *evidence*
passes the chokepoint — which is why the plugin must put internal free text in `Evidence`, not
in a `Signal` field.

The plugin also cannot be built or CI-tested in this tree (the public scrub gate rejects
internal identifiers), so amazon-plugin.md specifies a three-layer verification split: a
contract-test kit the public core must publish, a private CI matrix including a
redaction test over **real** internal identifier shapes, and a signed human checklist for
provenance, disclosure and off-shift behaviour.

### 6.4 Seam gaps, both halves in one list

amazon-plugin.md §14 carries **16** seam gaps: G-1..G-8 from the contract analysis, G-9..G-16 from
the capability analysis, each with the smallest core change that keeps the ADD-only and redaction
guarantees intact. Everything except the propose-mode backend is small — under ~40 lines. Two of the
new ones (G-13 knowledge export, G-14 document-shaped writes) are gaps whose recommended resolution
is *not to close them*. Only two are security-load-bearing: G-11 (a companion rotation source
cannot gate writes — the one that actively misleads) and G-12 (team-wide differential autonomy,
which must be narrow-only or it becomes remote privilege escalation over a git push).

The capability pass also **corrected** one contract-half claim: `VALID_ACTIONS` now holds four
verbs, not three, because `ACTION_SILENCE` landed after the report's line-number baseline. G-1's
argument is unchanged — the set is still closed and still validated in two places — but the plugin's
usable vocabulary is one verb wider, and it happens to be the safest one.

## 7. Recommended sequence

The ordering principle: **fix what silently lies, then close what a stranger hits on day two,
then add reach.** A new signal source multiplies the cost of every wrong-record bug, so
correctness precedes capability even though capability is more visible.

1. **Call `write_log`; stop advertising an empty field** (§4.2). One call site from
   `store.transition`. First because the renderer is already written, it is the app's cheapest
   unshipped capability, and the spec currently documents a file that cannot exist. Local file
   only — defer any HTTP route until redaction and a `security_posture.py` entry come with it.
2. **Make `needs_human` sweepable, with its own longer threshold** (§4.4). Small, plus the
   sweep test the existing guard does not provide. Second because it is the quietest real
   bug: an alarm silently stops being worked *and* stays claimed forever.
3. **Redact on the ledger write path** (§4.7). Before any ledger has entries in the wild —
   redaction changes the content-addressed id, which is correct (two entries differing only
   in a redacted secret should dedupe) but must not shift ids under existing installs. Do this
   before promoting team sync anywhere.
4. **Gate `POST /ledger/hygiene` on `is_primary()`; warn or refuse on a multi-login window;
   give non-leaders a pull path** (§4.8). Three small changes. Now, because the app's own
   record calls a duplicate prune worse than a duplicate claim, and because a SOP currently
   claims a gate that does not exist.
5. **Fix the fingerprint over-merge** (§3.2). Medium. Before step 6, because a proposal citing
   a collided ledger match is a confident wrong answer, and before any exact-identity work,
   because a shape hash that merges 4xx with 5xx makes every fast-path match suspect.
6. **Give `propose` a backend** (§4.1): persist the verbatim draft, an approve endpoint that
   executes *exactly* the stored text and refuses if it changed, and a bump-then-stop timeout.
   The largest item here and the one the internal plugin has the strongest claim on — but
   after 1–5, because a proposal built on a wrong record or a collided match is worse than no
   proposal.
7. **Human opt-out / per-fingerprint mute** (§4.5). Small, with a board button. This is the
   day-two problem for a stranger, and it is a prerequisite for turning any provider up.
8. **Credential-expiry watchdog + cron self-health** (§4.9). Reuses `cron_pause`/`cron_resume`,
   already declared and unused. Pause the on-shift tier, notify once per 24h, and resume only
   what it itself paused — `rotation-check` must stay the sole arming authority.
9. **Handover: stale bucket, unclaimed backlog, resolved count, aged bucket** (§4.10). Four
   small changes over data the app already owns and already serves elsewhere.
10. **`claimed_by` provenance** (§4.11). One string field, and it de-risks the mesh work the
    backlog already scopes.
11. **Then reach:** the curated knowledge tier and inbound doc ingestion (§4.6, the connector
    already exists), a generic expectation/absence-check source (§4.3), and `extra_actions`
    plus `redact_via_context` to unblock the companion (§6).

Two notes on how the companion work interleaves with this list. First, the companion's own read
path is **not** waiting on any of it — amazon-plugin.md §8.2 stages 1–4 and 6 are unblocked today,
so the internal signal sources can be built in parallel with steps 1–5 rather than after them.
Second, the two items above that the companion's *write* path genuinely gates on are step 3
(ledger-write redaction) and the pre-push veto that travels with it; a third, giving a companion
rotation source a say in the write gate (amazon-plugin.md G-11), is not in this list and should be —
it is ~20 lines and it currently misleads, because arming works and so writes look gated when they
are not.

Steps 1–4 are each small and each fix something that currently misreports. Nothing before
step 6 requires a new concept.
