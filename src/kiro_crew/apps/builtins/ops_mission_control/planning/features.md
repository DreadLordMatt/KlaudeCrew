# Ops Mission Control — Feature Backlog

Working doc. Durable behavior lives in
`docs/system-specs/modules/ops-mission-control.md`; design rationale in
`docs/task-specs/2026/07/ops-mission-control/`.

---

## Shipped state (2026-07-31)

Branch **`feat/ops-mission-control`**, off `origin/main` (928b63b0), pushed:

- `d39ff34f` — `feat(ops)`: the app + the three core changes it requires (App Kit
  skill-bridge fixes, the keystone secret-floor entry, the E2E stale-bundle guard).
  77 files.
- `52ff27fe` — `fix(chat)`: an embedded agent's approval request can actually be
  answered. Owner-requested behavior; it was silently non-functional because
  `ChatEmbed` passed no `onApprove` and `CollapsibleToolGroup` hid the buttons on
  expand — the state a pending approval auto-enters.

Deliberately NOT on this branch: the `auto_improvement` effort's work, which shares
the KiroCrew checkout. That tree is untouched on `feat/auto-improvement-app`; the two
chat files above are also modified there, so whoever lands it will see an overlap.

No PR opened — owner's call.

## Requested (owner's queue)

- [x] **Move Signals to its own tab** — DONE. `Board · Signals · Settings`. The
      Signals tab shows per-source health, **what the last poll actually returned
      (including the error text)**, and firing signals not yet claimed with a Claim
      action. Polling is on demand, not on a timer, because each poll hits a paid
      provider API — the dispatch cron is what polls continuously. The Board got its
      full width back.
- [x] **Slack as an output channel** — DONE. The pin board ships: one message per
      incident, glyph tracking state (⏳ dispatched / 🔍 investigating / 🧑 needs
      human / ✅ resolved / 🚨 escalated / 💤 stale), **edited in place** so the
      channel is a board and not a feed, with diagnosis in the thread. Wired at all
      three state-change points (heartbeat claim, manual claim, transition).
      **Deliberately NO bot token of its own** — it reuses the Slack client KiroCrew
      already has, so there is no second credential to enter, store, rotate or leak.
      That was the one real design decision here; see the module spec for the
      reasoning and `TestNoTokenOfItsOwn` for the regression guard. Outbound text is
      redacted (a separate egress boundary from `slack/handler.py` — provider payload,
      wider audience) and registered in `security_posture`.
- [ ] **AWS/Amazon-internal plugin** (separate package, built on the internal Ops
      app) — the private companion to this public core:
  - [x] **The discovery seam it plugs into** — DONE, and this was the real blocker
        in this repo. The ADD-only rule was enforced and tested, but nothing ever
        *looked* for a companion: `get_registry()` installed only public adapters, so
        the seam was a door with no handle. `backend/companion.py` adds
        `kirocrew.ops_providers` entry-point discovery, reusing the existing fleet
        `AdmissionPolicy` (evaluated before `ep.load()`, SEL-audited) rather than
        inventing a second supply-chain gate. Proven end-to-end with a real installed
        throwaway package: admitted under the open policy, refused under an enforcing
        one, core adapters intact either way. **The internal adapters themselves are
        out-of-tree by design and cannot be built here.**
  - [ ] New internal signal sources: ticket resolver-group monitoring (all, or per
        alias) and intake-folder monitoring (all, or per alias).
        *Blocked here by design* — belongs in the private package; the seam above is
        what it now attaches to.
  - [ ] **Team mesh** — a mesh of Ops agents talking to each other (SSH keys or
        another secure transport; all instances sit on the internal network). Work
        items get handled as a team with rotating Ops responsibility, plus a group
        chat.
        *Design note:* this is the one item that needs a new contract rather than a
        new adapter. The claim index is currently per-instance
        (`incidents/index.json` + a local file lock), which is what stops one
        instance double-claiming — it does NOT stop two instances claiming the same
        signal. A mesh needs claim arbitration across instances (the append-only,
        content-addressed ledger already merges cleanly; the dispatch index does
        not). Worth settling before building the transport.

## Requested (owner's queue) — added from source-material review

- [x] **Shift handover digest** — DONE. Not in the original queue; found by re-reading
      the source material after the engineering backlog emptied. The modeled workflow
      kept a hand-maintained handover doc (one of its most-used artifacts, hours of
      human upkeep, stale between edits) and the public app had **no** answer for "I'm
      taking over the rotation, what do I need to know?". Own tab + `GET /handover` +
      SOP. Built entirely from data the app already owned — the ledger ranked by
      `use_count` IS the "recurring issues by frequency" section. Rosters/tickets/
      runbooks deliberately omitted as organization-specific.

## Shared memory over git (owner-requested 2026-07-31)

Owner's idea: model shared memory + oncall on a git repo (as `InscopeTeamContext` does),
using GitHub logins for identity — no oncall service, no server. Then: *"make sure our
vectorized database → git → back to database adaptor is well written and tested and will
allow large amount of memories to be stored + vectorization."*

**Two design decisions, both confirmed with the owner:**

1. **Text in git; re-embed locally.** The store keeps three artifacts — SQLite (text +
   embedding BLOBs), a FAISS index, and 1024-dim float32 vectors. Only text is
   git-friendly, and the embedding model is **sha256-pinned**, so vectors are *derivable*:

   | memories | text in git | vectors if committed |
   |---|---|---|
   | 10,000 | 3.8 MB | 39 MB |
   | 100,000 | **38 MB** | **391 MB** |

   Vectors would also merge-conflict unresolvably as binary and rewrite on every push.
2. **Extend the ledger; no second store.** `ledger.jsonl` is already append-only with
   content-addressed ids and verified merge reconciliation.

**Built and tested:**

- [x] **Git transport** — `backend/ledger_sync.py`. init/remote/pull/merge/push,
      sandbox-routed per `test_spawn_audit`, 30s timeout so a hung fetch cannot stall the
      heartbeat, conflict detection + `resolve_conflict`. Tracks **only** `ledger.jsonl`
      via a generated `.gitignore` — the dispatch index must never be pushed, because it
      is last-writer-wins and syncing it would let two instances each believe they own an
      incident.
- [x] **Ledger → vector index adaptor** — `backend/ledger_index.py` + 18 tests. Two
      incrementality guards in cost order: a local id cursor (set difference, no DB round
      trip) plus the store's exact-text check as the backstop for a lost cursor. Embedding
      is DEFERRED — one `backfill_missing_embeddings()` sweep per batch, because inline
      embedding is ~0.4s per 2000 chars and would make a 10k import an hour-long stall.
      Bounded at 500/call so a 100k first import drains over cycles.
- [x] **Scale test.** 2000 entries drained in bounded batches; a full re-import costs
      **zero** embeddings; one sweep per batch, never per row; `preserve_existing`
      asserted on every write so an import can never tombstone a teammate's row; the
      cursor is asserted proportional to entry COUNT (ids only, never texts).

**Remaining:**

- [x] **Wire semantic recall in.** DONE 2026-07-31. `attach_similar_lessons()` +
      `_attach_similar_safely()` run on a worker thread from `run_cycle` after evidence
      gathering; hits land in a NEW `ClaimedIncident.similar` field, never in `matches`.
      The separation is the load-bearing part: merging them would let a near-miss inherit
      "used 4×, verified" authority it never earned, and would make `record_use` inflate
      the one number that tells a responder how proven a fix is. 8 tests
      (`TestSemanticRecallWiring`) pin: capped at limit, a fingerprint match is never
      repeated as similar, `use_count` unchanged after recall, no store / broken store are
      quiet no-ops, and the brief frames hits as *leads* ("fingerprints do NOT match …
      never as a fix to apply") rather than a ranked list inviting the top hit.
      **26/26 pass.**
- [x] **`RotationSource` from a committed schedule file** + GitHub-login identity.
      DONE 2026-07-31. `providers/schedule_file.py`: `rotation.yaml` in the synced ledger
      repo, date ranges → GitHub logins, identity from config or the local `gh` (cached
      per gateway lifetime, misses included, so a 5-minute tick never re-spawns `gh`).
      `ledger_sync` now un-ignores `rotation.yaml` — a schedule that never syncs looks
      configured while disagreeing with the team. 30 tests; **367/367 pass** in both
      repos, flake8 + mypy + black clean, 0 spawn-audit hits.

      **Found and fixed a bug that would have swallowed this whole feature:**
      `AlwaysOnRotationSource` is always configured and always on-shift, and
      `resolve_shift` returns the first on-shift answer — so a real rotation saying
      "someone else is on call" was discarded, and the `on_shift` tier armed permanently
      for everyone. Exactly the failure a rotation exists to prevent. Fallbacks now
      declare `is_fallback` and are consulted only when no real source can answer.
      Verified against the pre-fix code before changing it (it resolved `on_shift=True`),
      and end-to-end afterward: a schedule naming someone else yields
      `on_shift=False, who='someone-else'` and `tier_states.on_shift == False`.

      *Two tests initially failed for reasons in the TEST, not the code, and both are
      worth remembering:* a broad `subprocess.run` patch counts `sandboxed_spawn_argv`'s
      own `ssh -V` probe as ours, and matching on `argv[0]` misses `gh` entirely because
      the sandbox PREPENDS a wrapper — so the real `gh` ran and returned the developer's
      actual login. Match the whole argv.

*Ledger write cost, measured:* `ledger.upsert` re-reads the whole file to merge by id, so
bulk-seeding N entries through it is O(N²) — 21s for 2000 in a test. Fine in production (a
route calls it once per recorded lesson, never in a loop), but worth knowing before anyone
builds a bulk importer on it.

*Blocked on environment, not design:* the embedding model is not downloaded in this
sandbox, so same-text→same-vector could not be empirically confirmed. The sha256 pin makes
derivability sound, but the scale test must verify it where the model is present.

## Shared-memory loop: WIRED and verified against real git (2026-07-31)

- [x] **The loop has a caller now.** `ledger_sync` and `ledger_index.import_pending` were
      both built, both tested, and both wired to **nothing**: sync had no caller anywhere,
      and semantic recall queried an index nothing ever populated — so on a real install
      recall returned zero hits forever while every unit test passed. Both now run from
      the daily `ledger-hygiene` pass, in the order **pull → hygiene → index → push**
      (each step's ordering justified in the spec; pinned by a test that fails when two
      stages are swapped).

- [x] **Four fatal bugs found by a REAL two-instance roundtrip** against a bare remote —
      every one invisible to the mocked-git tests:
      1. First push in a fresh process always failed (transient sandbox-probe error that
         says "retry" and was never caught). Now retried once.
      2. An instance with a local ledger could **never** pull — `git merge` refuses to
         overwrite an untracked file. Any install that recorded one lesson before its
         first pull was permanently cut off.
      3. The **second teammate to join could never merge** — separate `git init` per
         instance means unrelated histories, which git refuses. That is the ordinary
         multi-instance case, not an edge case.
      4. `rotation.yaml` would never have been committed — push staged `ledger.jsonl`
         alone, so the schedule I had just un-ignored *specifically* to sync would have
         reached nobody.
      Plus: a clean tree isn't proof everything is shared — a locally-committed but
      unpushed lesson was stranded forever by an early "nothing to push".

      Verified live: A → push → B pulls → B adds → A pulls both. **Concurrent case:** both
      write blind, the stale push is correctly rejected, the pull reconciles to 3 entries
      preserving both sides, both instances converge identically. No entry lost.

      *Lesson worth keeping: two modules can each be correct and collectively dead. The
      unit tests were green the whole time because they mocked the one thing the feature
      actually is — git.*

- [x] **My own test polluted the suite.** The git tests evict `sys.modules` to simulate two
      processes; without restoring it, four unrelated `test_routes` tests failed (they
      patched a stale module object while the handler resolved a fresh one) — and passed
      when run alone. Now snapshot/restore in `tearDown`; full suite run 3× consecutively
      to confirm. A test that breaks other tests is a bug in the test.

## Testing tasks (owner-requested)

- [x] **Verify Datadog end to end against the dev account.** DONE 2026-07-31.
      Owner supplied `DATADOG_APP_KEY`; the monitors API then returned **HTTP 200**. The
      org had **zero monitors**, so `poll()` correctly returned 0 signals — credentials
      alone could not prove the signal path. With the owner's approval I created ONE
      clearly-named monitor (`kirocrew-omc-integration-test`, id 310040989), pointed it
      at a custom metric `kirocrew.omc.test_gauge > 10`, submitted a value of 42, waited
      for Datadog to evaluate it into `Alert`, verified the whole path, then **deleted
      it** — org confirmed back to 0 monitors.

      Verified: `Alert` → `severity: critical`, `state: firing`, working deep link
      (`app.datadoghq.com/monitors/310040989`), tags and `dd_monitor_id` preserved in
      labels, fingerprint computed. Full cycle claimed **INV-1** at `observe`, and a
      second cycle claimed nothing (dedupe holds).

      **No agent install was needed** — the adapter reads the monitors API, so the
      original ask ("install a Datadog agent") was not the missing piece; a monitor was.
      `No Data` is correctly NOT treated as firing (only `Alert`/`Warn`), which is why
      the monitor needed real metric data rather than just existing.

      *Not a defect:* `datadog-evidence` returned 0 items. It queries
      `/monitor/{id}/downtimes`, and the live API confirms that monitor had none — so
      empty is the right answer, checked rather than assumed.

## v1 success criteria (from ideation.md §8) — scored 2026-07-31

Scored against what is verified, not what is built:

| # | Criterion | Status |
|---|---|---|
| 1 | AWS profile + Slack → working board in <10 min, no write scope | **Met** (the <10-min timing is untested) |
| 2 | Adding PagerDuty/Datadog is one token in Settings, no restart | **Met for Datadog** — verified live. PagerDuty needs a tenant |
| 3 | Unwatched signal claimed, investigated, posted to Slack with a diagnosis | **Met (unattended run now OBSERVED)** — 33 real cron workspaces on the live env-2 gateway; 14 captured dispatch cycles each polled 2 real CloudWatch signals, claimed nothing correctly, and stayed silent. Slack itself is still off on this instance, so the Slack leg is verified by test, not live |
| 4 | Second occurrence resolves measurably faster from the ledger | **Met, after fixing a blocker that made it impossible** — a resolved alarm's signal was owned forever, so a second occurrence could never be claimed. Now: 1st = 0 matches / `fast_path=False` / "new to the ledger"; 2nd = fresh incident, 1 match, `fast_path=True`, brief carries the verified fix |
| 5 | No credential or raw payload in a transcript, prompt, or Slack message | **Met** — single redaction chokepoint, observed live (`receiptHandle` → `[REDACTED]`) |
| 6 | `scrub-lint.sh` passes, no internal marker | **Met**, both repos |
| 7 | Full backend gate green | **Met** — 382 app tests, mypy clean on 643 files |

**7 of 7 met** (2026-07-31). Closing #3 and #4 required reading the live gateway's own
audit trail rather than writing more code — and doing so exposed three real defects that
no test could have found, including one that made the app's central premise unreachable
in production. See "Unattended-run findings" below.

## Unattended-run findings (2026-07-31)

Closing v1 criteria #3 and #4 meant *observing* the live env-2 gateway rather than writing
more code. The audit trail held 33 real cron workspaces. Reading them found three defects
no unit test could have caught — and every one had been invisible for days.

- [x] **#3 is met: the unattended run is observed.** 14 captured dispatch cycles, each
      polling 2 real CloudWatch signals, correctly claiming nothing (both already claimed),
      and staying **silent**. Silence-by-default holds under real repetition, which is the
      property that decides whether an ops channel stays readable. (Slack is off on this
      instance, so that leg remains test-verified, not live.)

- [x] **The SOPs never said how to authenticate — and it cost a whole cron run.** All six
      SOPs plus SKILL.md instruct the agent to call HTTP endpoints; none mentioned tokens.
      A `rotation-check` run improvised: hardcoded a port belonging to a *different*
      gateway, collected `{"error": "Token required"}` **65 times**, spent **41 tool calls**
      hunting for a token the cron runner deliberately destroys before the first tool call,
      and hit the **1800s timeout** without ever reaching the API. To an operator that reads
      as "the app is broken"; the fix was six lines of documentation.
      Now in SKILL.md **and every SOP** (a cron agent may read only its own). Three tests
      guard it, and the "no hardcoded port" one immediately caught a `localhost:6777` I had
      just written into the recipe myself. The snippet is verified by extracting it from the
      SOP and **running it** — HTTP 200 — rather than by eye.

- [x] **#4 was impossible, not merely unmeasured — a resolved alarm was never claimed
      again.** `claim` treated any existing incident as "accounted for", including a closed
      one, and `signal.id` is stable for the alarm's lifetime. So the app **permanently
      stopped responding to any failure it had already handled once**: proven by resolving
      on day 1 and watching days 2, 3 and 30 all return `None`.

      This is the most serious bug found in the project. It made the app's *central
      premise* unreachable in production — the compounding-memory fast path can only pay
      off on a second occurrence, and a second occurrence could never be claimed, so the
      feature everything else is built around could never fire outside a test. The
      transition grammar already said the right thing ("a resolved incident that 'comes
      back' is a fresh firing with its own timeline"); `claim` just never honored it.

      Fixed with a `TERMINAL_STATUSES` set **derived** from the grammar (no second list to
      forget). A recurrence opens a NEW incident — the first keeps its diagnosis,
      resolution and thread — while an OPEN incident, `needs_human` included, still blocks
      a duplicate claim. Measured after: 1st → 0 matches, `fast_path=False`, "new to the
      ledger"; 2nd → fresh incident, 1 match, `fast_path=True`, brief carries the fix.

- [x] **Bounded the index my own recurrence fix had just unbounded.** Worth recording as
      a process note: after shipping the fix above I went looking for what it *broke*
      rather than moving on. "One incident per alarm, forever" had been an accidental
      ceiling on the dispatch index. Removing it means a flapping alarm mints an incident
      per flap, and every claim rewrites the whole index — measured superlinear (50→6ms,
      150→15ms, 300→30ms, 450→53ms); a month of flapping projects to **~21,600**
      incidents, all of which `/incidents` was serializing on every dashboard poll.

      `prune_closed(keep=500)` retires the oldest CLOSED incidents from the daily hygiene
      pass (never from `claim` — maintenance must not tax the hot path), ordered by when
      they closed. **Open work is exempt at any age.** Logs are separate files and left
      alone, so retiring an index row never destroys the record. `/incidents` capped at
      200 and reports `truncated` + `total` — silent clipping is how someone concludes an
      incident vanished; the frontend types both fields.

      Verified: 451 incidents / 301 KB / 43ms per claim → prune → 101 / 67.5 KB / **12ms**.

- [x] **Two more crons were shipping dead — including the sole caller of everything wired
      this session.** The cold-start deadlock I fixed earlier for `dispatch` was pinned by a
      test that named ONE job, so the same bug survived twice over. Nothing flips a manifest
      `enabled: false`, and the rotation-check SOP may only resume `tier_crons.on_shift`
      (touching `always`/`primary` is explicitly out of scope). So:

      - `ledger-hygiene` (`primary`) shipped disabled — **proven dead on the live install**:
        `enabled=False`, `last_run_at=None` after days of uptime. It is the ONLY caller of
        the git ledger sync, the vector-index import, and closed-incident pruning. All three
        were built, tested, documented, and **could never run in production.**
      - `reconcile` (`always`) shipped disabled — a tier whose name means "always armed",
        so the board was never reconciled against provider truth.

      Both now ship enabled with an explicit step-0 `configured=true` → else `NO output`
      guard, so a fresh install still pays nothing. The old rule ("everything except
      rotation-check ships paused") is replaced by the correct one: **only an `on_shift`
      cron may ship paused, because that is the only tier anything resumes.** Two generic
      tests now pin it — every non-`on_shift` cron ships enabled, and every *enabled* cron
      carries the cheap exit — instead of naming a single job.

      Verified in-process (the live gateway predates the edit and restarting it is the
      owner's call): all four hygiene stages present, `index.written: 1`, prune correctly
      no-op under the cap, `changed: True`.

- [x] **The board now shows the remembered FIX, not "2 matched".** The expanded incident
      panel rendered `ledger_matches.length` as a bare count — the app's central payoff
      reduced to a number, with the actual pattern and fix reachable only by opening the
      agent's chat transcript. It now resolves each matched id against the ledger the page
      already fetches (no new endpoint) and renders pattern + fix + `trust · confidence ·
      used N×`, because an unproven `observed/low` entry must not read like a verified one.
      A pruned/missing id says so explicitly rather than rendering nothing, which would
      read as "no prior knowledge". Also typed `similar` into the dispatch response so the
      semantic-recall field is not invisible to the frontend contract.

      **The E2E gate then caught two things, both mine:**
      1. The browser spec encoded the OLD cron rule (`must register paused` for everything
         but rotation-check) — the *same* enumerate-instead-of-state bug, now in
         Playwright. It now reads `tier_crons.on_shift` from the running app instead of
         restating the rule.
      2. My new spec used `test.skip` when the board had no incident to expand. The gate
         enforces a **zero-skip ceiling** — *"a skip reports green while verifying
         nothing"* — and failed it. Replaced with a seeded webhook signal plus an
         assertion on the real empty state.

      Result: executed specs **211 → 232**, skips **0**, **E2E gate 6/6 green** where it
      had been 1 failed / 5 passed — a clean run reporting
      `expected=232, skipped=0, unexpected=0, flaky=0`. The `embed-popout.spec.ts` failure
      seen mid-investigation belongs to a tracked parallel effort (commit 847b8d5a).

      **`MIN_EXECUTED_SPECS` raised 230 → 232 in the same pass.** The floor's own comment
      records that the ops suite once added 24 specs without raising it, leaving ~11 specs
      of slack — and adding one spec here reproduced that slack immediately. Raising the
      floor belongs to adding a spec, not to a follow-up. 232 is cross-checked two ways so
      it is not inferred from a green run: a mid-investigation report totalled 232
      (`expected=229 + flaky=1 + skipped=1 + unexpected=1`), and `grep -c '^\s*test('`
      across `playwright/*.spec.ts` independently counts 232 in both repos. Then verified
      by a full re-run at the new floor rather than assumed.

- [x] **Closed the systemic hole behind the stale bundle: the E2E gate now refuses to run
      against one.** The staleness that hid my vacuous spec was not specific to my spec —
      the gate **never builds or stages**, and the gateway serves
      `src/kiro_crew/static/dist` directly (`server.py` `_DIST_DIR`). So *all 232 browser
      specs* silently verify whatever bundle happens to be on disk, for every UI change
      anyone makes. Mine was simply the one that noticed.

      `_assert_served_bundle_is_current()` now compares the newest `website/src` mtime
      against the newest served-asset mtime and fails LOUD with the exact fix command
      (`npm run build && cp -R dist ../src/kiro_crew/static/dist`). Verified both
      directions: passes on a freshly staged bundle, and fires with
      *"the served bundle is 953s older than website/src"* after touching one source file.
      It also fails when no bundle is staged at all.

      Deliberate limit, stated in the docstring: an mtime comparison cannot catch an edit
      inside the same second or a touched-but-unchanged file. It catches the multi-hour
      staleness that actually happens — both repos were 2 and 10 hours behind — which is
      the failure mode worth a guard.

- [x] **My own new E2E spec was passing vacuously — and a STALE BUNDLE was hiding it.**
      Two compounding problems, both mine:

      1. The spec POSTed to `/webhook` **unsigned**, so `seeded.ok()` was always false and
         it always took a fallback branch asserting only that "Board" is visible — which is
         always true. It passed unconditionally while testing nothing: the same
         green-tick-claiming-coverage failure as the skip the E2E gate had already rejected
         here, just wearing a conditional instead of a skip. Now it seeds through
         `/incident/claim` (no HMAC needed), asserts the incident is open in `/state`
         *before* asking the browser — so a failure distinguishes "did not render" from
         "was never claimed" — then expands the row and reads the ledger outcome. No
         conditional, no skip.
      2. With the assertion made real it failed, and the page snapshot showed
         `INV-1 … Dispatched` **rendered in the DOM**. Cause: `data-testid` was in source
         but NOT in the bundle the gateway serves. `npm run build` writes `website/dist`;
         it does **not** stage into `src/kiro_crew/static/dist`. Both repos were serving
         bundles from *hours* before the UI edits (09:03 and 17:32), so every browser
         assertion about this session's UI work had been running against old code.

      Staged fresh bundles into both repos and re-ran: my spec passes against a live
      gateway, and the full gate is **6/6 with `expected=231, flaky=1, skipped=0,
      unexpected=0`** — all 232 specs executed.

      *Two lessons worth keeping: a conditional in a test is a skip in disguise — the
      zero-skip ceiling does not catch it. And `npm run build` is only half the step;
      without the `cp -R website/dist src/kiro_crew/static/dist` from AGENTS.md, browser
      tests silently verify the previous build.*

- [x] **Acted on my own warning: re-verified the unit-only features against a live
      gateway.** After the half-fix below I said unit-verified claims should be treated as
      provisional. Two features had never touched a gateway; both now have, on an
      ephemeral `--test-mode` instance with an isolated `HOME`:

      - **Schedule-file rotation.** Wrote `rotation.yaml` into the live data home naming
        `someone-else`; the provider showed `configured=True` but rotation still said
        `unknown` — because `_resolve_login_sync()` returned `''` (no `gh` in the isolated
        HOME). That is the designed fail-open, and it means the *disarm* path is
        unreachable without a login. Set `github_login` through the real config route and
        got the real answer: **`on_shift=False`, `who='someone-else'`, and
        `tiers.on_shift` flipped to `False`** — the tier genuinely disarmed. That also
        proves the `is_fallback` fix works end to end, since the always-on default did not
        mask it. A co-primary shift (`[someone-else, octocat]`) armed it back with
        `who='octocat'`, `unknown=False`.
      - **Incident pruning.** Claimed and closed 8 incidents through the real webhook ->
        dispatch -> transition routes, ran the hygiene pass (all four stages reported;
        `incidents_pruned` is a NUMBER where it was `None` pre-wiring; `changed=False`
        because 8 < the 500 cap — silence-by-default holding). Then pruned the gateway's
        own on-disk index with a low cap: **8 -> 3, removed 5, kept INV-6/7/8** — the most
        recently CLOSED, exactly as designed.

      *Two shell hazards worth recording: `noclobber` ate a `>` redirect twice and silently
      stopped a gateway from starting (reads as "the gateway hangs" — use `>|`), and
      `pkill -f "kiro_crew gateway"` matches the issuing shell's own command line, killing
      the caller. Use `ps`-filtered PIDs instead.*

- [x] **The recurrence fix was HALF a fix — found by driving a real gateway.** Booted an
      ephemeral `--test-mode` gateway on this session's code and ran the scenario end to
      end: inject a signed webhook signal → claim INV-1 → resolve it → record the lesson →
      re-inject the same alarm. Result: **`polled=1, claimed=0`.** The recurrence still
      never happened on a real install, with **410 unit tests green.**

      Cause: the ownership rule lives in **two** places. Fixing `store.claim` was correct
      but `run_cycle` keeps a cheap pre-filter — `owned = {signal.id for non-stale
      incident}` — that discarded the recurrence *before* `claim` was ever called. Every
      test I had written called `store.claim` directly, so none traversed the filter.
      Fixed the pre-filter to exclude `TERMINAL_STATUSES`, and added two tests that drive
      the FULL `run_cycle` (one for the recurrence, one proving an open `needs_human`
      incident still suppresses its signal). Verified after: cycle 1 → INV-1
      `fast_path=False`; cycle 2 → INV-2, `matches=1`, `fast_path=True`, remembered fix
      carried, INV-1 preserved `resolved`. **410/410 pass.**

      *The generalizable lesson, now in the module spec: a duplicated invariant needs a
      test at the OUTERMOST caller. A unit test aimed at the inner function proves nothing
      about the filter sitting in front of it — and "408 tests pass" was actively
      misleading here.*

      Also worth recording: five of my own probe failures along the way were MY errors, not
      the app's — wrong route (`provider/` vs `providers/`), wrong secret payload shape,
      wrong signature header (`X-Signature` vs `X-OMC-Signature`), a `?status=` query
      clobbered by appending `?token=`, and an expired 5-minute token. The app refused each
      one correctly. Worth knowing before reading a 401 as a bug.

- [x] **Windows compatibility audited and pinned.** AGENTS.md requires macOS + Linux +
      Windows for every change, and this session added two external-binary spawns (`git`,
      `gh`) plus timezone math — the three things that actually break on Windows. Audited
      the new modules and found them clean, then pinned it with `TestCrossPlatform` so it
      stays that way:

      - `preexec_fn` must come from `resource_limit_preexec()`, which returns `None` off
        POSIX. That is the whole reason both spawns are portable: `preexec_fn` is
        unsupported on Windows and passing *any* callable — even a no-op — raises
        `ValueError`. A hand-rolled `lambda` would pass locally and break every Windows
        spawn. Verified by temporarily swapping one in and watching the test fail.
      - No raw POSIX process calls, no `/bin/sh`, no `shell=True`, no hardcoded `/tmp`.
      - Timezone lookup degrades to UTC. Verified by making the `zoneinfo` **import**
        fail (the real failure shape, not a mocked return): the shift still resolves
        definitively — `on_shift=True, unknown=False`, correct `who` — just in UTC.

      Nothing needed fixing, which is the useful outcome to record: the audit converts
      "probably fine" into a checked property with a regression test. **408/408 pass.**

- [x] **i18n scoped and deferred with a corrected rationale** — see the Later list. The
      note in `SettingsPanel.tsx` blamed a parity failure that no longer exists; measuring
      it found the real blocker is that the codemod is a whole-corpus pass touching 14
      files this app does not own.

      *Also caught by my own earlier test:* reverting the codemod wiped repo-2's
      `builtinRegistry.ts` entry, leaving the app unreachable in the browser there. The
      registration-drift test I added weeks-equivalent earlier failed immediately. Restored
      only my line — `/auto-research` belongs to the parallel effort and was correctly
      absent.

*Pattern across all six: the gap was never in code the tests covered — it was in what the
app tells an agent to do, in an assumption ("a closed incident is done with its signal")
only a real timeline could disprove, in the second-order cost of fixing that assumption,
and twice in a test that pinned a rule by NAMING ONE CASE instead of stating the rule.
Shipping a fix is the point at which to ask what invariant it removed — and a test that
enumerates instances will not catch the next instance.*

## Built and verified

| Feature | Notes |
|---|---|
| Normalized `Signal` across providers | Fingerprint strips timestamps/ids/numbers so a recurrence matches its ancestor |
| Atomic claim / dispatch index | Exclusive file lock + compare-and-set; two heartbeats cannot double-claim (single instance) |
| Dispatch engine (`run_cycle`) | Poll → claim → ledger-match → stale sweep; deterministic Python, not an agent turn |
| Knowledge ledger + fast path | Append-only JSONL, content-addressed ids, confidence decay |
| Silence-by-default heartbeat | `changed == false` ⇒ the cron emits nothing |
| Manifest crons (4 SOPs) | Work crons ship paused; `rotation-check` ships ARMED (it bootstraps the others) and exits free when nothing is configured |
| Autonomy gate (observe/propose/act) | `act` needs BOTH app ceiling AND a scoped rule; no wildcard rule is expressible |
| Keystone secret store | Provider tokens on `_CREW_SECRET_LEAVES`; the agent can neither read nor overwrite them |
| Embedded incident chat | `ChatEmbed` keyed `ops-mission-control-<incident_id>`; height-bounded and scrollable |
| In-chat tool approvals | Wired `onApprove` through the embed; verified resolving a live approval (HTTP 200) |
| Slot-derived status | `needs_human` + `blocked_reason`, so a parked agent cannot read as "progressing" |
| Real-account verification | Beta `024848461597` / us-west-2: claimed 2 real alarms, investigated, recorded a verified pattern |
| Signals tab | Own tab with per-source last-poll outcome + unclaimed queue + manual claim |
| Slack pin board | One message per incident, edited in place; no token of its own (reuses KiroCrew's client); redacted egress |
| Companion discovery seam | `kirocrew.ops_providers` entry points, admission-gated before load, fail-open; verified with a real installed package |
| Diagnosis write-back | Investigate SOP now carries the concrete `/incident/transition` + `/ledger` calls; recording a diagnosis clears `awaiting_diagnosis` (verified live) |
| Rotation tier arming | Cron names fixed to the namespaced ones; `tier_crons` added so the SOP has a safe pause list; verified every tier cron resolves to a real job |
| Boolean config parsing | `config_flag` — `include_insufficient_data` only accepted the literal `"true"`; `provider_enabled` used `bool()`, so `"false"` ENABLED a provider |
| Shift handover digest | Own tab; headline orders coverage-gap → waiting-on-you → routine; unproven patterns visibly unproven; read-only, computed fresh |
| Evidence redaction chokepoint | `gather_evidence` is the sole funnel out of any adapter; redact-then-cap so the byte budget actually bounds emitted text |
| Compounding-memory loop | End-to-end: hygiene promote → re-claim → `fast_path: true`, use counts incremented |
| SOPs actually reach users | Fixed two App Kit bridge bugs that silently emptied `skills/ops-mission-control/` (packaged skill shares the app name); the five SOPs now survive a real gateway start — verified. Generic fix, not app-specific |
| Browser E2E | 24 Playwright specs, green BOTH hand-run on `:6777` and in the packaged `test_e2e` CI gate (against the fake ACP backend) |
| SOP→route contract guarded | **11 (method, path) pairs** the SOPs tell the agent to call are asserted to exist AND to accept the verb used. A renamed route would otherwise 404 mid-investigation with nothing failing at build time. **Corrected 2026-07-31:** this row previously claimed "all 10" while the scanner's input filter required a literal `GATEWAY/` prefix — once the SOPs were rewritten to derive `$BASE`, it silently saw **4 of 10** and stayed green. A companion test now pins a floor on the scanner's own yield, so a narrowing filter fails instead of quietly claiming coverage |
| Ledger git-merge correctness | Verified against a REAL `git merge` of two divergent ledgers: it conflicts (the spec claimed otherwise). Markers are tolerated, and duplicate ids now reconcile on read with fingerprints unioned — before, one shared lesson counted twice |
| Per-adapter evidence budgets | `evidence_budget_hint`, clamped so an adapter can only NARROW the operator's ceiling. Fixes CloudWatch's `_LOG_MAX_WAIT_SECS = 25.0` being unreachable dead code under the 20s global |
| Registration guarded | `BUILTIN_NAMES` + the frontend route are now asserted from the test suite. Every other test imports modules directly, so all of them pass whether or not the app is actually REACHABLE — one dropped line in a shared list would have made it vanish silently |
| README + companion contract | User-facing README shipped and packaged; documents the four Protocols and the `kirocrew.ops_providers` entry point a companion package implements. Every symbol pinned against the code by test |
| Wheel-install verified | Built a real wheel, installed it into a clean venv with full deps, and ran the app from `site-packages`: 5 adapters register, autonomy defaults to `observe`, dispatch explains itself, all 6 SOPs copy into a fresh data home |
| E2E floor corrected | `MIN_EXECUTED_SPECS` 208 → 230, measured from a real run: the collected count (220) is not the executed count (230), and the stale floor left ~11 specs of slack |
| Disable/uninstall lifecycle | Disable verified clean (crons deregister, routes 403). Credentials outlive uninstall by design — the keystone file is at the crew root, not under `apps/` — and Settings now DISCLOSES that next to Revoke |
| Cold-start bootstrap | `rotation-check` now ships ARMED — it is the only thing that resumes `dispatch`, and nothing flips a manifest `enabled: false`, so the app could never fire on a real install. Verified via disable/enable against the live scheduler |
| Fresh-install experience | Verified on a genuinely empty data home: handover refuses to imply health, dispatch is silent but now SAYS why (an agent previously got an empty result), a configured install still polls |
| Full-suite verification | Re-run 2026-07-31 after this session's changes: backend **21,805 passed / 9 failed**, frontend **6,104 passed / 2 failed**. Every failure attributed and none in this app: 5 are missing optional deps in the dev interpreter (`pdfplumber`, `qrcode` — both DECLARED in `setup.cfg`, so an environment gap not a code one), 4 are tracked files this work never touched (`test_acp_runtime`, `test_docker_entrypoint`), and the frontend failure is an untracked file from the parallel chat-approval effort. Confirmed zero failures among the three `test/` files this work does modify |

## Providers

| Adapter | Status | Notes |
|---|---|---|
| AWS CloudWatch (signal + evidence) | **Verified live (both)** | Signals + BOTH evidence branches against beta: alarm history and Logs Insights returning the real root cause. Log evidence was previously unreachable — the adapter read the wrong config namespace |
| Observe-only sink / always-on rotation | **Verified** | The defaults that make a fresh install safe AND useful |
| PagerDuty (signal + rotation + action) | Built, unverified | Never exercised against a real tenant |
| Datadog (signal + evidence + action) | **Signal path verified live** | Real org: `Alert` monitor → `critical` firing signal → claimed INV-1, dedupe holds. Test monitor created with approval and deleted after. Action path (mute/comment) still unexercised — it writes to the tenant |
| GitHub Issues (signal + action) | **Signal path verified live** | 90 real issues from `cli/cli` normalized; assignee filter provably exact (100 open − 10 assigned = 90); fingerprints stable + distinct across polls. Write path (`comment`/`close`) deliberately NOT exercised — least privilege |
| Inbound webhook (signal) | **Verified** | Full HMAC matrix exercised live (missing/wrong/truncated/other-body/tampered all refused; valid accepted). Found + fixed a 500-crash on non-dict `labels` and blanket-401 status codes. 27 tests, previously zero |

## Next (engineering, independent of the requests above)

1. ~~**Verify one non-AWS provider for real.**~~ **DONE (two)** — the inbound webhook
   is now verified as well, which mattered most: it is the only externally-reachable
   ingress and had no tests at all. Doing so found a signed-sender crash (500) and a
   secrets-path singleton that was silently defeating fail-closed test isolation.
   GitHub Issues verified
   end-to-end against `cli/cli`: poll → normalize → claim → brief, plus dedupe and a
   409 on double-claim. Multi-provider fan-out confirmed in the same poll (2
   CloudWatch + 90 GitHub). PagerDuty and Datadog still need real tenants.
2. ~~**Exercise every SOP.**~~ **DONE — all five now run against the live gateway**,
   and doing so found four real bugs (see the journal). The biggest: `TIER_CRONS`
   named `omc-*` crons that were never registered (`ops-mission-control/*` is what
   the scheduler creates), so tier arming was **entirely inert** — every pause/resume
   targeted a nonexistent job. Also verified the full compounding-memory loop:
   hygiene promoted an entry to `verified`, and a re-claim then returned
   `fast_path: true`.
3. ~~**Exercise the reconcile SOP.**~~ **DONE, and it found two real bugs** — see the
   journal. The SOP described pin/react/unpin operations that do not exist in the
   shipped Slack implementation (it was written against the internal system's model),
   and the transition grammar had no legal move for its core case. Both fixed;
   `dispatched → resolved` verified live (HTTP 200).
4. ~~**Cross-account read access for the investigator.**~~ **DONE — by brokering, not
   by granting credentials.** The framing was wrong: the agent did not need AWS access,
   it needed the *evidence*, and the credentialed gateway was already gathering and
   redacting it — the brief just never carried it. Now it does (both claim paths).
   Giving the agent its own profile would have created a second credential holder whose
   reads nothing redacts; least-privilege guidance points the other way. Verified live:
   flapping alarm history + the real `ValueError` root cause, with `receiptHandle`
   visibly redacted. Also bounded the brief — a real one measured 37k chars against a
   50k total context budget; now 7.5k with the diagnosis intact.

## Later

- Rotation from a real PagerDuty schedule (arming/disarming the on-shift tier).
  *Still blocked on a tenant.* The schedule-file source now covers the no-service case.
- ~~Ledger git sync plumbing~~ **DONE** — remote, cadence (daily hygiene pass), and a
  verified two-instance roundtrip incl. the concurrent-write case. See "Shared-memory
  loop" above. The mesh item's prerequisite is therefore met; what mesh still needs is
  cross-instance *claim arbitration*, which is a new contract rather than plumbing.
- i18n extraction for this app's UI copy. **Deferred — and the reason has now been
  corrected TWICE, so the current one is measured rather than inferred.**

  Reason v1 (wrong): a de/it catalog-parity failure. Shipping German and Italian had
  already fixed it; all 134 i18n tests pass.

  Reason v2 (wrong): "cannot land without dragging parallel uncommitted work in."
  Plausible when measured on a dirty tree, but re-measured on `feat/ops-mission-control`
  — a **pristine** branch off `origin/main`, zero working-tree files — the codemod still
  rewrote **12 files this app does not own** and added **59 non-ops keys**. Those strings
  are unextracted on `main` itself; no amount of tree hygiene changes that.

  Reason v3 (measured, structural): `i18n-codemod.mjs` is **whole-corpus by design**. Its
  only flags are `--check`, `--dry-run`, `--merge` — there is no path scope, so extracting
  this app's **93 keys** cannot be separated from extracting the other 12 files. Landing
  it here would put an unrelated 59-key core-i18n change inside an ops PR, and each of the
  152 keys then needs a value in all 10 locales (`i18n-shard.mjs split/join`, fail-closed)
  or catalog parity fails.

  So this is a **core-i18n change, not an ops change** — it belongs in its own PR that
  extracts the remaining corpus and does the translation pass. Full detail in
  `SettingsPanel.tsx`'s header.

## Source-material coverage review (2026-07-31)

Read the context package and skillset end to end after the engineering backlog emptied,
specifically to answer "is anything in the source workflow still missing?". Result: one
real gap (the handover digest, now built) and three deliberate non-gaps.

- **"Missions" need no new concept.** All 15 mission files are cron-scheduled SOPs — the
  construct this app already ships. `oncall-harbinger` IS the reconcile SOP (pin
  lifecycle + index reconciliation, 15-min cadence, 10-item cap, exit-silently);
  `oncall-rotation-check` gating the others IS the tier model. Nothing to port.
- **Absence-of-activity detection is already covered.** Several missions are proactive
  digests (`table-freshness`, `schema-drift`, `pipeline-health`) rather than alarm
  responses. The public equivalent is CloudWatch `INSUFFICIENT_DATA`, already supported
  behind the `include_insufficient_data` opt-in (default off — noisy on accounts with
  idle resources). **Reviewing it found two real bugs:** the flag only accepted the
  literal string `"true"`, so `yes`/`1`/`True `/a JSON boolean read as false silently;
  and `provider_enabled` used `bool()`, where `bool("false")` is True — a config saying
  `"enabled": "false"` would ENABLE the provider. Both fixed via `config_flag`, and the
  provider `detail` now advertises the opt-in (one nobody is told about is one nobody
  uses).
- **SLA tables are deliberately NOT modeled.** `knowledge/sla-table.md` maps table
  patterns → freshness SLA → escalation threshold. That is per-provider by nature: a
  CloudWatch alarm already encodes its own threshold, and a generic "SLA schema" for a
  stranger's data warehouse would be guessing at their org — the same reason the
  handover digest omits rosters and ticket ids. If a user needs it, it belongs in a
  provider adapter (or the Amazon companion), not the public core.

## Declined (and why)

- **Remediation execution.** The app diagnoses and proposes; a human applies the
  fix. Shipping an agent that executes changes against a stranger's production
  infrastructure is not a v1 decision.
- **Auto-resolve by default.** The source workflow auto-resolved two known
  machine-generated intakes. That team could reason about which intakes were safe
  because they had built them; a stranger's first install cannot. Autonomy is
  earned per-rule.
- **Publishing the source workflow's TTR numbers as product claims.** They describe
  one team's pipeline, with caveats that team states itself.
- **Hosted control plane / telemetry.** Local-first is the point: the user's
  credentials and alert streams never leave their machine.
