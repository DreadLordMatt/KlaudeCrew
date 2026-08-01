# Ops Mission Control — Test Plan

A feature-by-feature acceptance plan you can follow by hand. Every case names
what to do, what you should see, and **why the case exists** — several of them
encode bugs that were found the hard way and would silently return.

**Status legend used in §14:** `AUTO` covered by the automated suite ·
`MANUAL` needs a human (browser, real tenant) · `BLOCKED` needs a credential or
account we don't have.

---

## 0. Conventions

Set these once per shell. `$OMC` is the app's API root; everything else is
relative to it.

```bash
REPO=/home/zedmor/workplace/KiroCrew-2
HOME_2=/home/zedmor/.kiro/crew-2
PORT=6777
cd "$REPO"
TOKEN=$(KIROCREW_HOME=$HOME_2 ./.venv/bin/kirocrew token --port $PORT \
        | grep -oE 'token=[^ ]+' | cut -d= -f2-)
OMC="http://localhost:$PORT/api/apps/ops-mission-control"
q() { curl -s --max-time 15 "$OMC$1?token=$TOKEN"; }                    # GET
p() { curl -s --max-time 15 -X POST "$OMC$1?token=$TOKEN" \
        -H 'Content-Type: application/json' -d "${2:-{\}}"; }           # POST
```

The gateway serving this app must be the one built from `KiroCrew-2`. Confirm
before you start — a stale gateway is the most common cause of a confusing
result:

```bash
q /ledger/contradictions >/dev/null && echo "new backend live"
```

> **A note on `data/` paths.** The app's state lives under
> `$HOME_2/apps/ops-mission-control/data/`. Read it freely. Do **not** hand-edit
> `config.json` to set a token — see §7.1 for why that path is deliberately
> closed.

---

## 1. Install, enable, and the disabled-by-default floor

| # | Case | Steps | Expected |
|---|---|---|---|
| 1.1 | App appears in the App Store | Dashboard → Apps | `Ops Mission Control` listed with description + hero image |
| 1.2 | Enable it | Toggle on | Nav gains a **Mission Control** entry; no page error |
| 1.3 | Routes refuse while disabled | Disable, then `q /state` | 404/403 — **not** a 500 and not data |
| 1.4 | Re-enable is non-destructive | Re-enable, `q /incidents` | Prior incidents still listed |

**Why 1.3 exists.** Every route is wrapped in `_require_enabled`. A disabled app
that still answers is an app the operator cannot actually turn off.

---

## 2. Providers — configuration and the enablement contract

Seven providers ship. Each is inert until *both* enabled **and** holding its
required secrets/config.

| Provider | Kind | Needs |
|---|---|---|
| `webhook` | signal in | `signing_secret` |
| `cloudwatch` | signal in + evidence | AWS creds in env/profile, `region` |
| `pagerduty` | signal in + rotation + **write** | `api_token`, `from_email` |
| `datadog` | signal in + evidence | `api_key`, `app_key` |
| `github-issues` | signal in | `repo`, optional `labels` |
| `schedule_file` | rotation | committed `rotation.yaml` |
| `noop` | test double | — |

**Exact payload shapes** (verified live — getting these wrong produces a `400`
that looks like a bug but is not):

```bash
# config: a flat object of config_fields
curl -X PUT "$OMC/providers/datadog/config?token=$TOKEN" \
  -H 'Content-Type: application/json' -d '{"enabled":true,"site":"datadoghq.com"}'

# secret: ALWAYS {"field": <name>, "value": <secret>} — one field per call
curl -X PUT "$OMC/providers/datadog/secret?token=$TOKEN" \
  -H 'Content-Type: application/json' -d '{"field":"api_key","value":"…"}'
```

| # | Case | Steps | Expected |
|---|---|---|---|
| 2.1 | Inventory | `q /providers` | All listed, each with `configured` + `config` + `secret_fields` |
| 2.2 | Not-configured is honest | Enable `pagerduty`, set no token | `configured: false`; it does **not** poll |
| 2.3 | Config round-trips | `PUT /providers/cloudwatch/config` `{"region":"us-east-1"}` | 200; `q /providers` reflects it |
| 2.4 | Secret write | `PUT …/secret` `{"field":…,"value":…}` | 200; `configured` flips true |
| 2.5 | Secret is never echoed | `q /providers`, `q /state` | `secrets` maps field **names** to `""` — never a value |
| 2.6 | Secret delete | `DELETE /providers/webhook/secret` | 200; `configured` false again |
| 2.7 | Unknown provider | `PUT /providers/nope/secret` | 404, no crash |
| 2.8 | Unknown secret field | `{"field":"pwned","value":"x"}` | 400 — the keystone is not general-purpose agent-proof storage |

**Why 2.5 exists.** Provider tokens are live credentials against production
incident tooling — a leaked PagerDuty token can acknowledge or resolve a real
page. They live in the keystone file (§7.1), never in a response body.

---

## 3. Signal ingestion

### 3.1 Webhook — the zero-dependency path

Test this first: it needs no cloud account.

The header is **`X-OMC-Signature`** carrying a **bare hex digest** — no `sha256=`
prefix. HMAC-SHA256 over the **raw** bytes you send (re-serialising the JSON after
signing changes the bytes and breaks the signature).

```bash
SECRET=$(python3 -c "import secrets;print(secrets.token_hex(16))")
curl -s -X PUT "$OMC/providers/webhook/config?token=$TOKEN" \
  -H 'Content-Type: application/json' -d '{"enabled":true}'
curl -s -X PUT "$OMC/providers/webhook/secret?token=$TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"field\":\"signing_secret\",\"value\":\"$SECRET\"}"

BODY='{"id":"probe-1","title":"Checkout latency p99 breach","severity":"high","resource":"svc/checkout"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -r | cut -d' ' -f1)
curl -s -X POST "$OMC/webhook?token=$TOKEN" \
  -H 'Content-Type: application/json' -H "X-OMC-Signature: $SIG" -d "$BODY"
```

| # | Case | Expected |
|---|---|---|
| 3.1.1 | Valid signature | 200 `{"ok":true,"signal":"webhook:<id>"}`; appears in `q /signals` |
| 3.1.2 | **Wrong** signature | 401 `signature mismatch` — and **no** signal ingested |
| 3.1.3 | Missing signature header | 401 `signature mismatch` |
| 3.1.4 | Replayed identical body | Queue accepts it (`queued: 2`) — see note |
| 3.1.5 | Oversized body | Rejected on size, not parsed |
| 3.1.6 | Webhook disabled / no secret | 401 `no signing secret configured`, even with a valid signature |

> **On 3.1.4 — the queue is not id-deduped, and that is fine.** A replay increments
> `queued`, so the same signal id can appear twice in `/signals`. Dedup is enforced
> one layer down, where it matters: `claim` is a compare-and-set on `signal.id`, so
> **two deliveries still produce exactly one incident** (verified: two queued
> deliveries + two claim attempts → one `INV-*`, second claim `409`). Assert on the
> incident count, not the queue count.

**Why 3.1.2 is the most important case in this file.** An unsigned webhook that
still ingests turns the app into an open door: anyone who can reach the port can
manufacture an incident and drive an agent's attention. During development a
Playwright spec passed *vacuously* because an unsigned webhook silently made its
seed a no-op — so assert the negative, not just the positive.

### 3.2 Other inbound providers

| # | Case | Expected |
|---|---|---|
| 3.2.1 | `cloudwatch` alarms → signals | Alarm name/state map to title/severity |
| 3.2.2 | `datadog` monitors → signals | Only `Alert`/`Warn`; `No Data` excluded by design |
| 3.2.3 | `github-issues` by label | Only matching labels ingest |
| 3.2.4 | Provider throws / times out | Logged; other providers still poll |
| 3.2.5 | `pagerduty` incidents → signals | Only `triggered`/`acknowledged`; `urgency: high → critical` |
| 3.2.6 | Zero open work is not an error | `q /signals` → `errors: {}` with no signals |

> **On 3.2.6.** "No signals" and "the provider is broken" look identical on the
> board, so check `errors`. An empty `errors` map with zero signals means the
> provider authenticated and genuinely had nothing open — which is the normal state
> of a healthy account, and the state a fresh tenant is always in.

### 3.3 Credential scope (run this BEFORE granting `act`)

The token you paste is the ceiling on what can ever happen, so know its scope.

| # | Case | How | Expected |
|---|---|---|---|
| 3.3.1 | PagerDuty needs a REST token | Try login/password | No such auth path — `Token token=<api_token>` only |
| 3.3.2 | Identify read-only vs read-write | `PUT /incidents/PNONEXISTENT000` | **403** ⇒ read-only · **404** ⇒ has write scope |
| 3.3.3 | Least privilege | Prefer a read-only token | Signals + rotation work; writes refused at the API |
| 3.3.4 | A write token is still gated | Store a read-write token, stay in `observe` | Action attempt → **403** from the app, before the API is touched |

**Why 3.3.2 is shaped that way.** It establishes scope **without performing a
write**: the incident id does not exist, so a write-capable token fails on
*not-found* (404) while a read-only token fails on *authorization* (403). The
distinction is free and nothing is mutated.

**Why 3.3.4 is the one that matters.** Credential capability and app authority are
independent. Holding a write-capable token must not by itself enable a write —
verified live: a read-write PagerDuty token stored while in `observe` still returned
`403 mode is 'observe' — execution requires 'act'`.

**Why 3.2.4 exists.** One unreachable provider must not take the board down.

---

## 4. The incident lifecycle

Statuses: `unclaimed → dispatched → investigating → {needs_human, resolved,
escalated}`, plus `stale`. Terminal statuses are *derived* from the transition
grammar, not hand-listed.

**Exact payload shapes** (verified live):

```bash
# claim takes a full signal OBJECT under "signal" — not a signal_id
curl -X POST "$OMC/incident/claim?token=$TOKEN" -H 'Content-Type: application/json' \
  -d '{"signal":{"id":"webhook:probe-1","source":"webhook","title":"…","severity":"high","resource":"svc/checkout"}}'

# transition uses "id" (NOT "incident_id") and "status"
curl -X POST "$OMC/incident/transition?token=$TOKEN" -H 'Content-Type: application/json' \
  -d '{"id":"INV-4","status":"investigating"}'

# GET one incident: ?id=… (NOT ?incident_id=…)
curl "$OMC/incident?token=$TOKEN&id=INV-4"
```

| # | Case | Steps | Expected |
|---|---|---|---|
| 4.1 | Claim creates an incident | `POST /incident/claim` with a signal object | `INV-*`, status `dispatched`, + `brief` |
| 4.2 | Double claim refused | Claim the same signal again | **409** `signal is already claimed` |
| 4.3 | Legal transition | `dispatched → investigating` | 200 |
| 4.4 | **Illegal** transition | `resolved → investigating` | 4xx, state unchanged |
| 4.5 | `dispatched → needs_human` | Transition directly | **Allowed** |
| 4.6 | `dispatched → resolved` | Transition directly | **Allowed** |
| 4.7 | Stale sweep | Age an incident past the window | Becomes `stale` |
| 4.8 | Recurrence re-claims | Same signal fires after `resolved` | A **new** incident is claimed |
| 4.9 | Prune keeps open work | Exceed `MAX_CLOSED_INCIDENTS` (500) | Oldest *closed* dropped; open work never pruned |
| 4.10 | Investigation log | Write diagnosis/actions/next steps | Readable via `read_log` |

**Why 4.5 and 4.6 are explicit rows.** Both were missing edges that made the
board *lie*. An agent can block on a tool approval before its first turn ends
(4.5) — without that edge a blocked incident reads as progressing, the one thing
an ops board must never do. And a signal can clear between claim and first turn
(4.6) — without it, reconcile has **no legal move**, so the board claims work is
in progress on a problem that no longer exists.

**Why 4.8 is a row.** The recurrence fix was once only *half* a fix: `claim`
handled terminal status correctly, but `run_cycle`'s pre-filter discarded the
signal before `claim` ever saw it. Test the behaviour end to end, not the unit.

---

## 5. On-call rotation — the single-owner model

The core promise: **a shared, committed `rotation.yaml` decides who picks up
work, so N teammates running N instances do not all grab the same alarm.**

`rotation.yaml` at the ledger repo root:

```yaml
leader: octocat                 # optional — runs nightly ledger hygiene ALONE
timezone: America/Los_Angeles   # optional — UTC when absent
shifts:
  - from: 2026-08-01
    to:   2026-08-08
    who:  octocat               # a GitHub login
  - from: 2026-08-08T09:00
    to:   2026-08-15T09:00
    who:  [octocat, hubot]      # co-primary allowed
```

Three cron tiers, armed by shift:

| Tier | Crons | Armed when |
|---|---|---|
| `always` | `rotation-check` | always |
| `on_shift` | `dispatch`, `reconcile` | this operator is on shift |
| `primary` | `ledger-hygiene` | this instance is the leader |

| # | Case | Steps | Expected |
|---|---|---|---|
| 5.1 | On-shift arms | Login matches current window | `q /rotation` → `on_shift: true`; `dispatch` armed |
| 5.2 | Off-shift disarms | Login matches a *different* window | `on_shift: false`; `dispatch` **not** armed |
| 5.3 | **Indeterminate DISARMS** | Schedule present, login unresolvable | `on_shift: false, unknown: true`; `dispatch` **not** armed |
| 5.4 | Solo install still works | **No** `rotation.yaml` at all | `AlwaysOn` fallback → `on_shift: true` |
| 5.5 | Leader wins over local | `leader: alice`; bob has `primary_instance: true` | Only alice runs `ledger-hygiene` |
| 5.6 | Handover across weeks | Advance the clock a week | Ownership follows the file |
| 5.7 | Team composition renders | Board → On-call panel | Roster with the leader badged |
| 5.8 | Malformed YAML fails closed | Corrupt the file | `unknown: true`, tiers disarmed — not a crash |

**Why 5.3 is the subtle one.** `tier_states` once read `shift.on_shift or
shift.unknown`, which re-armed the tier for exactly the case strict gating was
written for — so every teammate would still pick up the same alarm. The
fail-open intent belongs in the *source*: a rotation **API** returns
`on_shift=True, unknown=True` (a network fault must not disable response), while
the committed **file** returns `on_shift=False, unknown=True`. Two sources, two
policies, one gate that just reads the answer. **5.4 is its counterpart** —
verify the fix did not disarm solo users.

---

## 6. Autonomy — arming vs. authority

Two different gates; both must be checked where they are enforced.

- **Arming** (tier) decides *when we look*.
- **Authority** (`authorize_action`) decides *whether we may act*.

`effective = min(app_mode, rule_mode)` over `observe < propose < act`.

| # | Case | Steps | Expected |
|---|---|---|---|
| 6.1 | `observe` cannot write | mode `observe`, try `ack` | Denied: "execution requires 'act'" |
| 6.2 | `propose` cannot write | mode `propose`, try `ack` | Denied |
| 6.3 | `act` + matching rule | mode `act`, rule on that source | **Allowed** |
| 6.4 | `act` with no rule | mode `act`, no rule | Denied: "no matching act-rule" |
| 6.5 | Rule narrows actions | rule grants `ack` only; try `resolve` | `resolve` denied, `ack` allowed |
| 6.6 | No wildcard `act` | rule with no `resource_glob` | Rejected at load |
| 6.7 | **Off-shift cannot write** | bob off shift, mode `act`, matching rule | **Denied** — on-call owns provider writes |
| 6.8 | Unknown action | `"delete_everything"` | Denied |
| 6.9 | Every decision audited | Any of the above | SEL entry for allow **and** deny |

**Why 6.7 exists.** `authorize_action` never consulted the shift, so an
off-shift teammate could acknowledge or resolve a real page in the operator's
production tooling — even though the tier gate had correctly disarmed their
scheduled work. The `/incident/action` route and an in-flight investigation both
reach this path *without* passing the tier gate. Deliberately narrow: it refuses
a **definite** off-shift answer, never a merely indeterminate one, so 5.4's solo
user is unaffected.

---

## 7. Security

### 7.1 The keystone

Provider tokens live in `$HOME_2/ops_mission_control_secrets.json`, which is in
`security._SENSITIVE_HOME_DIRS`. The agent can neither **read** nor **write** it.

| # | Case | Steps | Expected |
|---|---|---|---|
| 7.1.1 | Agent cannot read | Ask the chat agent to cat the file | Refused |
| 7.1.2 | Agent cannot write | Ask it to overwrite the file | Refused |
| 7.1.3 | Bash verbs covered | `cp`/`mv`/`tar`/`>` at that path | Refused |
| 7.1.4 | Settings PUT still works | Save a token in the UI | Succeeds |

**Why not `config.json`?** Two concrete reasons: an app's `data/config.json` is
served over `/api/apps/<name>/config` **without session auth**, and `config.json`
is writable by any auto-approved agent shell. The keystone is the only placement
where the agent can neither read the tokens nor overwrite them — and the
authenticated dashboard PUT is the sole writer.

### 7.2 Redaction

| # | Case | Expected |
|---|---|---|
| 7.2.1 | Evidence redacted at one choke | `gather_evidence` redacts **then** caps |
| 7.2.2 | `Bearer <token>` scrubbed | Absent from the model prompt |
| 7.2.3 | `api_key=…` / `app_key=…` scrubbed | Absent |
| 7.2.4 | Whitespace carrier | `Bearer  abc…` (no `:`/`=`) scrubbed |
| 7.2.5 | Slack egress redacted | Board messages carry no credential |
| 7.2.6 | HTTP 401 body scrubbed | An echoed token never reaches a log |
| 7.2.7 | No-credentials brief | Investigation brief always states it |

**Why 7.2.4 exists.** The carrier pattern once required a `:` or `=`, so a bare
`Bearer abc123…` reached the model prompt unredacted. The single choke is
deliberate: an adapter author who forgets to redact *cannot* leak, because
redaction happens centrally in `gather_evidence`, not per-provider.

### 7.3 Spawn discipline

| # | Case | Expected |
|---|---|---|
| 7.3.1 | `ledger_sync._git` sandboxed | Routed via `sandboxed_spawn_argv` |
| 7.3.2 | Resource limits applied | `resource_limit_preexec`; `None` off POSIX |
| 7.3.3 | Spawn audit green | `test_spawn_audit.py` passes |

---

## 8. Shared memory — the ledger

An append-only JSONL of `pattern → fix` lessons, synced through git so a team
converges on shared knowledge.

| # | Case | Steps | Expected |
|---|---|---|---|
| 8.1 | Append a lesson | `POST /ledger` | Entry gets a content-addressed id |
| 8.2 | Ids are content-addressed | Same pattern+fix on two instances | **Same** id → merge, not duplicate |
| 8.3 | Recall by fingerprint | New signal matching a lesson | Lesson surfaces in the brief |
| 8.4 | Semantic recall | Similar-but-not-identical signal | Appears under `similar` |
| 8.5 | Fast path | High-confidence + verified lesson | `is_fast_path` true |
| 8.6 | Use count | `record_use` | Increments; most-used ranks first |
| 8.7 | Contradictions | Two fixes, one fingerprint | `q /ledger/contradictions` lists the pair |
| 8.8 | Decay | Age past 90 days | Downgraded by hygiene |
| 8.9 | Cap | Exceed 500 entries | Oldest/least-used trimmed |
| 8.10 | Marker-tolerant read | Inject a conflict marker | Reader survives; `resolve_conflict` unions |
| 8.11 | Delete | `DELETE /ledger` | Entry removed |

**Why 8.2 is the design's keystone.** Content-addressed ids are what make a git
merge *reconcile* rather than duplicate — two teammates who learn the same lesson
independently converge on one row.

---

## 9. Git sync (multi-user)

`TRACKED_FILES` is exactly `ledger.jsonl`, `rotation.yaml`, `.gitignore`.

| # | Case | Steps | Expected |
|---|---|---|---|
| 9.1 | Configure a remote | `set_settings(remote, branch)` | `configured()` true |
| 9.2 | First push | Local lesson → push | Lands on the remote branch |
| 9.3 | Pull converges | Teammate's lesson → pull | Both visible on both instances |
| 9.4 | Unrelated histories | Each instance `git init`s its own | `--allow-unrelated-histories` handles it |
| 9.5 | `rotation.yaml` staged | Edit + sync | Actually committed, not left unstaged |
| 9.6 | Untracked-overwrite | Remote file exists untracked locally | Sync does not clobber blindly |
| 9.7 | Transient fault retried | Fail a spawn once | `sync_safely` retries once |
| 9.8 | Ledger conflict unions | Divergent appends | `resolve_conflict` keeps both |
| 9.9 | **Schedule conflict blocks push** | Conflicted `rotation.yaml` | Push **refuses**; resolves "theirs" |
| 9.10 | No stranded commits | Interrupt mid-sync | `_has_unpushed` reports honestly |

**Why 9.9 exists.** A merged-wrong rotation file silently reassigns who is on
call. Refusing the push is the safe failure.

---

## 10. Agent-driven operation (SOPs + crons)

Six SOPs ship in `builtin_skills/ops-mission-control/sops/`: `dispatch`,
`investigate`, `reconcile`, `rotation-check`, `ledger-hygiene`, `handover`.

| # | Case | Expected |
|---|---|---|
| 10.1 | SOPs installed | Present under `$HOME_2/skills/ops-mission-control/sops/` |
| 10.2 | `dispatch` claims + investigates | Un-owned signal → incident → brief |
| 10.3 | Step-0 guard | Unconfigured app → cron produces **NO output** |
| 10.4 | `reconcile` resolves cleared signals | Signal stopped firing → `resolved` |
| 10.5 | `rotation-check` refreshes shift | Tier arming follows the file |
| 10.6 | `ledger-hygiene` full pass | pull → hygiene → index → prune → push; leader only |
| 10.7 | `handover` summarises | Open work + who's next |
| 10.8 | Auth recipe parses | The SOP shell snippet passes `bash -n` |

**Why 10.3 exists.** A silent cron on an unconfigured app is noise that trains
the operator to ignore the app. Every cron's step 0 checks `configured` and
returns nothing when false.

---

## 11. Dashboard UI

| # | Case | Expected |
|---|---|---|
| 11.1 | Board renders | Incidents grouped by status |
| 11.2 | Incident detail | Signal, evidence, diagnosis, next steps |
| 11.3 | Ledger panel | pattern + fix + trust |
| 11.4 | On-call panel | Roster; leader badged |
| 11.5 | Settings | Providers, secrets, mode, rules |
| 11.6 | Companion chat | Embedded chat scoped to the incident |
| 11.7 | **Approval buttons work in the embed** | Approve/reject resolves the request |
| 11.8 | Approvals visible when expanded | Buttons present in *both* states |
| 11.9 | Empty state | Clean copy, no error |
| 11.10 | Truncation honest | >200 incidents → `truncated` + `total` |

**Why 11.7 and 11.8 exist.** The approval card *rendered* but its buttons did
nothing, so an embedded agent asking permission stalled forever — silently,
because the card looked interactive. And the buttons were `!expanded`-only,
which hid them exactly when they mattered: a group with a live pending approval
**auto-expands**, so the one turn waiting on you was the one turn you could not
answer.

---

## 12. Cross-platform + regression gates

| # | Case | Command | Expected |
|---|---|---|---|
| 12.1 | Backend suite | `pytest` (repo + app tests) | All pass |
| 12.2 | Lint | `flake8 src/kiro_crew` | Clean |
| 12.3 | Types | `mypy src/kiro_crew` | Clean |
| 12.4 | Format | `black --check`, `isort --check` | Clean |
| 12.5 | No internal markers | `scripts/scrub-lint.sh` | Clean |
| 12.6 | Frontend unit | `npm run test` | Pass |
| 12.7 | TS build | `npx tsc -b` | Clean |
| 12.8 | Browser E2E | `KIROCREW_E2E=1 pytest test/test_playwright_e2e.py` | `skipped=0`, ≥ floor |
| 12.9 | Windows safety | Review diff | No raw POSIX calls; `platform_compat` only |

---

## 13. Known gaps — deliberately not covered

Honest scope boundaries, so absence of a test is not mistaken for absence of a
feature:

- ~~PagerDuty write paths unexercised~~ — **CLOSED.** Verified against a live test
  tenant end to end; see §15.
- ~~PagerDuty rotation unverified~~ — **CLOSED.** A schedule now exists and
  `on_shift` resolves through the API with `unknown: false`; see §15.
- **`comment` is still unexercised** against the live tenant (only `ack` and
  `resolve` were). It posts to `/incidents/{id}/notes` — a different endpoint from
  the status write, so it is genuinely untested.
- **Credential hygiene follow-up (ARCC BSC5).** Provider tokens currently live in
  the local keystone file, and the guidance's preferred home for third-party
  credentials is a managed secret store with automatic rotation (≤ 90 days). For a
  single-user local app the keystone is a defensible fit — it is owner-only `0600`,
  agent-unreadable, and never echoed — but there is **no rotation mechanism**, so
  rotate by hand on your normal cadence. A managed-store backend would need the
  `EmbeddingBackend`-style seam treatment to stay optional for OSS users.
- **Datadog write path** — read/evidence only by design, pending consent.
- **Amazon-internal companion** — built separately, out of tree.
- **i18n** — the app's strings are not yet localised; tracked as its own core PR.
- **Long-run soak** — no unattended multi-day run yet. Recommended before
  trusting `act` mode.

---

## 14. Results — author's run (2026-08-01)

Run against the live gateway on `:6777` (repo `KiroCrew-2`, branch
`feat/ops-mission-control`), plus the automated suites.

### Automated gates

| Gate | Result |
|---|---|
| App test suite | **458 passed** (+99 subtests) |
| `flake8` (this app) | clean |
| `mypy` | clean — 605 files |
| `black` / `isort` | clean — 42 files |
| `scrub-lint` working tree | ✓ no internal markers, ✓ no credential leaks |
| `tsc -b` | clean |
| vitest (ops specs) | pass |
| Browser E2E | `executed=230, unexpected=1, flaky=1` — see caveat |

### Live API cases

| Section | Verdict |
|---|---|
| 2.1, 2.3, 2.4, 2.5, 2.7, 2.8 | **PASS** — `secrets` returns field names mapped to `""`, never a value |
| 3.1.1–3.1.4, 3.1.6 | **PASS** — forged and unsigned deliveries both `401`, nothing ingested |
| 3.2.2 Datadog (real tenant) | **PASS** — authenticated, 7 monitors read, `errors: {}` |
| 3.2.5 PagerDuty (real tenant) | **PASS** — authenticated, 1 service / 0 open incidents, `errors: {}`; payload→Signal mapping verified against a real v2 shape (`urgency: high → critical`) |
| 3.3.1–3.3.4 credential scope | **PASS** — token is read-**write** (404 probe); stored in the keystone at mode `0600`; app still returned `403` in `observe` |
| 9 Slack wiring | **PASS** — reuses KiroCrew's own client off gateway state; no app-level Slack token exists |
| 4.1, 4.2, 4.3, 4.4, 4.8 | **PASS** — `409` on double-claim and on illegal transitions; state unchanged |
| 5.3 / 5.4 strict gating | **PASS** — indeterminate-from-file disarms; solo/AlwaysOn still arms |
| 6.1, 6.8 | **PASS** — `403` in `observe`; unknown action `400` |
| 6.x gate suite | **PASS** — 26 targeted tests |
| 7.1 keystone | **PASS** — all keystone leaves read+write sensitive under the active home |
| 7.2.2–7.2.4, 7.2.6 | **PASS after a fix** — see below |
| 8.1, 8.2, 8.7 | **PASS** — identical lesson returned the same `entry_id`; real contradiction detected |

### Two real defects found and fixed during this run

1. **Prefixed Datadog keys escaped redaction.** Redaction assumed Datadog keys are
   bare hex (32/40 chars). Real tenants now issue `ddapp_…` / `ddapi_…`, which are
   not hex — so a live application key survived into a reproduced `curl` trace. The
   carrier pattern also missed `DD-APPLICATION-KEY`, because `app[_-]?key` does not
   match `APPLICATION-KEY`. Both fixed; regression tests added.
   **This is why testing with real credentials matters** — every fixture in the
   suite used `"a" * 32`, so the suite looked complete while the shape real users
   hold was covered by nothing.

2. **`reconcile` shipped armed on a gated tier.** It moved to the `on_shift` tier,
   but `app.json` still had `enabled: true`, so on a fresh install it ran before
   rotation-check could gate it — the multi-instance write race the tier move
   existed to prevent. Now ships paused like `dispatch`. The `rotation-check` SOP's
   prose was also stale (it still described the pre-strict-gating fail-open rule and
   claimed only one cron ships enabled); corrected.

### Caveats on this run

- **1 E2E failure is not this app**: `embed-popout.spec.ts:144` (`/embed/chat/<slot>`,
  which renders `ChatPage`, not `ChatEmbed`). The spec is byte-identical to `main`,
  landed upstream 2026-07-29, sends no message (so no tool group renders), and
  reported `flaky=1` — i.e. timing. Fixing the ops manifest raised `executed` from
  211 → 230 and cleared the ops failure.
- **The E2E floor is not met** (`MIN_EXECUTED_SPECS = 234`, `MAX_SKIPPED = 0`) while
  that upstream spec is red. It must be green before this branch merges.
- §5.1/5.2/5.6/5.7 (a real multi-week rotation across instances) and §9 (git sync)
  were verified earlier in development against the private repo
  `Zedmor/ops-mission-control-test`, not re-run here.
- §11 (UI) is **MANUAL** — not exercised in this pass beyond the E2E specs.
- §7.1.1–7.1.3 were verified at the `is_sensitive_path` layer, not by driving a live
  agent into refusing.

---

## 15. Live PagerDuty tenant — full-loop verification (2026-08-01)

The write and rotation paths were previously the two largest untested areas. Both
are now closed against a **real PagerDuty account**, with every provider write
confirmed by an *independent* read rather than trusting the app's own response.

### Fixtures created in the test account

| Kind | Id | Name |
|---|---|---|
| Service | `PW3WWXQ` | KiroCrew Ops Mission Control (test) |
| Events API v2 integration | `PPD5LFY` | KiroCrew Events v2 (routing key held out of tree) |
| Schedule | `P6RGE0P` | KiroCrew Test Rotation (weekly, single user) |
| Escalation policy | `P1FAJJ8` | KiroCrew Test EP → targets `P6RGE0P` |

The pre-existing `Default Service` and `Default` escalation policy were **not
modified** — a separate service and policy were created instead, so the account's
original configuration is intact and every fixture is independently deletable.

### What the full loop proved

| Step | Result |
|---|---|
| Trigger a real incident (Events v2) | `Q3TAR7W83MI3SD` created, `status: triggered`, `urgency: high` |
| App ingests it | `pagerduty:incident/Q3TAR7W83MI3SD`, `severity: critical`, `errors: {}` |
| Poller attaches the write handle | `labels.pd_incident_id = Q3TAR7W83MI3SD` |
| Rotation resolves through the API | `on_shift: true`, `who: "Akim Akimov"`, **`unknown: false`** |
| `observe` refuses a write | `403 mode is 'observe' — execution requires 'act'` |
| Rule granting `ack` only refuses `resolve` | `403 matching rule does not grant 'resolve'` |
| **Real `ack` lands** | app `200`; independent read → `status: acknowledged`, 1 acknowledgement |
| **Real `resolve` lands** | app `200`; open-incident count → `0` |
| Restored to safe defaults | `mode: observe`, `rules: 0`, write attempt `403` again |

### Two findings from this run

1. **The `PUT /settings` route deliberately cannot write autonomy rules.** `mode` is
   settable over the API, but `autonomy_rules` is not — the operator edits it out of
   band. That is a sensible fence (the API cannot widen its own authority in one
   call) and worth keeping; it is **not** a gap. Grant rules by editing
   `data/config.json`.
2. **A hand-crafted claim payload cannot execute an action.** The sink reads the
   PagerDuty id from `signal.labels.pd_incident_id`, which only the *poller* sets, so
   a signal object typed by hand fails with `signal carries no PagerDuty id` — after
   passing the autonomy gate. That ordering is correct (authority is decided before
   the sink is touched), but when testing writes you must let the app claim from its
   own poll (`POST /dispatch`) rather than posting a synthetic signal.

### State left behind

- **PagerDuty: 0 open incidents.** The test incident was acknowledged then resolved.
- **App: `mode: observe`, `rules: 0`** — writes refused again. The `act` grant was
  temporary and scoped to `KiroCrew Ops Mission Control*` on `pagerduty` only.
- The four fixtures above still exist so this loop can be re-run; delete them when
  finished with the test account.
