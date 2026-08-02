# Ops Mission Control

Last Updated: 2026-08-01 (post-action verification + the ledger's track record:
contracts 2b and 3b — a fast-path floor, a mechanical demotion path, and a recheck that
refuses to read a failed poll as success; local notification bus: three declared channels,
`backend/notify_out.py`; provider-side suppression: `STATE_SUPPRESSED`, contract 1b;
earlier revisions — dispatch engine + manifest crons + Settings panel; builtin app,
provider seam, keystone secret store, autonomy gate)

An autonomous ops first responder shipped as a **builtin app** (`origin: builtin`,
`defaultEnabled: false`). It polls signal providers, claims what is firing,
investigates it in a chat session mirrored to Slack, matches it against a
compounding knowledge ledger, and proposes an action. Read-only by default.

Task-spec provenance: `docs/task-specs/2026/07/ops-mission-control/{ideation,spec}.md`.

## Durable contracts

These are persisted-data or security contracts. Changing one requires updating
this spec in the same commit.

### 1. Signal fingerprint (persisted in the ledger)

`compute_fingerprint(source, resource, title)` = `sha256(source|normalized-shape)`
truncated to 16 hex chars. The shape substitutes out timestamps, uuids, long hex
runs, `i-`/`vol-`-style resource suffixes, and **all bare numbers**
(`models._VOLATILE_PATTERNS`).

This is load-bearing: a fingerprint that drifts per occurrence means a repeat
failure never matches its ledger ancestor, and the app keeps working while
silently no longer learning. Changing the normalization invalidates every stored
`LedgerEntry.fingerprints` entry.

**It also provably over-merges, and that is why it cannot be the only key.** Because
every bare number is stripped, distinct failures on one resource collide — verified and
pinned in `TestShapeHashOverMerges`:

| title A | title B | shared fingerprint |
|---|---|---|
| `4xx error rate above 5` | `5xx error rate above 1` | `413280c6ee0b5afa` |
| `p99 latency above 500ms` | `p50 latency above 100ms` | `c4dbf4e759b19ceb` |

The stripping is deliberate (a DLQ at 500 and at 900 *should* match), so the fix is not
to sharpen the hash — a fingerprint match means "looks like this", and a ledger that
presents it as "is this" hands a responder a fix learned from a different problem, which
is worse than no match. See contract 1a.

### 1a. Provider identity (persisted, additive)

`Signal.provider_key` carries the identity the **provider** computed — an Alertmanager
`fingerprint`, a Datadog monitor id, a CloudWatch `region/alarm-name`, a `repo#number`.
It is namespaced `"<source>:<key>"` so two providers cannot collide on a bare numeric id,
and it is set from **explicit adapter input, never derived**: a derived value would be
another heuristic wearing the word "exact".

`LedgerEntry.provider_keys` stores them, unions on merge exactly as `fingerprints` does
(preserving the git reconciliation property in contract 2), and is **bounded** by
`MAX_KEYS_PER_ENTRY` keeping the newest. The bound is required, not tidiness: PagerDuty
mints a new incident id per occurrence, so an unbounded list would grow one JSONL line
forever in a file that is git-synced and read into a model prompt.

`ledger.match(fingerprint, provider_key=...)` ranks exact hits **above** shape hits
regardless of trust or use count, and `dispatch` reports which kind matched in the brief
(`ClaimedIncident.exact_match_ids`) so the agent can weigh them differently.

Both fields default empty, so every incident and ledger line written before they existed
stays valid and keeps matching by shape alone.

### 1b. Provider-side suppression (persisted, additive)

`VALID_STATES` gains **`STATE_SUPPRESSED`** — *a human already parked this at the
provider* (an Alertmanager silence or inhibition, a Zabbix maintenance window, an Icinga
downtime, a Sentry archive). `normalize_state` maps
`suppressed`/`silenced`/`inhibited`/`muted`/`snoozed`/`downtime`/`in downtime` onto it.

**What its absence cost.** Every one of those words previously returned `unknown` —
verified, `suppressed` and `banana` were indistinguishable. So an adapter facing
`status.state = "suppressed"` had two options and both were wrong: report `firing`, and
the app investigates something an operator explicitly parked (the fastest way to lose
trust in an autonomous responder); or drop it, and "the app ignored my alarm" becomes
indistinguishable from "someone silenced it".

**A state, not a label**, argued against the two filters that consume state:

- `dispatch.run_cycle` claims `state == firing` in ONE place, so a new state is unclaimable
  by construction — no second predicate for a future edit to forget. A label would leave
  `state == firing` and force `run_cycle` to grow a label-reading condition, which is the
  *every adapter reimplements the filter privately* failure moved into core.
- `/signals` splits buckets BY STATE, and the reconcile SOP is written against those
  buckets. A label cannot produce a bucket, so a parked signal would keep arriving inside
  `firing` and reconcile would keep treating it as live work.
- `unknown` means "we could not read the state"; `suppressed` means "we read it and a human
  parked it". Collapsing the two is the defect, not the fix.

**Two decisions this does NOT reopen.** `acknowledged` still maps to `firing` — an
acknowledged page is unresolved and the point is to be working it (`pagerduty.py`
`_OPEN_STATUSES`). And no-data stays settled: CloudWatch `INSUFFICIENT_DATA` is an opt-in,
default-off detection-*sensitivity* choice with the truth kept in `labels['state']`.
Suppression is not a sensitivity knob — it is a fact about a person's action, and there is
no honest default-off reading of "someone silenced this".

**Distinct from `ACTION_SILENCE`.** That is a bounded suppression the app *issues*; this is
one somebody else applied, which we *read*. One word for both would merge our intent with
another party's decision.

`Signal.suppressed_by` (the provider's own attribution — an Alertmanager `silencedBy` id,
the alert in `inhibitedBy`) and `Signal.suppressed_reason` (`silenced` vs `inhibited`, since
a person's silence and an alert masking another alert need different next moves) are
**explicit adapter input, never derived**, and both **default empty** so every incident
already on disk stays valid. Unlike `provider_key` they are NOT namespaced by source: they
are display text for a human, not a match key.

The webhook adapter reads both Alertmanager status shapes — the v4 scalar `status`, and the
v2 `gettableAlert` OBJECT `{"state": "suppressed", "silencedBy": [...]}`. The previous
scalar-only read stringified that object, so it normalized to `unknown` and dropped
`silencedBy` entirely.

`normalize_state` therefore covers the **whole** v2 `alertStatus.state` enum —
`unprocessed` and `active` map to `firing`, `suppressed` to `suppressed`. Admitting only the
parked value would have been worse than not reading the object at all: the v2 payload would
parse a *silenced* alert correctly while dropping a *live* one into `unknown`, i.e. the app
going quiet on real work in exchange for reading a mute. The flat native envelope accepts `suppressed_by`/`suppressed_reason`
too, because Zabbix (`suppressed=1`) and Icinga (`downtime_depth`) do not speak
Alertmanager's shape and a forwarder normalizing them needs somewhere to put the
attribution.

`CycleResult.suppressed` counts what a cycle saw and left alone. It is deliberately excluded
from `changed` (a suppression is not news, and silence-by-default is a hard requirement) but
present in the payload, because `polled` counts firing signals only — without the count a
cycle reports a smaller world than it saw.

### 2. Ledger entry id (content-addressed, persisted)

`LedgerEntry.compute_id(pattern, fix)` = `sha256(lower(pattern)|lower(fix))[:16]`.

Content addressing is what makes the append-only `ledger.jsonl` mergeable across
git-synced team members: two people who learn the same lesson produce the same id.
Changing the basis orphans existing entries.

**Corrected against a real `git merge`:** the earlier claim that a merge is "a dedupe
rather than a conflict" was wrong at the git level. Two divergent ledgers conflict —
both branches appended to the same region, so git emits `<<<<<<< HEAD` / `=======` /
`>>>>>>>`. What content addressing actually buys is that the *entries* are reconcilable,
not that git resolves them for you. Two things make it work:

- The malformed-line skip in `read_entries` tolerates conflict markers, so the app stays
  usable while a user's tree is mid-merge (verified against a genuine conflicted file).
- `read_entries` **reconciles duplicate ids on read**, using the same algebra as
  `upsert` — **both** identity lists union and both are capped. `_reconcile` originally
  unioned `fingerprints` only, so a real `git merge` permanently wrote away one branch's
  `provider_keys`; since `match()` treats a provider key as the EXACT-identity signal, the
  next recurrence on the dropped alert would have matched by shape hash alone or not at all
  — the same silent knowledge loss the fingerprint union exists to prevent. The cap was
  missing on this path too (two already-capped lists unioned are up to 2× the cap, which
  `upsert` bounds). Found in review; the pre-existing `provider_keys` merge test went
  through `upsert`, which is exactly why it did not catch this. Pinned now by
  `TestLedgerGitMerge::{test_reconcile_unions_provider_keys_not_only_fingerprints,
  test_reconcile_caps_both_identity_lists}`, which write two RAW lines — the git-merge
  shape — rather than calling `upsert`. Confidence and trust take the strongest of the two
  and `use_count` the highest, exactly as `upsert` does.

  Before reconciliation existed at all, read appended every line, so one shared lesson
  counted twice: `stats()` inflated, `match()` returned the same entry twice, and the
  handover digest listed one pattern as two. Identity union is the load-bearing part —
  dropping one branch's fingerprint or provider key means that recurrence stops matching,
  and the ledger keeps working while silently no longer recognizing half its own history.

Measured end to end: two divergent ledgers → real `git merge` → conflicted file → 4 raw
entries read as **3**, shared lesson collapsed with both fingerprints preserved.

#### 2a. Record format version (`LedgerEntry.v`, `LEDGER_RECORD_V1 = 1`)

`ledger.jsonl` is the one artifact that **leaves the machine**: `ledger_sync` git-pushes it
and teammates on *different KiroCrew builds* pull it, so an older instance can be handed a
row a newer one wrote. Without a version stamp there is no way to notice — the reader
coerces the fields it recognises and defaults the ones it does not, so a row it only partly
understands reads as fully understood. Review called this the nearest thing in the app to a
**one-way door**, and the retrofit is only free while exactly one version exists.

- Every new line is stamped `"v": 1`. A line **without** `v` predates the field and reads as
  v1 — the standard retrofit for an optional field in append-only JSONL, and the reason
  adding this costs nothing now.
- An unparseable `v` also reads as v1 rather than raising. This reader's job is to salvage a
  git-merged team ledger, not to reject it, and a row whose version cannot be parsed is
  still a row every field of which is understood.
- A **future** version survives the round trip unchanged, so a later reader can act on it.
- Nothing gates on the value today — with one version a gate would be dead code. The field
  exists so the next format change has somewhere to say so.
- **Bump it only for a change a reader must know about** (a field whose *meaning* changed,
  or one that cannot be safely defaulted), never for an added optional field: every field on
  this record already defaults, which is what lets one version cover the whole history.

The field default is spelled as the literal `1`, not as `LEDGER_RECORD_V1`. A dataclass field
default is evaluated when the class object is built, and `test_ledger_sync_git` evicts this
module from `sys.modules` mid-test to simulate two instances — a name resolved at
class-creation time then hits a half-initialised module and raises `NameError` (observed).
The constant remains the single source of truth for readers, and a test pins the two equal.

### 2b. The fast-path bar, and the track record behind it (persisted, additive)

`ledger.is_fast_path` is what decides whether the investigation brief says **"KNOWN
PATTERN — propose this fix"** or **"hypotheses to test"**. It now delegates per entry to
`ledger.entry_unlocks_fast_path`, which requires FOUR things:

| condition | constant | why |
|---|---|---|
| `trust == verified` | `FAST_PATH_TRUST` | a human saw it work |
| `confidence == high` | `FAST_PATH_CONFIDENCE` | and was sure |
| `use_count >= 2` | `MIN_USES_FOR_FAST_PATH` | some incident OTHER than this one used it |
| `miss_count == 0` | `MAX_MISSES_FOR_FAST_PATH` | it has never been observed to fail |

**Why the first two alone were not enough.** `POST /ledger` takes `confidence` and `trust`
verbatim, so one hand-authored entry could arrive as `verified`/`high` and immediately
unlock "propose this fix directly" for a production failure, having never been applied to
anything. Contract 1a made that strictly worse rather than better: `record_use` **binds**
the provider key on the first match, so from the second occurrence onward that same single
piece of evidence presents as an *exact* match — a stronger-looking claim with nothing new
behind it.

**Why 1 would be vacuous.** `dispatch.attach_ledger_matches` calls `record_use` *before*
`is_fast_path`, so at the moment of judgement `use_count` already counts the incident being
judged. Every match whatsoever has `use_count >= 1`. 2 is the smallest floor that says
anything, and it lands on the same line `handover.MIN_USES_TO_RECUR` already draws.

**The accepted cost:** the fast path now unlocks on the third occurrence, not the second.
A non-fast-path match is not withheld — the brief carries the full pattern and fix either
way; the only difference is that the agent is told to confirm before proposing.

**The mechanical downward path.** Two new persisted, default-zero fields plus a bookkeeping
one:

- `LedgerEntry.miss_count` / `.last_miss` — times the fix was cited and the failure came
  back. Written by `ledger.record_miss`, whose **only** caller is
  `dispatch._record_verification_misses` (see contract 3b), so the standard of evidence
  lives in one place.
- `LedgerEntry.decayed_at_miss_count` — the `miss_count` value hygiene last spent on a
  demotion, so one failure costs exactly one confidence step. Required because hygiene runs
  nightly and its demotion test is a *ratio*, which stays true once true: without it a
  single miss would walk an entry `high → medium → low` across three nights on no new
  evidence.

All three take the **max** on every merge path (`_reconcile` on read, `upsert`, and
hygiene's dedupe). That asymmetry against `confidence`'s strongest-wins is deliberate: a
git pull, or a re-POST of the same pattern+fix, must not be able to launder away a
teammate's evidence that the fix failed. The re-POST case is the sharp one — that is
exactly how `ledger-hygiene.md` promotes `observed → verified`, so an accepted
`miss_count: 0` there would clear every recorded failure with one curl, on precisely the
entries most likely to have them. `POST /ledger` therefore does not read the three fields
from a body at all.

`hygiene()` demotes one confidence step when `miss_count >= max(1, use_count *
MISS_RATIO_FOR_DECAY)` and reports `demoted` **separately from `decayed`**: "nobody needed
this" and "this did not work" are opposite findings, and one number for both would let a
staleness report and a correctness report arrive as the same sentence. `trust` is never
rewritten — "somebody saw this work" stays true even after it failed elsewhere.

The prune order changed from `-use_count` to `-(use_count - miss_count)`. Before, an entry
that kept matching the *wrong* failure climbed the ranking on every mismatch and was
therefore the **last** thing the cap dropped: the ledger preferentially kept its most
misleading rows.

`stats()` gains `proven` / `demoted` / `total_misses`. `verified` and `high_confidence` are
each one HALF of the bar, so neither answers "how much of this ledger would an agent
propose without checking" — showing only those two overstated the ledger's authority.

### 2a. The sync loop, and where it is driven from

The daily `ledger-hygiene` pass (`POST /ledger/hygiene`) is the only caller of the git
transport and the vector index. Order is load-bearing: **pull → hygiene → index → push**.
Deduping before the merge leaves freshly-arrived duplicates for tomorrow; indexing before
hygiene embeds rows hygiene is about to prune; pushing before hygiene makes every instance
re-derive the same dedupe locally so the repo never converges. Pinned by
`test_stage_order_is_pull_hygiene_index_push`.

Before this, **both halves were wired to nothing.** `ledger_sync` had no caller anywhere,
and `dispatch`'s semantic recall queried an index `import_pending` never populated — so on
a real install recall returned zero hits forever while every unit test passed. Two modules
can be individually correct and collectively dead; only an integration caller proves
otherwise.

**Four fatal bugs were found by a real two-instance roundtrip against a bare remote,
every one of which the mocked-git tests passed** (`tests/test_ledger_sync_git.py`):

1. **The first push in a fresh process always failed.** The sandbox backend probe defers
   off the event loop on a cold cache and raises a self-described *transient* error saying
   "retry"; `push` did not catch it. `sync_safely` now retries **once** on a transient
   spawn fault, re-running only an idempotent git step.
2. **An instance with a local ledger could never pull** — `git merge` refuses when an
   untracked working-tree file would be overwritten, so any install that recorded even one
   lesson before its first pull was *permanently* cut off from the team's. Fixed by
   staging and committing local work before merging, which is also the correct semantic.
3. **The second teammate to join could never merge.** Every instance runs its own
   `git init`, so their roots are genuinely unrelated and git refuses outright — the
   *ordinary* multi-instance case. `--allow-unrelated-histories` is therefore required,
   and is safe here **only** because the tracked content is a content-addressed union and
   the conflict path reconciles rather than picking a side. On a normal source repo the
   flag would be reckless.
4. **`rotation.yaml` would never have been committed.** `push` staged `ledger.jsonl`
   alone, so the on-call schedule — un-ignored *specifically* so it could sync — would
   have reached nobody. `TRACKED_FILES` now names the whole shared set.

A **fifth** was found later, and not by a test — by inspecting the owner's live install:

5. **The local repo was never on the configured branch.** `git init` ran with no `-b`, so
   git picked its own default (`master`), and `branch()` was used **only** inside refspecs:  <!-- wokeignore:rule=master -->
   `fetch origin <b>`, `merge origin/<b>`, `push HEAD:<b>`, `rev-list origin/<b>..HEAD`.
   Nothing ever moved HEAD or wrote tracking config. Live install: config `main`,
   `.git/HEAD` `master`, and **no `[branch]` section at all**. Sync worked *by accident of  <!-- wokeignore:rule=master -->
   those explicit refspecs* — the app's signature "machinery that looks deliberate" shape,
   and the reason the real-git tests missed it: they only ever asked whether the content
   arrived, and it did.

   Four measured costs, none of them cosmetic. (a) `status()` reported "Syncing … on branch
   main" while HEAD was `master` — an overstated claim on the one surface the operator  <!-- wokeignore:rule=master -->
   reads. (b) **Manual recovery was blocked**: with no upstream, `git pull` fails with "no
   tracking information for the current branch" and `git push` with "the current branch
   master has no upstream branch" — and a conflicted `rotation.yaml` is *refused* by push  <!-- wokeignore:rule=master -->
   precisely so a human fixes it by hand, in that directory. (c) Changing
   `ledger_sync_branch` later re-pointed fetch/merge/push at a new remote ref while HEAD
   kept accumulating on the old one, so the first push to the new branch is either rejected
   non-fast-forward or publishes the old branch's history onto it. (d) `git status` and any
   agent reading the branch name reported a branch nobody configured.

   **Resolution: rename in place, then write tracking explicitly.** `_align_branch` runs
   from `_ensure_repo`, so one call site covers pull, push, and the operator changing the
   branch later. `git branch -m --` is the primitive because (verified against real git) it
   succeeds on an *unborn* branch, keeps the same sha on a born one, leaves a dirty tree
   untouched, and preserves an in-progress conflicted merge — none of which `checkout` /
   `switch` do. It **refuses** on a detached HEAD (moving refs under one can lose the
   operator's work) and when a *different* branch of that name already exists (`git branch
   -M` would delete it and every commit only it holds — the exact lesson-stranding this
   fixes). Tracking is written with `git config branch.<n>.remote/.merge`, **after** the
   rename and **not** with `--set-upstream-to`: the rename migrates `.remote` but leaves
   `.merge` on the old ref, and `--set-upstream-to` fails in both ordinary first-sync states
   (no `origin/<b>` fetched yet; unborn local branch) — an empty remote is how a team
   *starts*. A refusal is never a sync failure: publishing always worked through the
   refspecs, so the reason is surfaced through `status()` instead of failing `_ensure_repo`.

   `status()` therefore gained `local_branch` (what `.git/HEAD` points at, `""` when
   detached or uninitialized), `branch_matches` (the only field a UI should gate a warning
   on; **true** when uninitialized, since there is nothing yet to disagree with) and
   `detached`. `branch` keeps meaning the *configured* branch. The detail sentence only
   claims "Syncing … on branch b" when that is true of the local repo too. Rendered in
   Settings as a `wrong local branch` / `detached HEAD` badge plus a row naming the branch
   the repo actually sits on — shown only when they disagree, because two rows that usually
   agree invite the very conflation that caused this.

Also fixed: a clean tree is not proof everything is shared. A run that committed and then
failed to reach the remote left `push` reporting "nothing to push" forever, stranding that
lesson locally; `_has_unpushed` distinguishes the two and treats an unknown answer as
"push anyway" (a redundant push is cheap, a skipped one loses knowledge).

Verified live: A records → pushes → B pulls and sees it → B adds → A pulls both. Then the
concurrent case — both write without seeing each other — the stale push is correctly
**rejected**, the pull reconciles to 3 entries preserving both sides, and both instances
converge on identical ledgers with no entry lost.

#### Where an operator sets it, and what they are told

Config keys (all non-secret, so plain `data/config.json`, same tier as the Slack channel):
`ledger_sync_enabled`, `ledger_sync_remote`, `ledger_sync_branch`. Written through
`PUT /settings`; read back through `ledger_sync.status()` on `GET /state` (`ledger_sync`).

A remote URL is **not** a credential — auth is the operator's own SSH key, credential
helper or `gh` login — which is what makes it eligible for that file at all. The converse
constrains the UI: `config.json` is served **unauthenticated**, the write path only
length-caps the remote, and `redact_tokens` has no pattern for a PAT inside a URL. So the
Settings card asks for an SSH remote, states plainly that there is no credential to enter,
and strips a `userinfo@` component before *displaying* a remote — which is about not being
a second place a token is shown, **not** a claim that the stored value was sanitised.

`status()` reports `conflict` (the ledger) and `schedule_conflict` (`rotation.yaml`)
separately, because they are opposite severities:

- A **ledger** conflict is reconcilable and sync keeps publishing (content-addressed ids,
  `read_entries` skips markers, the next push rewrites the union).
- A **schedule** conflict makes `push` **refuse outright**, so nothing new reaches the team
  at all. That refusal previously existed only in the log and a SEL audit line —
  `sync_safely` swallows it into a warning — while `status()` still said "Syncing …". An
  operator therefore watched a card report a working sync through an indefinite publishing
  outage, with the on-call file unparseable for everyone who pulled it. `status()` now names
  the refusal, and Settings renders it as an error rather than a note.

`_ledger_sync_status`'s failure fallback carries the **same key set** as a real status, so
the UI can read every field instead of guarding each one; the two-key fallback it replaced
made `undefined` a possible rendered remote.

### 3. Incident status grammar (persisted in the dispatch index)

`models.LEGAL_TRANSITIONS` is the whole grammar; `store.transition` is the only
door and raises `ValueError` on an illegal move.

`TERMINAL_STATUSES` is **derived** from that grammar — a status with no outgoing
transition is terminal by definition — so a future status cannot disagree with a
hand-maintained second list.

**A closed incident no longer owns its signal.** `claim` treated *any* existing
incident as "accounted for", including a closed one. Because `signal.id` is stable for
the alarm's lifetime (`cloudwatch:alarm/DlqDepth` forever), that meant the app
**permanently stopped responding to any failure it had already handled once** —
verified live: resolve on day 1, and the same alarm re-firing on days 2, 3, and 30 all
returned `None`.

That also made the app's central premise unreachable in production. The
compounding-memory fast path can only pay off on a *second* occurrence, and a second
occurrence could never be claimed — so the feature this app is built around could
never fire outside a test. The grammar itself already said the right thing ("Re-opening
is a new signal, not a transition — a resolved incident that 'comes back' is a fresh
firing with its own timeline"); `claim` simply did not honor it.

A recurrence is a **new** incident, never a reopening: the first one owns its
diagnosis, resolution, and Slack thread, and overwriting those would destroy the record
that makes the ledger trustworthy. An OPEN incident — including `needs_human`, which is
*waiting on a person*, not closed — still blocks a duplicate claim; that dedupe is
what stops two heartbeats double-investigating one alarm, and a subtest covers all
three open statuses.

**The same rule lives in TWO places, and fixing `claim` alone was not enough.**
`run_cycle` keeps a cheap pre-filter in front of `claim` — `owned = {signal.id for
non-stale incident}` — which discarded the recurrence *before* `claim` ever saw it. The
app therefore still permanently stopped responding to an already-handled failure, and the
compounding-memory fast path stayed unreachable, while **410 unit tests passed**: they
call `store.claim` directly and never traverse `run_cycle`. Found only by driving a real
gateway end to end (inject → resolve → re-inject reported `polled=1, claimed=0`). The
pre-filter now excludes `TERMINAL_STATUSES` too, and
`test_a_resolved_alarm_refiring_is_claimed_through_run_cycle` exercises the path the cron
actually takes. Verified after the fix: cycle 1 → INV-1 `fast_path=False`; cycle 2 (same
alarm) → INV-2, `matches=1`, `fast_path=True`, remembered fix carried, INV-1 still
`resolved`.

The lesson generalizes past this bug: **a duplicated invariant needs a test at the outermost
caller**, because a unit test aimed at the inner function proves nothing about the filter
in front of it.

Measured after the fix: 1st occurrence → 0 matches, `fast_path=False`, brief says "new
to the ledger". 2nd occurrence → claimed as a fresh incident, 1 match,
`fast_path=True`, brief carries the verified fix; the first incident stays `resolved`.

**That fix removed a ceiling, so retention had to replace it.** "One incident per alarm,
forever" was accidentally bounding the dispatch index. A genuinely flapping alarm on the
2-minute cadence now mints one incident per flap, and every claim re-reads and re-writes
the **whole** index — measured superlinear: 50 entries → 6 ms/claim, 150 → 15 ms, 300 →
30 ms, 450 → 53 ms. A month of one flapping alarm projects to **~21,600** incidents, and
`/incidents` was serializing every one of them on each dashboard poll.

Two bounds, neither on the hot path:

- `store.prune_closed(keep=MAX_CLOSED_INCIDENTS)` (500) retires the oldest **closed**
  incidents, ordered by when they closed (`updated_at`) so a long-running incident that
  just finished counts as recent. **Open work is never pruned at any age** — live work
  vanishing because history is long would be far worse than a large index. It runs from
  the daily hygiene pass, not from `claim`, so an ordinary claim never pays for a large
  rewrite. Investigation *logs* are separate files and deliberately untouched: retiring
  an index row does not destroy the written record.
- `/incidents` is capped at `MAX_INCIDENTS_RESPONSE` (200) and sets `truncated` +
  `total` when it clipped. Silent truncation is how someone concludes an incident
  vanished; the frontend types both fields.

Verified: 451 incidents / 301 KB / 43 ms per claim → prune → 101 / 67.5 KB / **12 ms**.

```
unclaimed → dispatched → investigating → {needs_human, resolved, escalated}
dispatched|investigating|needs_human → stale   (idle past that status's window)
dispatched → resolved                   (signal cleared before the first turn)
stale → dispatched                      (re-claim, same incident id)
stale → resolved                        (signal cleared while released)
needs_human → investigating|resolved|escalated
resolved, escalated                     (terminal — no exits)
```

`unclaimed → resolved` is deliberately absent: resolving requires a claim first, so
every resolution has an incident timeline behind it.

**`dispatched → resolved` and `stale → resolved` exist for reconcile.** A signal can
stop firing between the claim and the agent's first turn (a flapping alarm; a GitHub
issue closed a minute later), and the reconcile SOP's entire job is to close
incidents whose signal cleared. Without these edges it has no legal move for that
case: the incident sticks at `dispatched` until the stale sweep hours later, so the
board asserts work is in progress on a problem that no longer exists — and from
`stale` the only move would be re-dispatching a dead signal, spending a whole
investigation to conclude nothing is wrong. Note this narrows the old claim that "a
resolved incident asserts an investigation happened": a claimed incident may resolve
without one when the underlying signal simply went away, which the SOP requires be
stated in the `resolution` text rather than implying a fix. Both edges were found by
exercising the reconcile SOP against a real cleared GitHub signal, and are pinned by
`test_models.py::TestTransitionGrammar`.

**`needs_human → stale` is now actually traversed.** The edge was legalised from the
start, for a stated reason — "an incident nobody ever answers must not pin a signal as
claimed forever" — and the sweep never used it: `needs_human` was absent from
`store._SWEEPABLE_STATUSES`, and `run_cycle`'s pre-filter counts every non-stale
non-terminal incident as owning its signal, so an unanswered question meant the alarm was
never re-claimed. The only guard asserted the transition was *legal* and never ran the
sweep, which is exactly why the gap survived — the same "test the outermost caller"
lesson as above.

It gets its **own, longer window** (`needs_human_stale_after_secs`, defaulting to
`DEFAULT_NEEDS_HUMAN_STALE_MULTIPLIER` × the working one, so 12 h at the 2 h default).
Waiting on a person is legitimately slower than an agent dying, and releasing a question
discards the investigation's context — but an *abandoned* question must not hold the
signal forever.

#### Where an operator sees these, and in what unit

`max_claims_per_cycle`, `stale_after_secs` and `needs_human_stale_after_secs` are written
through `PUT /settings` and read back through `rotation.sweep_windows()` on
`GET /rotation` (`sweep`). The read path was missing for the whole life of these keys, so
an operator could set a window and never see it again, and the defaults governing every
untouched install were reachable only by reading the source — the same
looks-deliberate-does-nothing shape this module exists to prevent, in the settings layer.

Two rules the response encodes, both load-bearing for the UI:

- **`needs_human_stale_after_secs` is reported RESOLVED, never as the stored `0`.** Unset
  does not mean "never released" — `store.sweep_stale` derives it from the working
  threshold — so returning the raw value would state the opposite of what the sweep does.
- **`needs_human_derived` says whose number it is.** A derived window moves when
  `stale_after_secs` changes; a pinned one does not, and an operator choosing between them
  needs to know which they currently have.

`sweep` is optional on the client type: an older gateway omits it, and the Settings card
then reports that the values were not sent rather than substituting the defaults, which
would confidently display 2 h against an instance possibly running something else.
`rotation` duplicates the three config-key strings (importing them from `dispatch` would
close an import cycle); `test_store_and_gate.py` asserts them equal to `dispatch`'s own, so
a rename cannot leave the panel displaying a default while the heartbeat uses a real value.

### 3a. Suppression is always time-boxed (safety)

`ACTION_SILENCE` exists because every low-risk provider write in the landscape is a
time-boxed suppression (Alertmanager silence with a mandatory `endsAt`, Datadog mute with
an `end`, PagerDuty snooze with a required `duration`, Sentry archive with an
`ignoreDuration`), and the vocabulary had no word for it — so an adapter had to express a
mute as `resolve`, asserting something false and hiding a live fault *permanently* rather
than temporarily.

The contract: **an action in `EXPIRING_ACTIONS` always carries a positive, bounded
expiry**, clamped by `resolve_silence_secs` into `(0, MAX_SILENCE_SECS]` at the
authorization boundary in `routes._handle_action` — not in each adapter. A sink must not
be able to opt out of the bound by forgetting to check, because an unbounded suppression
is the single outcome the verb exists to prevent. Unparseable or non-positive input
yields the DEFAULT, never "no expiry".

Why this matters beyond tidiness: **a wrong silence expires by itself.** That is what
makes granting `act` a bounded bet rather than an all-or-nothing one, which is what
"autonomy is earned per rule" requires in practice.

Fixed in the same change: `datadog.py` posted `/mute` with `body={}`, and Datadog reads a
missing `end` as *mute forever* — so the board showed an incident resolved while the
metric stayed bad, recoverable only by a human noticing. `resolve` is retained as an
alias onto the same bounded mute, because silently dropping it would revoke a capability
an existing act-rule already grants. `github-issues` deliberately does **not** advertise
`silence`: an issue tracker has no snooze, and claiming one would be a lie.

### 3b. An executed action is re-read, and a failed poll is not a success (persisted, additive)

`ActionResult.ok` means **"the provider returned 2xx"** and nothing more. That is not the
claim the board was making. Checkmk dispatches commands asynchronously through Livestatus
and its own docs warn a 2xx "only indicates whether the request was successfully
transmitted, NOT whether it was in fact successfully executed"; Nagios's command pipe
returns nothing at all. So the app could report a suppression or a resolve as applied while
the alarm kept firing, with no code anywhere in a position to notice — the silent lie an
ops agent must not tell, and the reason `use_count` meant "was shown" rather than "worked".

**Five persisted, default-empty fields on `Incident`:** `last_action`, `last_action_at`,
`verify_after`, `verification`, `verification_detail`. Empty `verification` means *no action
was ever executed* — which is the truth for every incident already on disk, and is
deliberately **not** back-filled to a verdict.

**The verdict vocabulary** (`VALID_VERIFICATIONS`), and why each exists separately:

| verdict | meaning |
|---|---|
| `pending` | an action landed a 2xx; the recheck is scheduled and has not run |
| `cleared` | the recheck ran against a **successful** poll and the signal is gone |
| `still_firing` | the recheck ran against a successful poll and it is **still firing** |
| `unknown` | the recheck was due and **we could not look** |
| `not_checkable` | executed, but its success is not observable here |

`OPEN_VERIFICATIONS = {pending, unknown}` — `unknown` is **not terminal**. "We could not
look" is a statement about us, not about the world, so a later cycle where the source
answers replaces it. Freezing it would be the absence-is-evidence bug in a new place.

**Absence is not evidence, restated at a second boundary.**
`dispatch.verify_pending_actions` refuses to reach any verdict for a source whose
`poll_health` entry is missing or `ok: false` — the same rule `reconcile.md` Pass 1 step 3
states for resolving on absence. Here it is worse than a wrong board row: reading a failed
poll as "the fix landed" would feed a **false positive** into the ledger's track record,
making a fix that never worked look proven.

**Three absences, not one — the other two also reach `unknown`.** A missing signal from a
poll that SUCCEEDED still proves nothing in two further cases, and both originally reached
`cleared`:

- **A push spool** (`snapshot: false`, see § Absence is not evidence). Absence is the steady
  state for the webhook source — a claim removes the signal (`webhook.ack`) and nothing ever
  re-asserts it — so one cycle after any delivery an action verified as `cleared` with the
  fault live.
- **A signal now `suppressed`.** This is the worst of the three to misread, because after a
  `silence` *this app itself issued*, "the provider reports it suppressed" is precisely what
  SUCCESS looks like — so the recheck congratulated the app for muting a live fault, and
  `use_count` grew on the entry that recommended it. `unknown` rather than `still_firing`
  because the condition is genuinely unobservable while muted: the provider has stopped
  evaluating it into a firing state, and only the suppression lifting can answer the
  question. The attribution in the detail comes from **this poll**, not from
  `Incident.signal` — that snapshot was taken at claim time, before anyone parked it, so it
  names nobody in exactly the case that needs a name.

**A SIMULATED action schedules no recheck at all.** `ActionResult.simulated` (default
`False`) marks a sink that RECORDED the intent instead of performing it — `NoopActionSink`,
the observe-only default. `ok=True` there means "we successfully did nothing", which the
recheck cannot distinguish from a real write: it read the still-firing alarm as the action
having failed and charged a `miss_count` to every entry in `ledger_matches`. On a default
install that is the ONLY path, because `cloudwatch` and `webhook` register no `ActionSink`
and every action falls through to `noop` — so exercising the proposal flow, which is exactly
what an operator is told to do before granting real authority, **demoted their own proven
knowledge for a write nobody made**. Verified: act mode plus one scoped cloudwatch rule took
a verified/high/2-use entry to `miss_count=1` and off the fast path. `routes._handle_action`
therefore gates on `result.ok and not result.simulated`.

**Only some verbs are verifiable** (`VERIFIABLE_ACTIONS = {resolve, silence}`). An `ack`
leaves an alert firing *by design* — `normalize_state` maps `acknowledged` onto `firing` on
purpose (see `providers/pagerduty.py`) — so firing state carries no information about
whether the ack landed. Those are stamped `not_checkable` with **no** due date rather than
judged against the wrong evidence, and the board says "not checked" instead of leaving a
blank that reads as success. That is an admitted gap: no adapter here reports
acknowledgement state back.

**Two schedules.** A `silence` is rechecked at the END of its own window — the schedule
contract 3a's mandatory expiry buys, and the more interesting moment, because a suppression
that expires straight back into the same firing condition is positive evidence nothing was
fixed. Everything else waits `DEFAULT_VERIFY_AFTER_SECS` (5 minutes), long enough for a
provider evaluating on a period to catch up.

**No new cron.** The recheck rides on the poll `run_cycle` already made, so it costs zero
extra provider calls and stays inside the heartbeat's flat cost. Verification is a **read**,
so it sits entirely inside the read-only-by-default posture.

`CycleResult.verifications` is `{incident_id: verdict}` for the incidents decided *this
cycle only*; an empty map is the normal case and is **not** "every action worked". It feeds
`changed` only for `still_firing` — the app discovering a claim it made was untrue is the
most newsworthy thing a cycle can find, while announcing `cleared` would make the heartbeat
congratulate itself and announcing `unknown` would broadcast a non-finding.

**A `still_firing` verdict charges a miss** to every entry in `ledger_matches`, not to "the
one we used": nothing records which match the investigation applied (`proposed_action` is
declared and never assigned), and `MAX_MATCHES_PER_SIGNAL` is 3 so the blast radius is
bounded. That join is what makes `use_count` mean "worked" (contract 2b).

The postmortem carries the verdict too (`store._verification_line`), because that artifact
is what a colleague reads with no access to the board, and "Actions taken: silenced the
alarm" is the sentence most likely to be believed as an *outcome*. It renders nothing at
all when no action was taken — a "not applicable" line on every incident would bury the
cases where it matters. `verification_detail` goes through the redactor (it quotes a
provider's poll-failure text); `verification` and `last_action` do not, being closed enums
we assign.

### 4. Keystone secret path (security)

`ops_mission_control_secrets.json` on the crew home, registered in
`security._CREW_SECRET_LEAVES`. That places it on the shared **read+write**
sensitive-path floor, so the agent's own file tools (`is_sensitive_path`) and shell
forms (`is_sensitive_bash_command` — `cat`, `>`, `tee`, `tar -C`/`unzip -d`) can
neither read nor overwrite it.

Do NOT move these tokens into `config.json` or the app's `data/config.json`: the
latter is served over `/api/apps/<name>/config` **without session auth**, and the
former is writable by any auto-approved agent shell. The authenticated dashboard
PUT handler is the only writer and opens the path directly, bypassing the gate, so
Settings still works.

`test_security.py` asserts `SECRETS_FILENAME in security._CREW_SECRET_LEAVES`, so a
rename that forgets the registration fails the build rather than silently dropping
the protection.

**Retention: credentials outlive an uninstall, deliberately — and it is disclosed.**
The file sits at the crew-home ROOT, which is what puts it on the sensitive-path floor.
The consequence is that `uninstall_app` (which removes `apps/<name>/`) cannot reach it,
so a PagerDuty/Datadog token survives uninstall. Moving it under the app dir would hand
the agent its own credentials; silently wiping tokens would break the legitimate
uninstall/reinstall flow. So the behavior stays, and Settings states it plainly next to
the Revoke button — the only control that changes it. Two tests pin both halves
(`test_secrets_live_outside_the_app_dir_so_uninstall_cannot_reach_them`,
`test_settings_discloses_that_uninstall_keeps_credentials`), the first of which also
fails if a future change moves the file under `apps/` and thereby drops the keystone
protection.

**Disable is clean** (verified live): crons fully deregister — `/api/crons` shows zero
for this app — and every route 403s via `_require_enabled`. Credentials are kept, which
is right for a pause.

## Provider seam (`backend/providers/base.py`)

Four narrow Protocols, each with a shipped default, following the CPP pattern in
`platform/interfaces.py`:

| Protocol | Question | Public adapters |
|---|---|---|
| `SignalSource` | What is firing? | `cloudwatch`, `pagerduty`, `datadog`, `github-issues`, `webhook` |
| `RotationSource` | Who is on shift? | `pagerduty`, `always-on` (default) |
| `ActionSink` | Ack / resolve / comment / silence | `pagerduty`, `datadog`, `github-issues`, `noop` (default) |
| `EvidenceSource` | Surrounding context | `cloudwatch-evidence`, `datadog-evidence` |

Split four ways rather than one fat interface because real providers cover
different subsets — CloudWatch has alarms and metrics but no rotation and nothing
to resolve.

### Evidence is brokered to the agent, never delegated

The investigating agent's sandbox has **no** AWS/provider credentials, and
deliberately gets none. The gateway already holds the operator's profile and already
redacts at a single chokepoint, so it gathers the evidence and the brief carries the
resulting *text*: **gateway reads (credentialed, bounded, redacted) → brief → agent
reasons**. Giving the agent its own profile would create a second credential holder
whose reads nothing redacts and whose scope nothing bounds — the opposite of the
least-privilege guidance that says prefer scoped access over distributing credentials.

Wired at both claim paths (`run_cycle` and the manual `/incident/claim`) via
`gather_evidence_safely`, which treats any fault as "no evidence": an investigation
without evidence is worse than one with, and far better than a dropped claim.

Before this the brief carried signal metadata and ledger hints and **nothing else**,
so an AWS investigation had no alarm history and no logs — the agent correctly reported
it could not proceed, which read as a credentials gap but was actually a plumbing one.

**The no-credentials statement is unconditional.** It first shipped inside the
`if claimed.evidence:` branch, which meant the case that most needs it — *no* evidence
gathered (unconfigured source, provider outage, empty poll) — was the only case that
never saw it. Two live beta sessions then spent their whole turn re-running
`aws … --profile motor_pe_beta`, collecting `NoCredentials` each time, and produced no
diagnosis; both concluded the profile was a hollow stub, when in fact the profile is
healthy and the gateway reads that same account fine. It is now emitted for every brief
and names the dead end concretely (`Do not run aws …`), because "you lack credentials"
alone still leaves one `sts get-caller-identity` looking worth a try. Pinned by
`test_brief_always_states_it_has_no_credentials`, which asserts with evidence **empty**
— with evidence present the buggy code passed too, which is why the gap survived.

Worth recording for future diagnosis: the sandbox layer is **not** what blocks the
agent's AWS access here. `agent.sandbox` defaults to `off` (so `wrap_argv` returns
immediately), `_STANDARD_DIRS` does not hide `.aws` at all, and `security.py` blocks
credential-file *content reads* (`cat`/`grep` on `~/.aws/`) but not AWS CLI invocation.
The agent's bash children are isolated by **kiro-cli's own** internal sandbox
(`~/.kiro/settings/amazon-internal.json` → `{"sandbox": true}`), a layer KiroCrew
delegates to rather than controls. Brokering is what makes that irrelevant: the gateway
holds the credential, so the agent never needs one.

**The brief bounds evidence separately from the adapter budget.**
`EvidenceBudget.max_bytes` (64 KB) caps what an adapter may *return* — right for a
spool, far too large for a prompt (6 calls × 64 KB ≈ 384 KB, against the documented
50k total session context budget in `context.py`). A real beta-account brief measured
**37,423 chars** from two items. `MAX_BRIEF_EVIDENCE_CHARS` (8k total) and
`MAX_BRIEF_EVIDENCE_ITEM_CHARS` (4k per item) bound the rendered text, and the brief
**says** when it truncates — an agent silently handed half a log dump will reason
confidently about a partial picture. Same brief after: 7,467 chars, still carrying both
the alarm history and the root cause.

### Per-adapter evidence budgets

One `EvidenceBudget` served every adapter, which does not match how they behave: a
CloudWatch Logs Insights query is submit-then-poll and legitimately wants ~25s, while a
Datadog REST call either answers in seconds or is broken. CloudWatch had already noticed
— it declared `_LOG_MAX_WAIT_SECS = 25.0` then applied `min(25.0, budget.timeout_secs)`,
so against the 20s global its own ceiling was **unreachable dead code**.

An adapter may now declare `evidence_budget_hint`, and `EvidenceBudget.for_source`
resolves it **clamped with `min` on every field**. The hint says "this is what I need";
the operator's configured value stays the authority. An adapter that could raise its own
spend ceiling would be an adapter that sets its own cost — the same reason the autonomy
gate is resolved outside the adapter. Measured: operator 30s → 25s (hint applies),
operator 20s or 8s → operator wins.

No hint means no change, so this is opt-in and every existing adapter behaves exactly as
before. The fan-out waits on the **same** resolved value the adapter was handed —
passing one timeout into `gather` and enforcing another outside it kills an adapter
mid-call while it believes it has budget left.

Expose a hint as `MappingProxyType`, not a bare dict: a mutable class attribute shared
across instances is one accidental assignment away from an adapter rewriting its own
ceiling at runtime. `for_source` therefore checks `Mapping`, not `dict` — checking `dict`
silently ignored every correctly-written hint, which the tests caught only because they
assert the clamped **value** rather than that the call returned something.

### Evidence redaction is a single chokepoint

`OpsProviderRegistry.gather_evidence` is the **only** caller of any adapter's
`gather()`, and it redacts every `Evidence.body` through
`redact_tokens(security.redact(...))`. That is what lets `Evidence`'s docstring
promise an adapter "cannot forget" — a second call site would silently bypass it, so
`test_providers.py::test_redaction_is_the_only_path_out_of_an_adapter` pins the funnel
by source inspection. This matters because evidence is largely **log content**, and
governance guidance on logging is explicit that logs must not contain secrets —
which is precisely why credentials turn up there by accident.

Redaction runs **before** the byte cap, not after. A redaction marker is longer than
most of what it replaces, so capping first let the emitted body exceed
`budget.max_bytes` (measured ~1.09x on an all-credential body) — and that budget
exists to bound what reaches the model's context, so it has to bound the text
actually emitted. A `_REDACT_HEADROOM` pre-trim still bounds the regex work so a
misbehaving adapter cannot hand us unbounded text to scan.

### Evidence config resolves in its own namespace

`CloudWatchEvidenceSource` advertises `config_fields` under its own id
(`cloudwatch-evidence`), so Settings writes to `providers["cloudwatch-evidence"]` —
but the gather code read `providers["cloudwatch"]`. Since `log_groups` exists **only**
on the evidence adapter, whatever the operator typed landed where nothing looked for
it and log evidence was silently always empty. `_evidence_value` / `_evidence_list`
now read the adapter's own namespace and fall back to the signal source's, so a
single-account install that configured `region`/`profile` on `cloudwatch` keeps
working. `configured()` accepts either namespace's enable for the same reason.

Generalized as a test: every advertised `config_fields` entry must actually be read
somewhere in the module. A field the UI renders an input for but the code never
resolves is a lie to the operator.

Verified live against the beta account: both branches return real data (alarm
history showing a flapping OK↔ALARM pattern, and Logs Insights returning the actual
`ValueError: File processing failed` root cause).

### Boolean provider config is parsed, not `bool()`-cast

`config_flag` reads boolean provider-config fields. Two real bugs it fixes:

- `include_insufficient_data` was compared against the literal string `"true"`, so
  `yes` / `1` / `True ` / a real JSON boolean all read as **false, silently** — the
  operator saw the setting applied and stale-metric detection stayed off.
- `provider_enabled` used `bool(...)`, and `bool("false")` is `True` — so a config
  carrying `"enabled": "false"` (hand-edited, or written by a form that stringifies)
  would **enable** the provider, the opposite of what it says.

An unrecognized value falls back to the caller's `default` rather than guessing:
treating garbage as false silently disables a detection the operator believes is on,
and treating it as true silently enables one they never asked for. `_FALSY` is
therefore listed explicitly rather than inferred as "not truthy".

`INSUFFICIENT_DATA` is the public equivalent of the source workflow's *table freshness*
checks — a pipeline that silently stopped running looks healthy when you only watch
`ALARM`. It stays opt-in (noisy on accounts with idle resources), but the provider
`detail` now says so, because an opt-in nobody is told about is one nobody uses.

### Registry is ADD-only

`OpsProviderRegistry.register_*` refuses an id that already exists and logs a
warning; the incumbent wins. A companion package can ADD adapters, never repoint a
core one — otherwise auditing the public core would require auditing every
companion. The core never imports a companion and never branches on edition.

### Fan-out resilience

`poll_all` runs configured sources concurrently with a per-source timeout and
returns `(signals, errors)`. One unreachable provider yields a per-source error
entry, never an exception — the heartbeat must survive a dead provider.

### Absence is not evidence (`registry.poll_health`)

**A signal missing from a poll means one of THREE opposite things: it cleared, we could
not look, or this source structurally cannot tell us.** Nothing recorded which, so a 429, a
timeout, an expired token, or a storm
that pushed a signal off the first page all read exactly like "resolved" — and the
reconcile SOP closed live incidents on that basis, into a *terminal* status carrying the
resolution text "signal cleared at the provider". Recovery required the alarm to fire
again as brand-new work.

Two mechanisms, both in `registry`:

#### Deciding a proposal is a compare-and-set

`decide_proposal` runs its whole read → state-check → write inside one `_IndexLock`, so
exactly one caller can move a proposal out of `pending`. It previously read via
`get_incident`, tested the state, and wrote via `update_fields` — three separate index
accesses with no lock held across them.

**Scoped honestly, because the first description of it was wider than the code.** The old
write path went through `update_fields` → `transition`, which itself takes `_IndexLock` and
re-reads the index, so two decisions were already serialised *at the write*. The reachable
defect is therefore not "both callers execute the provider action" but "both pass the state
check, and the second write lands on top of the first" — `transition` re-read the index and
then overwrote `proposed_action` unconditionally, without re-checking the proposal state it
had been handed. Narrower, still wrong, and the fix is the same.

Pinned **structurally** (the state check is inside the lock; the locked write does not
re-enter it through `update_fields`) rather than by a concurrency test. Several harnesses
were tried — pausing on `utc_now_iso`, `get_incident`, `_read_index_unlocked`,
`update_fields`, plus barriers — and every one produced a single winner against the pre-fix
code, because of that same serialisation at the write. A concurrency test that passes against
the bug it names is worse than no test.

- **`poll_health()`** — per source, whether the LAST poll attempt succeeded, with the
  reason and timestamp. A source **absent** from the map has not been polled and must be
  treated as "cannot conclude", not as healthy. Surfaced at `/signals` alongside
  `all_sources_healthy`; `reconcile.md` now requires consulting it before resolving on
  absence, and resolving directly only for signals in the new `cleared` list (an explicit
  provider `ok` is positive evidence rather than an inference).
- **`poll_health()[src]["snapshot"]`** — whether that successful poll was a COMPLETE
  picture, i.e. whether absence from it is evidence at all. `ok` answers "did we look";
  this answers "did we see everything", and for one source they differ. Read off
  `SignalSource.is_snapshot`, defaulting `DEFAULT_IS_SNAPSHOT = True` so every polled
  provider API — and every companion adapter written before the flag existed — keeps its
  correct behaviour, and only a queue-draining source opts out.

  `WebhookSignalSource.is_snapshot = False` is the sole exception and the reason the flag
  exists: it is a **push spool**, so a delivered signal leaves the spool once an incident
  claims it (`webhook.ack`) and is absent from every cycle after that whether or not the
  fault is still live at the sender — a push source announces a fault, it never re-asserts
  one. `poll_health` recorded that empty result as
  `{"ok": True, "signals": 0}` — which `dispatch.verify_pending_actions` reads as "the
  source answered and the signal is gone". So one cycle after any webhook delivery, an
  action against that signal verified as `cleared` ("the resolve held") with the fault
  untouched. Same class as resolving on a failed poll, reached through a **successful**
  one, which is exactly why the `ok` guard could not catch it. Both consumers now gate on
  it: `verify_pending_actions` returns `unknown` (open, so a later cycle retries) and
  `reconcile.md` Pass 1 step 3 requires `snapshot != false` on top of `ok`. Rendered on the
  Signals source row and qualified into the "every source answered" banner, because that
  banner is the line an operator reads before trusting a quiet board.
- **An adapter must RAISE on a fault, never return a short list.** `poll_all` records a
  source as unhealthy only when `poll()` raises, so an adapter that catches its own failure
  and returns `[]` is recorded as a *successful* poll that saw nothing — which is precisely
  the "it cleared" reading this whole section exists to prevent, reached through the `ok`
  guard rather than around it.

  `CloudWatchSignalSource._poll_sync` did exactly that on both of its failure paths: a
  `None` client (boto3 missing, bad profile, **expired credentials**) returned `[]`, and a
  `describe_alarms` exception returned the signals gathered so far — which with
  `include_insufficient_data` on means a failure in the second pass silently truncated the
  estate. Expired credentials therefore rendered as an all-clear over a live estate, and
  `all_sources_healthy` promised absence-means-recovery on top of it. Both paths now raise
  with a message naming the likely cause. Found in review.

  This costs nothing on a default install: `provider_enabled` defaults to **False**, so
  nothing polls CloudWatch until the operator turns it on — at which point a credential
  fault is exactly what they need to be told. `configured()` remains the honest place to
  say "no AWS here". Pinned by
  `test_providers.py::TestACloudWatchFaultIsNotAQuietEstate`, which asserts the registry
  records the fault as unhealthy rather than asserting only that an exception escapes.

  **Rule for any new adapter:** catching an exception inside `poll()` to keep the cycle
  alive converts "we could not look" into "nothing is firing". Let it propagate; the
  registry already turns it into per-source health plus a backoff window.
- **Backoff.** A failed source is skipped for a window and says so in `errors`, honouring
  the provider's own `Retry-After` when sent (clamped by `MAX_RETRY_AFTER_SECS`) and a
  flat `DEFAULT_BACKOFF_SECS` otherwise. `HttpError` now carries `status` + `retry_after`
  and `is_retryable`; previously `status` was assigned and **read nowhere**, so a
  rate-limited provider was re-polled at full rate every 120 s — which is how a rate limit
  becomes a ban. A success clears the backoff. Only `RETRYABLE_STATUSES` get the
  provider's own delay: a 404 or 401 is a config fault that waiting will not fix.

  **A TIMEOUT arms the window too**, and it originally did not: `asyncio.TimeoutError` *is*
  an `Exception`, so its own `except` clause shadowed the generic one that calls
  `_note_backoff`, leaving the single most expensive failure mode as the only unthrottled
  one. A source that fails fast costs a socket; a hung one burns the full
  `DEFAULT_POLL_TIMEOUT_SECS` (15 s) out of **every** 120 s heartbeat for as long as it
  stays hung. Verified before fixing: three consecutive timing-out polls left
  `_backoff_until` empty.

Related boundary fix: `/signals` returned every signal regardless of state under
`signals`, while `dispatch.run_cycle` claims only firing ones. Harmless while no adapter
could emit `ok` — but the webhook now can, so the route exposes a state-filtered `firing`
list (and `unclaimed` is derived from it), or an already-recovered signal would appear as
apparent work in the very list reconcile reads as "still firing".

`/signals` now carries a **third** bucket, `suppressed` (contract 1b) — the third reason a
signal can be absent from `firing`, and neither of the other two: it did not clear, and we
did look. It must not be resolved on absence (nothing was fixed) and must not be folded into
`cleared` (which asserts recovery). The raw `signals` key is unchanged for compatibility,
which is exactly why the bucket is required: a UI counting `signals` under a column headed
"Firing" would render "3 firing" above an empty queue with no explanation. `reconcile.md`
Pass 1 step 5 states the rule for the agent side.

`gather_evidence` passes every body through `security.redact` **and**
`secrets.redact_tokens` centrally, so an adapter author cannot leak a credential
into a model prompt by forgetting to redact.

## Security model

### The autonomy ceiling lives on the keystone floor

`mode` (observe/propose/act) and the per-signal `autonomy_rules` are the app's SECURITY
CEILING: `effective = min(app_mode, rule_mode)` is only a ceiling if the party it constrains —
the agent — cannot raise it. They lived in `data/config.json`, which is served over
`/api/apps/<name>/config` **without session auth** and is writable by any auto-approved agent
shell. So a prompt-injected agent could mint the dashboard token, PUT `mode=act` with a rule
matching a signal, and unlock a provider write the operator never granted. Found in review.

They now live in `ops_mission_control_policy.json` on the `security._CREW_SECRET_LEAVES` floor
(`backend/policy_store.py`), exactly like the provider secrets — the agent can neither read nor
overwrite it. The authenticated dashboard PUT is the sole writer and opens the path directly.
A pre-fix install's values are lifted out of `config.json` and deleted from it on first read
(`migrate_from_config_if_needed`), so no live agent-writable shadow copy survives the upgrade.
A test pins the policy path onto `is_sensitive_path` and the filename equal to the fence entry.

### Authoring an act-rule (`PUT /settings` → `autonomy_rules`)

Fencing the ceiling left it with **no write path at all**: `policy_store.set_rules` had zero
callers, `PUT /settings` handled `mode` but not `autonomy_rules`, and `GET /rotation` returned
only a rule *count*. So the app's headline `act` tier was unreachable — Settings said grants
came from "patterns you have explicitly allowlisted with a rule", rendered an un-actionable
empty state, and offered nothing to click; the manual said to edit `data/config.json`, which
`migrate_from_config_if_needed` ignores once the policy file exists. Every act-mode adopter
silently got Propose behavior, with no error anywhere. Found in review.

The write path is `rotation.save_rules` → `policy_store.set_rules`, reached from the
authenticated `PUT /settings` and nowhere else:

- **Validation reuses `AutonomyRule.from_dict`** rather than restating the rules, because that
  is where "an act-rule may not be a blanket grant" already lives.
- **A rejected rule is refused with a 400, not stored and dropped on the next read.**
  `load_rules` skips unparseable entries, so persisting one would show the operator a grant
  that looks saved and never matches — the exact failure the two-key design exists to prevent.
  The error code names the offending index (`rule_1_invalid`).
- **All-or-nothing:** a submission containing one bad rule leaves the previous grants intact,
  so a bad edit cannot partially revoke working authority.
- **`GET /rotation` now returns `rules_detail`** — the parsed rules in the same shape the PUT
  accepts, so the editor reads, edits and PUTs back the same object. A count could not be
  rendered, reviewed or revoked. Entries that failed validation are absent rather than
  displayed as if live.
- The Settings card offers only **configured signal-role providers** (a rule naming an
  unconfigured provider can never match), requires the pattern before enabling the grant
  button, and has a per-rule revoke — authority must be retractable, and saving an empty list
  clears every grant.

**Outbound DESTINATIONS are operator-only for the same reason**, found by auditing for this
class rather than waiting for it to be reported. Two more keys were sitting in the
agent-writable file:

| key | what an agent gains by rewriting it |
|---|---|
| `ledger_sync_remote` | redirects the shared-ledger **git push** to a repo it controls — straight exfiltration of the team's accumulated incident knowledge, and `POST /ledger/hygiene` (which the agent's own hygiene cron calls) performs the push |
| `slack_channel` | redirects the incident mirror — every title, diagnosis and resource name — to a channel it chose |

Verified before fixing: writing `config.json` moved both, and that file is neither path-fenced
(`is_sensitive_path`) nor shell-write-blocked (`is_sensitive_bash_command("echo x > …")`). Both
now live in `policy_store.OPERATOR_ONLY_KEYS` on the fenced floor, with their `*_enabled` flags
— being able to turn an exchange ON is most of the way to redirecting it.

`ledger_sync_branch` deliberately stays in plain `config.json`: it selects a ref inside a remote
the operator already chose, is shape-validated at the write path, and cannot move data off-box.
The rest of `config.json` — regions, prefixes, alarm filters — is config the agent legitimately
reads and it stays there. `policy_store.put`/`get` refuse any key outside the allow-list, so a
typo cannot silently create a fenced key or quietly write a security value to the wrong file.

### A manual claim authorizes against the provider's signal, not the caller's

`/incident/claim` took a fully caller-supplied `Signal`, and `resolve_mode` matches rules on
its `source`/`resource`/`labels`. A caller controlling the whole object could pair a resource
an operator's rule authorizes with a different provider's target in `labels` — the resource
passes the gate while another field drives the sink, so the authorization describes a signal
that does not exist. The route now polls and resolves the claimed `id` to the provider's OWN
signal, refusing (`signal_not_firing`, 409) if that id is not currently firing; the caller's
other fields are discarded. The board already sends a signal it got from `/signals`, so this
rejects only a fabricated or stale one. Found in review.

### Autonomy gate (`backend/rotation.py`)

`effective = min(app_mode, matching_rule_mode)` over `observe < propose < act`.

- Default `app_mode` is **`observe`** — nothing is written to any provider.
- `act` requires **both** `app_mode == act` AND a user-authored rule whose
  predicate matches the specific signal.
- **No blanket grants.** `AutonomyRule.from_dict` refuses an `act` rule that names
  only a source, with no `resource_glob` or `label_match`. "Act on everything from
  CloudWatch" is not expressible.
- A rule can only NARROW the app ceiling, so it cannot escalate an instance the
  operator pinned to `observe`.
- Every authorize decision — allow and deny — is SEL-audited as
  `ops-mission-control.action_authorize`.

This deliberately diverges from the internal workflow it models, which
auto-resolved two known machine-generated intakes by default. That team could
reason about which intakes were safe because they built them; a public install has
no such basis.

Remediation *execution* (running a fix) is out of scope. The app diagnoses and
proposes; the human applies.

#### The gate is a chokepoint, not a convention

`ActionSink.execute` does not police its own authority — by design, §5.3 — and the gate
originally ran at two independent call sites (`/incident/action` and the approved-proposal
path) with nothing but a docstring (*"callers MUST have resolved the autonomy gate first"*)
joining the two. Review named the consequence exactly: **a third caller could silently skip
the gate**, and no code disagreed.

Authority is now a value rather than a comment:

- **`routes._authorize(signal, action)`** runs the gate and is the ONLY place an
  `_Authorized` permit is constructed. A test pins that, because a permit minted next to
  the write would be a rubber stamp.
- **`routes._execute_authorized(sink, permit, payload)`** is the ONLY caller of
  `ActionSink.execute`. A test asserts that call site count is exactly one, so a future
  third caller fails CI instead of shipping an ungated write.
- The permit carries the signal and action it was minted for, and the executor reads the
  write's target **from the permit** rather than from a parallel argument — so spending a
  `comment` permit on a `resolve` is unrepresentable, not merely rejected.

#### Anything that can reach `resolve_login()` runs off the loop

`schedule_file._resolve_login_sync` spawns `gh api user` **synchronously** with a 10s
timeout whenever `schedule-file.github_login` is unset — the documented default, because
the provider resolves identity from `gh`. Two independent paths reach it:

- `rotation.authorize_action` → `_definitely_off_shift` → `resolve_now`
- `rotation.describe` → `is_primary` → `_schedule_me` → `resolve_login`

Both now go through `asyncio.to_thread` at **every** call site (`_authorize`, and the three
`rotation.describe` sites in `/state`, `/handover` and `/rotation`). Run inline, one request
in a fresh gateway froze the whole event loop for up to 10s — the user's chat turn and the
liveness heartbeat with it.

**The reasoning that made this a two-round bug, recorded because it is the trap.** An
earlier revision left the `describe()` sites inline, arguing that each is preceded by an
awaited `registry.resolve_shift()` which warms the login cache off-loop. That is wrong:
`resolve_shift` wraps each source in `asyncio.wait_for(..., DEFAULT_POLL_TIMEOUT_SECS)`
(15s), and a timeout cancels the *awaiting coroutine* while the `to_thread` worker keeps
running — so the poll can give up with `_login_cache` still unset, and the inline
`describe()` then pays the full spawn on the loop. "Something upstream probably warmed the
cache" is not a guarantee; `to_thread` is. Review caught it after the first fix shipped.

One site was subtler than the others:
`await asyncio.to_thread(handover.build, providers, rotation.describe(shift))` moved
*`build`* off the loop while still evaluating `describe()` on it, because arguments are
computed before the call. Moving a slow call off-loop does not move its arguments.

Guarded two ways: a static check that no `rotation.describe` call in `routes.py` sits
outside a `to_thread`, and a behavioural one that ticks a heartbeat coroutine during a
`/rotation` request and fails if the loop stalls.

#### Every stored-file read on a request path is off-loop

`companion.companion_summary()` walks `importlib.metadata.entry_points()`, enumerating every
installed distribution's metadata from disk (~9.3 ms). The store and ledger helpers parse a
JSON/JSONL file whose size grows with use:

| helper | empty | 100 | 1k | 5k | 20k |
|---|---|---|---|---|---|
| `ledger.stats()` | 0.1 ms | 1.8 ms | 13 ms | 93 ms | 275 ms |
| `store.counts_by_status()` | 0.1 ms | 4.3 ms | 42 ms | 188 ms | — |
| `store.open_incidents()` | 0.2 ms | 2.3 ms | 70 ms | 251 ms | — |

All of them, on every request path, go through `asyncio.to_thread`.

**An earlier revision left the store and ledger reads inline, calling them "negligible" at
~0.03 ms.** That number was measured against an EMPTY store and the conclusion generalised
from it — the single input where the cost is zero by construction. Review caught
`ledger.stats`; auditing for the class then found `store.counts_by_status`,
`store.open_incidents` (called *twice* inline in `/state`), and inline index parses in
`/handover`, `/incidents` and `/signals`. A flapping alarm minting hundreds of incidents is
documented elsewhere in this very spec, so the growing case was never hypothetical.

**The rule:** for anything that parses an accumulating file, the empty case is not the case
worth measuring. And a helper that RETURNS one record does not necessarily DO one record's
work — `store.get_incident` and `find_by_signal` are `read_index().get(...)`, so each pays the
full parse. Both were missing from the first version of the guard's helper list, and review
then found six inline calls across the incident, transition, action and proposal handlers. If
it touches disk, it goes in the list.

#### A provider-supplied URL is never rendered as a live link

`Signal.url` comes from a provider — including the HMAC-signed webhook, which accepts anything
able to POST JSON — and was rendered straight into `href={s.url}` on four surfaces. A signal
carrying `javascript:alert(document.cookie)` therefore produced an executable link **in the
dashboard's own origin**, on an element labelled "Provider" that an operator is invited to
click. Found in review.

Every one now goes through `lib/safeUrl.safeHttpUrl`, which rejects any non-http(s) scheme and
any userinfo, and a rejected URL renders **no link** rather than a dead one. The helper already
existed for precisely this, and sibling apps (`issue-radar`, `ArtifactDeployPage`) already used
it — this app was the outlier, which is why the guard asserts the class: no `href={…url}` in
the app without `safeHttpUrl` on the same line, and the gate condition must read the VALIDATED
value (gating on the raw URL would render `href="null"` for a rejected one — visibly broken
rather than absent). A third test pins the helper's own behaviour, so loosening `safeHttpUrl`
cannot silently re-expose the app while the first two still pass.

Two guards, both asserting the class rather than the known sites — the per-site version of
this lesson has now been learned twice:

- no `store.*`/`ledger.*` file-parsing call in `routes.py` sits outside a `to_thread`;
- no slow call is *evaluated as an argument* to `to_thread`. `to_thread(f, g())` moves `f`
  off-loop and runs `g` on it, reads as fixed at a glance, and shipped once
  (`to_thread(handover.build, providers, rotation.describe(shift))`).

### AWS access

Ambient credential chain only — profile, role, or instance role. The app never
accepts, stores, or transmits an AWS access key. `boto3` is an **optional lazy
import**: the module imports cleanly without it and the adapter reports
unconfigured. Read-only permissions requested (`cloudwatch:DescribeAlarms`,
`GetMetricStatistics`, `GetMetricData`, `logs:StartQuery`, `GetQueryResults`,
`DescribeLogGroups`); no write permission.

### Route gating

Builtin routes are registered at gateway startup and exist while the app is
disabled, so **every** handler is wrapped in `_require_enabled` (403 when
disabled). `test_routes.py::test_every_registered_handler_is_gated` walks the
router and fails if a route lacks the wrapper, so a newly added route cannot ship
ungated.

Secrets are **write-only** over HTTP: `PUT .../secret` accepts a value, and no read
endpoint ever returns one (`describe_secrets` reports set/unset only). Unknown
secret field names are refused so the keystone file cannot become arbitrary
agent-inaccessible storage.

**The config route refuses secret fields.** `PUT /providers/<id>/config` writes
`data/config.json`, which is served over `/api/apps/<name>/config` **without
session auth**. A settings form that posted a token there would put a live
credential behind nothing but the gateway port, so the route rejects any key
matching the adapter's `secret_fields` (400, SEL-audited) and accepts only keys the
adapter declares in `config_fields`. Pinned by
`test_config_routes.py::test_secret_field_is_refused`.

**A config value that names a HOST is a credential-exfiltration surface.** Refusing to
store secrets in `config_fields` is not sufficient on its own: an ordinary, non-secret
config value can still decide *where the stored secrets are sent*. Datadog's
`site` is region-specific config (`datadoghq.eu`, `us3.datadoghq.com`, …) and was
interpolated into the request host verbatim, while `_headers()` attaches **both**
`DD-API-KEY` and `DD-APPLICATION-KEY` to every request — so a prompt-injected agent
writing `site` through the config route had both keys posted to a host it chose, on the
next poll, with no user-visible step. Found in review.

`site` now passes through `datadog._site()`, which admits only Datadog's published site
domains (`_ALLOWED_SITES`) and otherwise logs and falls back to the US default. An
**allowlist**, deliberately: a `.datadoghq.com` suffix test is defeated by
`evil-datadoghq.com`, and a "looks like a domain" test admits every domain there is. It
falls back rather than raising, because "we talked to Datadog US and your monitors were
not there" is visible and harmless, while sending credentials to an unvetted host is
neither. The monitor *link* is gated by the same function — no credential rides it, but an
attacker-controlled URL rendered as a clickable link on the incident board is a phishing
target inside the operator's own tooling. Pinned by
`test_providers.py::TestDatadogCredentialsOnlyReachDatadog`, including the userinfo
(`datadoghq.com@attacker.example`) and path/port injection shapes.

**The general rule this establishes:** when adding a `config_fields` entry, ask whether it
can influence a request URL, and if it can, constrain it at the point of use rather than
trusting the writer.

**`cloudwatch.region` is the second instance, found by auditing for the class** rather than
waiting for it to be reported. It is interpolated into the console HOSTNAME
(`https://{region}.console.aws.amazon.com/…`), which becomes an "open in provider" link on
the incident board. Measured with `urlsplit`: `region="evil#"` renders
`https://evil#.console.aws.amazon.com/…` whose real host is **`evil`** (the `#` starts the
fragment and discards the rest), and `region="attacker.example.com"` yields
`attacker.example.com.console.aws.amazon.com`. No credential rides this URL — it is a
phishing vector rather than an exfiltration one, which is the same reason review gave for
gating the equivalent Datadog monitor link.

`_validated_region()` applies a SHAPE check (`^[a-z]{2,}(?:-[a-z0-9]+)+$`), not an allowlist:
AWS adds regions regularly and a stale list would silently break a legitimate install, which
is the failure mode an allowlist is only acceptable for when the set is genuinely closed
(Datadog's published sites are; AWS regions are not). Every injection character that matters
(`/ # ? @ :` and `.`) is excluded by construction. An unrecognised value degrades to "no
link", never to "a link somewhere else". Case is normalised first so an operator's
`US-EAST-1` typo works, which cannot widen the gate. Validated in BOTH namespaces that expose
the field — the signal source's and the evidence adapter's via `_evidence_value` — because
guarding one read leaves the same field open one namespace over.

**A remote URL is refused, not stored, when it embeds a password.** `ledger_sync_remote`
accepted `https://user:ghp_xxx@github.com/org/repo.git` and persisted it into
`data/config.json`, which is served **without session auth**, and `redact_tokens` has no
pattern for a PAT inside a URL. The frontend's `displayRemote()` strips userinfo for display
only, and its own docstring said so — a documented hole rather than a fixed one. `PUT
/settings` now rejects it (`remote_has_credentials`, 400) with a message telling the operator
to rotate the token and use a credential helper or SSH.

Refused rather than silently stripped: the pasted token is compromised either way, and a
remote quietly rewritten to an unauthenticated URL would fail to push later with no hint why.
`_url_has_userinfo` parses with `urlsplit` and only flags a **password** on an http(s)
remote, so the two legitimate `@`-bearing shapes keep working — scp-style
`git@github.com:org/repo.git` (no `://`, so no userinfo) and `ssh://git@host/...` (a username
authenticating by key, not a secret).

`PUT /settings` refuses an unrecognized `mode` rather than falling back — a typo
must not quietly change what the agent is allowed to do.

### The postmortem is a redaction sink

`store.write_log` is a registered `security_posture` redaction sink alongside
`slack_out.py` (the Slack board) and `registry.py` (provider evidence), and it runs
**both** passes: core `security.redact` (credential + exfiltration-URL scanners) and
the app's `secrets.redact_tokens` (provider-token shapes). Both are needed and it is
worth naming why, because picking one looks sufficient: core alone leaves a bare-hex
Datadog key and a prefix-less `Bearer` token in place, and the app pass alone leaves
an AWS access key id. `registry.gather_evidence` composes them the same way, so the
two paths provider text leaves this app sanitize to one standard.

What is redacted: the signal title, id, source and resource (provider text), the
model-authored `diagnosis` and `resolution`, and each matched ledger id. What is not:
the incident id, severity, status, operating mode, `fired_at` and the fingerprint —
our own identifiers, closed enums, a timestamp, and a hash we compute. Running the
scanners over those is a verified no-op.

**What this does and does not promise.** It masks credential *shapes*: recognized
vendor key formats, the carrier forms (`Bearer …`, `token=…`, `DD-APPLICATION-KEY:
…`), and exfiltration URLs. It does **not** understand meaning, so it will not remove
an internal hostname, a customer identifier, a stack trace naming private code paths,
or a secret with no recognizable shape. The artifact is safe to share in the sense
that a credential is very unlikely to ride out inside it; it is **not** a
declassification pass, and an operator sending it outside their organisation should
still read it. That distinction matters here more than at any other sink, because
this is the one output a human is expected to forward by hand.

### Subprocess spawn

`github_issues._run_gh` is the app's only subprocess spawn and is routed through
**`sandboxed_spawn_argv`** (OS filesystem isolation + credential-scrubbed env) with
a kernel resource ceiling from `resource_limit_preexec`. The repo, label set, and
comment body all come from agent-influenceable config, and `gh` reads the target
repo's own config on the way — so this is an agent-influenced spawn in the sense
`test/test_spawn_audit.py` polices, and it is routed rather than allowlisted.

That audit also scans test files, so async tests use
`unittest.IsolatedAsyncioTestCase` rather than bare `asyncio.run` (which the
scanner reads as an `asyncio.<spawn attr>` call).

### Webhook ingress

`POST /api/apps/ops-mission-control/webhook` on the authenticated gateway surface,
requiring an HMAC-SHA256 signature (`X-OMC-Signature`) over the raw body keyed by a
keystone secret and compared with `hmac.compare_digest`. **Fail-closed**: not
enabled, or no configured secret, means reject everything. Accepted deliveries land
in a bounded (200-entry) spool. No public ingress or tunnel is shipped.

**A read never consumes the spool — only a claim does.** `poll()` calls `peek()`, and
`dispatch.run_cycle` calls `webhook.ack({claimed ids})` after the claim loop. `poll_all`
has three callers and only ONE of them claims: the heartbeat, `GET /signals` (the Signals
tab's "Poll now"), and the `POST /incident/claim` authorization re-poll. When `poll()`
drained, the other two destroyed delivered alerts outright — an operator refreshing the
board while five Alertmanager alerts sat spooled got them rendered once as JSON and then
permanently gone: signature-verified, 200-accepted, no incident, no trace. The claim path
was the same bug and worse — claiming one signal discarded every *other* queued delivery.
Reported in review for the dashboard path; the claim path was found by auditing the other
two callers.

Even the heartbeat could not safely drain: `run_cycle` claims at most `max_claims` per
cycle, so a burst larger than the cap had its remainder destroyed by the very poll that
delivered it. Tying consumption to "an incident owns this id" is what makes the cap safe.
`maxlen` still bounds the spool, so a sender that permanently outruns the heartbeat drops
oldest-first — the same trade as before, no longer triggered by a read.
`webhook.reset_spool()` exists for test isolation only and is named so it cannot be
mistaken for a consumer.

**Check order is load-bearing** (`webhook.enqueue`): enabled → secret → size →
signature → parse. Nothing unauthenticated is ever handed to `json.loads`, and an
oversized body is refused *before* it is hashed. `test_webhook.py` pins the order by
asserting an unsigned malformed body is rejected for its **signature**, not its
syntax.

**Two accepted envelope shapes** (`signals_from_payload`), with the check order above
untouched:

- **Alertmanager / Grafana v4** — `{status, alerts: [...], commonLabels, ...}`. Each
  entry of `alerts` becomes its **own** Signal. Previously a raw Alertmanager body was
  rejected outright with 400 "payload has no title" — it carries no top-level
  `title`/`summary`, only `annotations`/`labels` — while this module's docstring named
  Alertmanager as a supported sender. Grafana *does* send a top-level `title`, so its
  notification was accepted and then collapsed into **one** board row, losing every
  per-alert instance in a group. Title falls back `annotations.summary` →
  `description` → `labels.alertname`; resource from `instance`/`job`/`pod`; url from
  `generatorURL`; `commonLabels` merge *under* per-alert labels; Grafana's per-alert
  `values` (the actual breaching numbers) are kept as a label, which is free evidence
  for a source that otherwise arrives with none. The provider's own `fingerprint`
  becomes `provider_key` (contract 1a) and the `native_id`, so re-deliveries dedupe.
  Fan-out is bounded by the existing `MAX_QUEUED_SIGNALS`, and one malformed entry is
  skipped rather than failing the whole delivery — one bad alert in a group of forty
  must not discard the thirty-nine that are fine.
- **The flat native envelope** — unchanged, so every existing sender keeps working.

**A sender can now report a clearance.** `state` was passed as the literal
`STATE_FIRING` with no `state`/`status` key read, so a sender could create work but never
retract it — leaving reconcile to infer recovery from absence, which is the inference that
closes live work when a poll fails. Read through `normalize_state`, so an unrecognized
value becomes `unknown` and cannot manufacture phantom firing work — and a *recognized
suppression* vocabulary becomes `suppressed` rather than `unknown` (contract 1b), read from
either the v4 scalar `status` or the v2 status OBJECT with its `silencedBy`/`inhibitedBy`.

**Shape is necessary, not sufficient, for those two senders.** Alertmanager's
`webhook_config` supports basic/bearer/authorization headers but **cannot** HMAC-sign the
raw body, and Grafana signs into `X-Grafana-Alerting-Signature`, not `X-OMC-Signature`. So
neither works out of the box against this fail-closed ingress without an accepted
signature-header list or a bearer verification mode — a **security posture decision,
deliberately not made here**. Both are reachable today via any forwarder that can sign.

**Rejection status codes are differentiated** (`_webhook_reject_status`): 401 for
trust failures (not enabled / no secret / signature mismatch), 413 for an oversized
body, 400 for payload faults (malformed JSON / non-object / no title). Everything
previously returned 401, so a sender debugging a bad payload was told
"Unauthorized" and would re-check credentials that were fine, while a real signature
failure looked identical to a typo. An *unrecognized* reason deliberately falls
through to **401**, not 400 — a refusal we cannot classify should not be advertised
as "your request was fine". A test derives the reason set from `enqueue`'s source, so
adding a rejection without classifying it fails CI.

**Two bugs found by writing the first tests for this adapter** (it had none, despite
being the only externally-reachable ingress):

- `signal_from_payload` put its `isinstance` check in a comprehension's `if` clause,
  which is evaluated *per item* — after `.items()` had already been called on the raw
  value. A payload with `"labels": "text"` raised `AttributeError`, which escaped
  `enqueue`'s `except` (JSON/Unicode only) and **500-ed the ingress**: a
  correctly-signed sender could crash the endpoint with one malformed field. Now
  `_normalize_labels` guards the type first and caps key/value lengths and pair count
  (`MAX_LABELS`), since labels reach the model's context and the fingerprint.
- `KeystoneFileBackend.__init__` snapshotted `secrets_path()`. The backend is a
  module-level singleton, so the data home was frozen at import and the whole process
  shared one secrets file — silently defeating per-test home isolation, which made
  "no secret configured must reject" pass only because a sibling test had written a
  secret. The path is now resolved per access (an explicitly-passed path is still
  pinned). This is a **testability** defect with a security consequence: the
  fail-closed assertion that protects this endpoint was not actually testing
  anything.

## Tier model

| Tier | Gate | Armed by | Crons |
|---|---|---|---|
| `always` | always armed — **never pausable** | nothing (ships enabled) | `ops-mission-control/rotation-check` (5m) |
| `on_shift` | `RotationSource.on_shift()` | `POST /rotation/arm` | `ops-mission-control/dispatch` (2m), `.../reconcile` (15m) |
| `primary` | `rotation.is_primary()`, enforced in `POST /ledger/hygiene` (409 `not_primary`) | nothing (ships enabled) | `ops-mission-control/ledger-hygiene` (daily 03:17) |

`reconcile` is on `on_shift`, not `always`: it POSTs `incident/transition` and edits Slack
messages, so on a team every instance would race to resolve the same incidents. The
"Armed by" column is load-bearing — only one tier is scheduler-armed, and the other two are
gated elsewhere or not at all.

**Cron names are the namespaced ones the scheduler actually registers**
(`<app-name>/<manifest cron name>`), NOT bare `omc-*`. `TIER_CRONS` originally
carried `omc-dispatch` and friends, which matched no registered job — so every
pause/resume the tier mechanism emitted silently targeted nothing and tier arming was
entirely inert. Found by exercising the rotation-check SOP against the real
scheduler. `test_tier_cron_names_match_the_manifest` now derives the expected set from
`app.json`, so adding or renaming a manifest cron fails the suite instead of quietly
re-breaking arming.

`rotation-check` lives on the `always` tier by necessity — on the gated tier an
off-shift instance could never re-arm itself (`test_store_and_gate.py` asserts this).

**Only an `on_shift` cron may ship paused.** Nothing in the codebase flips a manifest
`enabled: false`, and `POST /rotation/arm` arms *only* the `on_shift` tier — it cannot
pause an `always` job and deliberately leaves `primary` alone (see "Arming is
server-side"). So a cron on any other tier that ships disabled stays disabled **forever**. The earlier rule here was
"everything except rotation-check ships paused", justified as "they must not fire before a
provider is configured" — but shipping paused is the wrong mechanism for that; the step-0
cheap exit is, which is exactly why rotation-check was already exempted on that basis.
Enforced as "paused" it silently killed two more crons:

- `ledger-hygiene` (`primary`) — **proven dead on a real install**: still
  `enabled=False` with `last_run_at=None` after days of uptime. It is the ONLY caller of
  the git ledger sync, the vector-index import, and closed-incident pruning, so all three
  could never run in production however well tested they were.
- `reconcile` (`always`) — a tier whose name means "always armed" shipped disarmed, so
  the board was never reconciled against provider truth and drifted into fiction exactly
  as its own SOP warns.

Both now ship enabled with an explicit step-0 guard (`configured=true` → else `NO
output`), so a fresh install still pays nothing. Two tests pin the rule generically —
every non-`on_shift` cron must ship enabled, and every *enabled* cron must carry the cheap
exit — rather than naming one job, which is how this recurred.

### Rotation without a rotation service (`providers/schedule_file.py`)

A team with PagerDuty has a rotation API. For everyone else the schedule is
`rotation.yaml`, committed to the **same git repo the ledger syncs through**, with
identity resolved from the operator's **GitHub login** (config `github_login`, else the
local `gh` CLI, cached per gateway lifetime so a rotation tick every 5 minutes does not
re-spawn `gh` forever — including on a cache *miss*).

```yaml
timezone: America/Los_Angeles     # optional; UTC when absent
shifts:
  - from: 2026-08-01
    to: 2026-08-08
    who: octocat                  # scalar or list (co-primary allowed)
```

Reusing the ledger transport means no second integration and no second credential: a
shift swap is a reviewable diff, and the schedule arrives on the same pull that brings
teammates' lessons. `ledger_sync`'s generated `.gitignore` therefore un-ignores
`rotation.yaml` alongside `ledger.jsonl` — a schedule that never syncs is *worse* than
none, because it looks configured while disagreeing with everyone else.

Sharing the transport also means sharing its branch, and that is why bug 5 above mattered
here specifically: both the ledger **and** `rotation.yaml` publish through a refspec, so a
HEAD sitting on an unconfigured branch left the operator unable to `git pull` in the one
directory where a *refused* conflicted schedule has to be resolved by hand. The exchange
looked healthy the whole time.

Two decisions worth keeping:

- **A date-only `to` means through the END of that day.** `to: 2026-08-08` read as 00:00
  would silently drop the last day of every shift written date-only — the most likely
  misreading of this format, so it is handled in the parser.
- **It arms tiers; it never grants authority.** Any teammate can push a schedule, so it
  is wired to the cheap decision (when to look) and not the expensive one (what to do).
  `effective = min(app_mode, rule_mode)` still governs every action.

Every degradation — missing file, invalid YAML, unresolvable login, expired schedule,
reversed window — resolves to `unknown=True`, which the tier gate treats as ARMED. The
file arrives by `git pull` from a shared repo, so it is untrusted input: size-capped
(256 KB), shift-count-capped (5000, and it *logs* when truncating), and parsed with
`yaml.safe_load` — asserted by a test that greps for `yaml.load(`.

**The always-on default used to mask every real rotation.**
`AlwaysOnRotationSource` is always configured and always on-shift, and `resolve_shift`
returns the first on-shift answer — so a real source reporting "someone else is on call"
was discarded and the `on_shift` tier armed permanently for everyone, which is precisely
the failure a rotation exists to prevent. Fallbacks now declare `is_fallback = True` and
are consulted **only when no real source can answer**. Verified against the pre-fix code
(a real off-shift source resolved to `on_shift=True`) before changing it; pinned by
`test_the_always_on_default_does_not_mask_a_real_rotation` plus a companion test that the
floor still arms a solo operator.

### Arming is server-side: `POST /rotation/arm`

**The agent does not choose which crons to pause, and no longer holds `cron_pause` at
all.** `rotation.apply_tiers(shift, cron_service)` computes the tier map and moves the
app's jobs itself; the rotation-check cron just POSTs `/rotation/arm` and reports whether
`changed` came back non-empty.

This replaced an agent-driven loop, and the reason is worth keeping: off shift the armed
set still legitimately contains `ops-mission-control/rotation-check`, because that is an
`always` job — and it is the *only* job that can re-arm a gated instance. An agent told
to "pause the armed crons" would pause exactly the cron that turns the instance back on,
silently ending the team's incident response until a human noticed. The only thing
preventing it was a sentence of SOP prose saying "never pause an always-tier cron".

Prose is not an enforcement mechanism, and neither is the manifest: `permissions.mcpTools`
is **declarative only** — `check_tool_permission` exists but nothing calls it at runtime —
so listing `cron_pause` there never gated anything either. One misfollowed LLM turn was
the whole distance between "armed" and "permanently dead", which is the same
quiet-versus-broken conflation this app refuses everywhere else ("a source that failed is
shown as failed, never as quiet").

So the decision moved into code the model does not mediate:

- `protected_cron_names()` is **derived** from `TIER_CRONS[always]`, not restated — a job
  that moves onto the always tier is protected by that move alone. A second hand-kept list
  could protect the old name and forget the new one.
- `apply_tiers` skips a protected name unconditionally, even if the tier map says to pause
  it. That branch is unreachable through `tier_states` today (`always` is hardcoded `True`)
  and is deliberately still there and still tested: it is the invariant, not a consequence
  of how one caller happens to be written. `tier_states` has already had exactly this class
  of regression once (the `on_shift or unknown` fail-open that defeated strict gating).
- Jobs outside `TIER_CRONS` are never touched, so a user's unrelated paused cron is not
  resumed as a side effect of a shift starting.
- `cron_pause`/`cron_resume` are removed from `app.json`, with a test pinning their absence
  and another pinning the cron prompt to `/rotation/arm` — so a future SOP edit cannot
  quietly hand the capability back.

`GET /rotation` still returns `armed_crons` (flat union across every armed tier — "what is
running now") and `tier_crons` (the per-tier breakdown) for explaining a transition to the
operator. It is now read-only context, not a work list.

**Fail-open:** `ShiftStatus.unknown` (no source, or every source errored) arms the
`on_shift` tier. Wrongly arming costs API polls; wrongly disarming means nobody
notices an outage.

With the default `always-on` rotation source, `on_shift` is permanently armed, so a
solo operator gets continuous coverage rather than a tier that never fires.

## Dispatch engine (`backend/dispatch.py`)

`run_cycle()` is the loop that makes the app function. It is **deterministic
Python called once by the cron**, not an agent turn — the expensive part (an
actual investigation) is reserved for signals that need one, which keeps the
heartbeat's cost flat at a 2-minute cadence.

1. Rotation gate: off-shift returns immediately with `skipped_reason`. This is
   checked here as well as by the cron tier, so a manual trigger cannot dispatch
   off-shift.
2. `poll_all` across configured sources; drop `state != firing`. Three distinct reasons a
   signal is dropped, and they are not interchangeable: it is `ok` (the provider reports
   recovery), it is `suppressed` (a human parked it — **counted** in
   `CycleResult.suppressed` and deliberately never claimed, see contract 1b), or it is
   `unknown` (we could not read its state).
3. Diff against the dispatch index by `Signal.id` (a `stale` incident is
   re-claimable).
4. Claim up to `max_claims_per_cycle` (default 3) — `store.claim` takes an
   exclusive `platform_compat.file_lock` and compare-and-sets, so exactly one
   caller wins and the loser skips. The cap turns an alarm storm into a queue that
   drains over successive cycles instead of spawning 200 sessions.
5. **`attach_ledger_matches`** — match `Signal.fingerprint` against the ledger,
   record the use, and persist `ledger_matches`. This is the step that makes the
   second occurrence of a failure cheaper than the first; without it the ledger is
   decorative. `record_use` returns the UPDATED entry so a brief cannot report
   "used 0×" for a pattern the same incident just used.
5b. **`webhook.ack({claimed ids})`** — the ONE place the push spool shrinks, and only for
   ids that now have an incident on disk. Anything the cap deferred, anything already
   owned, and anything lost to a claim race stays spooled for a later cycle. See § Inbound
   webhook for the data loss that draining on `poll` caused.
6. **`verify_pending_actions`** — for every incident whose post-action recheck has come
   due, re-read whether its signal is still firing, using the poll THIS cycle already
   made (so no extra provider call) and the same `poll_health` map. A source that did not
   answer yields `unknown`, never a success. See contract 3b; a `still_firing` verdict
   charges a `miss_count` to every entry the incident cited, which is the join that makes
   `use_count` mean "worked".
7. Sweep incidents idle past the window back to `stale` for re-pickup.
8. **If nothing changed, `CycleResult.changed` is false and the cron emits
   nothing.** Silence-by-default is a hard requirement, not an optimization — it
   is why the modeled channel stayed readable. `changed` counts a `still_firing`
   verification (the app retracting a claim it made) and deliberately not a `cleared`
   or `unknown` one.

`investigation_brief()` renders the claim's facts (signal, mode, matched patterns,
fast-path flag, authority reminder) deterministically, so an investigating agent
does not spend its first turn re-fetching context Python already has. A match with a
non-zero `miss_count` carries an explicit `WARNING` line naming how many times the fix was
applied while the signal kept firing — stated separately because a ranked list reads as an
endorsement, and an agent told only "used 4×" reads the count as corroboration when part of
it is the record of the fix not holding.

`POST /dispatch` is the endpoint the cron calls; the board's **Check now** button
calls the same cycle so a user who just entered a token can verify it immediately
rather than waiting a heartbeat to discover a typo.

### A fresh install explains itself

`run_cycle` returns early with a `skipped_reason` when **no signal source is
configured**, before polling. `polled == 0` is ambiguous — "nothing is wrong" and
"nothing is watching" are opposite conclusions, and a new user's very first action is
the moment the app most needs to admit it is not set up. The dashboard derived this
itself, but an agent calling `POST /dispatch` on a fresh install previously got a
silent empty result.

`configured_signal_sources()` treats a `configured()` that **raises** as not
configured: an adapter whose own readiness check is broken cannot be trusted to poll,
and counting it as ready converts "nothing is watching" into a source-level error every
cycle — noise the operator cannot act on.

Verified on a genuinely empty data home (not the dev environment): the handover digest
leads with "the board is quiet because nothing is being watched", dispatch is silent
(`changed: False`) but now says why, and a configured install still polls normally.

## Incident status is derived, not stored

`backend/slot_watch.py` reconciles each open incident against its investigation
slot on every `/state` read:

| Slot state | Status | `blocked_reason` |
|---|---|---|
| pending approval, **or** a trailing `permission` message | `needs_human` | `awaiting_approval` |
| waiting for input | `needs_human` | `awaiting_input` |
| running | `investigating` | — |
| idle, turns taken, no diagnosis recorded | `needs_human` | `awaiting_diagnosis` |
| no slot | unchanged | unchanged |

**Derived rather than stored** so it cannot go stale: approving from the embedded
chat clears the block on the next read, with no flag anyone has to remember to
reset. The trailing-`permission`-message check matters because the slot's
`pending_approval` flag LAGS the transcript — relying on the flag alone leaves the
board wrong for that gap.

**Read through the slot's PUBLIC contract.** `routes._slot_state` asks
`_ChatSlot.to_dict()`, which the core keeps correct, rather than deriving
`pending_approval` from `slot._approval_futures` itself. It used to do the latter, and
review flagged it: a private attribute of another module is not a contract, so a core
refactor renaming it would silently turn *"waiting on you"* into *"progressing"* on this
board — the operator stops being told an incident needs them, and nothing anywhere fails
to say so. A serializer fault degrades to the public `pending_approval` attribute rather
than 500ing, because this read paints the whole board; the fallback is deliberately not
the old private access. Pinned by a test that drives the real `_ChatSlot` through
baseline → pending → resolved and asserts our answer equals the core's at each step.

`dispatched → needs_human` is a legal edge because an agent can block on its very
first action (observed live: the opening move was a read-only AWS probe that parked
on an approval). `needs_human → stale` is legal too, so an unanswered incident does
not pin its signal as claimed forever.

The board renders `blocked_reason` INSTEAD of the bare status ("Approve to
continue", "Waiting on you", "Stopped, no diagnosis") because "Needs human" reads
identically whether the agent wants one click or has run out of ideas — and the
operator's next action differs completely.

**Phase 4 closes the loop.** `awaiting_diagnosis` clears only when the
investigation records its finding via `POST /incident/transition` with a
`diagnosis`. The investigate SOP carries that call verbatim, and the dispatch
kickoff names it as mandatory: an agent that writes its analysis in chat and stops
leaves the board reporting a finished investigation as a dead end.

## Embedded incident chat

The board's expanded row mounts `ChatEmbed` against the incident's slot
(`ops-mission-control-<incident_id>` — the SOP and cron prompt both state the key,
since a mismatch shows an empty conversation beside a live investigation). It needs
its own `AppApiProvider`: builtin pages have none, and the provider is
permission-scoped, so `/api/chat*` **and** `/api/approvals*` must both be in
`allowedApiPaths` or the approval buttons 403 with no visible error.

Two core fixes were required to make approvals work from an embed at all, both
upstream of this app:

- `ChatEmbed` never passed `onApprove`, so approval cards rendered with buttons
  that did nothing and an embedded agent stalled forever behind an interactive-
  looking card.
- `CollapsibleToolGroup` rendered its approval buttons only when **collapsed** —
  but a group with a live pending approval auto-expands, so the one turn waiting on
  the user was the one turn they could not answer. Pinned by
  `website/src/test/collapsibleToolGroupApproval.test.tsx`.

Layout: the embed scrolls via `h-full` + an inner `flex-1 overflow-y-auto`, so an
ancestor MUST bound its height (`IncidentChat` owns a fixed-height flex column with
`min-h-0`). Without the bound the transcript grows without limit and pushes the
input row out of reach; without `min-h-0` a flex child's default
`min-height: auto` refuses to shrink below content and silently defeats the
overflow.

## Slack output — the pin board (`backend/slack_out.py`)

Mirrors incidents to a Slack channel as a **board, not a feed**: one message per
incident whose glyph tracks its state (`⏳` dispatched, `🔍` investigating, `🧑`
needs human, `✅` resolved, `🚨` escalated, `💤` stale), with detail in its thread.
This reproduces the half of the modeled workflow that made an ops channel the live
dashboard. Emoji is correct here and only here — the no-emoji rule in
`website/AGENTS.md` governs rendered dashboard UI, and `slack/blocks.py` already
uses them.

### Slack-bound text passes BOTH redaction passes

`slack_out._safe()` is the single chokepoint for everything outbound and applies
`security.redact` **and** `secrets.redact_tokens`. Neither is a superset of the other:
`redact` knows AWS keys and exfiltration URLs, `redact_tokens` knows the PROVIDER-specific
token shapes this app handles.

Measured: a Datadog app-key shape and a PagerDuty `u+` token both pass through `redact`
**completely unchanged** and are masked only by `redact_tokens` — whose own docstring already
listed Slack among its sinks, and Slack was the one sink not wired to it. So a
provider-authored alarm title carrying a key was republished into the channel verbatim. Found
in review. The pre-existing redaction tests used an `AKIA` key, which `redact` *does* catch,
which is exactly why they stayed green.

One function rather than three call sites: three sites each remembering two passes is three
chances to forget the second, which is how this happened. A test asserts the only
`redact(` in the module is the one inside `_safe`, and the token tests assert their own
premise (that core `redact` really does miss these shapes) so they cannot quietly become
vacuous if `redact` later learns them.

**A URL is not exempt, and the first version of that guard missed it.** `signal.url` is
interpolated straight into a Slack block and a signed webhook or console link can carry a
token in its query string. It went unwired when `_safe` was introduced for
title/resource/detail, because the guard test asserted only that no bare `redact(` remained
— not that every *field* went through the chokepoint. Found in review. The test now asserts
on the fields, which is the property that actually matters.

### Text this app SENDS to a provider has the same floor

`routes._safe_outbound()` = `redact_tokens(redact_via_context(...))`, applied to every action
note before it reaches an `ActionSink`. A note becomes an acknowledgement comment, a resolve
reason or a mute note **on someone else's system**, where we cannot unpublish it — and it is
agent- or operator-authored free text, so an agent that pasted a provider token into its
diagnosis published that token into the provider's own comment thread. Found in review; the
Slack sink and the ledger write path already had this floor and this third outbound surface
did not.

Redaction happens **before** the `_MAX_NOTE_LEN` clip: truncating first can sever a token so
the pattern no longer matches, whereas clipping after masking only ever shortens a
placeholder. Pinned by a test that asserts the ordering, not just the presence of the call.

`redact_via_context` rather than `security.redact` directly, for the reason the ledger write
path documents: a loaded companion's declared patterns apply, and an enterprise host that
fails to compose its companion fails CLOSED rather than silently falling back to public
patterns.

### It stores no Slack credential — by design

The app has **no** bot-token field and adds nothing to its keystone secret store.
KiroCrew already holds a Slack token for its own gateway, and the live
`SlackClientOps` is reused. Governance guidance on credential storage puts "prefer
no secret to rotate" ahead of storing a third-party token, and permits the latter
only where no such path exists; here one does, so a second copy would be duplicated
credential material with a second rotation obligation and a second thing to leak,
for zero capability gain. `test_slack_out.py::TestNoTokenOfItsOwn` pins this against
a future "just add a token field" regression.

The consequence is a real dependency rather than a hidden one: with Slack
unconfigured on KiroCrew itself, this channel is unavailable, and `status()`
distinguishes the three cases (off / no channel / no host Slack) because each has a
different fix. The channel ID **is** stored in plain app config — it is not a
credential.

### Explicit client, no global

There is no module-level gateway-state accessor in KiroCrew (state is per
`web.Application`), so the client is threaded in from the route layer:
`routes._slack_client(request)` → `slack_out.client_from_state(...)` →
`publish/post_detail/publish_all(..., client)`, and `dispatch.run_cycle(
slack_client=...)`. `None` is always a quiet no-op, which is what lets every send
be tested without a gateway.

### Invariants

- **Never fatal.** Every send is wrapped; a Slack outage must not fail the dispatch
  cycle or the transition that triggered it. Notifying is not the work. Sends happen
  *after* the claim/transition is durable.
- **Edited in place.** The first post records `slack_thread_ts` on the incident;
  later changes `chat_update` that message. If the update fails (message deleted,
  channel changed) it **reposts** rather than going silent — a duplicate line is
  cosmetic, a missing alarm is not.
- **Redacted.** Titles, resources, and diagnoses pass through `security.redact`
  before leaving. This is a separate egress boundary from `slack/handler.py`: the
  text originates in a third-party alarm payload rather than a model turn, and the
  channel audience is usually wider than the dashboard's. Registered in
  `security_posture._REDACTION_SINKS`.
- **Blocked reason beats bare status** in the summary line, for the same reason the
  board shows it: "Needs human" does not say whether a click or a decision is wanted.

Wired at three call sites: new claims in `dispatch.run_cycle`, manual claims in
`_handle_claim`, and every status change in `_handle_transition` (which also threads
a new `diagnosis`/`resolution` into the thread). The investigate SOP therefore tells
the agent **not** to hand-post to Slack — doing so would duplicate the finding.

## Local notifications (`backend/notify_out.py`)

The second output channel, and the only one that needs **no credential and no inbound
URL**. `app.json` declared the `notification` event permission from the app's first
commit and the app produced nothing, so this was inert machinery: every operator-facing
fact the app computes required an open dashboard tab or a Slack workspace it holds no
token for.

**Three declared channels** (`app.json` → `notifications.channels`; the cap is 8). This
is a manifest contract — `register_builtin_apps` persists `app.json` into the data home
and the bus refuses an id the persisted manifest does not declare:

| id | default priority | fires on |
|---|---|---|
| `waiting-on-you` | `critical` | the transition INTO `needs_human` |
| `source-health` | `default` | the poll where a source flips ok → failing |
| `incident-released` | `passive` (24 h TTL) | each id `sweep_stale` released |

`critical` on `waiting-on-you` is defensible — it is the one state in this app that
blocks an agent turn — but it is **not** `system.approval` and is deliberately NOT in
`notifications.settings.PROTECTED_CHANNELS`: an app must not be able to hand itself a
channel the operator cannot mute.

**In-process, and therefore both HTTP guards replicated.** `POST
/api/notifications/push` is unreachable here, twice over. It authenticates with an app
token whose secret lives at `~/.kiro/crew/apps/<name>/.app_secret`, and
`register_builtin_apps` writes that file only for a manifest declaring
`backend.entryPoint`; this app declares `backend.routes`, so no secret exists (verified
on disk — `dev-fleet`/`file-explorer`/`workflows` have one, this app does not). And even
with a secret, a handler that HTTP-calls its own gateway needs an auth token and can
deadlock the loop — the same reason `routes._slot_state` and
`slack_out.link_thread_to_investigation` read through `DashboardState`.

So `notify_out._push` re-implements what the handler owns, in the handler's order:
enablement (`is_app_enabled`) → the channel is declared in the installed manifest → lazy
one-time `register_channel` → `payload.validate()` **before** the limiter (an invalid
payload delivers nothing and must not drain the budget) → `state.notification_rate_limiter`
→ `bus.push`. The limiter is the **state-owned instance**, not a fresh one, so the
in-process path and any future HTTP push share one 30-per-300 s budget instead of two. A
local-first app must not gain an unthrottled notification path.

**One push per STATE CHANGE, never per tick.** `dispatch.run_cycle` snapshots
`registry.poll_health()` **before** `poll_all` and diffs; a source that was already
failing pushes nothing, because at a 120-second heartbeat an hour of downtime would
otherwise be 30 identical toasts — the unchanged condition `SKILL.md`'s noise discipline
forbids. A source absent from the *before* map counts as "was ok" on purpose: its first
failure is news, and it is usually a provider the operator has just configured.
`routes._handle_transition` captures the pre-transition status for the same reason —
`update_fields` re-enters `transition` with the same status.

**Nothing is pushed on a claim.** A claim is the heartbeat working correctly and already
shows on the board and in Slack; notifying it would make this the heartbeat feed the
design refuses. Recorded in the module docstring and pinned by a test so it does not read
as an omission to a later reader.

**`group_key`** is the incident id (the source id for `source-health`, since consecutive
failures of one source are one condition), so repeats collapse into one feed row instead
of a column of near-identical notes. **`url`** is `/ops-mission-control` — path-only,
which is all `bus._validate_internal_url` accepts, and deliberately not a per-incident
deep link: the page selects an incident from React state and reads no query parameter, so
`?id=` would promise a jump the UI cannot make.

**Default off.** `notify_enabled` absent reads as False, so every existing install stays
silent until an operator flips the toggle. Not a credential, so it lives in plain
`config.json` alongside the Slack channel id.

**Redacted at the producer, both passes**, matching `store.write_log` and
`registry.gather_evidence` rather than `slack_out` (which runs core only). Measured, not
assumed: core `security.redact` leaves `401 from https://api.datadoghq.com?api_key=<hex>`
untouched and `secrets.redact_tokens` catches it. `DashboardState._deliver_note` also
redacts centrally, so this is belt-and-braces — and it is what earns the row in
`security_posture._REDACTION_SINKS`, which is mandatory rather than optional: the posture
drift guard walks every module matching the redactor regex and fails on one that is
neither a registered sink nor allowlisted.

**A discovery fix landed with this.** `apps/discovery._manifest_to_builtin_dict` copied
name/permissions/ui/backend/crons/dependencies/setup/publishProvider and `manifest.extra`
but had **no `notifications` branch** — and because `notifications` is a `_KNOWN_FIELDS`
member it did not survive in `extra` either. Since `register_builtin_apps` persists that
dict as the app's on-disk `app.json`, and `get_app_manifest` reads that file, a builtin's
declared channels were silently dropped for every consumer: `_resolve_app_channels`, `GET
/api/notifications/channels`, the Settings rail, and our own manifest check. Nothing
caught it because no builtin had ever declared a channel. Builtin manifests now carry
`notifications` through.

**No persisted-schema change.** Nothing new is stored on an `Incident` or a
`LedgerEntry`; the only new persisted key is the `notify_enabled` config flag, whose
absence is False.

### Where an operator sees it

`/state` returns `notify` beside `slack` (readiness depends on live gateway state, not
config alone, so it cannot be answered from the unauthenticated config file). The Settings
tab's "Desktop notifications" card owns the app-level on/off and lists the DECLARED
channels with what each fires on.

It deliberately has **no per-channel mute**: KiroCrew renders that centrally (`GET
/api/notifications/channels` → `pages/settings/NotificationsPanel.tsx`, one row per
channel with a mute switch and a priority override, grouped under an app-badged header),
and a second copy would be two controls that can disagree about one stored setting. The
card names that location instead — and lists the declaration precisely because the central
rail cannot: it shows channels that are *registered*, and registration is lazy, so a
freshly installed app appears there only after its first notification fires.

## Shift handover (`backend/handover.py`)

`GET /handover` returns a digest of what an incoming responder needs: the one-line
headline, work **waiting on a person**, work that stopped without recording anything,
recurring patterns ranked by `use_count`, and which sources are **not** configured.
Plus a pre-rendered `text` field, so a Slack paste and the Handover tab cannot word the
same shift differently.

This reproduces the modeled workflow's hand-maintained handover document — one of that
team's most-used artifacts, which cost hours of upkeep and went stale between edits.
Everything *generic* in it was already data this app owns; the ledger ranked by
`use_count` **is** the "recurring issues by frequency" section, because that count is
the only honest frequency signal (it counts real fingerprint matches, not what someone
thought was important).

**Deliberately omitted:** rosters, per-person assignments, ticket ids, runbook links.
Those are the organization-specific half of a real handover doc, and inventing a schema
for a stranger's org would be guessing. The digest is a synthesis of observed behavior,
not a CMDB — and the SOP forbids the agent inventing an owner, because a fabricated
assignment is worse than an absent one.

Invariants:

- **Read-only projection, computed fresh.** Stores nothing and decides nothing; a
  cached handover goes stale between shifts, which is worse than none.
- **Headline ordering is load-bearing:** no coverage → work waiting on you → the
  ordinary case. "Nothing is watching" must outrank everything, because a board with no
  configured source looks calm and reporting "all quiet" would be actively misleading.
- **Unproven patterns are visibly unproven.** `proven` reuses the ledger's own
  `FAST_PATH_*` constants rather than restating "verified/high" — a digest that
  disagreed with the engine about what counts as proven would tell a responder to trust
  the wrong entry.
- **Escalated is read from the index, not the open set.** It is a terminal status, so
  it is correctly absent from `open_incidents` — but "we passed this to another owner"
  is exactly what gets lost at shift change. It is therefore NOT subtracted from the
  `progressing` remainder, which counts open work only (doing so went negative once
  several incidents were escalated).
- **`waiting_on_you` requires a fresh slot reconcile**, so the route reconciles first
  exactly as `/state` does; `blocked_reason` is only true if it has just been derived.

Not a cron. A handover is read by a person at a moment they choose, and a scheduled one
nobody reads is the noise this app exists to avoid — so `sops/handover.md` ships with
`cron: null` and `test_config_routes.py` pins that it still reaches an install.

## Crons (manifest-declared)

**`rotation-check` ships ENABLED; the other three ship paused.** This is a cold-start
requirement, not an inconsistency. `dispatch` is armed by the `on_shift` tier, and the
only thing that arms that tier is the rotation-check cron — and **nothing flips a
manifest `enabled: false`**. Ship rotation-check paused too and a user enables the app,
configures CloudWatch, and it never fires: the store listing's "the on-shift tier arms
and disarms itself" was impossible. Found by asking what a stranger's install actually
does, not by reading code.

Safe to arm because its SOP's **step 0** exits with no output when no provider reports
`configured: true`, so a fresh install pays nothing for a 5-minute poller. Both halves
are pinned (`test_rotation_check_ships_enabled_or_nothing_ever_arms`,
`test_rotation_check_exits_cheaply_when_unconfigured`) plus the Playwright cron check.

Note the registration semantics this depends on: the app bridge writes manifest
`enabled` verbatim to `apps/<name>/app-crons.json` **on install/enable**, and the
CronService then preserves live user intent. So changing a manifest default reaches
existing installs only on a disable/enable cycle — correct (it must not silently
un-pause a cron an operator paused), but it means this fix helps new installs and
re-enables, not a running one.

The four SOPs are declared as crons in `app.json`, so the enable-time bridge
(`bridges._register_crons` → `register_app_crons_with_service`) promotes them into
the running scheduler. **All four ship `enabled: false`**, i.e. registered but
PAUSED: they are visible in the Schedule view and resumable, but cannot fire
before a provider is configured. All are `silent: true` and
`persistent_session: false` (a poller must not accumulate session context).

| Cron | Cadence | Tier |
|---|---|---|
| `dispatch` | 120s | on_shift |
| `reconcile` | 900s | always |
| `rotation-check` | 300s | always |
| `ledger-hygiene` | `17 3 * * *` | primary |

Caveat inherited from the App Kit: disabling the app deletes its crons and
re-enabling re-registers them from the manifest, so a cron the user resumed
returns to paused after a disable→enable cycle.

## On-disk layout

```
<crew_home>/apps/ops-mission-control/data/
├── config.json            # NON-SECRET only (served unauthenticated)
├── incidents/index.json   # dispatch index — {incident_id: Incident}
├── incidents/<id>.md      # postmortem, written when the incident CLOSES (0o600)
└── ledger.jsonl           # append-only LedgerEntry stream
<crew_home>/ops_mission_control_secrets.json   # KEYSTONE (see contract 4)
```

All writes go through `atomic_write`. File locking goes through
`platform_compat` (never raw `fcntl` — Windows support).

### The per-incident postmortem (`store.write_log`)

Written by `store.transition` when the resulting status is in `TERMINAL_STATUSES`,
and only then — an open incident has no artifact, which is a different fact from an
artifact that is blank. `transition` is the only door to a terminal status
(`sweep_stale` writes only `stale`, and `slot_watch.derive_status` never returns a
terminal one), so that single call site covers every close there is.

**It is the only thing this app produces for a reader who does not run KiroCrew** —
attachable to a ticket, pasteable into a review. Its content is sourced from the
persisted `Incident` (`diagnosis`, `resolution`), never from the closing call's
kwargs, so an unrelated later field update cannot blank a finished record. The
`Next steps` section renders `_none_` on purpose: no `Incident` field carries one
(`proposed_action` is declared and never assigned), and a postmortem that invents its
own follow-ups is worse than one that admits it has none.

Failing to write it can never fail the close. The index write is already durable by
that point, so an `OSError` is logged and swallowed — the same rule the Slack mirror
in `_handle_transition` already follows: a record of a state change must not be able
to fail the state change.

It also carries a **verification** line when an action was executed (contract 3b), because
"Actions taken: silenced the alarm" is the sentence in this file most likely to be believed
as an *outcome* by a colleague with no access to the board. It renders nothing at all when
no action was taken.

Readable over HTTP through `GET /incident`, which returns `log` (the text) and
`log_path` (where the file is, `""` when there is none). There is deliberately **no
download route**: a non-JSON response would be a second egress boundary needing its
own redaction pass and its own `security_posture` row, and the JSON field already
makes the artifact readable.

**It never git-syncs.** `ledger_sync` tracks exactly `.gitignore`, `ledger.jsonl` and
`rotation.yaml` — the shared *lessons* and the schedule, not one team's raw incident
narratives. So the artifact is local-only, and a postmortem reaches a colleague
because a human sent it, not because a repo replicated it.

`prune_closed` bounds the dispatch INDEX and deliberately leaves these files alone.
That means a flapping alarm accumulates one file per flap, which is a real (small,
bounded, owner-only) disk cost accepted knowingly: pruning an index row must not
destroy the written record, least of all when a long history is what someone is
looking through.

### Windows compatibility (`TestCrossPlatform`)

This app is portable, and the three places that could break it are pinned by tests rather
than left to review:

- **`preexec_fn` must come from `resource_limit_preexec()`.** Both external-binary spawns
  (`git` for ledger sync, `gh` for the rotation login) pass it. The shim returns `None`
  off POSIX, which is what makes them portable — `preexec_fn` is unsupported on Windows
  and passing *any* callable, even a no-op, raises `ValueError`. A hand-rolled
  `preexec_fn=lambda: ...` would work locally and fail on every Windows spawn; the test
  asserts the shim appears on each `preexec_fn=` line (verified by temporarily swapping in
  a raw lambda and watching it fail).
- **No raw POSIX process calls** (`os.killpg`, `os.getpgid`, `os.getuid`, `fcntl.`,
  `signal.SIGKILL`), no `/bin/sh`, no `shell=True`, no hardcoded `/tmp`.
- **Timezone lookup degrades to UTC.** `rotation.yaml` may name an IANA zone, and Windows
  ships no system tz database, so `ZoneInfo(...)` can raise. `tzdata` is a declared
  Windows dependency, but an install missing it must still resolve a rotation rather than
  crash the 5-minute cron. Verified by making the `zoneinfo` **import itself** fail — the
  real failure shape — and asserting the shift still resolves definitively
  (`on_shift=True, unknown=False`, correct `who`), just in UTC.

Asserted from source because CI here is POSIX: the goal is to catch a raw POSIX call at
review time, not to simulate the platform.

## Files

- `src/kiro_crew/apps/builtins/ops_mission_control/app.json` — manifest
- `.../__init__.py` — **re-exports `register_routes`** (the startup loop checks the
  PACKAGE, not `backend.routes`; without it routes silently never register)
- `.../backend/models.py` — `Signal`, `Incident`, `LedgerEntry`, transition grammar,
  fingerprinting, `effective_mode`
- `.../backend/store.py` — dispatch index, atomic claim, transitions, stale sweep, the
  closing postmortem (redacted, incl. its verification line)
- `.../backend/ledger.py` — append-only ledger, matching, the four-condition fast-path bar,
  `record_miss`, hygiene (decay + evidence-based demotion + prune)
- `.../backend/secrets.py` — keystone token store, `SecretBackend` seam, redaction
- `.../backend/registry.py` — ADD-only registry, fan-out
- `.../backend/rotation.py` — autonomy gate, tier arming
- `.../backend/dispatch.py` — **the cycle**: poll → claim → ledger-match → sweep,
  plus `investigation_brief`
- `.../backend/notify_out.py` — the local notification bus as an output channel: three
  declared channels, the replicated manifest + rate-limit guards, edge-triggered pushes
- `.../backend/routes.py` — HTTP surface (`register_routes(app)`, full paths)
- `.../backend/providers/` — the four Protocols + public adapters; the package
  `__init__` also owns config read/merge (`merge_provider_config`, `set_top_level`)
- `src/kiro_crew/builtin_skills/ops-mission-control/` — the agent skill AND the
  five SOPs (`sops/dispatch|investigate|reconcile|rotation-check|ledger-hygiene.md`).
  **They live here, not under the app**, because `register_builtin_apps` copies only
  `app.json` + `installed.json` into the data home for a builtin — so a
  `manifest.skills` entry pointing at an app-local dir silently registers nothing
  (verified: `code_review_sage` has the same latent gap). `builtin_skills/**/*` is
  packaged (`setup.cfg`) and `_ensure_builtin_skills` copytrees it into every
  install, which is the only path that reaches end users. The cron prompts
  reference `~/.kiro/crew/skills/ops-mission-control/sops/<name>.md` accordingly.

  **Every SOP carries the auth recipe, not just SKILL.md.** The SKILL and all six SOPs
  told the agent to call HTTP endpoints and never said how to authenticate. An
  unattended `rotation-check` run therefore improvised: it hardcoded a port belonging to
  a different gateway, collected `{"error": "Token required"}` **65 times**, spent **41
  tool calls** hunting for a token the cron runner *deliberately destroys* before the
  first tool call, and hit the 1800s cron timeout without ever reaching the API. That
  reads to an operator as "the app is broken" when the fix was six lines of docs.

  The recipe derives base URL and token from one `kirocrew token` call and passes
  `?token=`. It is repeated in each SOP because a cron agent may read **only** its own
  SOP. Three tests guard it: every SOP mentions `kirocrew token` and `?token=`; no auth
  code block contains a literal `host:port` (a hardcoded port was the original failure —
  and this test caught one I had just written myself); and the block passes `bash -n`,
  because `${URL%%\?*}` is easy to mangle in markdown and an unparseable snippet sends
  the agent straight back to improvising.

  **The SOP→route contract scanner had silently narrowed to 4 of 10 endpoints.** It
  filtered lines on a literal `GATEWAY/api/apps/...` prefix, so rewriting the SOPs to
  derive `$BASE` left six routes unguarded while the test stayed green — a renamed route
  would have 404'd mid-investigation with nothing failing at build time, which is the
  exact failure the test exists to prevent. The filter now matches the *path* and covers
  **11 (method, path) pairs**, and a companion test pins a floor on the scanner's own
  yield. A test whose input filter can quietly shrink is worse than no test, because the
  green tick still claims the coverage.

  **A same-named app must never touch this directory.** Because the skill and the
  app share the name `ops-mission-control`, the packaged skill lands at
  `skills/ops-mission-control/` — the exact path the App Kit's skill bridge treats
  as an app-owned link farm. Two bridge bugs each independently emptied it (silently
  — a missing SOP file errors nowhere), so every cron prompt pointed at SOPs that no
  longer existed: (1) `_register_skills` `mkdir`-ed the namespaced dir before
  checking whether the manifest declared any skills, and (2) `_deregister_skills`
  (called for any skill-less manifest, to clean stale symlinks) `rmtree`-d the whole
  directory. Both are fixed to act only on what registration created — no skills →
  no directory; deregister removes symlinks only and never a real file. Pinned by
  `test_app_bridges.py::{test_no_skills_creates_no_directory,
  test_deregister_preserves_a_same_named_packaged_skill}`. The manifest deliberately
  declares **no** `skills` key.
- `website/src/apps/ops-mission-control/` — board page, Settings panel, API client
- Wiring: `apps/builtins/__init__.py::BUILTIN_NAMES`,
  `website/src/apps/builtinRegistry.ts`
- `docs/task-specs/2026/07/ops-mission-control/planning/` — development journals (research
  sweeps, feature ledger, parity report). These lived in the package tree
  (`apps/builtins/ops_mission_control/planning/`) and were pruned from the sdist by a
  `MANIFEST.in` special case, because `recursive-include src/kiro_crew/apps *.md` — which
  exists to ship ATTRIBUTION/README credit — swept them in as a side effect (11 files, ~5.2k
  lines). Design review pointed out the better fix: `AGENTS.md` already names
  `docs/task-specs/YYYY/MM/<task-id>/` as the home for archived task material, and this
  task's other specs were already there. Moving them removes the special case entirely
  rather than papering over it. Measured after the move: **0 planning files in the sdist**
  with no prune rule at all (`docs/` is not included), and the app's own `app.json`,
  `README.md` and backend still ship.
  In-package `tests/` is deliberately NOT pruned or moved — `code_review_sage` and
  `issue_radar` ship theirs, so that is the convention for a builtin app.

## Known debt

**i18n — CLOSED.** All five components plus `api.ts` now route through `i18nT` (~310
keys), and the keys are mirrored into all nine non-English catalogs so
`catalogParity.test.ts` passes. The catalogs carry the **English fallback** for those keys
rather than real translations: that is the interim state the `i18n-translate.mjs` pipeline
is built to replace, and parity checks key sets, placeholders and non-emptiness rather than
translation quality (only `destructiveConfirm.test.ts`'s three SchedulePage keys must
genuinely differ). Producing real translations for 310 keys × 9 languages remains open.
Do NOT hand-edit `en.json` to add keys — it is generated by `scripts/i18n-codemod.mjs`.

**Label tables hold catalog KEYS, not English.** Six `Record<…, string>` tables
(`STATUS_LABEL_KEY`, `MODE_HELP_KEY`, `CHANNEL_WHEN_KEY`, `BLOCKED_LABEL_KEY`, and the two
`SegmentedControl` segment lists) originally held English literals. `eslint-plugin-i18next`
exempts anything inside an ALL-CAPS module constant by default, so those 26 strings were
invisible to the i18n gate and would have shipped untranslated in all nine other languages
with nothing to catch it — the exact hole `check-i18n-strings.mjs`'s strict wrapper exists to
measure. Converted to the `FILTER_LABEL_KEY` pattern from `pages/ChatSidebar.tsx`. Three
non-obvious constraints, each learned from a gate failure:

- The map needs `as const` and **no** `Record<K, string>` annotation — the annotation widens
  every value to `string`, and `check-i18n-keys` can then no longer prove the keys exist.
- Keys must be indexed **directly** inside `i18nT(...)`. A local (`const key = MAP[x]; i18nT(key)`)
  or a closure parameter (`MAP.map(s => i18nT(s.labelKey))`) is unresolvable, and an
  unresolvable site is exempt from *every* downstream check — so the two segment lists are
  written as inline literal keys instead of mapped tables.
- The resolver matches by shape within one file and does **not** follow imports, so
  `BLOCKED_LABEL_KEY` is consumed through `blockedLabel()` / `isKnownBlockedReason()` exported
  beside it rather than indexed from `OpsMissionControlPage.tsx`.

Four strings deliberately remain in ALL-CAPS constants and account for a +6 bump of
`untranslated-strict-baseline.json` (1118 → 1124): three API-path allow-lists in
`IncidentChat.tsx` and `BACKOFF_PREFIX` in `api.ts`, which is matched against backend error
text. None is user-visible; renaming them was measured and only moves the finding into the
per-file ceiling instead. The shared `untranslated-baseline.json` is deliberately NOT
re-snapshotted (`--update` rewrites 412 lines of it and causes the cross-branch conflict its
own comment warns about).

Converting these also revealed a real copy defect the gate names precisely: three
`channel_when_*` strings were sentence fragments starting mid-sentence ("the moment an
incident starts waiting…") that render standalone in a `<dd>`. They are now whole sentences.

**`jsx-a11y/label-has-for`.** `SettingsPanel.tsx` carries 9 warnings of this rule (one per
labelled field, including the act-rule pattern input). The labels are correct — the input is
both nested AND `htmlFor`/`id`-bound, which is what the rule asks for — but it cannot see
through the shared `Input` wrapper component. Other files in the repo, including another
builtin app page, carry the same warning: it is the accepted baseline, not a regression.
These are warnings, not errors, and `eslint` reports 0 errors for this file.

## Amazon-internal companion

Internal ticketing / oncall / pipeline adapters live in a **separate companion
package**, developed out of tree, and reach the core only through the ADD-only
registry. This repo contains no reference to it beyond the neutral extension
point; `scripts/scrub-lint.sh` gates the public tree.

### The discovery seam (`backend/companion.py`)

The ADD-only rule was enforced and tested from the start, but `get_registry()`
installed only public adapters and nothing ever looked for a companion — the seam
was **a door with no handle**: an out-of-tree package could implement every Protocol
correctly and still never be reached. `companion.py` is the handle.

**Entry points, not a config path.** A filesystem path to import would be a new,
unaudited code-loading channel in an app whose security story is that the agent
cannot reach its own configuration. Contribution therefore requires *installing a
package* — outside the agent's reach and visible to `pip list`. Mirrors
`platform/discovery.py` including its `entry_points()` API split (the `group=`
keyword is 3.10+; 3.9 returns a dict), because a companion silently invisible on the
oldest supported interpreter is the worst failure mode — everything appears to work.

Group is `kirocrew.ops_providers`, deliberately **distinct** from
`platform.discovery.PLUGIN_GROUP`: contributing an ops adapter must not require or
imply authority over the platform edition seam.

```toml
[project.entry-points."kirocrew.ops_providers"]
my-company = "my_pkg.ops:register_adapters"
```

```python
def register_adapters(registry) -> None:
    registry.register_signal_source(MyTicketSource())
```

**Admission is reused, never reinvented.** Importing a separately-installed
package's code into the gateway is a supply-chain decision, and governance guidance
on third-party packages requires 3P code to arrive through a reviewed channel. Every
candidate runs through the SAME fleet `AdmissionPolicy` that gates platform plugins,
evaluated **before `ep.load()`** so rejected code never executes, and each decision
(allow and deny) lands on the SEL trail. A companion is not more trusted for being
ours — and a fleet that banned a package must not be able to have that bypassed by
installing it as an ops adapter instead.

**Fail-OPEN here, unlike platform discovery** — a deliberate product divergence, not
an oversight. `platform/discovery.py` fails closed because a missing companion there
could drop a *security overlay*; running without it is less safe. A companion here
only ADDS signal sources, so a missing one means fewer alarms are watched — visible
on the Signals tab — and aborting boot over it would take down a working public
install (chat, crons, every other app) to punish an optional integration. Rejected /
unimportable / throwing companions are logged, audited, and skipped. One bad
companion does not block a good one.

The single fail-CLOSED path inside the module is the admission check itself: if the
evaluator raises, the candidate is **denied**. "The gate broke" must never read as
"the gate said yes".

**Order is load-bearing.** `get_registry()` installs public adapters *first*, then
companions. Since ADD-only means the incumbent wins, that ordering is what makes a
core id un-shadowable — pinned directly by
`test_companion.py::test_public_adapters_are_installed_before_companions`.

`/state` reports `companions` (name + target, read WITHOUT loading plugin code) and
Settings renders it under Providers only when one is installed. This exists because
"no companion installed" and "companion installed but rejected at admission" look
identical in the provider list and need completely different fixes.

### What remains out of scope here

The **team mesh** (multiple Ops agents sharing work with rotating responsibility) is
NOT unblocked by this seam and must not be built on it. The claim index is
per-instance (`incidents/index.json` + a local file lock), which stops one instance
double-claiming but does **not** stop two instances claiming the same signal. The
ledger is append-only and content-addressed so it merges cleanly; the dispatch index
does not. A mesh needs cross-instance claim arbitration designed first — that is a
new contract, not a new adapter.

The one allowlist entry this app needs is the **public** AWS console host
(`<region>.console.aws.amazon.com`) in the CloudWatch adapter's deep link, matched
only because `INTERNAL_PATTERN` carries a broad `amazon\.com` alternative. It is
line-anchored so a genuinely internal reference in that file is still caught.

## Tests

`src/kiro_crew/apps/builtins/ops_mission_control/tests/` — 647 tests:

- `test_models.py` — fingerprint stability, normalization fallbacks, transition
  grammar, mode algebra
- `test_security.py` — keystone floor incl. bash forms, redaction, write-only
  secret store, owner-only mode
- `test_store_and_gate.py` — claim atomicity, illegal transitions, stale sweep,
  autonomy gate incl. blanket-rule refusal, ledger dedupe/decay
- `test_providers.py` — ADD-only registry, fan-out resilience, central redaction,
  adapters unconfigured-not-raising, webhook fail-closed
- `test_routes.py` — namespace containment, every-route-gated, secrets never echoed
- `test_dispatch.py` — cycle silence (an unchanged firing signal must not
  re-announce), claim cap under a 50-alarm storm, ledger matching + fast path +
  post-increment use count, recurrence-matches-ancestor, rotation gate, one broken
  provider not fatal, and **post-action verification** (contract 3b): a still-firing
  recheck charges a miss, a cleared one does not, and a FAILED poll charges nothing and
  reaches no verdict. That last one is the important guard — the property is invisible in
  the code, so a later "simplify the health check" would look harmless and would start
  reporting every unreachable source as a confirmed fix.
- `test_store_and_gate.py::TestLedger` also pins the **track record** (contract 2b): the
  use floor, one-miss re-lock, and the three laundering doors — a re-POST, a simulated real
  `git merge` producing duplicate ids, and hygiene's own dedupe — each of which must take
  the MAX rather than the incoming value.
- `test_config_routes.py` — **secret field refused on the config route**, unknown
  field/provider refused, merge preserves untouched fields, invalid mode refused,
  and manifest-cron assertions (all four present, all paused, all silent and
  stateless, exactly one schedule each)

Frontend: `website/src/test/opsMissionControl.test.ts` (route registration, panel-parity
assertions read from the .tsx source, and the pure helpers `describeSourceHealth` /
`describeVerification` / `entryIsProven` exercised directly).
