# Ops Mission Control

Last Updated: 2026-07-30 (dispatch engine + manifest crons + Settings panel; initial
revision same day — builtin app, provider seam, keystone secret store, autonomy gate)

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
  `upsert` (fingerprints union, strongest confidence/trust, highest `use_count`). Before
  this it appended every line, so one shared lesson counted twice: `stats()` inflated,
  `match()` returned the same entry twice, and the handover digest listed one pattern as
  two. Fingerprint union is the load-bearing part — dropping one branch's fingerprint
  means that recurrence stops matching, and the ledger keeps working while silently no
  longer recognizing half its own history.

Measured end to end: two divergent ledgers → real `git merge` → conflicted file → 4 raw
entries read as **3**, shared lesson collapsed with both fingerprints preserved.

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

Also fixed: a clean tree is not proof everything is shared. A run that committed and then
failed to reach the remote left `push` reporting "nothing to push" forever, stranding that
lesson locally; `_has_unpushed` distinguishes the two and treats an unknown answer as
"push anyway" (a redundant push is cheap, a skipped one loses knowledge).

Verified live: A records → pushes → B pulls and sees it → B adds → A pulls both. Then the
concurrent case — both write without seeing each other — the stale push is correctly
**rejected**, the pull reconciles to 3 entries preserving both sides, and both instances
converge on identical ledgers with no entry lost.

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
dispatched|investigating → stale        (idle past the sweep window)
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
| `ActionSink` | Ack / resolve / comment | `pagerduty`, `datadog`, `github-issues`, `noop` (default) |
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

`gather_evidence` passes every body through `security.redact` **and**
`secrets.redact_tokens` centrally, so an adapter author cannot leak a credential
into a model prompt by forgetting to redact.

## Security model

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

`PUT /settings` refuses an unrecognized `mode` rather than falling back — a typo
must not quietly change what the agent is allowed to do.

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
in a bounded (200-entry) spool the heartbeat drains. No public ingress or tunnel is
shipped.

**Check order is load-bearing** (`webhook.enqueue`): enabled → secret → size →
signature → parse. Nothing unauthenticated is ever handed to `json.loads`, and an
oversized body is refused *before* it is hashed. `test_webhook.py` pins the order by
asserting an unsigned malformed body is rejected for its **signature**, not its
syntax.

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

| Tier | Gate | Crons |
|---|---|---|
| `always` | always armed | `ops-mission-control/rotation-check` (5m), `.../reconcile` (15m) |
| `on_shift` | `RotationSource.on_shift()` | `ops-mission-control/dispatch` (2m) |
| `primary` | `primary_instance` config (default true) | `ops-mission-control/ledger-hygiene` (daily 03:00) |

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
`enabled: false`, and the rotation-check SOP resumes *only* `tier_crons.on_shift` — it is
explicitly forbidden from touching the `always` and `primary` tiers. So a cron on any
other tier that ships disabled stays disabled **forever**. The earlier rule here was
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

### `armed_crons` is not a pause list

`GET /rotation` returns both `armed_crons` (flat union across every armed tier — "what
is running now") and **`tier_crons`** (the per-tier breakdown). The rotation-check SOP
must act **only** on `tier_crons.on_shift`. Off shift, `armed_crons` still legitimately
contains `ops-mission-control/rotation-check` because that is an `always` job — so an
agent told to "pause the armed crons" would pause the cron that re-arms the instance,
silently ending incident response until a human noticed. `tier_crons` exists to make
the safe list nameable; two tests pin both halves.

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
2. `poll_all` across configured sources; drop `state != firing`.
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
6. Sweep incidents idle past the window back to `stale` for re-pickup.
7. **If nothing changed, `CycleResult.changed` is false and the cron emits
   nothing.** Silence-by-default is a hard requirement, not an optimization — it
   is why the modeled channel stayed readable.

`investigation_brief()` renders the claim's facts (signal, mode, matched patterns,
fast-path flag, authority reminder) deterministically, so an investigating agent
does not spend its first turn re-fetching context Python already has.

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

## Shift handover (`backend/handover.py`)

`GET /handover` returns a digest of what an incoming responder needs: the one-line
headline, work **waiting on a person**, work that stopped without recording anything,
recurring patterns ranked by `use_count`, and which sources are **not** configured.
Plus a pre-rendered `text` field, so a Slack paste and the Handover tab cannot word the
same shift differently.

This reproduces the modeled workflow's hand-maintained handover document — one of that
team's most-used artifacts, which cost a human hours and went stale between edits.
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
├── incidents/<id>.md      # investigation log (human-readable, git-friendly)
└── ledger.jsonl           # append-only LedgerEntry stream
<crew_home>/ops_mission_control_secrets.json   # KEYSTONE (see contract 4)
```

All writes go through `atomic_write`. File locking goes through
`platform_compat` (never raw `fcntl` — Windows support).

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
- `.../backend/store.py` — dispatch index, atomic claim, transitions, stale sweep
- `.../backend/ledger.py` — append-only ledger, matching, fast path, hygiene
- `.../backend/secrets.py` — keystone token store, `SecretBackend` seam, redaction
- `.../backend/registry.py` — ADD-only registry, fan-out
- `.../backend/rotation.py` — autonomy gate, tier arming
- `.../backend/dispatch.py` — **the cycle**: poll → claim → ledger-match → sweep,
  plus `investigation_brief`
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

## Known debt

**i18n.** The board and Settings copy is inline English rather than catalog keys,
against the `website/AGENTS.md` rule. `catalogParity.test.ts` requires every one of
the 10 shipped languages to carry each new key, so extracting ~50 strings en-only
would convert one pre-existing de/it parity failure into ~9 failures per key.
Extraction therefore needs a translation pass and is tracked separately. Do NOT
hand-edit `en.json` to work around it — that file is generated by
`scripts/i18n-codemod.mjs`.

**`jsx-a11y/label-has-for`.** `SettingsPanel.tsx` carries 2 warnings of this rule.
The labels are correct (input both nested AND `htmlFor`-bound); the rule cannot see
through the shared `Input` wrapper. 12 other files in the repo, including another
builtin app page, carry the same warning — this is the accepted baseline, not a new
regression.

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

`src/kiro_crew/apps/builtins/ops_mission_control/tests/` — 145 tests:

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
  provider not fatal
- `test_config_routes.py` — **secret field refused on the config route**, unknown
  field/provider refused, merge preserves untouched fields, invalid mode refused,
  and manifest-cron assertions (all four present, all paused, all silent and
  stateless, exactly one schedule each)

Frontend: `website/src/test/opsMissionControl.test.ts` (route registration + shape).
