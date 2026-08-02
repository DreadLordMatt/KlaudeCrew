# Ops Mission Control — Work Journal

Running log: what was done, what was decided, and what broke. Newest first.
Bugs are recorded with how they were FOUND, because the how is usually the
transferable part.

---

## 2026-07-31 (last) — Built the vectorization half; lost two doc sections to my own sync

### The adaptor

`backend/ledger_index.py` + 18 tests — the "back to database" half of the owner's
vector-DB → git → DB request. Design choices that carry the scale requirement:

- **Deferred embedding.** `write_episodic(defer_embedding=True)` then ONE
  `backfill_missing_embeddings()` sweep per batch. Inline embedding is ~0.4s per 2000
  chars, so a 10k import would hold the caller for over an hour; the store's own docstring
  flags this and the deferral path exists for exactly this kind of bulk writer.
- **Two incrementality guards, in cost order.** A local id cursor first (set difference, no
  DB round trip), then the store's exact-text check as the correctness backstop for a lost
  or corrupt cursor. A corrupt cursor degrades to "re-check everything", never to "assume
  imported" — the latter would leave the index permanently and silently stale.
- **Bounded per call** (500), so a 100k first import drains across dispatch cycles instead
  of stalling one.
- `preserve_existing=True` on every write, asserted by test: import is merge-only and must
  never tombstone a row a teammate owns.

**A measured finding worth keeping:** my first scale test took **21 seconds** for 2000
entries. Cause is real — `ledger.upsert` re-reads the whole file to merge by id, so bulk
seeding through it is O(N²). Checked whether that matters in production before "fixing"
anything: it does not, because a route calls `upsert` once per recorded lesson, never in a
loop. So the fix belonged in the test's seeding (one `_write_all` pass, 23s → 0.77s), and
the O(N²) is now documented for whoever writes a bulk importer next.

### I lost two doc sections to the cross-repo hazard I keep warning about

While updating `features.md` I found that BOTH the "Shared memory over git" design section
(written last turn) and the "v1 success criteria" scorecard (the turn before) were **gone
from both repos**. Not a tool failure — my own doing: I wrote them to repo-2, then later
copied repo-1 → repo-2 for a different file set and clobbered repo-2 with the older copy.
The journal survived because it happened to be synced in the other direction.

This is precisely the two-working-trees risk I have flagged every turn, landing on me. It
cost the *record* of two decisions, not the code — but a lost design rationale is how a
future reader ends up re-litigating a settled trade-off (vectors in git) from scratch.
Restored both sections and verified byte-identical: 296 lines, 11 sections, both repos.

**Process change:** when a turn writes docs in repo-2 and code in repo-1, sync direction is
per-file, not per-turn. Verify with `grep -c` on the destination after every copy — the same
lesson as the registration restore two turns ago, which I evidently had not generalized.

382 tests each, mypy clean on 39 files, spawn audit clean for this app, scrub-lint clean.

---

## 2026-07-31 (final) — Git as shared memory; and a vectors-in-git decision

Owner proposed modelling shared memory + oncall on a git repo (as `InscopeTeamContext`
does) with GitHub logins for identity, then asked for a "vectorized database → git → back
to database adaptor" that scales to a large number of memories.

**Studied the source first.** `shared-lessons.jsonl` is 2,165 lines of append-only JSONL
with `author`/`ts`/`category`/`tags` — structurally the same shape as this app's ledger,
which is exactly why the idea works. And `conclave.yaml` points `oncall_schedule` at an
internal service URL, which is precisely the dependency putting the schedule *in the repo*
removes.

**The vectors question had a clear answer once measured.** The store keeps THREE artifacts
— SQLite (text + embedding BLOBs), a FAISS index, and 1024-dim float32 vectors. Only
text/metadata is git-friendly. Since the embedding model is **sha256-pinned**, vectors are
derivable, so committing them buys nothing and costs a lot:

| memories | text in git | vectors if committed |
|---|---|---|
| 10,000 | 3.8 MB | 39 MB |
| 100,000 | **38 MB** | **391 MB** |

Binary blobs also merge-conflict unresolvably and rewrite on every push, so history would
grow well past the raw size. Took this to the owner rather than picking silently; they
chose text-in-git with local re-embedding, and extending the ledger rather than adding a
second store. Both are the choices I would have argued for, and now they are recorded
decisions instead of my assumptions.

**Built:** `backend/ledger_sync.py` — the git transport. Tracks ONLY `ledger.jsonl` via a
generated `.gitignore`, because the dispatch index is last-writer-wins and syncing it would
let two instances each believe they own an incident. Conflict-aware: `read_entries` already
tolerates markers and reconciles duplicate ids, so the app stays correct mid-merge, and
`resolve_conflict` makes that view durable.

**Two of my own errors, both caught by gates rather than review:**

1. I assumed `sandboxed_spawn_argv`'s third return value was a cleanup *callable* and
   called it. mypy: `"str" not callable`. It is a temp-profile **path** the sibling
   `github_issues` adapter unlinks — I had the pattern to copy and did not read it closely
   enough.
2. The spawn audit flagged **four** hits in `test_slack_out.py` — my own test file from
   earlier this session, using `asyncio.run`, which the scanner treats as spawn-shaped.
   `ledger_sync._git` was clean (correctly sandbox-routed); the failure was tests I wrote
   turns ago and never ran this gate against. Converted to `IsolatedAsyncioTestCase`,
   which is the convention `test_config_routes.py` already documents for exactly this
   reason. App is now at **0** spawn-audit hits; the remaining failures are
   `auto_improvement`'s.

**Not built, deliberately:** the vectorization half (ledger→index adaptor, semantic match,
scale test) and the schedule-file `RotationSource`. Design is settled and validated against
the real `VectorMemoryStore` API — `write_episodic(preserve_existing=True,
defer_embedding=True)` plus `has_episodic_text` for the incremental guard, which is the
store telling me the intended shape. Recorded in features.md as actionable work rather than
half-built.

*Environment gap worth stating:* the embedding model is not downloaded in this sandbox, so
I could NOT empirically confirm same-text→same-vector. The sha256 pin makes derivability
sound, but the scale test must verify it where the model is present rather than trusting
the pin.

364 tests each, mypy clean on 37 files, spawn audit clean for this app, scrub-lint clean.

---

## 2026-07-31 (last) — Audited the goal's FIRST deliverable against what shipped

The goal opened with "start with the ideation phase, write plans and specifications." I
wrote both on day one and then never looked at them again — every turn since has
maintained the *module* spec. So I audited the originals against reality.

**The task spec is incomplete, not wrong — an important distinction.** Three subsystems
shipped after it was written and appear nowhere in it: Slack output, slot-derived status,
and the shift handover digest. Several planned details also changed under real use (the
two `→ resolved` transitions reconcile needed, evidence brokering, per-adapter budgets).

But I checked before concluding it was stale in the damaging sense:

- It promises **nothing** that was not built — no orphaned commitments to explain away.
- All 8 `§`-numbered citations from shipped code resolve to real sections. The citations
  are accurate for what the document does cover.
- §12 already declares itself **archival**, naming the module spec as the living
  document — which is the repo's convention and exactly what I have been maintaining.

So a rewrite would have been wrong: rewriting a historical design record to match the
present destroys the record. What was missing was a **pointer**, so a reader who lands
there (10 code comments send them) knows which three subsystems are absent and where
current behavior lives. Added that to §12.

**Then I scored the v1 success criteria from ideation §8**, which is the closest thing the
goal has to a definition of done. Result: **5 of 7 fully met, 2 partially**, and the two
gaps are honest ones I will not claim:

- #3 wants an *unattended* run — cron fires, nobody watching. Every component is verified
  against a real account, but I have only ever triggered cycles manually; the crons ship
  paused. I have never observed the thing the app exists to do happening on its own.
- #4 wants the second occurrence "measurably faster". I verified the loop closes
  (`fast_path: true` after a hygiene promotion) but never timed two investigations, so
  "measurably" is unearned.

Both need a live install left running, which is the owner's call, not more code from me.
Recording them as partial rather than met is the point of the exercise.

No code changed this turn.

---

## 2026-07-31 (final) — Confirmed the regression is closed, and stopped there

Last turn's finding was that the app had been invisible in repo-1 for a full turn while I
reported it verified. Rather than add more, I checked whether that state is genuinely
closed and whether the existing coverage would catch a recurrence.

**Closed.** Both registrations present in both repos, app and skill trees byte-identical,
`tsc -b` clean (so the hand-restored lazy import resolves), app visible and enabled on the
live gateway, 24/24 browser specs green, 364 tests green in both repos.

**Coverage is adequate — verified rather than assumed.** I traced what actually happens
when a route is unregistered: `BuiltinAppRoute` calls `getBuiltinComponent(path)` and, on
a miss, returns `<Navigate to="/chat" replace />`. So the page silently redirects. The
spec at line 195 asserts the *page header* renders, which that redirect breaks — meaning
the browser suite does catch this class of regression, on top of the two static assertions
added the turn before.

So there are three independent guards on the same failure now (backend list, frontend
registry, rendered page), and I deliberately added a fourth: **none**. The right response
to "a test caught a real bug" is to trust the test, not to pile on more.

**No code changed this turn.** The finding is that the previous turn's damage is fully
repaired and the guards that caught it are sufficient.

---

## 2026-07-31 (last) — Datadog verified live; the registration guard earned itself

Two things, and the second is the more useful lesson.

### Datadog verified against a real org

Owner added `DATADOG_APP_KEY`. Findings from measuring rather than assuming:

- **The key was not 40-hex lowercase** — my shape check said "other". Datadog app keys
  can be mixed-case, so the heuristic was wrong, not the key. The live endpoint returned
  **HTTP 200**. Shape checks are a hint; the API is the authority.
- **The org had ZERO monitors**, so working credentials still did not prove the signal
  path. This is where the owner's original instinct pointed at something real, just not
  what they named: the adapter reads the **monitors API**, so what was missing was a
  monitor, not an agent. Asked before writing to their org rather than assuming approval
  stretched that far.
- `No Data` is correctly excluded from firing states (`Alert`/`Warn` only), so the test
  monitor needed real metric data rather than merely existing.

Verified end to end: `Alert` → `critical`/`firing`, working deep link, `dd_monitor_id`
and tags in labels, fingerprint computed, cycle claimed INV-1 at `observe`, second cycle
claimed nothing (dedupe holds against a live provider). Then deleted the monitor — org
back to 0, dev home shows Datadog disabled with an empty secrets file.

*Non-defect chased down:* `datadog-evidence` returned 0 items while `configured: True`,
which looked like the CloudWatch namespace bug again. It queries
`/monitor/{id}/downtimes` and the live API confirms that monitor had none. Checked the
endpoint instead of concluding from the empty result.

### The registration guard caught a regression I caused

Running the suite afterwards, repo-1 had **2 failures repo-2 did not** — and they were
the two registration tests I added LAST turn. Both `ops_mission_control` registrations
were missing from repo-1: my `cp`-from-backup restore, after deliberately deleting them
to prove those tests worked, had silently not taken effect. **The app was invisible in
repo-1 and I had reported it as verified.**

That is the sharpest example this session of why the negative case matters. I removed the
registrations to prove a test could catch their absence; the restore failed; the test then
caught the absence for real, one turn later, in a repo I was not looking at. Without those
two assertions the only symptom would have been a blank page nobody hit for weeks.

Restored by editing repo-1's files directly (preserving `auto_improvement`'s entries
rather than overwriting from repo-2), and confirmed both repos now agree. 364 tests each.

**Process change:** verifying a deliberately-broken-then-restored file needs the restore
CHECKED, not assumed — `grep -c` after `cp`, every time. I had done that for some of these
and not for this pair.

---

## 2026-07-31 (last) — Datadog verified against a real org

Owner added `DATADOG_APP_KEY`, which unblocked the testing task. Two findings before the
verification even started, both from measuring rather than assuming:

**The app key was not 40-hex lowercase** — my shape check reported "other". Datadog app
keys can be mixed-case, so the shape heuristic was wrong, not the key. Tested it against
the live endpoint instead: **HTTP 200**. Shape checks are a hint; the API is the authority.

**The org had ZERO monitors.** So credentials working did not prove the signal path —
`poll()` correctly returned 0 signals with nothing to read. This is where the owner's
original instinct ("install an agent") pointed at a real gap, just not the one they
named: the adapter reads the **monitors API**, so what was missing was a monitor, not an
agent. Asked before writing to their org rather than assuming approval extended that far.

With approval: created ONE clearly-named monitor, and found that `No Data` is correctly
excluded from firing states (only `Alert`/`Warn`), so it needed real metric data — pointed
it at a custom metric, submitted 42 against a threshold of 10, waited for evaluation.

**Verified end to end:** `Alert` → `severity: critical`, `state: firing`, working deep
link, `dd_monitor_id` and tags preserved in labels, fingerprint computed. Full dispatch
cycle claimed INV-1 at `observe`; a second cycle claimed nothing, so dedupe holds against
a live provider.

Then **deleted the monitor** and confirmed the org is back to 0. Local test state was a
throwaway home, so the dev home still shows Datadog disabled with an empty secrets file —
no live third-party credential left configured anywhere.

**One non-defect I chased down.** `datadog-evidence` returned 0 items while reporting
`configured: True`, which looked like the CloudWatch namespace bug all over again. It is
not: it queries `/monitor/{id}/downtimes` and the live API confirms that monitor had none.
Checked the endpoint rather than concluding from the empty result.

Datadog signal path now verified live. The action path (mute/comment) stays unexercised
because it WRITES to the tenant, which is a different consent question.

---

## 2026-07-31 (final) — Guarded the agent's API contract

Datadog and PagerDuty both need tenants from the owner, so I took the last surface I can
reach alone: the **SOP→route contract**. The SOPs are the agent's API documentation —
every one instructs `curl "$GATEWAY/api/apps/ops-mission-control/..."` — and a wrong path
or verb there fails only at runtime, mid-investigation, with nothing failing at build
time. Structurally identical to the `omc-*` cron names that made tier arming inert.

**The contract is currently correct.** All 10 endpoints the SOPs reference resolve to
registered routes, and every `curl -X` verb matches what its route accepts. Rather than
report that and move on, I made it a standing test — a contract that is right today and
unguarded is one route rename from being wrong.

**Two of my own measurement errors, both caught by not trusting the first result:**

1. My initial method check reported **three mismatches** (`GET` against POST-only
   routes). The SOPs were fine; my regex treated `-X` as optional immediately after
   `curl`, and these commands are `curl -sS -X POST`, so `-sS` broke the match and every
   call defaulted to GET. Read the actual curl lines before believing the tool. The test
   now matches `-X` anywhere on the line, with that trap written down in the docstring.
2. The first version of both tests **SKIPPED** — wrong `parents[]` index for
   `builtin_skills`. A skip reads as a pass in the summary. Counted the parents instead
   of guessing. That is the fifth time this session a green line meant "did not run"; the
   habit of checking the failure case is the only reason any of them surfaced.

Verified by typo'ing `/incident/transition` → `/incident/transitions` in the SOP: the test
fails naming the file and the unregistered path, then passes on restore.

364 tests each (40 subtests), both repos consistent, flake8/scrub-lint clean.

---

## 2026-07-31 (last) — The git-merge claim was wrong, and it cost a real bug

Datadog is still blocked (no Application key in `.env` — rechecked), so I took the next
`Later` item: ledger git sync. Before building plumbing I tested the claim the whole
design rests on, and it did not hold.

**The spec said a git merge of two ledgers is "a dedupe rather than a conflict".** I set
up two real data homes, had each learn the same lesson plus one local one, committed them
to divergent branches, and ran an actual `git merge`. Result: **CONFLICT**, with
`<<<<<<< HEAD` markers — both branches appended to the same region, which is exactly what
git conflicts on. Content addressing makes the *entries* reconcilable; it does not make
git resolve them for you. Those are different claims and the spec conflated them.

**And the reconciliation did not exist.** `read_entries` appended every line, so after a
merge one shared lesson counted twice: `stats()` inflated, `match()` returned the same
entry twice (presenting one hypothesis to the agent as two), and the handover digest
listed one pattern as two. Fixed at the read boundary, using the same algebra as
`upsert` — fingerprints union, strongest confidence/trust, highest `use_count`.

The fingerprint union is the load-bearing part. Dropping one branch's fingerprint means
that recurrence stops matching, so the ledger would keep working while silently no longer
recognizing half its own history — the same class of failure as the fingerprint-drift risk
already documented in §1.

**Verified against the real artifact, not my simulation of it.** I first duplicated lines
by hand to model a merge; that was a guess about git's behavior and it was wrong in an
interesting way (git conflicts rather than duplicating). Feeding the genuinely conflicted
file through the app: markers skipped as malformed (already handled — that one was
existing good design), 4 raw entries read as **3**, shared lesson collapsed with both
`fp-alice` and `fp-bob` preserved. Pinned with a test that builds a conflicted file
containing real markers.

**A false alarm I chased.** Mid-investigation I saw "DLQ fills on AccessDenied" listed
twice and thought the ids differed, which would have meant content addressing was broken
outright. It did not — both were `d8be5ebb38c38aa5`, the same entry twice, which is the
duplicate case. Checked the actual ids instead of trusting the pattern list.

362 tests each, both repos consistent, mypy/flake8/scrub-lint clean.

---

## 2026-07-31 (final) — Full-repo lint, then per-adapter evidence budgets

Two things this turn: closed a verification gap in my own process, then took the one
`Later` item that was genuinely unblocked.

**The process gap.** `AGENTS.md` mandates `flake8 src/kiro_crew test` and `mypy
src/kiro_crew` — repo-scoped. I had been running both scoped to my app while editing
shared core files (`security.py`, `security_posture.py`, `apps/bridges.py`,
`providers/__init__.py`). Ran them properly: **mypy clean across all 643 files**, and
flake8's 157 findings are all pre-existing pep8-naming in files I never touched (my two
modified `test/` files are absent from the list). So the narrow runs happened to be
sufficient — but I could not have known that, and saying "gates clean" while running a
subset of the mandated gates was overclaiming.

**Per-adapter evidence budgets.** One `EvidenceBudget` served every adapter, which does
not match reality: a Logs Insights query is submit-then-poll and wants ~25s, a Datadog
REST call answers in seconds or is broken. CloudWatch had already noticed — it declared
`_LOG_MAX_WAIT_SECS = 25.0` then applied `min(25.0, budget.timeout_secs)`, so against
the 20s global **its own ceiling was unreachable dead code**. That is the concrete bug
the backlog item was pointing at, which I only found by reading for it.

`EvidenceBudget.for_source` now resolves an adapter's declared
`evidence_budget_hint`, **clamped with `min` on every field**. The hint says "this is
what I need"; the operator's value stays the authority. An adapter able to raise its own
spend ceiling would be an adapter that sets its own cost — same reason the autonomy gate
lives outside the adapter. Verified live in all three directions: operator 30s → 25s
(hint applies at last), operator 20s or 8s → operator wins.

**I broke my own feature and the test caught it.** I made the hint a `MappingProxyType`
so a mutable class attribute could not be reassigned at runtime — a good instinct that
silently disabled the feature, because `for_source` checked `isinstance(hint, dict)` and
a mappingproxy is not a dict. Every correctly-written hint was ignored. It surfaced only
because the test asserts the **clamped value** (`25.0`) rather than merely that
`for_source` returned a budget. A weaker assertion would have shipped a no-op feature
with a green suite — the fourth time this session the difference between "ran" and
"verified" mattered.

Also pinned that the fan-out waits on the *same* resolved budget it hands the adapter:
passing one timeout in and enforcing another outside kills an adapter mid-call while it
believes it has budget left.

355 tests each, mypy clean on 643 files, both repos consistent, scrub-lint clean.

---

## 2026-07-31 (last) — Audited the drift I had been warning about

Last turn's README test exposed a real cross-repo packaging divergence, so instead of
flagging the risk again I went and measured it: diffed every file this work touches
across both repos.

**The result is mostly reassuring, which is worth stating as plainly as a finding.** The
app tree, the skill tree, the frontend app dir, the spec, and both planning docs are
byte-identical. Four shared files differ and every one belongs to the parallel
`auto_improvement` effort, not to a gap in mine:

- `apps/builtins/__init__.py` + `builtinRegistry.ts` — their app's registrations, repo-1
  only. **Mine is present in both** (checked explicitly rather than inferred from the
  diff being small).
- `setup.cfg` — down to one line, their `agents/*.json` glob. The README glob that was
  genuinely my gap got fixed last turn.
- `scrub-allowlist.txt` — repo-2 carries upstream lines my repo-1 copy predates.

Also confirmed the staged `dist/` in BOTH repos now has the current UI, closing the
stale-bundle defect from two turns ago in both places rather than just the one I fixed.

**The audit did surface one real hole.** Nothing tested that the app is *registered*.
Every other test in the suite imports the app's modules directly, so all ~350 of them
pass whether or not the app is reachable — the single line in `BUILTIN_NAMES` that makes
it exist to the loader was unguarded, and a merge dropping it would produce an app that
is fully tested and completely invisible. The frontend half had vitest coverage; the
backend half had none.

Added both assertions, and verified them the way this session has taught me to: removed
each registration, watched both tests fail, restored, watched both pass. Third
consecutive turn where insisting on the negative case was the thing that made a test
worth having.

351 tests each, both repos green and consistent, scrub-lint clean.

---

## 2026-07-31 (final) — The README, and a cross-repo packaging divergence

The goal's second half is an Amazon-private companion "developed separately". I found
early in the session that the app had no README (siblings do) and got diverted by the
cold-start bug before circling back. Wrote it now, because the companion author is the
one reader who most needs a single place with the contract: the `kirocrew.ops_providers`
entry point, the four Protocols and their exact method signatures, and the four rules
that will not bend (ADD-only, admission-before-import, redaction-for-you,
don't-police-your-own-authority).

Verified every factual claim against the code rather than trusting memory — the backend
file list, the `register_*` method names, the `ActionResult`/`ShiftStatus`/
`EvidenceBudget` types, the provider set — and added a test that pins the companion
contract's symbols against their definitions, so the doc fails CI if it drifts from what
it teaches.

**Then the README test found a real cross-repo divergence.** It passed in repo-1 and
FAILED in repo-2: repo-1's `setup.cfg` carries the `apps/builtins/*/README.md` packaging
glob, repo-2's does not. The glob exists in repo-1 only as part of the parallel
`auto_improvement` effort's uncommitted `setup.cfg` change — so my README would have
shipped from one repo and silently not from the other. This is precisely the "two
working trees, entangled uncommitted state" risk I have been flagging, now made concrete:
the two repos already disagree about what gets packaged.

Asked the owner rather than deciding unilaterally, since the glob is not my change to
make in repo-2. Answer: add it. Did so — one self-contained line any app README needs —
and both repos now package the README identically.

349 tests each, both repos green and consistent, scrub-lint clean.

---

## 2026-07-31 (last) — Verified the app from an actual pip install

Last turn found repo-1's staged `dist/` had been stale for hours, which was a
*packaging* defect rather than a code one. That pointed at the one delivery path I had
never exercised: **the wheel a stranger actually installs**. Everything until now ran
from a source checkout with `PYTHONPATH=src`.

So I built a real wheel and ran the app out of `site-packages`.

**The good news is that it works, and it is worth having proven rather than assumed:**

- 78 app files + 7 skill files in the wheel; `app.json`, all 22 backend modules, and all
  **6 SOPs** present.
- `planning/` correctly absent — working notes must not ship.
- Installed into a clean venv with full dependencies: all 5 signal adapters register,
  autonomy defaults to `observe`, the handover digest refuses to imply health, dispatch
  returns its "nothing is watching" explanation, and `_ensure_builtin_skills` copies all
  6 SOPs into a fresh data home.

**Two things I checked rather than assumed.** `tests/` is NOT in `package_data`, which
looked like it might mean the SOP-delivery test cannot run for installed users — but
every sibling builtin (`auto_improvement`, `code_review_sage`, `issue_radar`) also has
`tests/__init__.py` and ships the same way via `packages = find:`. Consistent with the
established convention, so not a defect I introduced. And a first run failed on
`ModuleNotFoundError: croniter` — that was my own `--no-deps` install, not a packaging
bug; re-ran with the real dependency set.

**Added a fast guard instead of a slow one.** A wheel build takes ~2 minutes, far too
slow for the per-commit gate, so the new test asserts the *rule* the wheel obeys: every
directory under the app holding runtime files must be matched by a `setup.cfg` glob,
with `planning/` and `tests/` explicitly reasoned as exceptions. A new `backend/` sibling
(say `templates/`) would otherwise ship as nothing and break only for pip-installed
users — invisible from a source checkout.

**A test that lied, caught by insisting on the negative case.** My first attempt to
prove the guard works used `sed` to delete the `backend/**` glob; the regex silently did
not match, the test "passed", and I nearly recorded that as verification. Checked the
file, found the line intact, redid it in Python — and the guard then failed correctly.
That is the third time this session a green result meant "did not actually run"; the
habit of always confirming the failure case is what caught all three.

352 tests, wheel install verified end to end, flake8/scrub-lint clean, artifacts removed.

---

## 2026-07-31 (final) — My browser specs had never run in CI

Every "24 Playwright green" in this journal was me invoking Playwright by hand against
my own gateway. The project has a **packaged** browser gate — `test_e2e` /
`test_playwright_e2e.py`, which boots a real gateway wired to the fake ACP backend and
runs the whole spec set. I had never run it. Three findings, in ascending severity.

**1. The executed-spec floor was stale.** `MIN_EXECUTED_SPECS = 208`, and I had added
24 specs (197 pre-existing + 24 = 221 collected). The floor's own comment says to raise
it when adding specs, with a documented history of this suite being silently darkened —
36 of 103 specs once excluded, gate still green. Left at 208 it carried ~11 specs of
slack, so most of my new suite could have been deleted and the gate would have passed.
Exactly the failure the constant exists to prevent, reintroduced by not maintaining it.

**2. Two of my own specs were broken, and only this gate caught them.**

- `Board shows... all three tabs` asserted a `Signals` tab that was not found. Cause:
  the packaged gate serves the STAGED `src/kiro_crew/static/dist`, and repo-1's dist was
  from 04:53 — before I built any tabs. I had been building and staging only into
  repo-2 this whole time, so **repo-1's package would have shipped a stale UI**. That is
  a process defect, not a code one, and worth more than the test failure.
- `shows the four stat cards` asserted `Needs human`, but I renamed that card to
  `Waiting on you` when `blocked_reason` landed. My hand-run suite passed because
  repo-2's bundle still had the old label. A rename that breaks a test only in the
  opt-in gate is invisible for as long as nobody runs the gate.

Added a cheap vitest guard that reads the rendered `<StatCard label=…>` values out of
the page and requires the Playwright loop to assert each one — so this drift now fails
in the default `npm run test` instead of waiting 8 minutes for the browser gate.
Verified it catches the exact regression by putting the old label back.

**3. `--list` is not the executed count.** After fixing my specs, executed jumped
**215 → 230** and skipped went **15 → 0**: one failing spec in a file-level `serial`
describe had been aborting 14 others, which the report counted as *skipped*, not failed.
So `--list` said 220 while only 215 ran. The floor is now set from an observed
green-path run (230), not from collection — a distinction I would have got wrong if I
had trusted `--list`.

**All 24 of my specs now pass in the packaged gate.** The 2 remaining failures
(`embed-popout`, `session-tags-e2e`) are pre-existing and unrelated — `embed-popout`
failed identically in the very first run, before I changed anything.

350 tests + 5 vitest, packaged E2E gate exercised for real, mypy/flake8/scrub-lint clean.

---

## 2026-07-31 (last) — Uninstall keeps your credentials; now it says so

Finished the lifecycle sweep with the step I had never exercised: **disable and
uninstall**. Since it turns on whether third-party tokens are retained, I checked
governance guidance on credential retention first — there is no specific
uninstall-cleanup control, so I applied the general principle (minimize stored
credential lifetime) and then went and measured what actually happens.

**Disable is genuinely clean.** Verified live: crons fully deregister (`/api/crons`
reports zero for the app, not four paused ones) and every route 403s. Credentials are
kept, which is correct — disable is a pause, and wiping tokens would punish a toggle.

**Uninstall leaves live third-party tokens on disk.** `uninstall_app` removes
`apps/<name>/`, but the keystone secret file lives at the crew-home **root** — which is
exactly what puts it on `security._CREW_SECRET_LEAVES` so the agent cannot read or
overwrite its own credentials. The protection and the retention are the same fact.

I deliberately did NOT "fix" this by deleting on uninstall:

- Moving the file under the app dir would hand the agent its own credentials. That
  trade is not close.
- Silently wiping tokens would break uninstall/reinstall, a legitimate flow, and
  surprise-deleting a credential is its own bug.
- The `onUninstall` manifest hook is a **shell command string** — a weaker and less
  auditable path than the app's own already-SEL-audited `delete_secret`.

So the behavior stays and the **disclosure** was the actual gap: nothing told the user.
The uninstall preview lists agents, skills, crons, and dependencies — not credentials.
Settings now states it plainly next to the Revoke button, which is the only control that
changes it. Per-provider revoke already existed and works (verified by revoking my own
probe token).

Two tests pin both halves. The path one is the more valuable: if a future change moves
the secret file under `apps/` — which would look like a tidy-up and would silently drop
the keystone protection — it fails.

**A test that skipped instead of running.** My first disclosure test computed the
`website/` path with `parents[4].parents[0]`, which was wrong, so it SKIPPED — and a
skip reads as a pass in a summary line. Counted the parents properly rather than
leaving a test that asserts nothing. Worth recording because this is the second time
this session a green result meant "did not actually run".

345 tests (343 + 2), 24 Playwright green, mypy/flake8/tsc/scrub-lint clean.

---

## 2026-07-31 (final) — The app could never have fired on a real install

Kept pulling the "what does a stranger actually get" thread and found the most serious
functional bug of the whole effort — one that made the app **inert on every real
install**, and that every previous test missed because my dev environment had crons
resumed by hand weeks ago.

**All four crons shipped `enabled: false`.** The intent was right: a cron that fires
before any provider is configured polls nothing every two minutes. But `dispatch` is
armed by the `on_shift` tier, and the ONLY thing that arms that tier is the
rotation-check cron — which also shipped paused. Nothing in the codebase flips a
manifest `enabled: false` (grepped for it). So the chain was:

> user enables the app → configures CloudWatch → waits → nothing ever happens.

Meanwhile the store listing promised "Rotation-aware: the on-shift automation tier arms
and disarms itself". That was not just unimplemented, it was impossible.

Fixed by shipping `rotation-check` ARMED — the cheap always-tier job whose whole purpose
is to arm the others. Safe because I also gave its SOP a **step 0**: if no provider
reports `configured: true`, produce no output and stop. Otherwise arming it would cost a
fresh install an agent turn every five minutes forever, which is the noise this app
exists to avoid.

**Two existing tests asserted the bug.** `test_crons_ship_paused` and the Playwright cron
check both required all four paused — they encoded the deadlock as intended behavior.
Split them: work crons must be paused, rotation-check must be armed, each with the
reasoning attached so the next person does not "fix" it back.

**A registration subtlety worth recording.** After the manifest change, the live gateway
still showed `rotation-check: False`. Not a failed fix — the app bridge writes manifest
`enabled` verbatim only on install/enable, and the CronService then preserves live user
intent. Correct design (it must never silently un-pause a cron an operator paused), but
it means a manifest default change reaches existing installs only on a disable/enable
cycle. Verified by doing exactly that: `rotation-check` flipped to `True` in both
`app-crons.json` and the live `/api/crons`. Worth stating plainly — this fix helps new
installs and re-enables, not a running one, so anyone upgrading needs the toggle.

343 tests (341 + 4, minus 2 rewritten), 24 Playwright green, mypy/flake8/scrub-lint clean.

---

## 2026-07-31 (last) — Tested the install a stranger actually gets

The goal says "usable to **any external AWS user**", and every verification in this
journal was against my dev environment: CloudWatch wired to a real beta account, two
ledger entries, incidents on the board. That is the most-configured state this app will
ever be in. A stranger's install is the least-configured one, and I had never run it.

So I pointed `KIROCREW_HOME` at an empty directory.

Most of it was right, which is worth stating because it was designed for rather than
lucky: autonomy defaults to `observe`, zero incidents, zero ledger entries, and the
handover digest leads with "the board is quiet because nothing is being watched, not
because nothing is wrong" — refusing to imply health, which is the whole point of that
headline ordering.

**One real gap.** `run_cycle` on a fresh install returned `changed: False` with an EMPTY
`skipped_reason`. Correct in that the cron stays silent, but `polled == 0` is ambiguous:
"nothing is wrong" and "nothing is watching" are opposite conclusions. The dashboard
happened to derive this itself ("No providers are set up yet — open Settings"), so I had
never seen the gap; an agent calling `POST /dispatch` got silence with no explanation.
The very first thing a new user does is press the button, and that is the one moment the
app most needs to admit it is not configured.

Now the backend says it once, so no caller has to infer it. Added
`configured_signal_sources()`, which treats a `configured()` that RAISES as not
configured — an adapter that cannot answer "am I ready" must not be polled, and counting
it as ready would turn "nothing is watching" into a source-level error every cycle.

Verified both directions, because a fix that silences a working install would be worse
than the gap: fresh home now returns the explanation, and my configured environment
still polls its 2 real alarms with an empty `skipped_reason`.

**A probe bug worth recording:** my first fresh-install script crashed with `'bool'
object is not callable`. Every adapter defines `configured()` as a method, while
`ProviderInfo` (the catalog dataclass) declares `configured: bool` — I called the field
as a function. The registry handles both correctly; the inconsistency is only in my
probe. Checked rather than assumed, because "the adapters disagree about their own
interface" would have been a real finding.

341 tests (338 + 3), 24 Playwright green, mypy/flake8/scrub-lint clean.

---

## 2026-07-31 (final) — Ran the gate I had been skipping

Every "gates clean" claim in this journal was against a NARROW slice: the app's own
tests plus `test_app_bridges.py`. But this work modified 8 core files outside the app —
`security.py`, `security_posture.py`, `apps/bridges.py`, `apps/builtins/__init__.py`,
`scripts/scrub-allowlist.txt`, plus two React core components and `builtinRegistry.ts`.
A slice cannot tell you whether a core edit broke something 400 files away. So I ran
both suites in full, which I should have done before saying "clean" the first time.

**Backend: 21,803 passed, 10 failed. Frontend: 6,098 passed, 4 failed.** Every one of
the 14 attributed, because "pre-existing" is a claim that needs checking:

- **5 backend** — missing optional deps in this interpreter (`pdfplumber`, `qrcode`).
  Environment, not code; verified by importing them directly.
- **2 backend tripwires** (`test_spawn_audit`, `test_security_posture`) — the flagged
  module list contains ONLY `apps/builtins/auto_improvement/`, the parallel untracked
  effort. My `slack_out.py` was previously flagged by the same redaction tripwire and I
  registered it as a sink at the time, so this is the check working.
- **3 backend** `test_docker_entrypoint` — "seed must land under $HOME/crew-data". I have
  modified no Docker file.
- **4 frontend** — de/it catalog parity, on keys `chatInput.browser_use` and
  `webPreviewPanel.*`. Browser-use copy in files I never touched. This is the exact
  failure `SettingsPanel.tsx`'s debt note cites as the reason this app's Settings copy
  stays inline English rather than adding catalog keys.

Also confirms two things I had asserted but not proven: the frontend **jscpd
duplication gate reports 0 clones** across 1,001 files (so none of the panels I added
duplicate existing code), and `setup.cfg` + the 10 i18n catalog diffs in the working
tree belong to `auto_improvement`, not to me — worth knowing before anyone commits, since
a careless `git add -A` would sweep another effort's work into this one.

No code changed this turn. The finding is that the verification story now matches the
claim.

---

## 2026-07-31 (latest) — Answered the "missions" question, found two config bugs

Last turn I flagged "missions" as a product question for the owner. That was me
deferring work I could have done myself, so I read all 15 mission files.

**Answer: missions need no new concept.** Every one is a cron-scheduled SOP — the
construct this app already ships. `oncall-harbinger` *is* the reconcile SOP (pin
lifecycle + index reconciliation, 15-min cadence, 10-item cap, exit-silently);
`oncall-rotation-check` gating the others *is* the tier model. Nothing to port. Recorded
that in features.md with the evidence, rather than leaving an open question that reads
like a gap.

Two related non-gaps, also recorded:

- Several missions are proactive digests (`table-freshness`, `schema-drift`) rather than
  alarm responses. The public equivalent of "did this pipeline silently stop" is
  CloudWatch `INSUFFICIENT_DATA`, already supported.
- `knowledge/sla-table.md` maps table patterns → SLA → escalation threshold. Deliberately
  NOT modeled: a CloudWatch alarm already encodes its own threshold, and a generic SLA
  schema for a stranger's warehouse is guessing at their org — same reason the handover
  digest omits rosters. It belongs in a provider adapter or the companion.

**But checking the `INSUFFICIENT_DATA` claim found two real bugs.**

`include_insufficient_data` was compared against the literal string `"true"`. So an
operator typing `yes`, `1`, or `True ` into that Settings text input — or any UI sending
a real JSON boolean — got **silence**: the setting looked applied and stale-metric
detection stayed off. Worst kind of failure for a detection opt-in, because the operator
believes coverage exists.

Then, fixing it, I noticed `provider_enabled` used `bool(...)` — and `bool("false")` is
`True`. A config carrying `"enabled": "false"` would **enable** the provider, the
opposite of what it says. Reachable from a hand-edited config or any form that
stringifies. The failure direction is the dangerous one: a provider the operator thinks
is off starts polling.

Both now go through `config_flag`, which accepts what a human would write and falls back
to the caller's default on anything unrecognized — treating garbage as false silently
disables a detection someone turned on; treating it as true silently enables one they
never asked for. `_FALSY` is listed explicitly rather than inferred as "not truthy", so
that fallback is reachable.

Also: the provider `detail` said only "Alarms in ALARM state", so nothing told an
operator the stale-metric opt-in existed. An opt-in nobody is told about is one nobody
uses. Now advertised, with the noise trade-off stated.

**A verification that did NOT prove what I first wanted it to.** Polling with and without
the flag returned 2 signals both times — the beta account has no `INSUFFICIENT_DATA`
alarms right now, so that showed nothing. Re-ran against the API call itself: `['ALARM']`
vs `['ALARM', 'INSUFFICIENT_DATA']`. Worth recording because the first result looked like
a passing test and was actually a no-op.

338 tests (333 + 5), 24 Playwright green, mypy/flake8/scrub-lint clean.

---

## 2026-07-31 (last) — Re-read the source material, found a missing feature

The engineering backlog was empty, so the useful question stopped being "what's next on
my list" and became "does this actually reproduce the workflow I was asked to
reproduce?". Went back to the context package and skillset.

Two things there had no counterpart in the public app: **missions** and an **oncall
handover digest**. The handover one is the real gap — that file is 12 months of shift
notes synthesized into one document, and it was clearly among the team's most-used
artifacts. My app tracked incidents perfectly well and had **no answer at all** for "I'm
taking over the rotation, what do I need to know?".

The source file itself is unusable here — names, aliases, internal URLs, ticket ids,
Quip links. But the *structure* is generic, and its most valuable section is "recurring
issues by frequency", which is structurally identical to my ledger ranked by
`use_count`. So the digest is a synthesis of data the app already owned, with nothing
internal crossing over.

Built: `backend/handover.py` + `GET /handover` + a Handover tab + `sops/handover.md`.

Decisions worth keeping:

- **Headline ordering is the product.** No coverage → waiting on you → routine. A board
  with nothing configured looks calm, so "all quiet" would be actively misleading; that
  case outranks everything.
- **Deliberately omits rosters, owners, tickets, runbooks.** Those are the
  organization-specific half of a real handover doc. Inventing a schema for a stranger's
  org would be guessing, and the SOP explicitly forbids the agent fabricating an owner —
  a made-up assignment is worse than an absent one.
- **Not a cron.** A handover is read by a person at a moment they choose; a scheduled
  one nobody reads is precisely the noise this app exists to avoid.
- **`proven` reuses the ledger's own `FAST_PATH_*` constants** rather than restating
  "verified/high". A digest that disagreed with the engine about what counts as proven
  would tell a responder to trust the wrong entry.

**Two of my own bugs, both caught by tests I wrote for the feature:**

I expected `escalated` in `open_incidents` — it is deliberately terminal, since the work
was handed to another owner. But it still belongs in a handover ("we passed this on" is
exactly what gets lost at shift change), so it reads from the index instead. That
exposed a second bug: I had been subtracting it from the `progressing` remainder, which
counts OPEN work — so `progressing` went negative once several incidents were escalated.
Both pinned.

**A false alarm worth recording:** I set `blocked_reason` directly on disk to test the
"waiting on you" path, and the digest reported nothing. That was the route correctly
reconciling from the live chat slot and clearing my synthetic value — the reconcile
working as designed, not a bug. Verified through the pure function instead, which showed
`Start here: 1 incident(s) waiting on you`.

**Verified live** against the real board: the digest surfaces both accumulated
beta-account patterns ranked 5× and 3×, both marked `proven`, with the four unconfigured
sources named as blind spots.

Also: a pre-existing test (`test_every_sop_ships_beside_the_skill`) failed on the new
SOP — correctly. It hardcodes the expected SOP set precisely so a new file cannot
silently fail to ship.

333 tests (311 + 22), 24 Playwright green, mypy/flake8/scrub-lint clean.

**Still open from the source material:** "missions" — a longer-lived unit of work than an
incident. Not built, because I do not yet understand whether it maps onto anything a
stranger's ops install would want, and guessing at that is how an app grows a concept
nobody uses.

---

## 2026-07-31 (final) — The credentials gap was a plumbing gap

Closed the last open engineering item, and the useful part is that **the item was
framed wrong**. It read: "the investigating agent's sandbox does not see the operator's
AWS profiles, so it cannot finish an AWS investigation." The implied fix is to plumb
credentials into the agent's sandbox.

That is the wrong fix. Least-privilege guidance prefers scoped access over
distributing credentials, and the agent does not need AWS access — it needs the
*evidence*. The gateway already holds the profile, already gathers alarm history and
logs under a budget, and already redacts every body at a single chokepoint. The only
thing missing was that `investigation_brief` never carried any of it: the brief had
signal metadata and ledger hints and nothing else. So the agent honestly reported it
could not proceed, which *looked* like a credentials gap and was actually plumbing.

Fixed by brokering: gateway reads (credentialed, bounded, redacted) → brief carries the
text → agent reasons. Wired at both claim paths, with `gather_evidence_safely` treating
any fault as "no evidence" — an investigation without evidence is worse than one with,
and far better than a claim we drop because a provider was slow.

**Verified live** against the beta account: two evidence items in the brief — a flapping
OK↔ALARM history and the failing S3 object with the real `ValueError` — and the
redaction visibly working (`receiptHandle` → `[REDACTED: credential]`, etags masked).
Giving the agent its own profile would have produced a second credential holder whose
reads nothing redacts; this way there is still exactly one.

**Then the measurement caught a second problem.** The brief came out at **37,423
chars**. `EvidenceBudget.max_bytes` is 64 KB, which is a sensible cap on what an
adapter may *return* into a spool and a terrible one for a prompt — six calls is ~384
KB, against this project's documented 50k *total* session context budget. Evidence had
never reached the prompt before, so this was new work rather than a regression, but it
would have been an expensive default. Added `MAX_BRIEF_EVIDENCE_CHARS` (8k) and a
per-item cap (4k), and made the brief **say** when it truncates — an agent silently
handed half a log dump reasons confidently about a partial picture. Same brief after:
7,467 chars, still carrying both the alarm history and the root cause.

Also updated the investigate SOP's Phase 2 from "gather evidence" to "read the evidence
you were given", stating plainly that the agent has no credentials and should not try
to run `aws` — the failure is by design, not a misconfiguration to work around. Without
that the agent burns turns discovering the boundary.

311 tests (306 + 5), 23 Playwright green, mypy/flake8/scrub-lint clean.

---

## 2026-07-31 (latest) — Log evidence was unreachable through the UI

Exercised the last unverified core mechanism: evidence gathering. Governance guidance
on logging is explicit that logs must not contain secrets — which is exactly why they
do, by accident — so I checked the redaction path first.

**One thing I got wrong, and it is worth recording.** I grepped for `redact` in the
provider package, found nothing, and concluded the documented control ("MUST be passed
through `security.redact`... the gather helpers do that centrally so an adapter cannot
forget") was never implemented. It is implemented — in `registry.gather_evidence`,
under the aliases `core_redact` / `redact_tokens`, which my grep missed. The docstring
was accurate and I nearly "fixed" a non-problem. Verified the real behavior by feeding
an AKIA key through a fake adapter: redacted.

What was genuinely wrong, found while verifying it:

**The byte budget did not bind.** Truncation ran before redaction, and a redaction
marker is longer than what it replaces, so the emitted body could exceed
`max_bytes` — measured 1094 bytes against a 1000-byte budget on an all-credential
body. Small, but that budget exists to bound what reaches the model's context, so it
has to bound the text actually emitted. Now redact-then-cap, with a `_REDACT_HEADROOM`
pre-trim so the regex work stays bounded too (otherwise I would have traded a cap
overshoot for an unbounded scan).

**Log evidence could never be configured.** `CloudWatchEvidenceSource` advertises
`config_fields` under its own id, so Settings writes `providers["cloudwatch-evidence"]`
— but the gather read `providers["cloudwatch"]`. `log_groups` exists ONLY on the
evidence adapter, so anything the operator typed landed where nothing looked for it
and log evidence was **silently always empty**. Found by trying to configure it, not
by reading. Fixed to read its own namespace with a fallback to the signal source's, so
a single-account install that set `region`/`profile` on `cloudwatch` keeps working.

Generalized it into a test: every advertised `config_fields` entry must be read
somewhere in the module. A field the UI renders an input for but nothing resolves is a
lie to the operator, and this was an instance of that class.

**Verified live end-to-end** against the beta account after the fix: two evidence
items — alarm history showing a flapping OK↔ALARM pattern, and Logs Insights returning
today's actual root cause (`ValueError: File processing failed`). The log half had
never run before.

**A false start on the same investigation:** my first evidence probe returned zero
items and I briefly took that for a bug. It was correct — I had picked the DLQ
incident, whose alarm genuinely has no history in the retention window. The
`ErrorAlarm` incident had five entries. Checking account-wide history is what
distinguished "code is broken" from "this alarm has no history".

Also fixed a test-isolation defect I introduced: `TestEvidenceRedaction` writes
provider config, and with no home isolation one test's value leaked into the next, so
the fallback test read the wrong namespace. It passed alone and failed in the full
suite — the full-suite run is what caught it.

306 tests (299 + 7), 23 Playwright green, mypy/flake8/scrub-lint clean.

---

## 2026-07-31 (later) — The webhook had no tests, and two bugs

Verified the inbound webhook, the last adapter I could exercise without external
credentials — and the one that mattered most: every other adapter POLLS a provider
the operator chose, while this one ACCEPTS input from whoever can reach the port. It
had **zero** tests.

The HMAC implementation itself was sound (`compare_digest`, fail-closed, size checked
before hashing, signature before parse). Exercised the whole matrix live — missing,
wrong, truncated, other-body, and captured-signature-over-tampered-body all refused;
valid and uppercase-hex accepted. That check ORDER is the part worth keeping: nothing
unauthenticated is ever handed to `json.loads`, and there is now a test that pins it
by asserting an unsigned malformed body is rejected for its *signature*, not its
syntax.

Two real bugs, both found by writing tests rather than by reading:

**A signed sender could 500 the ingress.** `signal_from_payload` put its `isinstance`
check in a comprehension's `if` clause — evaluated per item, *after* `.items()` had
already been called on the raw value. `{"labels": "text"}` raised `AttributeError`,
which escaped `enqueue`'s `except` (JSON/Unicode only) and crashed the endpoint.
Replaced with `_normalize_labels`, which guards the type first and caps key/value
length and pair count, since labels reach both the model's context and the
fingerprint. Confirmed the original raised by running the old expression directly.

**The secrets backend froze its own path.** `KeystoneFileBackend.__init__`
snapshotted `secrets_path()`, and the backend is a module-level singleton — so the
data home was fixed at import time and the entire process shared one secrets file.
That silently defeated per-test home isolation: my "no secret configured must reject"
test passed only because a sibling test had written a secret. A fail-closed assertion
that isn't actually testing anything is worse than no test, because it reads as
coverage. Now resolved per access, with an explicitly-passed path still pinned.
Verified load-bearing by restoring the old behavior — the test fails.

**One design defect fixed too:** every rejection returned 401, including "malformed
JSON" and "payload has no title". Those are *authenticated* requests with bad bodies.
A sender debugging a payload was told "Unauthorized" and would go re-check working
credentials, while a genuine signature failure looked identical to a typo. Now 401 /
413 / 400 by reason, with unrecognized reasons falling through to **401** rather than
400 — a refusal we cannot classify must not be reported as "your request was fine". A
test derives the reason set from `enqueue`'s source so a new rejection cannot slip in
unclassified.

**Two of my own mistakes, worth recording.** I guessed the signature header was
`X-KiroCrew-Signature` (it is `X-OMC-Signature`), which made every case in my first
live matrix "fail" — including the ones that should pass. The all-red result is what
told me it was my error and not four separate bugs. Then I removed this file's home
isolation on the theory that `test/conftest.py`'s autouse fixture covered it; it does
not, because these tests live under `src/` where that conftest never loads. The
sibling app tests all isolate manually for exactly that reason.

299 tests (272 + 27 new), 23 Playwright green, mypy/flake8/scrub-lint clean.

---

## 2026-07-31 (final) — The SOPs the whole app depends on weren't on disk

Chased the stale `omc-*` cron names into the SOP frontmatter and, in verifying the
fix reached users, found the most serious bug of the entire effort: **the SOP files
did not exist in the data home at all.** Every cron prompt says "follow the SOP at
`~/.kiro/crew/skills/ops-mission-control/sops/<name>.md`", and every one of those
paths was missing. The app's entire automation layer would have run blind on any
real install — and silently, because a missing skill file is an error nowhere.

Two independent App Kit bridge bugs, both stemming from the skill and the app sharing
the name `ops-mission-control` (which they must — the skill ships under
`builtin_skills/` precisely because a builtin app's own dir is never copied to the
data home). The packaged skill lands at `skills/ops-mission-control/`, the exact path
the skill bridge treats as an app-owned link farm:

1. `_register_skills` did `mkdir(skills/<app>)` **before** checking whether the
   manifest declared any skills. Ours declares none, so it created an empty dir.
2. `_deregister_skills` — called for any skill-less manifest to clean stale symlinks
   — did an unconditional `shutil.rmtree(skills/<app>)`. For our app that path is the
   packaged skill's real directory, so it deleted the skill and all five SOPs.

`_ensure_builtin_skills` runs at gateway start and copies the skill in; app
registration runs after and wiped it. The copy's mtime guard then made it sticky:
once the dir existed (empty), the guard skipped re-copying.

Fixed both to act only on what registration created: no manifest skills → create no
directory; deregister unlinks symlinks and removes the dir only if nothing real
remains, never `rmtree`-ing a directory that holds actual files. Four tests pin it,
including the mixed case (an app symlink beside a packaged file). Verified each test
fails against the pre-fix code.

**Verified end-to-end on a real gateway**, which is the only thing that actually
proves it: removed the data-home skill dir, restarted, and confirmed all six files
(SKILL.md + five SOPs) are present afterwards and carry the corrected content
(reconcile's "Slack is automatic" note, ledger-hygiene's curl, the namespaced
frontmatter). This is a **generic App Kit fix**, not app-specific — any builtin whose
skill shares its app name was exposed; the spec notes `code_review_sage` has the same
shape.

Also swept the last stale `omc-*` references out of the SOP frontmatter and two prose
comments, and added `test_sop_frontmatter_cron_names_match_the_manifest` — the stale
frontmatter is what misled me into hardcoding `omc-*` in `TIER_CRONS` to begin with,
so documentation that lies toward a real bug now fails CI.

272 tests (215 app + 57 bridges), 23 Playwright green, mypy/flake8/scrub-lint clean,
both repos.

---

## 2026-07-31 (last) — Tier arming was inert; the memory loop is now proven

Exercised the two remaining never-run SOPs (`rotation-check`, `ledger-hygiene`). Same
lesson as the reconcile pass: running the documented procedure against the real system
finds things no unit test does.

### The big one — tier arming never worked at all

`TIER_CRONS` listed `omc-dispatch`, `omc-reconcile`, `omc-rotation-check`,
`omc-ledger-hygiene`. The scheduler registers manifest crons **namespaced**:
`ops-mission-control/dispatch`, etc. So every name the tier mechanism emitted matched
no job, and every `cron_pause` / `cron_resume` the rotation SOP issued silently did
nothing. Rotation-driven arming — a headline feature — was entirely inert, and looked
fine from the outside because nothing errors when you pause a cron that isn't there.

Fixed by deriving the names from the real prefix. The test now reads `app.json` and
requires `TIER_CRONS` to equal exactly the registered set, so renaming or adding a
manifest cron fails the suite instead of quietly re-breaking arming. Note the
**pre-existing** `test_rotation_check_is_on_the_always_tier` had been passing this
whole time against a name that never existed — a test can be green and still assert
nothing real.

### `armed_crons` was a foot-gun the SOP stepped into

The rotation SOP said "only ever pause or resume the crons listed in `armed_crons` for
the `on_shift` tier". But `armed_crons` is a **flat union across all armed tiers**, and
off shift it still contains `omc-rotation-check` (an `always` job) — while the SOP's
very next rule warns that pausing that one "would strand the instance with no way to
re-arm itself". The response had no per-tier list to point at, so the safe instruction
was not expressible.

Added `tier_crons` to `GET /rotation` and rewrote the SOP to act only on
`tier_crons.on_shift`. Verified live: the SOP's pause list is now exactly
`['ops-mission-control/dispatch']`.

### Two SOP steps referenced things the agent cannot do

`ledger-hygiene` step 1 said "Call the ledger hygiene pass (`ledger.hygiene()`)" — a
Python call, with no interpreter to make it from. It is an HTTP POST; the SOP now
carries the curl. Step 3 said to promote `observed → verified` without saying how, and
there is no update route. Documented the actual mechanism: re-post with a
**byte-identical** `pattern` and `fix`, because ids are content-addressed over those
two fields, so it merges in place and upgrades trust. Change one character and you get
a near-duplicate instead of a promotion.

### The compounding-memory loop is proven end-to-end

This is the payoff and it now demonstrably closes: hygiene promoted the beta-account
diagnosis to `verified`/`high` **in place** (same entry id, `use_count` preserved, no
duplicate), and a subsequent re-claim of the real alarm returned **`fast_path: true`**
with both ledger entries matched and their use counts incremented.

### One thing I got wrong mid-investigation

I claimed `/signals` and `run_cycle` disagreed about whether a *resolved* signal is
claimable, and started writing it up as a bug. Wrong: `owned` is
`status != "stale"`, which **includes** resolved, so resolved signals are correctly
treated as owned by both. `store.claim` is the single authority and only re-claims from
`stale`. No inconsistency. Verified by driving INV-2 to `stale` and re-claiming, which
is the documented path and worked.

Also: `/api/crons` returned `{"error": "Forbidden"}` for a query-param token, and my
parser turned that into an empty list — which briefly looked like "no crons
registered". Cookie auth showed all four, paused as designed. Two apparent findings,
one real bug.

214 backend tests (211 + 3), 23 Playwright green, mypy/flake8/scrub-lint clean.

---

## 2026-07-31 (latest) — Exercising the reconcile SOP found two real bugs

Closed the two remaining engineering items. Both were "verify a thing we already
built", and both turned up defects that no amount of unit testing would have — they
only appear when you run the documented procedure against real data.

### GitHub Issues verified live

Configured against the public `cli/cli` repo (deliberately, so nothing of the
operator's is touched) and confirmed the whole path: poll → normalize → claim →
brief, then dedupe and a 409 on re-claim.

The check worth keeping: the adapter returned **exactly 90** signals, and `gh`
independently reports 100 open `bug` issues of which 10 are assigned. So the
assignee filter — "an issue with a human assignee is already owned" — is provably
exact against real data, not just plausible. Fingerprints were stable and distinct
across two polls, which is the property ledger matching depends on. And one poll
returned 2 CloudWatch + 90 GitHub signals, so multi-provider fan-out works.

Per least-privilege guidance I exercised only the **read** path and left
`issue comment` / `issue close` alone: verifying a write by writing to somebody's
real repo is not a trade I should make unilaterally. Provider set back to disabled
afterwards so no live provider is left armed.

### Bug 1 — the reconcile SOP described a Slack model that does not exist

It instructed the agent to "react ✅ on its Slack thread and unpin it", and to unpin
threads for terminal incidents. `slack_out` ships no pin/react/unpin helpers at all —
those steps were carried over from the internal system's Slack model. An agent
following the SOP would have burned turns attempting operations that cannot succeed.

Rewrote it around what actually ships: the agent's only job is to make the incident
*status* true, and the board follows automatically because the pin board edits its own
message in place. Also added a pass the original lacked — a signal that cleared
because someone fixed it and one that flapped look identical from the API, so the SOP
now requires saying "cleared without diagnosis — may recur" rather than implying a
fix. A ledger entry written from that guess would be worse than none, because the next
responder inherits it with confidence attached.

### Bug 2 — the grammar had no legal move for reconcile's core case

`dispatched → resolved` was illegal, on the reasoning that "a resolved incident
asserts an investigation happened". But a signal can clear in the gap between the
claim and the agent's first turn — a flapping alarm, or a GitHub issue someone closes
a minute later. Reconcile's entire purpose is to close incidents whose signal stopped
firing, and it had **no** legal transition for that: the incident stuck at
`dispatched` until the stale sweep hours later, so the board asserted work was in
progress on a problem that no longer existed. Exactly the failure an ops board must
never have.

Added `dispatched → resolved` and `stale → resolved` (from `stale` the only move was
re-dispatching a dead signal — spending a whole investigation to conclude nothing is
wrong). This genuinely narrows the old invariant, so I rewrote the test's claim rather
than leaving a docstring that no longer matches: what stays forbidden is resolving
something never claimed at all.

**Two test-quality notes.** An existing test used `dispatched → resolved` as its
example of an illegal transition, so it failed — correctly. My first replacement
(`dispatched → dispatched`) also failed, because same-status is deliberately a no-op
update in `store.transition`, not an illegal move. Settled on `→ unclaimed` and added
a separate terminal-state test, which is the durable illegality worth pinning.

**Process note:** three gateway processes were alive on env 2 and the one holding the
port predated my sync, so the fix looked like it had not applied. Verified via
`ss -tlnp` which PID owned 6777 rather than trusting `pgrep` order.

211 backend tests (208 + 3), 23 Playwright green, mypy/flake8/scrub-lint clean.

---

## 2026-07-31 (late) — The companion seam was a door with no handle

Owner-queue item 3 is the Amazon-internal plugin. Most of it **cannot be built in
this repo** — that is the design, not a limitation. What *is* this repo's job is the
seam it plugs into, and inspecting that turned up the real finding:

`registry.py` documented itself as "the seam an Amazon-internal companion package
plugs into". The ADD-only rule was enforced, and `test_providers.py` already proved a
companion cannot shadow a core adapter. But `get_registry()` installed **only** the
public adapters and nothing anywhere looked for a companion. So an out-of-tree
package could implement every Protocol perfectly and still never be reached. A door
with no handle — and the kind of gap that survives review precisely because the
tested part genuinely works.

`backend/companion.py` is the handle. Decisions worth keeping:

- **Entry points, not a config path.** A path-to-import setting would be a new,
  unaudited code-loading channel in an app whose security story is that the agent
  cannot reach its own config. Requiring a *package install* keeps it outside the
  agent's reach and visible to `pip list`.
- **Reuse the admission policy; do not invent a second one.** Loading a
  separately-installed package into the gateway is a supply-chain decision.
  Governance guidance on third-party packages says 3P code arrives through a reviewed
  channel, and this app is in no position to adjudicate that itself — so every
  candidate goes through the SAME fleet `AdmissionPolicy` that gates platform
  plugins, evaluated **before `ep.load()`**, each decision SEL-audited. A companion is
  not more trusted for being ours. It also closes a bypass: a fleet that banned a
  package must not be able to have that overridden by shipping it as an ops adapter.
- **Fail OPEN here, unlike `platform/discovery.py`.** That one fails closed because a
  missing companion could drop a security overlay. Here a companion only ADDS signal
  sources: a missing one means fewer alarms watched (visible on the Signals tab),
  while aborting boot would take down a working public install — chat, crons, every
  other app — to punish an optional integration. The one fail-CLOSED path inside the
  module is the admission check itself: if the evaluator raises, deny. "The gate
  broke" must never read as "the gate said yes".
- **Mirrored the 3.9/3.10 `entry_points()` split** from platform discovery. Skipping
  it makes a companion silently invisible on the oldest supported interpreter, which
  is the worst failure mode — everything appears to work.

**Verified against a real package, not just mocks.** Mocks would have proven only
that I called my own code correctly. I built a throwaway `omc-probe` distribution
with a real entry point, `pip install -e`'d it into env 2, and confirmed both halves:
its adapter appeared in the live registry alongside the core five under the open
policy, and was **refused** under an enforcing policy ("ships no
`kirocrew_plugin.json` manifest") with the core intact either way. Then uninstalled
it and confirmed discovery returned to empty.

**Found by writing the tests:** I guessed `registry.signal_source(id)` existed —
only `action_sink` has a singular accessor; signals expose a list. Used a local test
helper rather than adding a production method that exists only for tests.

Also pinned the install ORDER (public before companions) as its own test. That
ordering is the entire reason ADD-only is meaningful, and nothing had been asserting
it.

**Still genuinely blocked, and left blocked:** the team mesh. It needs cross-instance
claim arbitration, not another adapter — the dispatch index is per-instance, so two
instances can claim the same signal even though the ledger merges cleanly. Building
the transport before settling that contract would be building the wrong thing.

208 backend tests (194 + 14 new), 23 Playwright green, mypy/flake8/scrub-lint clean.

---

## 2026-07-31 (evening) — Slack pin board, with no token of its own

Owner-queue item 2 done. This is the half of the modeled workflow that had existed
only on paper: their ops channel WAS the dashboard, one line per incident whose
emoji tracked state, so the room's health was readable without opening a tool.

**The one real decision: do not store a Slack token.** The queue item said "bot
token + IDs + channel", and the obvious build adds a token field to the app's
keystone secret store. I checked governance guidance on credential storage first,
and its decision priority is explicit — prefer a path with no secret to rotate;
store a third-party token only where no such path exists. Here one does: KiroCrew
already holds a Slack bot token for its own gateway, and the live `SlackClientOps`
is reachable. So this reuses it and adds **zero** new credential material.

That is not a shortcut, it is strictly better: no second copy to leak, no second
rotation obligation, no second thing for the user to paste. The cost is a real
dependency — with Slack unconfigured on KiroCrew itself the channel is unavailable —
so `status()` distinguishes the three cases (off / no channel / no host Slack)
because each has a different fix, and the UI renders that sentence rather than a
generic "not ready". `TestNoTokenOfItsOwn` guards it: a future "just add a token
field" fails the suite, and a Playwright test asserts no password input exists in
that card.

Design notes worth keeping:

- **Board, not feed.** The first post records `slack_thread_ts`; every later change
  edits that same message. A stream of updates would be unreadable, which is the
  thing that made the original work. If the update fails (message deleted) it
  reposts rather than going silent — a duplicate line is cosmetic, a missing alarm
  is not.
- **Never fatal.** Sends happen after the claim/transition is durable and every one
  is wrapped. A Slack outage must not stop the agent investigating; notifying is not
  the work.
- **Redacted, and it counts as its own egress boundary.** `test_security_posture`
  caught this correctly: my module called `redact` without being a registered sink.
  I registered it rather than allowlisting it, because it genuinely is one — and a
  *distinct* one from `slack/handler.py`, since the text originates in a third-party
  alarm payload rather than a model turn and lands in a channel whose audience is
  usually wider than the dashboard's.

**Found by writing it:** I first reached for a module-level gateway-state accessor
(`get_state()`) — it does not exist; KiroCrew's state is per `web.Application`. The
fix is better than what I intended: the client is threaded explicitly from the route
layer, so there is no hidden global and every send is testable without a gateway.
`None` is always a quiet no-op.

**Stale instruction corrected:** the investigate SOP told the agent to post its
finding to the Slack thread by hand. Now that recording a diagnosis posts it
automatically, that instruction would have produced two copies of every finding —
so the SOP now says explicitly not to.

Note: `test_spawn_audit` and `test_security_posture` also report failures from the
untracked `auto_improvement` app (a parallel effort, not this work). Those are
theirs to resolve; the ops-mission-control entries are clean.

194 backend tests (171 + 23 new), 23 Playwright tests, mypy/flake8/scrub-lint clean.

---

## 2026-07-31 (later still) — Signals promoted to its own tab

First item off the owner's queue. The Signals rail was a 280px column beside the
Board, which limited it to a status readout: it could say "ready" or "not set up"
and nothing else.

The tab is not just the rail moved over — it answers three things the rail could
not:

- **What the last poll actually returned, per source, including the error text.** A
  provider with expired credentials still reports "ready" from config alone; only a
  real poll distinguishes healthy from broken, and only the message says which.
- **Which firing signals are not yet incidents.** Under the 3-per-cycle claim cap an
  alarm storm legitimately leaves a queue. Before this the remainder was a bare
  number — you could see "12 remaining" without seeing what they were.
- **Claim one now** instead of waiting for the next heartbeat.

Polling is deliberately on demand (`enabled: false`, explicit "Poll now") rather
than on a `refetchInterval`: every poll fans out to paid, rate-limited provider
APIs, and a background timer in an open browser tab would quietly multiply that
cost. The dispatch cron is the thing that polls continuously.

Removed the rail and gave the Board full width — incident titles and their
status/age columns were being squeezed for a two-state badge.

**Test-quality note:** the old assertion `getByText('Signals')` would now pass
purely because "Signals" is a tab label, so it would have kept passing with a
completely broken panel. Rewrote it to assert the tab CONTROLS plus the absence of
`Signal sources` on the Board, and added a test that drives a real poll and checks
the per-source `errors` map is present.

21 Playwright tests. Gates clean.

---

## 2026-07-31 (later) — Diagnosis write-back loop closed

The board was reporting both beta-account incidents as `needs_human /
"Stopped, no diagnosis"` while a complete root-cause analysis sat one scroll away in
the transcript. The status logic was right — nothing had recorded a diagnosis — but
the SOP only said "transition the incident with your `diagnosis`" without the call,
so the agent reasonably wrote its answer in chat and stopped.

Fixed the instruction, not the detector: Phase 4 of the investigate SOP now carries
the concrete `POST /incident/transition` (with the status-per-decision mapping and a
note that a 409 means an illegal edge) and the `POST /ledger` call, plus an explicit
"bind the incident's own fingerprint or the entry will never match the recurrence it
was written for". The dispatch kickoff prompt now names Phase 4 as mandatory and
says why: skipping it misreports a finished analysis as a dead end.

Verified live: wrote the agent's real diagnosis back to INV-1 → `awaiting_diagnosis`
cleared, diagnosis recorded; INV-2 (still no diagnosis) correctly kept the flag. So
the signal is genuinely per-incident, not cosmetic. Added a test that walks the full
before/after so a regression shows up as a failure rather than as a board that lies.

Also added to the SOP: if a prior ledger entry turns out wrong, write a correcting
entry — which had already happened once this session (an entry blamed a missing
caller permission when the cause was target-side trust), and leaving it would have
sent the next responder down the wrong path with `high` confidence behind it.

171 backend tests. Gates clean.

---

## 2026-07-31 — Beta-account wiring, incident chat, approvals, truthful status

### Wired to a real AWS account

Configured the CloudWatch adapter against profile `motor_pe_beta`
(account `024848461597`, us-west-2) through the app's own config route — no code
change needed, the adapter already supported a named profile.

Governance guidance was consulted first (credentials + infrastructure domain), and
applied: environment isolation (beta ≠ prod, separate credentials) and least
privilege. **Noted deviation:** only an admin-level role is configured for that
account, so polling runs with more privilege than it needs. The app itself only
ever makes read-only calls, so a read-only role would be the correct credential
here — worth adding.

It found real work immediately:

- `DlqAlarm-ScosShipment` and `ErrorAlarm-ScosShipment-Lambda`, both firing
- claimed both as incidents, distinct fingerprints, `matches=0` on first sight
- **DLQ depth: 47,745 messages** — a genuine backlog, not a synthetic test

### The investigation was better than mine

I recorded a first ledger entry from my own manual dig (Lambda role denied on
`sts:AssumeRole`). Then the agent investigated and **corrected it**: the caller's
identity policy *already allows* that exact action/resource, so the denial is
target-side (trust policy in account `405366036723`) — and it spotted a
**same-named role in this account with reverse trust**, raising a live hypothesis
that `EXTERNAL_ROLE_ARN` simply carries the wrong account id.

That is the compounding-memory mechanism doing its job, so the ledger now holds a
generalized `verified` pattern instead of my narrower `observed` one. Then verified
the loop closes: cleared the index, re-dispatched the same alarms, and both
re-claimed **with the ledger match attached** and `use_count` incremented.

### Embedded incident chat (requested mid-session)

`IncidentChat.tsx` mounts the dashboard's real chat renderer against the
incident's slot, so the user can watch the agent work and reply to it.
Slot key convention is now explicit in the SOP **and** the cron prompt
(`ops-mission-control-<incident_id>`) — the panel polls that key, so any other key
would leave the user staring at an empty conversation beside a live investigation.

### Bugs found (three real, all silent)

1. **`ChatEmbed` never passed `onApprove`.** Approval cards rendered with buttons
   that did nothing. An embedded agent asking permission would stall forever, and
   the card *looked* interactive. Wired the handler through to
   `/api/approvals/{id}/{approve|reject}`; `trust*` maps to approve since the embed
   has no session-scoped trust store of its own.
   *Found by:* trying to click Approve in a real blocked investigation.

2. **`CollapsibleToolGroup` rendered approval buttons only when COLLAPSED.** A
   group with a live pending approval *auto-expands* while running — so the one
   turn waiting on the user was the one turn they could not answer. Core bug, not
   app-specific. Fixed; added `collapsibleToolGroupApproval.test.tsx` (4 tests),
   which the existing `ChatMessageList.test.tsx` could never catch because it mocks
   the group out entirely.
   *Found by:* the click test finding no button, then reading the DOM snapshot
   (`button "Collapse Approval needed"` was present, the Approve button was not).

3. **The chat grew unbounded instead of scrolling.** `ChatEmbed` scrolls via
   `h-full` + inner `flex-1 overflow-y-auto`, which only works if an ancestor
   bounds the height. My wrapper broke that chain, pushing the input row off the
   bottom of the incident row. Fixed with a bounded flex column + `min-h-0`
   (a flex child's default `min-height: auto` refuses to shrink below content and
   silently defeats overflow). Verified in-browser: 985px of content in a 290px
   viewport.
   *Found by:* the owner asking whether scroll was enabled — it was not.

### Truthful status (requested mid-session)

The board said `Dispatched` for an incident whose agent was **parked on an
approval**. That is the worst thing an ops board can get wrong, since the operator
reads it specifically to find what needs them, and it failed silently.

Added `slot_watch.py`: status is **derived** from the live slot, not stored as
intent — `pending_approval` (or a trailing `permission` message, because the flag
lags the transcript) → `needs_human` / `awaiting_approval`; running →
`investigating`; idle-with-turns-but-no-diagnosis → `awaiting_diagnosis`. Derived
means approving from the chat clears it on the next read with no flag to reset.
Also made `dispatched → needs_human` a legal edge, which it was not: observed live
that an agent can block on its FIRST action.

Board now reads **"Waiting on you: 2"** with per-row reasons ("Approve to
continue", "Stopped, no diagnosis") in amber. 18 new tests.

### Verified end-to-end in the browser

- Approve button from the embedded chat: **HTTP 200**, approval marked
  `resolved: approved`, agent resumed and continued its turn.
- Both blocked reasons appear correctly on the live board.
- Transcript is height-bounded and scrollable.

### Environment note

Two gateway settings mattered and cost time to spot:
`agent.approval_mode` was `auto` on env 2, so nothing ever waited — no approval
could be tested until it was set to `interactive`. And the investigating agent runs
in a sandbox that does **not** see `motor_pe_beta`; it only had `agent_beta` /
`agent_prod`. It handled that correctly (said so, asked how to proceed) rather than
guessing, but cross-account read access for the investigator is unresolved.

### Gate status

170 backend tests (both repos) · 20 Playwright + 4 new core tests ·
`black`/`isort`/`flake8`/`mypy` clean · `tsc -b` clean · `scrub-lint` clean.

---

## 2026-07-30 — Second dev environment

Cloned `KiroCrew-2` on port 6777 with its own data home (`~/.kiro/crew-2`), venv,
and `node_modules`, plus `kctoken-2` and a `kc2` wrapper that pins home + port
together (setting only one yields a half-configured instance). Verified isolation
by cross-authentication: each env's token returns 403 against the other.

Two build gotchas on this host: GCC 7.3.1 is too old to compile numpy/Pillow
(`pip install --prefer-binary`), and `make backend` picks a python3.13 whose
`ensurepip` fails, leaving a venv with no pip (build it from uv's 3.12 instead).

**Security caveat:** env 2 is OUTSIDE the keystone floor —
`security._CREW_HOME_PREFIXES` hardcodes `.kiro/crew` and `.kirocrew`, so under
`~/.kiro/crew-2` the agent's own tools are not blocked from reading that home's
secrets. Dev sandbox only; no real provider tokens there.

---

## 2026-07-30 — Ideation, spec, and build

Studied the source workflow (internal ops site + context package + skillset) and
distilled the four mechanisms that made it work — rotation-aware cron tiering,
claim-based dispatch, compounding institutional memory, and the channel as the
dashboard — none of which are Amazon-specific. Wrote ideation + spec, then built
the app: provider seam behind four Protocols with an ADD-only registry, incident
store, ledger, autonomy gate, keystone secret store, dashboard page.

Deliberate divergence from the source: autonomy defaults to `observe` for everyone
(they auto-resolved two known intakes; a stranger's install has no basis for that),
and remediation execution is out of scope for v1.

Bugs the gates caught during the build: the app was initially **inert** (no crons
declared, ledger never matched, no Settings UI); a builtin's app dir is not copied
into the data home so `manifest.skills` silently registered nothing (moved to
`builtin_skills/`); an unrouted `gh` subprocess (routed through
`sandboxed_spawn_argv`); a hardcoded legacy-home path in a test; and a test that
read the operator's live config instead of an isolated one.
