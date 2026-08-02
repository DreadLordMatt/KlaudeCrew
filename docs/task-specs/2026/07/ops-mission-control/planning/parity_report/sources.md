# Sources — what was read, what was re-verified, what could NOT be read

Companion to [README.md](README.md). Date: 2026-08-01.

This file exists so a reader can tell the difference between a claim grounded in code I read,
a claim I re-ran this session, a claim taken from the app's own planning docs, and a claim
resting on evidence nobody could obtain. The last category is the one that matters most and is
listed first.

---

## 0. What was produced, and in what order

Four files, and they were **not** all produced in one pass. A reader inspecting timestamps or
line-number drift deserves to know why.

| File | Content | Provenance |
|---|---|---|
| [README.md](README.md) | The parity report proper: what is implemented, what is missing ranked by operator cost, deliberate non-gaps, the companion summary, the recommended sequence | First pass |
| [parity-matrix.md](parity-matrix.md) | One row per source capability, four domains, with placement calls | First pass |
| [amazon-plugin.md](amazon-plugin.md) §9–§17 | The companion plugin's **seam, security and packaging contract** — *can this exist safely* | First pass |
| [amazon-plugin.md](amazon-plugin.md) §4–§8 and §14's G-9..G-16 | The companion plugin's **capability surface, acting/remembering half** — action sinks with required autonomy tiers, the rotation adapter, the three knowledge bridges, the scope boundary, the eight-stage build order | **Second pass, re-run separately.** The first attempt at this half was lost to a stalled agent and produced nothing. It was re-commissioned as its own research task and integrated afterwards |
| [amazon-plugin.md](amazon-plugin.md) §2–§3 and §14's G-16 | The companion plugin's **capability surface, ingestion/context half** — the eight signal classes, the fingerprint hazard, the snapshot discipline, the six evidence types, and the deterministic-handling seam gap | **Third pass, written directly.** The agent commissioned for this half stalled on all six attempts in the second pass as well — twelve stall retries across two runs, the same slot both times. Rather than re-commission it a third time, the source facts were gathered by a single focused read (the nine SOPs and missions, both architecture doc pairs, the two vendor-data scripts) and the section was written directly against them. **Consequence a reader should weigh:** this half did not go through the adversarial claim-check the other three files did. Its app-side citations were each verified against the live code at the time of writing, and its source-side numbers come from a fresh read with line numbers, but no second agent tried to refute it |
| This file | Sources, gaps, re-verification | First pass, amended in the second and third |

**Why the provenance matters, not just as bookkeeping.** The capability half was written against a
worktree that had moved on from the report's `e7a90677` line-number baseline (§7), and it found
four symbols the contract half could not have seen because they did not exist yet:
`ACTION_SILENCE` / `EXPIRING_ACTIONS` (a fourth action verb with a mandatory bounded expiry),
`Signal.provider_key` / `LedgerEntry.provider_keys` (an exact provider-identity match tier), and
`registry.poll_health` (per-source last-poll outcome). The first of those **corrects** a claim in
the contract half: amazon-plugin.md §14's G-1 says `VALID_ACTIONS` holds three verbs and calls a
fourth unreachable, which was true at the baseline and is not true now. The gap's argument survives
(the set is still closed, still validated in two places); its arithmetic did not. That correction is
recorded inline at G-1 rather than silently applied, and amazon-plugin.md's header states the
baseline caveat for both halves.

The two halves also differ in what they were allowed to assume. The capability half references the
contract half by section number and treats redaction, credentials, provenance, admission and
packaging as **settled** — it does not re-derive them. So a reader who disagrees with a §9 or §10
claim should expect §1–§8 to inherit the error rather than catch it.

**A note on the sources for §2–§3 specifically,** since that half was written without an
adversarial pass. Every threshold, cadence and do-not-act rule attributed to the source carries a
`path:line`, and the read that produced them also surfaced four internal inconsistencies in the
source itself which are recorded where they are relevant rather than smoothed over: a freshness SOP
whose declared input columns do not exist in the file it points at; a drift SOP referencing a
baseline document that does not exist; a vendor-data grace window stated as 6h in one place, 12h in
its own templates, hardcoded 6h in the heartbeat script and defaulted to 12h in the checker; and two
architecture documents duplicated across both packages that have since drifted apart. The last of
these became a design constraint in §3.3 rather than a footnote — an evidence adapter that reads the
stale copy supplies a confidently wrong architecture.

---

## 1. Could NOT be read — evidence gaps

These are stated in the report body where the claim would otherwise sit, not only here.

| What | Why it failed | What the report does about it |
|---|---|---|
| The internal ops workflow site's static-asset and metrics paths (`/sitemap.xml`, `/robots.txt`, `/llms.txt`, `/api/metrics`) | All three static paths returned HTTP 403; the metrics path returned an error with empty content | Cannot rule out that the site exposes machine-readable metrics or a route manifest never seen. Four headline figures on that site (an automation-rate percentage, a weekly hours-saved figure, a 24/7 coverage claim, and a "~50% of tickets from one pipeline family" figure) appear **only** as headline tiles with no derivation anywhere reachable. They are treated as unaudited marketing figures and are **not** used anywhere in this report as evidence of a capability |
| The source context package's repository ROOT listing | One tree listing timed out (>175s) | Worked around by listing the `sops/` subtree directly, so the full SOP filename list is known. But `knowledge/`, `lessons/`, `oncall-context/`, `scripts/`, `skills/` and `templates/` contents are known from targeted reads and from what the SOPs describe, **not** from a directory listing. No claim in this report depends on a file being absent from those directories |
| 7 of the source's 33 SOPs | Not read: `appsec-cr-monitor`, `auto-implement-watch`, `intake-refresh`, `roadmap-refresh`, `roadmap-sized-refresh`, `smartsheet-ingestion`, `task-folder-sync` | The capability list is a **floor, not a ceiling**. Domain 4's missing count in particular could grow |
| The source's re-sizing pipeline | Explicitly external to the package ("workflow scripts in a separate directory") | The multi-stage classification logic that produced the source's cached sizing state is unavailable; only its output and daily refresher were readable. No report claim depends on it |
| The source's deployed cron directory | Contains script crons with **no** repo counterpart — a one-shot archival job and two per-item zero-token monitors that poll, resolve, update the index, post, and self-remove | This ad-hoc per-item monitor pattern is a real capability living nowhere in the package's `scripts/`. It informs matrix entry 1C-6 and 4C-11 but could not be read in full |
| The app's 15 test files (7,163 lines, plus an empty `__init__.py`) | Counted and spot-grepped, not read line by line | Where the report says a behaviour is "pinned by test" it names the test; it does **not** claim to have verified each assertion. Test-count claims come from `grep -h 'def test_' tests/*.py \| wc -l` = **463**, which contradicts the spec's "145 tests" (spec:1087-1109) |
| PagerDuty adapter behaviour against a real tenant | The app's own record says it was never exercised | The report states this adapter is **built and unverified** for all three of its roles rather than treating it as working. Source: `planning/features.md:901` |

---

## 2. Re-verified independently this session

The research input asserted a set of negative claims ("X has no caller", "Y is never
written"). Negative claims are the easiest thing to get wrong in a summary, so each
load-bearing one was re-run against the tree. All of these **confirmed**:

| Claim | How it was checked | Result |
|---|---|---|
| `store.write_log` has no caller | `grep -rn "write_log" src/ website/src/` | Confirmed. Only `store.py:385` (the def) plus planning-doc references. No route, no SOP, no test invokes it |
| `Incident.proposed_action` is never written or read | `grep -rn "proposed_action\|proposedAction" src/ website/src/` | Confirmed. Only `models.py:325` (field), `models.py:341,353` (`from_dict`), `api.ts:48` (TS type), plus planning references |
| `POST /ledger/hygiene` has no `is_primary()` gate | `grep -n "is_primary" backend/*.py` and `grep -n "primary" backend/routes.py` | Confirmed. `is_primary` appears only at `rotation.py:281,289,359,389`; `routes.py` mentions `primary` only in the settings handler at 531-554 |
| `redact_via_context` is not used by the app | `grep -rn "redact_via_context"` over the app | Confirmed: zero hits |
| `supported_actions()` is unenforced at the route | `grep -c "supported_actions" backend/routes.py` | Confirmed: `0` |
| `claimed_by` does not exist in the app | `grep -rn "claimed_by"` over the app | Confirmed: zero hits |
| `VALID_ACTIONS` is a closed core frozenset validated at the route and the gate | Read `models.py:157`, `routes.py:395`, `rotation.py:220` | Confirmed |
| `needs_human` is not sweepable | Read `store.py:60-66` | Confirmed: `_SWEEPABLE_STATUSES = frozenset({STATUS_DISPATCHED, STATUS_INVESTIGATING})`, while `models.py:88-89` legalises `needs_human → stale` |
| The handover digest emits `age_from` but never buckets on it | `grep -nE "stale\|unclaimed\|progressing\|age_from" backend/handover.py` | Confirmed: `age_from` at `:104`; `open_work` at `:110-147` buckets only waiting/escalated/stalled/progressing; `render_text` never mentions age |
| A stale incident lands in the digest's `progressing` bucket | Read `models.py:126-128` + `handover.open_work` + `store.open_incidents` | **Refuted, and the report was corrected.** `stale` is **not** in `OPEN_STATUSES` (`{unclaimed, dispatched, investigating, needs_human}`), and all four `open_work` buckets derive from `store.open_incidents()` (`store.py:325-331`), so a stale incident appears in **no bucket and no count** — including `total_open`. The gap is real and the operator cost is the same shape, but the mechanism is omission, not mis-bucketing. README §4.10, matrix 1B-11 |
| `reconcile` is on the `on_shift` tier in code | Read `rotation.py:56-78` | Confirmed, **with the in-code reasoning** — the comment explains the deliberate move off `always` because reconcile mutates shared state. SOP frontmatter and the spec's cron table still say `always`; the code is authoritative |

### 2.1 One claim re-run that changed a conclusion

The research input listed fingerprint matching under the app's strengths. A sibling planning
doc (`planning/emitters-comms-gap-research.md:481-507`, dated the same day) claims the
fingerprint over-merges. I ran `compute_fingerprint` directly rather than trusting either,
with `source="cloudwatch"`:

```
python3 -c "import models as m; ..."   # run from backend/

58538b8e259f59c9  4xx error rate high      svc/api
58538b8e259f59c9  5xx error rate high      svc/api
c4dbf4e759b19ceb  p99 latency above 500ms  svc/api
c4dbf4e759b19ceb  p50 latency above 100ms  svc/api
fbf3afe769949bba  replication lag          shard-1
fbf3afe769949bba  replication lag          shard-47
```

**Both the collisions and the digest values reproduce**, including the sibling doc's
`58538b8e259f59c9` for the 4xx/5xx pair — so the finding is independently confirmed twice and
that doc's hashes are current, not stale. Cause is the last entry of `_VOLATILE_PATTERNS`
(`models.py:180-182`) stripping all bare numbers. **`source` is part of the hash basis**
(`models.py:239`), so any digest quoted without its source is unverifiable; an earlier run of
mine omitted it and appeared to disagree with the sibling doc for that reason alone. This is
recorded in README §3.2 as a caveat *inside* the section that would otherwise praise the
ledger, and in matrix entry 2B-7.

---

## 3. App sources read

Under `src/kiro_crew/apps/builtins/ops_mission_control/`:

- `app.json`, `README.md`, `__init__.py`
- All 15 backend modules: `routes.py`, `models.py`, `registry.py`, `companion.py`,
  `dispatch.py`, `store.py`, `ledger.py`, `ledger_sync.py`, `ledger_index.py`, `rotation.py`,
  `slot_watch.py`, `handover.py`, `slack_out.py`, `secrets.py`, `backend/__init__.py`
- All 10 provider files: `base.py`, `__init__.py`, `http.py`, `cloudwatch.py`, `pagerduty.py`,
  `datadog.py`, `github_issues.py`, `webhook.py`, `schedule_file.py`, `noop.py`
- `planning/features.md` (1008 lines — header, the agent-SOPs re-review, the source-material
  coverage review, and the Declined section read in full; the rest targeted)
- `planning/emitters-comms-gap-research.md` (76 KB, dated 2026-08-01 — TL;DR, all of §5's
  13 verified gaps by heading, §7's recommended sequence, and §5.5/§5.8/§5.11 in full)
- Test files: counted and spot-grepped only (see §1)

Skill and SOPs at `src/kiro_crew/builtin_skills/ops-mission-control/`: `SKILL.md` plus all six
SOPs (`dispatch.md`, `investigate.md`, `reconcile.md`, `rotation-check.md`, `ledger-hygiene.md`,
`handover.md`).

UI at `website/src/apps/ops-mission-control/`: `OpsMissionControlPage.tsx`, `SignalsPanel.tsx`,
`HandoverPanel.tsx`, `SettingsPanel.tsx`, `IncidentChat.tsx`, `api.ts`.

Core files consulted for the plugin analysis: `src/kiro_crew/platform/context.py`,
`platform/interfaces.py`, `platform/admission.py`, `platform/discovery.py`,
`platform/bootstrap.py`, `src/kiro_crew/security.py` (credential patterns and the secret-leaf
registration), `src/kiro_crew/security_posture.py`, `src/kiro_crew/apps/routes.py` (the generic
app-config route), `apps/bridges.py`, `apps/manifest.py`, `apps/hooks_integration.py`,
`src/kiro_crew/knowledge/connectors/local_folder.py` (existence and role only).

Specs: `docs/system-specs/modules/ops-mission-control.md` (1109 lines — header, contracts,
crons, layout, companion and test sections read; remainder skimmed) and
`docs/task-specs/2026/07/ops-mission-control/mesh-claim-arbitration.md` (status header and
rationale).

---

## 4. Source-workflow sources read

Under the source context package:

- **All 33 files in `sops/`** and **all 15 in `missions/`** — except the 7 SOPs listed in §1
- `AGENTS.md`, `README.md`, `doc-sync.yaml`, `doc-sync-state.json`, `doc-sync-heartbeat.json`
- `scripts/install-crons.sh` (frontmatter parsing, the mutex, role detection, the
  dry-run-before-mutation ordering, the five-rule on-call pre-check, the duplicate purge and the
  final session-flag re-assert), `scripts/setup.sh`, `scripts/test.sh`, and the ops-slot / pin /
  chat helper scripts
- `knowledge/`: `sla-table.md`, `aws-accounts.md`, `permissions-matrix.md`,
  `oncall-handover-digest.md`, `deployment-guide.md`, `runbooks/oncall-sop.md` and several other
  runbooks in full; `patterns/README.md` in full; `patterns/remediation.md` 151 of 178 rows;
  `patterns/common-pitfalls.md` structurally (290 KB exceeds a single-read cap — entry *shape*
  is certain, ~320 entry contents were not read); `architecture/service-map.md` and
  `data-flow.md` headers plus section greps
- `lessons/shared-lessons.jsonl` — all 2168 records parsed programmatically for field and value
  distribution; ~10 read verbatim
- `oncall-context/`: `dispatch/index.json` (all 130 entries analysed programmatically for field
  frequency and value distributions; ~8 read verbatim); 10 of 121 per-incident dossiers, chosen
  to span all three id prefixes, both operating modes, two vintages and four outcomes; 3 of 19
  alarm snapshots; `triage-state.json`; the single dated shift-week folder; the single history
  file; the one outbound-comment file
- `templates/`, all three `skills/*/SKILL.md`, and the repo's steering and MCP config
- `scripts/roadmap/` (all 18 Python files plus the driver and design docs),
  `scripts/roadmap-sized/`, `scripts/auto_implement/` (all 10 files),
  `scripts/ticket_folder_sync/`, `scripts/crons/` (both zero-token crons),
  `scripts/check_vendor_data.py`, `scripts/heartbeat_vendor_check.py`

Under the source skillset package: all 26 committed source files
(2143 lines total, excluding the generated `build/` tree) — `README.md`, `Config`, `docs/ROADMAP.md`, all 7 `agent-sops/`, both agent specs,
all 6 `context/` files, all 6 `skills/` files, `scripts/setup-crons.sh` — plus the generated
internal-package-manager build manifest.

The internal ops workflow site (URL in the task brief, deliberately not repeated here): the
root page fetched successfully and is a single-page app; `/index.html` and `/sops` returned
byte-identical content apart from a context marker. Its linked methodology document was also
read and corroborates the site's outcome table verbatim.

### 4.1 Additional sources read for the capability half (second pass)

Re-read or read for the first time when the lost capability section was re-commissioned. Every
citation in amazon-plugin.md §1–§8 was re-opened against the live files rather than carried over
from the research input:

- `source: sops/ticket-investigation.md` in full — the mandatory disclosure block (9-17), the five
  pre-write gates (48-73), the disposition table and the CR lane (126-167, 269), the
  draft-is-the-contract rule (213), the correspondence-not-worklog default (256)
- `source: sops/appsec-cr-monitor.md` and `source: sops/staleness-escalation.md` — both listed as
  NOT read in §1 above, and both read in the second pass. The appsec SOP supplies the isolated
  throwaway clone rule (33) and the never-self-approve / never-force-push rule (38); the staleness
  SOP supplies the three-tier severity ladder and the suppressed-row skip (20-26). §1's "capability
  list is a floor, not a ceiling" caveat still stands for the other five
- `source: sops/key-expiration-handler.md` (seven deterministic steps; the first-time-only comment
  guard at 53), `source: sops/oncall-rotation-check.md` (the ±14-day window and the
  `startDateTime <= now < endDateTime` selection, 15-19), `source: sops/wiki-sync.md` (the
  authority rule at 20, the four guardrails at 27-30, the empty-list no-op at 16),
  `source: sops/mindcraft-sync.md` (mirror-not-migrate at 19-21; the sha1 idempotency and the
  external-PII redaction step at 26-31), `source: sops/knowledge-dream.md` (the ≥3-across-authors
  promotion rule at 23), `source: sops/ticket-dispatch.md` (assignee filter at 35, opt-out phrase
  at 38)
- `source: knowledge/runbooks/oncall-sop.md:28-29` (severity downgrade and reassignment both gated
  on justification, the latter also on a chat heads-up),
  `source: knowledge/runbooks/maxis-intake-folders.md` in full (the opaque folder identifiers, the
  ops-vs-dev routing rule at 27-32, the four-step add-a-folder procedure at 43-50),
  `source: knowledge/patterns/README.md:12-19` (the keyword→file index and the literal grep
  instructions), `source: knowledge/patterns/remediation.md:1-12` (the Confidence / Trust Level
  columns), and the auto-updated / manual marker convention across the four runbooks that carry it
- `source: doc-sync.yaml:93` and `source: doc-sync-state.json:57` (the wiki watch list is empty in
  the live config, which is what makes the wiki bridge inbound-only in practice),
  `source: scripts/setup.sh:307-341` (roster resolution from the directory group)
- `source: lessons/shared-lessons.jsonl` — row count re-counted (2168) and line 1 re-parsed for its
  field set (`ts`, `author`, `category`, `lesson`, `tags`)
- `source-skillset: agents/inscope-leader.agent-spec.json` and `…/inscope-worker.agent-spec.json` —
  **diffed programmatically** rather than described. The delta is exactly four auto-approval
  differences plus the description string; the MCP capability sets, the shared-knowledge loader
  hook and the read-only data-store profile are byte-identical
- `source-skillset: context/workspace-conventions.md:22-32` (the canonical disclosure rule and its
  explicit exemptions), `source-skillset: agent-sops/wiki-sync.sop.md:23-24` (the second statement
  of the wiki-vs-runbook authority rule), `source-skillset: context/doc-sync-schema.yaml:8,21-23`
  (the directory group as membership source of truth, hand-merge allowed)
- The internal ticketing write tool's own parameter schema and the internal on-call service's
  read-tool schema — read directly, which is why the field names and the two hard constraints in
  amazon-plugin.md §4 and the shift-window constraint in §5 are read rather than guessed

---

## 5. Claims taken from the app's own planning docs, not independently reproduced

Marked as such wherever they appear in the report. Self-authored records are useful but are not
evidence of behaviour.

| Claim | Source | Status in the report |
|---|---|---|
| Provider verification status (CloudWatch, webhook, GitHub, Datadog signal path, schedule-file verified live; PagerDuty unverified) | `planning/features.md:899-904`, `planning/journal.md` | Stated as **the author's claim**, not reproduced |
| The two-instance git roundtrip found 4 bugs mocked tests missed | `features.md:526-546` | Cited as recorded history |
| `tier_states` previously computed `on_shift or unknown`, defeating strict gating | `features.md:203-219` | Cited as a recorded self-correction; the current code was read and is correct |
| Three default installs all reported `is_primary=True` before the leader fix | `features.md:226-249` | Cited as recorded history; the fix was read at `rotation.py:281-299` |
| The E2E browser gate ran clean (234 expected, 0 unexpected) | `features.md` browser-gate entry | Cited, not reproduced |
| Claim cost is superlinear in index size (50 entries → 6 ms, 450 → 53 ms) | `features.md` / spec | Cited as the recorded motivation for the 500-row prune, not re-measured |

---

## 6. Where this report disagrees with the app's own records

Recorded here in one place because a parity report that merely echoes a self-authored backlog
is worthless.

| App record | What it says | This report's position |
|---|---|---|
| `planning/features.md:975-987` | "Absence-of-activity detection is already covered… The public equivalent is CloudWatch `INSUFFICIENT_DATA`" | **Disagree.** That is one narrow instance (a CloudWatch metric stopped reporting) of a job class the source treats as first-class. The entry's reasoning about the *SLA schema* being org-specific is sound; the entry conflates the schema with the job class. README §4.3, matrix 1C-1 |
| `planning/features.md:19-22` | `knowledge-preserve` "Covered"; `package-scan`/`wiki-sync` declined as internal build + wiki systems | **Partly disagree.** Correct for the auto-population **mechanism**; it silently takes the runbook **artifact** with it. Ten of the source's 14 runbook files are purely hand-written and depend on no internal system (only 4 carry auto-update markers). README §4.6, matrix 2C-1 |
| `sops/rotation-check.md:34-35` | Hygiene "self-gates on `is_primary()` at runtime" | **False of any code.** Verified: `is_primary` is never called from `routes.py`. README §4.8, matrix 3B-5 |
| `backend/ledger_sync.py:504` (docstring) | "The dispatch cycle and the daily hygiene pass call this" | **False.** `grep ledger_sync backend/dispatch.py` → 0 hits. Only caller is `routes.py:750-759`. README §4.8, matrix 3C-3 |
| `planning/user_manual.md:205` | `autonomy_rules` is "deliberately *not* settable over the API" | **True of the app's own route, false in effect.** The generic `PUT /api/apps/{name}/config` (`src/kiro_crew/apps/routes.py:1406`) replaces the whole config file, and that file is on no protection floor. amazon-plugin.md §12.3 |
| `mesh-claim-arbitration.md` STATUS: SUPERSEDED + `features.md:269-271` | The single-owner model removes the race, so arbitration is unnecessary | **Sound only for a scalar `who`.** Co-primary shifts are a shipped, tested feature (`tests/test_schedule_file.py:430-434`) and the spec itself says the per-instance index does not stop two instances claiming one signal (spec:1074-1080). README §4.8, matrix 3C-1 |
| `docs/system-specs/modules/ops-mission-control.md:886` | Documents `incidents/<id>.md` as an on-disk artifact | Describes a file that **cannot exist** — `write_log` has no caller. README §4.2 |
| Spec cron table + `sops/reconcile.md:4` | `reconcile` is `tier: always` | Stale docs. `rotation.py:56-78` puts it on `on_shift` **with the reasoning in-comment**; the code is authoritative and correct |
| `sops/ledger-hygiene.md:3` | `schedule: "0 3 * * *"` | Disagrees with `app.json`'s `17 3 * * *`. Cosmetic, noted for completeness |
| Spec:1087-1109 | "145 tests" | `grep -h 'def test_' tests/*.py \| wc -l` = **463**. The spec also names only 7 of the 15 test files |
| `planning/emitters-comms-gap-research.md:481-507` | Specific fingerprint hash values | **No disagreement.** Collisions *and* hashes reproduce on a fresh run (`58538b8e259f59c9` for the 4xx/5xx pair). Finding confirmed independently (§2.1) |

### 6.1 Where this report disagrees with *itself*

One entry, from the second pass, recorded here rather than silently fixed:

| Report claim | Where | Correction |
|---|---|---|
| `VALID_ACTIONS = frozenset({ack, resolve, comment})` — three verbs, a fourth unreachable | amazon-plugin.md §14 G-1, and the §0 verdict table's "extend action vocabulary" row | **True at `e7a90677` (`models.py:157`), false in the current worktree.** `ACTION_SILENCE` landed with `EXPIRING_ACTIONS` and `MAX_SILENCE_SECS = 24h`, clamped at the authorization boundary (`models.py:170-199`, `routes.py:437-442`). The gap's *argument* survives — the set is still closed and still validated at both `routes.py` and `rotation.py` — so G-1 stands with the arithmetic corrected inline, and amazon-plugin.md §4.2 A4 puts the new verb to work |

---

## 7. Method notes

- **Line-number baseline.** Every app line number in these four files is against commit
  `e7a90677` (`feat(ops): make the team memory-exchange repo configurable`), the tip when the
  report was written. That commit inserted 78 lines into `backend/routes.py`, so any earlier
  draft's `routes.py` numbers are offset; they were rewritten against `e7a90677` and
  spot-checked line by line. The backend was under active edit while this fact-check ran — if a
  cited line no longer says what is claimed, re-anchor on the named symbol (`_handle_post_ledger`,
  `_SWEEPABLE_STATUSES`, `OPEN_STATUSES`, …) rather than the number. Every **verdict** in this
  report was re-confirmed against `e7a90677` itself, not the dirty worktree.

  **Exception, and it is the one place the baseline is deliberately broken.**
  amazon-plugin.md §1–§8 and its G-9..G-16 were written in the second pass (§0) against the live
  worktree, because their whole subject is what a plugin author would build *now*. Where a symbol
  they cite does not exist at `e7a90677` at all — `ACTION_SILENCE`, `EXPIRING_ACTIONS`,
  `resolve_silence_secs`, `Signal.provider_key`, `LedgerEntry.provider_keys`,
  `registry.poll_health`, `ClaimedIncident.exact_match_ids` — that is stated inline, because a
  reader diffing against the baseline will not find it. Verified: `git show e7a90677` finds zero
  occurrences of each of those seven names anywhere in the app backend.

  **And the offsets, because "written against the live worktree" is not only about new symbols.**
  The capability half's line numbers are worktree numbers throughout, not just where a new symbol
  appears. Measured drift between `e7a90677` and that worktree: `registry.py` 410→485,
  `models.py` 454→529, `routes.py` 932→1002, `ledger.py` 387→447, `dispatch.py` 548→580,
  `store.py` 430→461, `slack_out.py` 281→333, `providers/pagerduty.py` 227→259,
  `providers/github_issues.py` 212→217. Any capability-half citation into one of those nine files
  is offset by that much and lands on unrelated text if resolved at the baseline — checked
  mechanically: of 74 distinct `file:line` citations in §1–§6 and G-9..G-16, 74 point at
  substantive code in the worktree and 60 do at the baseline. The other nine backend files
  (`rotation.py`, `handover.py`, `providers/base.py`, `providers/schedule_file.py`,
  `providers/noop.py`, `companion.py`, `secrets.py`, `ledger_sync.py`, `ledger_index.py`) are
  byte-identical at both points, so citations into them are unambiguous. amazon-plugin.md's
  header now states this split; the fix for a reader is the same either way — re-anchor on the
  named symbol.

- **Placement calls** (public core / companion / nowhere) are my judgement, applying one test:
  does the capability's *shape* require an internal system, or only its *content*? A curated
  runbook tier is portable; a specific internal wiki's API is not.
- **Counts** in the summary tables count *source capabilities*, one row per capability, not
  effort or lines of code. A domain with more rows is not necessarily further from parity.
- **"Undocumented"** means: grep across the app backend, `app.json`, the README, the six SOPs,
  `SKILL.md`, the 1109-line module spec, `features.md`, and (added after it was discovered)
  `emitters-comms-gap-research.md` found no entry declining or acknowledging the item. Several
  items the four research passes labelled undocumented **are** documented in the emitters doc;
  those were reclassified and are listed in README §4.12.
- **Redaction of this report itself.** Internal systems are named generically throughout. No
  internal hostname, account identifier, alias, ticket id, review id or internal tool
  invocation appears in any of these four files. The output was grepped against the full
  forbidden-marker set before delivery and returned clean.
