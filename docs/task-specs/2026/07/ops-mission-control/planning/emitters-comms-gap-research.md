# Emitters, comms channels, and auto-resolution — research report

Scope: which error emitters are worth monitoring, which of them are genuinely safe to
close automatically, which communication channels are worth building next, and what is
actually missing in the shipped app. Paths are relative to
`src/kiro_crew/apps/builtins/ops_mission_control/` unless prefixed `src/kiro_crew/` or
`website/`. Every code claim below was re-verified in this pass; anything I could not
verify is marked **unverified**.

---

## 0. Implementation status (updated 2026-08-01, after the first fix pass)

Seven of the 19 verified gaps are **fixed and running**. Every §5 heading below carries a
`STATUS:` line, so the report stays readable as a work list rather than a snapshot of a
moment that has passed.

Verification for the whole pass: **512 tests pass, 0 fail** (baseline 463, so 49 net new:
`test_providers` +16, `test_webhook` +14, `test_store_and_gate` +11, `test_slack_out` +8), and
`isort` / `flake8` / `mypy` (605 files) / `tsc --noEmit` are clean. Then rebuilt the
frontend, restarted the KiroCrew-2 gateway on `:6777` (PID 26141, old process confirmed
gone and the port confirmed released first), and checked the new behaviour against real
providers rather than only against tests:

- `/signals` went from `[errors, signals, unclaimed]` to `[all_sources_healthy, cleared,
  errors, firing, poll_health, signals, unclaimed]`.
- `poll_health` reported real successful polls — `cloudwatch {ok: true, signals: 2}`,
  `pagerduty {ok: true}`, `webhook {ok: true}`.
- Both live CloudWatch alarms now carry a namespaced exact key, e.g.
  `cloudwatch:us-west-2/DlqAlarm-ScosShipment`. Before the change no signal had one.
- One Alertmanager POST fanned out to 2 signals (one `firing`, one `ok`), each keyed by
  the provider's own fingerprint. Before, a raw Alertmanager body was rejected outright.

| § | Gap | Status |
|---|---|---|
| 5.1 | Slack board thread not replyable | **FIXED** |
| 5.2 | Webhook single-shape / single-signal / firing-only | **FIXED** (auth-header decision still open) |
| 5.3 | No exact provider-identity match layer | **FIXED** |
| 5.4 | Reconcile resolves live incidents on a failed poll | **FIXED** (incl. the 429/`Retry-After` blindness behind it) |
| 5.5 | `needs_human` never swept | **FIXED** |
| 5.7 | `ACTION_COMMENT` has no caller | **FIXED** |
| 5.12 | No verb with a mandatory expiry; Datadog mute indefinite | **FIXED** |
| 5.6 | Local notification bus unused | **FIXED** |
| 5.8 | No artifact for a non-KiroCrew reader | **FIXED** (local file; HTTP download still deferred) |
| 5.9 | Fast path has no track record; confidence never demotes | **FIXED** |
| 5.10 | No post-action outcome verification | **FIXED** |
| 5.11 | No representation for provider-side suppression | **FIXED** |
| 5.13 | Source reach (Sentry/GlitchTip, `/metrics`, ICS rota, …) | Open |

**A second pass then closed the UI half of the shared-memory feature**, which no §5 row
covers because it is not an emitter gap — it is the owner's own report: *"I do not see where
we can specify memory exchange / SOP / on-call schedule repository."* They were right.
`PUT /settings` had accepted `ledger_sync_enabled` / `_remote` / `_branch` and `/state` had
returned `ledger_sync.status()`, and **nothing in the UI sent or read either** — the app's
headline team feature was reachable only by hand-editing `data/config.json`, which is what
the owner had done. `SettingsPanel` now carries a **Shared team memory** card (the three
keys, plus status) and an **On-call schedule** card (the `rotation.yaml` shape, and the
`github_login` field that was previously unreachable because the Schedule-file row painted
an enable toggle the backend 400s and hid its own error message behind that toggle).

The one backend change in that pass was read-only and closed a silent stop of exactly the
kind §5 catalogues: `ledger_sync.push` REFUSES while `rotation.yaml` holds conflict markers,
and said so only to the log and a SEL line, while `status()` reported "Syncing …". So sync
could stop publishing indefinitely behind a card that claimed it worked. `status()` now
reports `conflict` and `schedule_conflict` separately — a ledger conflict is reconcilable, a
schedule conflict is not — and Settings renders the latter as an error.

**One thing the fixes did not settle, deliberately.** §5.2's caveat stands: envelope shape
alone does not make Alertmanager or Grafana work against this fail-closed ingress, because
Alertmanager cannot HMAC-sign a raw body and Grafana signs a different header. That is a
security-posture call, not a shape change, and it is still open. Both senders are reachable
today via any forwarder that can sign.

**The ordering held up.** §7 put two correctness fixes ahead of the high-reach webhook work
on the principle *fix what silently lies before adding reach*, and that was right: 5.4 is a
prerequisite for 5.2 (accepting `ok` makes reconcile worse until `/signals` filters state),
and 5.3 is what stops a new source's first exact key landing in a ledger that could only
match on a hash that provably collides.

---

## 1. TL;DR

- **One change unlocks more integration reach than everything else combined**: make the
  inbound webhook accept an Alertmanager-shaped body (`alerts[]` fan-out), accept a
  `status`/`state` key, and carry a provider-supplied stable id. That is one file
  (`backend/providers/webhook.py`, 176 lines today), no new route, no new credential, no
  tunnel, and it reaches Prometheus Alertmanager, Grafana unified alerting, Sentry,
  GlitchTip, Datadog Error Tracking, and every Alertmanager-shaped forwarder.
- Today that ingress rejects a raw Alertmanager v4 body outright: `signal_from_payload`
  returns `None` when there is no top-level `title`/`summary` (`webhook.py:110-112`),
  which `routes.py:889` turns into a 400 `payload has no title`. Grafana *does* send a
  top-level `title`, so its notification is accepted and then collapsed into **one** board
  row, losing every per-alert instance. `grep -c alerts backend/providers/webhook.py` = 0.
- **The classes genuinely amenable to auto-resolution are narrow and identifiable**:
  provider-fingerprinted exception groups (Sentry/GlitchTip/Datadog ET), rule-based
  threshold alerts whose rule identity is server-stable (Alertmanager/Grafana/Zabbix
  trigger id), and synthetic/uptime checks with a debounce. Everything whose identity is
  a rendered string (Splunk saved searches, CloudWatch alarm titles, Netdata composite
  keys after digit-stripping) is not, because this app's fingerprint provably over-merges.
- **Proof it over-merges** (run against `compute_fingerprint` this session):
  `4xx error rate high` and `5xx error rate high` on `svc/api` both hash to
  `58538b8e259f59c9`; `p99 latency above 500ms` and `p50 latency above 100ms` both hash to
  `c4dbf4e759b19ceb`; `replication lag` on `shard-1` and `shard-47` both hash to
  `fbf3afe769949bba`. The ledger will hand a responder a fix learned from a different
  failure. Cause is the last entry of `_VOLATILE_PATTERNS` (`models.py:180-182`).
- **The safest write-back verb in the whole landscape is not in the vocabulary.**
  `VALID_ACTIONS` is `{ack, resolve, comment}` (`models.py:157`). Every low-risk provider
  write is a *time-boxed suppression* (Alertmanager silence with mandatory `endsAt`,
  Datadog mute with `end`, Icinga ack with `expiry`, Sentry archive with
  `ignoreDuration`). Concrete consequence in shipped code: `datadog.py:177-181` posts
  `/monitor/{id}/mute` with `body={}` — an **indefinite** mute — because there is no
  `duration_secs` in the contract to pass.
- **`ACTION_COMMENT` is fully implemented, correctly gated, and has zero callers of any
  kind** — not a button, not a cron, not an SOP step. `opsApi.action` is declared at
  `website/src/apps/ops-mission-control/api.ts:274-278` and referenced nowhere;
  `Incident.proposed_action` (`models.py:325`) is never assigned. The propose path
  terminates in a chat transcript.
- **The most-advertised channel is broken.** `app.json:18` sells "replyable Slack
  threads"; `investigate.md:10-12` asserts the mirror as fact. But nothing in the app or
  its skill ever calls the host's `slack-link` path (`set_slack_link|slack-link|SessionMap`
  → zero hits under `backend/` and `builtin_skills/ops-mission-control/`), so a reply into
  the board thread hits `get_session_for_thread() -> None` and is dropped silently.
- **There is a second output channel already sitting in the host, needing no credential,
  that the app ignores**: the local notification bus (`src/kiro_crew/notifications/bus.py:281`).
  Every app token may reach `POST /api/notifications/push` unconditionally
  (`src/kiro_crew/dashboard/token_auth.py:1110-1113`), it validates and redacts centrally
  (`dashboard/state.py:2371-2374`), it supports `group_key`, `ttl`, `priority`, up to 4
  actions and a dashboard-internal deep link, it rate-limits at 30 per 300 s
  (`notifications/rate_limit.py:15-16`), and it drives a real desktop toast
  (`website/src/hooks/useNativeNotification.ts`). `app.json` already asks for the
  `notification` event permission (`app.json:33-36`) and then never produces one. No
  builtin app declares `notifications.channels` — verified across all seven.
- **An unanswered incident is a silent black hole.** `_SWEEPABLE_STATUSES` is
  `{dispatched, investigating}` (`store.py:66`) and `store.py:307` skips everything else,
  so the `needs_human → stale` edge that `models.py:88-92` legalises *specifically* so
  "an incident nobody ever answers must not pin a signal as claimed forever" is never
  traversed. `dispatch.py:332-337` keeps counting it as owning its signal.
- **A postmortem renderer exists and is dead.** `store.write_log()` (`store.py:385-423`)
  renders the full Markdown artifact the module spec promises on disk
  (`docs/system-specs/modules/ops-mission-control.md:886`) and has exactly one reference in
  the repo: its own definition. `read_log` is served at `routes.py:277`, typed at
  `api.ts:259-260`, and `opsApi.incident(` is called by no component. Every one of the 26
  registered routes returns `web.json_response`.
- **Teams/Discord parity is not the win it looks like** — refuted below. `MessagingTransport`
  (`src/kiro_crew/messaging/transport.py:79-127`) has five abstract methods and **no edit
  primitive**; `rich_blocks=True` on Slack only. Routing the pin board there would degrade
  the edited-in-place board into the feed the spec names as the failure mode.
- Ranked value: ~~fix the Slack thread link~~ → ~~generalise the webhook envelope~~ → ~~add
  an exact provider-identity match layer~~ → ~~accept `ok` and stop resolving on poll
  failure~~ → ~~sweep `needs_human`~~ → ship the local notification channel →
  ~~wire `ACTION_COMMENT`~~ → land the artifact export. Sources beyond that (Alertmanager
  poller, `/metrics` scraper, declarative REST poller, ICS rota) are real but second-order
  once push ingest is generic.

  **Struck items shipped 2026-08-01** (plus §5.12's bounded-suppression verb, which the
  original ranking placed lower but which came along with the Datadog mute fix). Remaining,
  in order: the notification channel (§5.6), the artifact export (§5.8), then the
  verification/trust pair (§5.10 + §5.9). See §0 for the full status table and §7 for what
  the sequence looks like from here.

---

## 2. The emitter landscape

Popularity figures are the survey's; GitHub stars were verified 2026-08-01 via the API.
"Auto-resolution fit" answers one question only: *can this system's identity and state be
trusted enough to close work on it automatically?*

### 2a. Metrics and alerting

| System | What it emits | Poll or push | Auth | Free tier | Auto-resolution fit | Adapter effort |
|---|---|---|---|---|---|---|
| **Prometheus Alertmanager** (8.6k★) | `gettableAlert`: `labels`, `annotations`, `startsAt/endsAt`, `generatorURL`, server-computed `fingerprint`, `status.state` = active/suppressed/inhibited + `silencedBy`/`inhibitedBy` | **Both.** `GET /api/v2/alerts`; `webhook_config` POST (v4 envelope, `alerts[]`) | **None by default**; optional basic auth / proxy | Free, Apache-2.0, self-hosted | **Best in class.** Server-side stable `fingerprint`, explicit `resolved` status, server-side suppression state, and silences are mandatory-expiry | Small |
| **Grafana unified alerting** (75.9k★, #2 at 62.2% SoV) | Alertmanager-shaped webhook + per-alert `values` (the actual breaching numbers), `fingerprint`, `silenceURL`, `dashboardURL`. Poll: rules + instance state | **Both.** Webhook contact point HMAC-SHA256 over raw body (header `X-Grafana-Alerting-Signature`); `GET /api/prometheus/grafana/api/v1/rules` w/ service-account Bearer | Bearer, or webhook HMAC | Free self-hosted (AGPL); Cloud free tier | **Very strong.** Per-alert fingerprint + `values` = free evidence for sources that currently get none | Small |
| **Netdata** (80.0k★) | `chart.alarm` keyed alerts: `status` (CLEAR/WARNING/CRITICAL/UNDEFINED/UNINITIALIZED), `value`, `duration`, `non_clear_duration`, `silenced` | Poll `GET /api/v3/alerts` | **None by default** on localhost:19999 | Free, GPL-3.0 | Good source, poor auto-close: threshold-flappy, self-clears. Needs `non_clear_duration` debounce | Small |
| **Zabbix** (6.2k★, undercounted) | `problem.get`: `eventid`, `objectid` (trigger), `name`, `severity`, `acknowledged`, `suppressed`, `cause_eventid` (built-in cause/symptom correlation) | Poll JSON-RPC `/api_jsonrpc.php` | Bearer API token | Free, AGPLv3 | **Best full-loop.** `objectid` is a stable identity; `event.acknowledge` bitmask maps 1:1 onto ack(1)/close(2)/comment(4). `suppressed=1` = maintenance window | Medium |
| **Icinga 2** (2.2k★) | Host/service state + `acknowledgement`, `acknowledgement_expiry`, `downtime_depth`, `state_type` (hard/soft) | Both (`/v1/objects/*`; `/v1/events` long-poll stream) | Basic auth or client cert, mandatory | Free, GPLv2 | Good. `host!service` is a perfect identity; the canonical unhandled filter excludes acked + in-downtime **server-side**; hard-state filter kills transients | Medium |
| **Uptime Kuma** (89.7k★ — highest in survey) | Heartbeats + Prometheus gauges (`monitor_status`, `monitor_cert_days_remaining`) | Poll `/metrics` or status-page heartbeat JSON | API key / basic (metrics); status page public | Free, MIT | Read-only. Binary DOWN/UP, no diagnostic payload, flaps hard. **No ActionSink possible** (monitor CRUD is Socket.IO-only) | Small |
| **Checkmk** (2.3k★) | Host/service OK/WARN/CRIT/UNKNOWN + ack/downtime flags | Poll REST (`/{site}/check_mk/api/v1`) or Livestatus | `Authorization: Bearer $USER $SECRET` | Raw Edition free, GPLv2 | Workable, with a trap: commands dispatch **asynchronously** via Livestatus, so 2xx ≠ executed. An ActionSink must re-read state or `ActionResult.ok` lies | Medium |
| **Nagios Core** (2.0k★) | `statusjson.cgi?query=servicelist` — object *state*, not alerts | Poll CGI; write path is a **named pipe** | Whatever Apache enforces | Free, GPL-2.0 | Poor. No alert object; pipe writes return nothing so success is unknowable. **Cover via Icinga or the webhook instead** | Large |
| **Google Cloud Monitoring** | Read-only `Alert`: `state` OPEN/CLOSED, `openTime/closeTime`, `resource`, `metric`, `policy` | Poll `projects.alerts.list` | OAuth2 SA | Allotment (**unverified 2026 numbers**) | Clean state model, no write-back at all (alerts are read-only). Launch stage **unverified** — treat as possibly unstable. Page tokens expire in 72 h | Medium |
| **Azure Monitor** | Two-axis: system `monitorCondition` (Fired/Resolved) + user `alertState` (New/Acknowledged/Closed) | Both (AlertsManagement REST; action groups) | Entra OAuth2 + RBAC | Allotment (**unverified**) | **Best-designed autonomy surface**: `alertState` is a user axis the platform never overwrites, so an agent ack cannot mask a live fault. 30-day retention < ledger lifetime. REST api-version **unverified** (docs 404'd) | Medium |
| **Datadog** (shipped) | `overall_state` ∈ Alert/Warn/No Data/OK/Ignored/Skipped/Unknown | Poll `/api/v1/monitor` | `DD-API-KEY` + `DD-APPLICATION-KEY` | **No meaningful free tier** | Already shipped; mute is time-boxable via `end` — which the app does not pass | Small |
| **Elastic / Kibana** | Rules via `/api/alerting`; firing instances only as `.alerts-*` docs (`kibana.alert.status` active/recovered, `kibana.alert.reason`) | Poll, but **two APIs and two credentials** | ApiKey/basic + `kbn-xsrf` on writes | Basic tier free | Good once you commit to querying ES directly. One-active-doc-per-rule-group = free dedupe. Per-alert `_mute` + `snooze_schedule` are correctly scoped | Large |
| **Dynatrace** | Davis `problem` — pre-correlated, with `rootCauseEntity` | Poll `/api/v2/problems` | `Api-Token` + `problems.read` | Trial only | Best signal *quality*, worst audience fit. Field list **unverified** (docs 404'd). Don't auto-close: Davis owns lifecycle | Medium |
| **New Relic** | `aiIssues` via NerdGraph GraphQL; states CREATED/ACTIVATED/CLOSED | Poll GraphQL | `API-Key` header | **Genuinely usable free tier** | Moderate. Titles are correlation-generated and **mutate**, which is a fingerprint-stability hazard. Schema **unverified** (docs 404'd) | Large |
| **Splunk** | `alerts/fired_alerts` — triggered saved searches, payload is arbitrary SPL rows | Poll `:8089` | Basic / session / Bearer | Free tier has no auth at all | **Weakest fingerprint story in the survey.** Only stable identity is the search name; content has no schema. Only mutation is DELETE (destructive) | Large |
| **SolarWinds Orion** | `Orion.AlertActive` join: `AlertName`, `Severity`, `Acknowledged`, `AcknowledgedNote`, `ClearedTime` | Poll SWIS REST + **SWQL query strings** | Basic auth | **None** | Mechanically fine, strategically irrelevant here. Port moved 17778 → 17774 (2023.1). Embedded query = schema coupling + injection surface if ever interpolated | Large |

### 2b. Error tracking / exception aggregation

| System | What it emits | Poll or push | Auth | Free tier | Auto-resolution fit | Adapter effort |
|---|---|---|---|---|---|---|
| **Sentry** (44.4k★; 32% of SO-2025 respondents) | Issue: `id`, `shortId`, `title`, `culprit`, `level`, `status`, **`substatus`** ∈ new/ongoing/escalating/regressed/archived_*, `count`, `firstSeen/lastSeen`, `isUnhandled` | **Both.** REST `/api/0/organizations/{org}/issues/`; integration webhooks HMAC-SHA256 over raw body (`Sentry-Hook-Signature`) | Bearer, `event:read`/`event:write` | Yes (SaaS); self-hosted FSL but 16 GB RAM floor | **Strongest in survey.** Three stable identities (id, shortId, 32-hex grouping hash) *and* the only system that natively labels `regressed`. Poll can't see transitions; webhook can. Archive-with-`ignoreDuration` is the safest mute anywhere | Small–medium |
| **GlitchTip** (MIT, 512 MB) | Sentry-shaped issue; status only unresolved/resolved/ignored (**no `substatus`**) | Poll `/api/0/...` (Sentry-compatible) | Bearer token | Free, self-hosted | Excellent, and the **only** system where all three of this app's actions map to native writes — including a real `POST /api/0/issues/{id}/comments/`. Lowest blast radius anywhere (own box, no regression detector to poison). Best target for a real-backend integration test | Small if Sentry adapter is shared |
| **Datadog Error Tracking** | Issue: `error_type`, `file_path`, `function_name`, `first/last_seen_version`, `is_crash`, `state` ∈ OPEN/ACKNOWLEDGED/RESOLVED/IGNORED/EXCLUDED, and **`regression{resolved_at, regressed_at, regressed_at_version}`** | Poll `POST /api/v2/error-tracking/issues/search` | Reuses the two secrets already shipped | No | **Highest ROI extension**: adapter, secrets, `site` config and HTTP plumbing already exist. Grouping inputs arrive decomposed (service/type/file/function) — strictly better raw material than any title string. `EXCLUDED` stops ingestion irreversibly → must never be exposed | Small |
| **Rollbar** | Item: `id`, `counter`, `hash`, `title`, `level`, `status` active/resolved/muted/archived, `resolved_in_version`, occurrence counts | Poll `X-Rollbar-Access-Token` | Project token, `read`/`write` | Yes | Good. Fingerprint = SHA1 over all frame filenames + methods, line numbers excluded; environment is a hard pre-partition (matching that in `resource` is *correct*). `total_count` is NULL above 100k items — never page on it | Medium |
| **Bugsnag / Insight Hub** | Event with `grouping_reason` and `grouping_fields` — the vendor tells you exactly which fields it grouped on | Poll; documented `X-RateLimit-*` | Token | Limited | Best grouping introspection of any commercial vendor: you can normalise on the vendor's own inputs. PATCH verbs fix/ignore/snooze/open with explicit `reopen_rules` | Medium |
| **OpenTelemetry Collector / OTLP** (CNCF graduated, 78% production use) | Not a source — the universal **ingress**. Exception semconv: `exception.type`, `exception.message`, `exception.stacktrace` + Resource + Scope | Push (collector → HTTP exporter) | Whatever the receiver enforces | Free | Read-only by construction (no write-back → no autonomy risk). Fingerprint from Resource + scope/event name + `exception.type` + top in-app frame; **never** from Body or stacktrace text | Medium (it's a webhook shape problem, not an adapter) |

### 2c. Paging / ITSM / CI-CD

The survey's paging tier is thin because PagerDuty is already shipped (built, never
exercised against a real tenant — `planning/features.md:901`) and Opsgenie appears exactly
twice in the whole repo history, both in ideation. CI/CD is **unverified**: no
GitHub Actions / GitLab CI / Jenkins emitter analysis was supplied, and I did not run one.
Treat "CI-CD-infra" as an unresearched class rather than an empty one. The one thing the
code analysis does establish about CI-shaped signals is that the grammar cannot hold them:
a failed job has no field for run id, branch, commit, duration or step — only free-text
`title` + `resource` + string labels (`models.py:243-256`, `from_dict` stringifies every
label value at `:306`).

### The ones that unlock the most for the least work

1. **Alertmanager-shaped push, via the ingress you already ship.** Grafana + Alertmanager
   + every forwarder in between share one envelope. The HMAC scheme
   `verify_signature` already implements (`webhook.py:63-74`) is byte-for-byte the scheme
   Grafana's contact point uses — different header name, same construction. This is one
   file's worth of work for the largest slice of the landscape.
2. **Sentry, once, covering three products.** One adapter with a configurable `base_url`
   and the same field extraction serves Sentry SaaS, Sentry self-hosted, and GlitchTip.
   GlitchTip additionally gives you a docker-composable 512 MB real backend to test the
   write path against, which the repo's own testing convention prefers over fakes.
3. **Datadog Error Tracking as an extension, not an adapter.** Same secrets, same `site`
   config (`datadog.py:76-77`), same `request_json`. Issues are a strictly better signal
   than monitor state, and `regression{}` is the single best ledger-feedback object in the
   landscape.
4. **Alertmanager poll for the credential-free case.** Empty `secret_fields`, one URL
   config field, server-supplied `fingerprint`, and `status.state` gives you the
   server-side noise filter the claim loop has no equivalent of. Cost: it needs a
   loopback carve-out in `providers/http.py:39`, whose https-only rule is deliberate and
   test-pinned (`tests/test_providers.py:675-685`) — arguable, because that call carries
   no credential, but it must be argued and tested, not slipped in.

### Which classes are genuinely amenable to auto-resolution

This is the actual question, so directly: **three classes, and only under stated
evidence.** The gating property is not "is this alert repetitive" — it is "does the
provider hand me an identity I did not have to guess, and a state transition I did not
have to infer from absence."

**Class A — provider-fingerprinted exception groups. Safe.**
Sentry, GlitchTip, Datadog ET, Rollbar, Bugsnag. Evidence that makes them safe:
- The provider computes the group identity and publishes it (Sentry issue id / shortId /
  32-hex grouping hash; Datadog `issue_id` UUID; Rollbar item id + counter). No heuristic
  is involved, so a match is *the same failure*, not *a similar-shaped string*.
- The provider publishes state transitions, not just current state: Sentry
  `substatus=regressed`, Datadog `regression{resolved_at, regressed_at,
  regressed_at_version}`. That means "the fix did not hold" is an ingested fact.
- The safe action is a **time-boxed archive/mute**, not a resolve. A wrong
  `ignoreDuration` expires by itself; a wrong `resolve` on Sentry is worse than
  mis-labelling because it arms the regression detector and corrupts the classification of
  the *next* occurrence.
- Firm exclusion: Datadog `state=EXCLUDED` stops ingesting errors and drops them from
  billing. The un-collected window is unrecoverable. It should not be gateable — it should
  be absent from `supported_actions()`.

**Class B — rule-based threshold alerts with a server-stable rule identity. Safe with a
suppression check.**
Alertmanager, Grafana, Zabbix, Icinga, Azure Monitor. Evidence:
- Identity comes from the rule/trigger, not the rendered message: Alertmanager
  `fingerprint`, Zabbix `objectid`, Icinga `host!service`, Azure rule-name +
  `targetResource`. This matters *specifically because* of the collision result above —
  a Zabbix problem name embeds macro-expanded values that would fingerprint into mush.
- Resolution is explicit (`status: resolved`, `endsAt`, `monitorCondition: Resolved`), so
  closing an incident is positive evidence rather than an inference from a poll that might
  simply have failed.
- Provider-side suppression is readable and must be honoured: Alertmanager
  `status.state=suppressed` + `silencedBy`/`inhibitedBy`, Zabbix `suppressed=1`, Icinga
  `downtime_depth`. Investigating something a human explicitly parked is the behaviour that
  destroys trust fastest.
- Safe action is a short silence/mute with a mandatory expiry, plus an attributed comment.
  Not close. Zabbix close only works on triggers with manual-close enabled and hides a
  live fault until the trigger re-fires; Grafana rule `_pause` blinds the operator with no
  expiry at all.
- Azure is the standout for autonomy specifically because `alertState` is a *user* axis
  the platform never overwrites — an agent ack there cannot race the platform's own
  `monitorCondition`.

**Class C — synthetic / uptime checks, with a debounce. Safe but low value.**
Uptime Kuma, Netdata, CloudWatch uptime-style alarms. Evidence: identity is stable
(`monitor_name` + `monitor_url`; `chart.alarm`), and the condition genuinely self-clears.
The required evidence is *K consecutive failing observations before claiming*, and
Netdata hands you `non_clear_duration` / Icinga hands you `state_type=hard` to do it with.
Without a debounce these flood the board and every occurrence inflates a ledger entry's
`use_count`. Value is low because a Kuma signal carries no diagnostic payload — it is a
trigger to gather evidence elsewhere, not self-describing work.

**Classes that are NOT amenable, and why:**
- **Log-search alerts** (Splunk `fired_alerts`, Elastic security detections used loosely):
  the only stable identity is the saved-search name and the payload is arbitrary
  user-defined rows. Every signal fingerprints to "this search fired," which collapses
  distinct failures into one ledger entry. The only mutation Splunk offers on a fired
  alert is DELETE.
- **Correlation-engine issues whose titles mutate** (New Relic `aiIssues`): a title that
  accretes incidents is a fingerprint that drifts, which is exactly the failure mode
  `compute_fingerprint`'s docstring says would make the ledger useless.
- **Anything whose lifecycle the platform owns** (Dynatrace Davis problems, Datadog
  monitors): an agent close both loses information and races the platform's own state
  machine.
- **Absence-of-data as a firing condition.** Already decided in-tree
  (`planning/features.md:979-990`): CloudWatch `INSUFFICIENT_DATA` behind an opt-in,
  default off. `cloudwatch.py:154-188` implements that by relabelling it `firing` and
  keeping the truth in `labels['state']`; Datadog excludes `No Data` from
  `_OPEN_MONITOR_STATES` (`datadog.py:63`). Not a gap — a chosen semantics.

**The one cross-cutting prerequisite.** None of Class A or B is safe on this app *today*,
because the ledger can only match on the fuzzy shape hash, and that hash provably merges
distinct failures (three verified collisions above). Provider identity already reaches the
app for every shipped adapter — `cloudwatch` `alarm/<name>`, `datadog` `monitor/<id>` +
`labels['dd_monitor_id']`, `pagerduty` `labels['pd_incident_id']`, `github` `repo` +
issue number, `webhook` `payload['fingerprint']` (`webhook.py:113`) — and
`ledger.match` (`ledger.py:187-205`) cannot see any of it.

---

## 3. Communication methods

| Channel | Edit-in-place | Threading | Inbound replies | Credential / inbound URL | Cost | Verdict |
|---|---|---|---|---|---|---|
| **Slack pin board** (shipped) | **Yes** — `client.update_message` (`slack_out.py:215`), the board's load-bearing property | Yes (board ts) | **No** — the ts is never registered with the host session map | Borrows host `slack_client`; no token of its own (guarded by `tests/test_slack_out.py:130`) | Shipped | Keep. Fix the reply path (§5.1) |
| **Slack investigation thread** (via host `slack-link`) | n/a (chat mirror) | Yes | **Yes**, when linked | Same | Prose-only instruction (`sops/dispatch.md:45-46`), no endpoint named | Make it code, not prose |
| **KiroCrew local notification bus** | No (but `group_key` collapses; `ttl` expires) | No | No (up to 4 actions + a dashboard-internal deep link) | **None.** Every app token may POST `/api/notifications/push` (`token_auth.py:1110-1113`) | Small: an `app.json` `notifications.channels` block + one call site | **Build this.** The only credential-free push the app is missing |
| **Desktop / OS toast** | No | No | No | Rides the bus (`website/src/hooks/useNativeNotification.ts`, browser permission) | Free once the bus is used | Comes with the bus. This is what reaches an operator who is not looking at the dashboard |
| **Dashboard board + Handover tab** (shipped) | Yes | n/a | Yes (embedded chat) | None | Shipped | Pull-only by design; `HandoverPanel.tsx:72-75` has no refetch interval, deliberately |
| **Teams / Discord via host `MessagingTransport`** | **No — no edit primitive exists** in the ABC or in any of the seven implementations | Yes (`thread_id`) | Yes | Host transport already configured; no new credential | Requires a **core messaging** change (add edit to the ABC + N transports) | **Refuted as an ops change.** See §6 |
| **Email / SMTP** | No | Weak | Weak | Needs a *new* credential — which `TestNoTokenOfItsOwn` forbids the module from holding, and `smtplib` is on the blocked-import list (`src/kiro_crew/skills_script_validator.py:105`) | Build a host transport first | No |
| **ntfy / Gotify / Pushover** | No | No | No | New credential/endpoint; no host client to borrow | Host transport first | No — the bus already covers "reach me on this device" |
| **Outbound webhook (app → foreign system)** | n/a | n/a | n/a | Operator-supplied URL; egress surface | Small | Only as `ACTION_COMMENT` write-back into the originating provider (§5.7), not as a generic egress |
| **Local Markdown artifact** (`store.write_log`, dead) | n/a | n/a | n/a | **None** — a local file the user chooses to share | Renderer already written | **Build this.** Cheapest unshipped capability in the app |
| **RSS / status page / iCal / CSV** | n/a | n/a | n/a | Would need hosting | New egress + hosting posture | No. `spec:590` declines hosting ingress/tunnels; the same reasoning applies outward |

### The 2–3 worth building next, and why

**1. The local notification bus — build first.** For a laptop-resident, local-first app
where a public inbound URL is a dealbreaker, this is the only channel that is *already
inside the trust boundary*:
- **No new credential.** Grant is structural: `app_token_path_allowed` returns True for
  `/api/notifications/push` for every app, and the handler independently enforces app
  identity from the verified token, manifest-declared channels, and per-app rate limits
  (`dashboard/handlers/notifications_push.py:110-145`).
- **No inbound URL, no tunnel, no port.** Delivery is SSE/WS to the local dashboard plus
  a browser `Notification` toast.
- **It already redacts.** `_deliver_note` scans every string value recursively before SSE
  or disk (`dashboard/state.py:2371-2374`), so it is not a new redaction boundary to
  register the way an HTTP text egress would be.
- **Its semantics fit the app's noise discipline exactly.** `group_key` collapses repeats
  (so a nudge is not a new row), `ttl` expires passive rows, `priority` ∈
  critical/default/passive maps onto severity, `url` must be a dashboard-internal path
  (`notifications/bus.py:105-111`) so the note can deep-link to `/ops-mission-control`,
  and the 30-per-300 s limiter (`rate_limit.py:15-16`) puts a hard ceiling on any nudge
  bug you write.
- **It closes the two silent-failure holes.** `needs_human` with no answer, and "nothing
  is watching" from `handover.coverage` (`handover.py:150-164`) — the two facts nobody
  currently learns unless they open a tab.
- Two honest caveats. (a) `app.json` currently declares no `notifications` block; the
  manifest schema caps an app at 8 channels (`apps/manifest.py:673`), so pick a small set
  (`incident`, `waiting-on-you`, `coverage`) deliberately. (b) A builtin whose routes are
  registered in-process on the gateway router could call
  `state.notification_bus.push(...)` directly and thereby bypass both the manifest-channel
  check and the rate limiter — do not do that silently. Declare the channels, register
  them, and either go through the HTTP endpoint or replicate the limiter.

**2. Make the Slack board thread actually replyable — build second, and it is a bug fix,
not a feature.** It needs no new credential (the ts already exists, `slack_out.py:229`),
no new channel, and it converts the app's single most-advertised capability from a silent
drop into the working bidirectional path `investigate.md:10-12` already claims it is. It
is also the load-bearing dependency of any escalation story: there is no point nudging
into a thread nobody can answer.

**3. The local Markdown artifact — build third.** `write_log` already exists and already
produces the structure the spec documents at `spec:886`. It needs no credential and no
network. Two corrections to the obvious framing: it is *not* "the artifact a git-synced
team diffs" — `ledger_sync.py:193` tracks only `ledger.jsonl`, `rotation.yaml`,
`.gitignore` on purpose, so incident files never sync; and it is *not* redaction-safe by
construction — the `gather_evidence` chokepoint (`registry.py:302`) redacts evidence
bodies, while signal titles/resources are redacted only at the Slack egress
(`slack_out.py:168`/`:177`). A local file under the 0o700 incidents dir
(`store.py:53-58`) is fine; any HTTP download route for it is a new egress boundary that
must redact and register in `src/kiro_crew/security_posture.py` beside the entry at `:170`.

**Which need no new credential:** the notification bus, the Slack thread link, the local
Markdown file, and Teams/Discord *if* the host contract ever gains an edit primitive.
Everything else in the table needs a credential the app is explicitly forbidden to hold.

---

## 4. What the app is today

**Provider surface.** Four `@runtime_checkable` Protocols in `backend/providers/base.py:169-247`
(`SignalSource`, `RotationSource`, `ActionSink`, `EvidenceSource`); nothing ever
`isinstance()`s them — the registry duck-types via `getattr`, and four load-bearing
attributes (`config_fields`, `secret_fields`, `detail`, `is_fallback`,
`evidence_budget_hint`) are undeclared conventions read with defaults at
`registry.py:145-148` and `:227`. An adapter that omits `config_fields` silently becomes
unconfigurable through the only UI that exists. `_install_public_adapters`
(`registry.py:388-410`) registers 5 signal sources, 4 action sinks, 3 rotation sources (2
real), and **2 evidence sources**, both hard-gated to their own signal source
(`cloudwatch.py:267`, `datadog.py:215`) — so PagerDuty, GitHub Issues and webhook
incidents get literally zero evidence.

**Polling.** `poll_all` (`registry.py:171-206`) fans out under `asyncio.gather` with a
15 s per-source timeout and a 100-item truncation (`base.py:45,50`), returning
`(signals, errors)`. Neither constant is operator-configurable: both production call
sites pass nothing (`dispatch.py:318`, `routes.py:440`), and `PUT /settings`
(`routes.py:530-553`) accepts only six keys. There is no pagination, no cursor, no
watermark, and no per-source "last successful poll" state anywhere — `NextToken` is never
read (`cloudwatch.py:160,169-174`), PagerDuty passes `limit` with no `offset`
(`pagerduty.py:110-118`). There is no 429/`Retry-After`/backoff handling at all:
`HttpError.status` is set at `http.py:47` and read by nobody, so a rate limit is
indistinguishable from a bad token and the next 120 s tick re-polls at full rate.

**Signal grammar.** Ten flat fields plus `dict[str,str]` (`models.py:243-256`).
`VALID_SEVERITIES` = 3 buckets (`:38`), `VALID_STATES` = `{firing, ok, unknown}` (`:43`),
and `run_cycle` claims only `firing` (`dispatch.py:319`) — so every third vendor state
collapses to `unknown` and vanishes. `STATE_OK` appears in exactly three places
(`models.py:41`, `:43`, `:222`) and is emitted by **zero** adapters: all five hardcode
`state=STATE_FIRING` (`cloudwatch.py:188`, `datadog.py:135`, `github_issues.py:175`,
`pagerduty.py:137`, `webhook.py:119`). The only way the app learns a signal cleared is its
absence from a poll, which is what a 429 also looks like.

**Fingerprint.** `compute_fingerprint` (`models.py:226-240`) hashes
`source|normalize(title + " " + resource)` — severity, labels, state, native id, url and
`fired_at` are all excluded — and the last of seven volatile patterns replaces every bare
digit with `#` (`:180-182`). Verified collisions this session: 4xx vs 5xx on one resource
→ `58538b8e259f59c9`; p99-500ms vs p50-100ms → `c4dbf4e759b19ceb`; shard-1 vs shard-47 →
`fbf3afe769949bba`. `tests/test_models.py:15-52` asserts the *desired* collapses and no
test asserts that two different numbered resources stay distinct.

**Ledger and fast path.** `ledger.match` linear-scans for `fingerprint in e.fingerprints`
(`ledger.py:187-205`), caps at 3, and `is_fast_path` (`:208-215`) is satisfied by **any
single** `verified`+`high` entry with **no `use_count` floor** — so one hand-POSTed entry
via `POST /ledger` (`routes.py:701-721`, which takes `confidence`/`trust` verbatim) is
instantly fast-path. `record_use` increments unconditionally at *claim* time
(`ledger.py:218-240`, called from `dispatch.py:267-274`), so `use_count` means "was shown
to an investigation," and `ledger.match` then ranks by it. Confidence moves down in
exactly one place: `hygiene`'s one-step decay after 90 days of **non-use**
(`ledger.py:335-349`). `upsert`/`_reconcile` take the strongest of two values by design
(`:136-138`, `:172-184`). The shipped answer to "the fix was wrong" is an agent-authored
corrective entry surfaced by `find_contradictions` (`ledger.py:252-304`,
`investigate.md:135-139`) — detect-don't-decide, chosen deliberately.

**Autonomy gate.** `authorize_action` (`rotation.py:213-267`) checks action validity →
not-definitely-off-shift → app mode == `act` → a matching rule with `mode == act` → that
rule permits the action, auditing every decision. The rule grammar is four things:
exact `source`, `resource_glob` (fnmatch), all-keys-equal `label_match`, and `actions`
(`rotation.py:89-150`). It cannot express time-of-day, a rate cap, a severity ceiling, a
blast-radius cap, a dry-run-first, or a require-N-prior-successes — and no success data
exists to require. A blanket `act` rule with neither glob nor label is refused
(`rotation.py:120-126`), but `resource_glob: "*"` passes and is even asserted-accepted
(`tests/test_store_and_gate.py:472-481`). There is no writer for `autonomy_rules`: not
`PUT /settings`, not `PUT /providers/{id}/config` (no adapter declares the key), and no UI
— `SettingsPanel.tsx:397` renders "No rules defined yet." with no way to add one. So
selecting `act` in the UI yields propose behaviour, and the only path is hand-editing
`data/config.json`, which is agent-writable while the provider tokens are not.

**Action execution.** `POST /incident/action` (`routes.py:380-431`) is the only write path,
gates before touching a sink, defaults to the signal's own sink then `noop`, audits, and
returns. It has zero automated callers: no cron message references it, no SOP invokes it,
and `opsApi.action` (`api.ts:274-278`) is called by no component. `Incident.proposed_action`
(`models.py:325`) is never assigned. Nothing re-reads the signal afterwards, so
`ActionResult.ok` means only "the HTTP call returned 2xx" — which Checkmk's docs
explicitly warn is not the same as executed.

**Output surface.** One module, `backend/slack_out.py` (281 lines), with no Protocol/ABC
(zero `Protocol|ABC|abstractmethod` hits), literal Block Kit at `:181-195`, Slack emoji
per status at `:70-78`, two hardcoded settings keys at `:64-65`, and a client acquired as
`getattr(state, "slack_client", None)` at `:113-121`. Every send is swallowed
(`:219-226`, `:231-234`, `:264-270`) — deliberately: "notifying is not the work"
(`spec:777-779`). `publish()` fires only on a status change, so a parked incident's line is
never re-posted. The Slack channel identity leaked into the domain model:
`Incident.slack_thread_ts` (`models.py:322`), parsed at `:350`, writable over HTTP at
`routes.py:290`, typed at `api.ts:46`.

**Escalation.** None. `grep -rn 'nudge|re-page|reminder|autonudge' backend/` returns
nothing while the host ships `src/kiro_crew/autonudge.py` untouched. `STATUS_ESCALATED`
has zero Python writers (`models.py:50/86/91/101`, `handover.py:128` read,
`slack_out.py:75` glyph) and is terminal, so escalating *removes* the incident from
`open_incidents` and from the sweep. `_SWEEPABLE_STATUSES` (`store.py:66`) excludes
`needs_human`, so the edge legalised at `models.py:88-92` and restated at `spec:698` is
never traversed; the only guard, `tests/test_slot_watch.py:76-84`, asserts the dict's
contents and never exercises the sweep.

**Handover.** A read-only projection computed per request (`handover.py:24-27`), delivered
by exactly one `GET /handover` (`routes.py:914`), rendered in a tab with no refetch
interval, pushed only by a human clipboard click (`HandoverPanel.tsx:84-93`) or an agent
hand-pasting `text`. `sops/handover.md:2` is `cron: null` on purpose. `slack_out.py` has
no digest function.

**External rendering.** Nothing. All 26 registered routes (`routes.py:906-931`) return
`web.json_response`; zero `web.Response(`. `store.write_log` (`store.py:385-423`) has one
reference in the repo — its own definition — so the file `spec:886` documents
(`incidents/<id>.md`) is never created. `read_log` is served at `routes.py:277` and the UI
never calls it.

---

## 5. Verified gaps, most valuable first

### 5.1 The Slack board thread is not replyable, and the failure is silent

**STATUS: FIXED (2026-08-01).** `slack_out.link_thread_to_investigation()` registers the
board ts against the incident's slot through `DashboardState.link_slack`, in-process for the
same reason `_slot_state` is. Called from `_handle_transition` rather than at claim time,
because the slot does not exist yet when the incident is claimed — the dispatch SOP creates
it and reports the key on its first transition. The route returns
`slack_thread_replyable` so a caller can tell whether a reply will land instead of assuming
it, and the function returns `False` on a missing slot rather than claiming success. The
vague prose instruction in `dispatch.md` ("link it to the ops channel", naming no endpoint)
is replaced by the mechanism, so there is one path and not two. 8 tests in
`TestBoardThreadIsReplyable` cover the link, an explicit slot key, no-thread-yet, missing
slot, a host without the method, a host that raises, and Slack disabled.

**Claim.** `slack_out.py:238` stores the board ts only on the app's private incident
record. Nothing in the app or its skill ever calls the host's `slack-link` path — grep for
`set_slack_link|slack-link|SessionMap` under `backend/` and
`builtin_skills/ops-mission-control/` returns nothing — so the host's `_thread_to_session`
index (populated only by `dashboard/chat_slack.py:119`) never learns it. Inbound routing
depends on `sessions.get_session_for_thread(reply_ts)` (`slack/handler.py:2907`); with no
entry, and with the default channel activation `mention` (`config/loader.py:2274`), the
message returns at `slack/events.py:2143` *before* the authorization ephemeral at `:2154`.
No error, no ephemeral, silence.
**Why it matters.** This is the app's only bidirectional channel and its headline claim
(`app.json:18`). A silent drop on the operator's most natural action is worse than having
no Slack at all, because they believe they answered. It is also the prerequisite for any
escalation or `needs_human` nudge.
**Sketch.** After the board post succeeds in `slack_out.publish`, call the host link path
in-process for the incident's slot key (`ops-mission-control-<incident_id>` — the
convention already fixed at `IncidentChat.tsx:29-31` and `dispatch.md:42-43`), guarded by
`getattr` so a host without it is a no-op. Then delete the prose instruction at
`dispatch.md:45-46` so there is one mechanism rather than two, and pin the pairing. Note
the SOP→route contract scanner (`tests/test_config_routes.py:571-620`) only covers ops API
paths, so it structurally cannot catch this — the pin needs to be a direct assertion.
**Effort.** Small. **Queued?** No — absent from the queue and from "Declined (and why)".

### 5.2 The webhook envelope is single-shape, single-signal, firing-only

**STATUS: FIXED (2026-08-01) — except the auth-mode decision, which is still open.**
`signals_from_payload()` accepts the Alertmanager/Grafana v4 body and fans `alerts[]` out
one Signal per alert, falling back to the flat envelope unchanged so every existing sender
keeps working. Title falls back `annotations.summary` → `description` → `title` →
`labels.alertname`; resource from `instance`/`job`/`pod`; url from
`generatorURL`/`panelURL`; `commonLabels` merge *under* per-alert labels; Grafana's
per-alert `values` are kept as a label. The provider's own `fingerprint` becomes both
`provider_key` (§5.3) and the `native_id`, so re-deliveries dedupe. State is read through
`normalize_state`, so a sender can now retract work — and an unrecognised value becomes
`unknown` rather than manufacturing phantom firing work. A per-alert `status` beats the
envelope's. Fan-out is bounded by the existing `MAX_QUEUED_SIGNALS`, and one malformed
entry is skipped rather than failing the delivery: one bad alert in a group of forty must
not discard the other thirty-nine. HMAC path and check order untouched. 14 tests in
`TestAlertmanagerEnvelope`. Verified live: one POST → 2 signals, one `firing` one `ok`.

**Still open, and it is a posture call not a shape one:** the caveat below stands in full.
Alertmanager's `webhook_config` cannot HMAC-sign a raw body and Grafana signs
`X-Grafana-Alerting-Signature`, so neither works against this fail-closed ingress without
an accepted-signature-header list or a bearer mode. Deliberately not decided here.

**Claim.** Three defects in one 176-line file. (a) `signal_from_payload` requires a
top-level `title`/`summary` (`webhook.py:110-112`), so a raw Alertmanager v4 body — which
carries only `commonAnnotations`/`groupLabels` — is rejected 400, despite `webhook.py:3-4`
and `:166` both naming Alertmanager as a supported sender. (b) `enqueue` appends exactly
one Signal per POST (`webhook.py:143-147`), so Grafana's notification (which *does* carry
a top-level `title`) collapses into one board row and loses every per-alert instance in
`alerts[]`. (c) `state=STATE_FIRING` is passed literally (`:119`) with no `state`/`status`
key read, so a sender can create work but never retract it.
**Why it matters.** This is the highest reach per line of code in the landscape, through
an ingress that already exists and already HMAC-verifies. Grafana's per-alert `values`
map arrives with the actual breaching numbers — free evidence for the three signal
sources that currently get none. And accepting `resolved` turns reconcile from inference
into positive evidence.
**Sketch.** Add `signals_from_payload(payload) -> list[Signal]`: if `payload["alerts"]` is
a list, iterate it (bounded by the existing `MAX_QUEUED_SIGNALS=200` / `MAX_BODY_BYTES=256 KB`
caps), deriving title from `annotations.summary` → `labels.alertname`, resource from
`labels.instance`/`labels.job`, url from `generatorURL`, severity from `labels.severity`
through `normalize_severity`, `fired_at` from `startsAt`, merging `commonLabels` under
per-alert labels; otherwise fall back to the current single-envelope path unchanged. Read
`payload.get("state") or payload.get("status")` through `normalize_state`. Keep
`Signal.create(...)` as the only constructor. HMAC path and check order untouched.
**Caveat that must ship with it.** Envelope shape alone does not make either sender work
out of the box against this fail-closed ingress: Alertmanager's `webhook_config` supports
basic / bearer / authorization headers but **cannot** HMAC-sign the raw body, and
Grafana signs into `X-Grafana-Alerting-Signature`, not `X-OMC-Signature`
(`webhook.py:50`). So the shape work is necessary and not sufficient — plan an accepted
signature-header list, and decide explicitly whether a bearer/basic verification mode is
acceptable. That is a security decision, not a shape decision.
**Effort.** Small (shape) + small (auth modes, but needs a posture call).
**Queued?** No. The nearest non-goal, `spec:590` "Hosting webhook ingress or any tunnel,"
declines *hosting* — not envelope shape or fan-out.

### 5.3 The ledger has no exact-identity match layer, and the shape hash over-merges

**STATUS: FIXED (2026-08-01).** `Signal.provider_key` + `LedgerEntry.provider_keys`, both
defaulting empty so every incident and ledger line written before they existed stays valid
and keeps matching by shape alone. Keys are namespaced `"<source>:<key>"` so two providers
cannot collide on a bare numeric id, and set from explicit adapter input, never derived — a
derived value would be another heuristic wearing the word "exact". All four shipped signal
adapters now pass one: `cloudwatch` `region/alarm-name`, `datadog` `monitor/<id>`,
`github-issues` `repo#number`, `webhook` the sender's `fingerprint`. PagerDuty passes
`incident/<id>` with an honest comment that it identifies the *occurrence*, not the
recurring failure, so it will not generalise across occurrences.

`ledger.match(fingerprint, provider_key=...)` ranks exact above shape regardless of trust
or use count, `is_exact_match()` lets a caller say which kind it is showing, and the brief
now prints "exact provider identity" vs "same shape (heuristic — verify it is really this
failure)". `exact_match_ids` is captured *before* `record_use` binds the key, or every
match would look exact from the second occurrence onward and the distinction would erase
itself. `record_use` and `upsert` union the new field exactly as they do `fingerprints`,
preserving the git reconciliation property.

**One thing the fix added that the gap did not ask for:** `MAX_KEYS_PER_ENTRY` (200,
newest kept) bounds both key lists. Required, not tidiness — PagerDuty's per-occurrence id
would otherwise append a key on every recurrence and grow one JSONL line without limit, in
a file that is git-synced across a team and read into a model prompt.

The collision itself is now pinned as a test (`TestShapeHashOverMerges`, 12 tests) rather than left
as a claim in this document, so nobody later "fixes" the hash and quietly removes the
reason the exact path exists.

**Claim.** `compute_fingerprint` excludes labels, severity and native id, and strips every
bare digit (`models.py:180-182`, `:235-240`). Verified: `4xx error rate high` and
`5xx error rate high` on `svc/api` both hash to `58538b8e259f59c9`. Meanwhile a stable
provider identity already reaches the app for every shipped adapter (`alarm/<name>`,
`monitor/<id>`, `dd_monitor_id`, `pd_incident_id`, repo+issue number,
`payload['fingerprint']`) and `ledger.match` (`ledger.py:187-205`) cannot see any of it —
`LedgerEntry` (`models.py:362-375`) has `fingerprints: list[str]` and no second key.
**Why it matters.** The ledger's whole proposition is that the second occurrence gets the
first one's fix. A hash that merges distinct failures hands a responder a fix learned from
a different problem — worse than no match — and it is exactly what unlocks the fast path,
which has no `use_count` floor. Every serious platform in the survey has solved grouping
and publishes the result; Sentry even normalises frames and strips revision hashes for the
same reason this app strips digits.
**Sketch.** Additive, so nothing on disk changes meaning: `provider_key: str = ""` on
`Signal` (set from explicit adapter input, never derived) and `provider_keys: list[str]`
on `LedgerEntry`, both defaulting empty so every existing line stays valid and every
stored `fingerprints` value keeps working. In `ledger.match`, try exact `provider_key`
first, fall back to the fingerprint scan, rank exact above shape, and surface *which kind*
matched in `ClaimedIncident` so the brief can say "exact provider identity" vs "same
shape" — the same separation reasoning that keeps `similar` out of `matches`.
`record_use` unions the new key exactly as it unions fingerprints today, preserving the
git-merge union property. Persisted-schema change → spec in the same commit.
**Effort.** Medium. **Queued?** No, and not declined — the existing design note constrains
what the basis must *exclude*, not the addition of an orthogonal exact key.

### 5.4 Reconcile resolves live incidents on a failed poll

**STATUS: FIXED (2026-08-01), and it took code as well as SOP text.** The gap called this
"cheaper than it looks — achievable by editing `reconcile.md` alone", because `/signals`
already returned `errors`. That was true but not sufficient: `errors` tells you a source
failed *this* call, and the question reconcile actually has to answer is "did the poll that
would have reported this signal succeed". So `registry.poll_health()` now records, per
source, whether the last poll attempt succeeded, with the reason and timestamp — and a
source **absent** from that map is explicitly "cannot conclude", not "healthy".

`/signals` exposes `poll_health` plus a single `all_sources_healthy` boolean, and
`reconcile.md` now requires consulting it: resolve on absence only when that signal's own
source polled OK, and resolve directly for signals in the new `cleared` list, since an
explicit provider `ok` is positive evidence rather than an inference. Added to the SOP's
Rules as the one rule whose violation destroys real work rather than leaving the board
untidy.

**The second-order bug the gap predicted was real and is fixed in the same change.**
`/signals` returned every signal regardless of state while `dispatch.run_cycle` claims only
firing ones — harmless until an adapter could emit `ok`, which §5.2 now makes possible. The
route exposes a state-filtered `firing` list, `unclaimed` derives from it, and `cleared` is
separate.

**Also fixed: the 429 blindness underneath it**, which the report flagged separately (§4,
and its own confirmed gap). `HttpError.status` was assigned and read nowhere, so a
rate-limited provider was re-polled at full rate every 120 s — how a rate limit becomes a
ban. `HttpError` now carries a clamped `retry_after` (parsed where the response headers
still exist) and `is_retryable`; a failed source backs off and says so in `errors`, and a
success clears it. Every failure backs off, not only retryable ones: a source failing on a
401 every cycle burns the heartbeat re-learning the same thing. What `is_retryable` buys is
honouring the provider's *own* delay, so only that path can exceed the default. Monotonic
deadlines, so a clock adjustment cannot strand a source. 10 tests across
`TestPollHealthAndBackoff` (7) and `TestRetryAfterParsing` (3), including the one that
matters most — a quiet source and a broken source both contribute zero signals and are now
distinguishable.

**Claim.** `poll_all` contributes zero signals for a failing source and reports the reason
in `errors` (`registry.py:187-206`). `sops/reconcile.md:33-44` instructs the agent to
resolve any open incident whose signal is "no longer in the firing set," with resolution
text "signal cleared at the provider — no longer firing," and never consults `errors`
(grep across `sops/*.md` + `SKILL.md` hits only `dispatch.md:30`). `resolved` is terminal
with no edge out, so recovery requires the signal to fire again as a brand-new incident.
**Why it matters.** A 429 or a provider outage silently closes real unresolved work with a
resolution string that asserts something false. This is the most damaging *correctness*
bug in the loop, and it compounds with the 100-item page cap: a signal that falls off page
1 because the storm grew reads identically to "cleared."
**Sketch.** Cheaper than it looks: `routes.py:445-449` already returns `errors` from
`registry.poll_all()`, so "resolve only on an explicit `ok`, or on absence **plus** a
successful poll for that source" is achievable by editing `reconcile.md` alone. No new
endpoint. **One second-order bug the fix exposes:** `/signals` returns the unfiltered
`signals` list (`routes.py:445-447`) while `dispatch.py:319` filters to firing — so once a
webhook can deliver `state=ok` (§5.2c), that signal appears in the very list
`reconcile.md:37` treats as "what is still firing," and also in `unclaimed`. Accepting
`ok` therefore requires filtering or labelling state at the `/signals` boundary in the
same change.
**Effort.** Small. **Queued?** No.

### 5.5 `needs_human` is never swept, so an unanswered incident pins its signal forever

**STATUS: FIXED (2026-08-01).** `STATUS_NEEDS_HUMAN` added to `_SWEEPABLE_STATUSES`, with
its own longer threshold as the gap recommended: `needs_human_stale_after_secs`, exposed
beside `stale_after_secs` on the settings route, defaulting to
`DEFAULT_NEEDS_HUMAN_STALE_MULTIPLIER` (6) × the working window, so 12 h at the 2 h
default. `sweep_stale(stale_after_secs, needs_human_after_secs=None)` derives it when unset
so the two stay coupled unless an operator separates them.

The reasoning for a *separate* threshold, recorded because it is the part a future reader
will want to change: waiting on a person is legitimately slower than an agent dying, and
releasing the incident discards the investigation's context to re-derive it. The point is to
stop an *abandoned* question pinning a signal, not to punish a slow answer.

3 new tests in `TestStaleSweep` (7 total), and per the gap's own note they exercise **the
sweep** rather than only asserting
the transition is legal — that is precisely how the gap survived: the existing guard checked
`LEGAL_TRANSITIONS` and never ran the sweep. One test pins the asymmetry directly (at 5 h
idle an `investigating` incident is released and a `needs_human` one is not).

**Took the gap's advice and did NOT build the nudge ladder.** It collides with the shipped
"do not re-notify for an unchanged condition" stance, and an unanswered `needs_human` *is*
an unchanged condition. If a nudge is ever wanted it belongs on the notification bus
(§5.6) with a `group_key`, where collapsing is native rather than a violation.

**Claim.** `_SWEEPABLE_STATUSES = frozenset({STATUS_DISPATCHED, STATUS_INVESTIGATING})`
(`store.py:66`, docstring "both pre-terminal working states are sweepable") and
`store.py:307` skips everything else — while `models.py:88-89` legalises
`needs_human → stale` precisely because "an incident nobody ever answers must not pin a
signal as claimed forever," and `spec:698` repeats it. `dispatch.py:332-337` counts every
non-stale non-terminal incident as owning its signal, so the alarm is never re-claimed.
The only guard, `tests/test_slot_watch.py:76-84`, asserts the `LEGAL_TRANSITIONS` entry
and never exercises the sweep; `tests/test_store_and_gate.py:347-385` covers
dispatched/investigating/terminal with no `needs_human` case.
**Why it matters.** The app's quietest failure: an alarm stops being worked, on
purpose-looking machinery, and nothing says so.
**Sketch.** Add `STATUS_NEEDS_HUMAN` to `_SWEEPABLE_STATUSES` with its own longer
threshold (`needs_human_stale_after_secs`, exposed beside `stale_after_secs` at
`routes.py:530-553`), and add a sweep test that actually runs it.
**Effort.** Small. **Queued?** No.
**Explicitly not recommending the nudge ladder half.** A nudge for an unanswered
`needs_human` collides with a shipped stance — `SKILL.md:133-141` "Do not re-notify for an
unchanged condition" — and an unanswered `needs_human` *is* an unchanged condition. It is
also not true that nothing surfaces it: the board line renders `blocked_reason` instead of
the bare status (`slack_out.py:170-178`) and the handover digest's `waiting_on_you` is
ordered first in the headline (`handover.py:110-141`, `:195-219`). If you want a nudge,
put it on the notification bus with a `group_key` (§5.6), where "one row per incident,
collapsed" is the channel's native semantics rather than a violation of the Slack rule.

### 5.6 The local notification bus is unused — the only credential-free push the app is missing

**STATUS: FIXED (2026-08-01).** `backend/notify_out.py` pushes on three declared channels
(`waiting-on-you` critical, `source-health`, `incident-released` passive+TTL), default off,
edge-triggered, `group_key` = incident id, surfaced in Settings as a "Desktop
notifications" card. Both decisions the sketch named were made deliberately and are
recorded in the module docstring and the module spec:

**(a) In-process, with BOTH guards replicated — and HTTP was never actually an option.**
The sketch offered "either go through HTTP or replicate the limiter" as two open choices;
the code says otherwise. `POST /api/notifications/push` authenticates with an app token
whose secret is written only for a manifest declaring `backend.entryPoint`
(`apps/manager.py`); this app declares `backend.routes`, so no `.app_secret` exists —
verified on disk, `dev-fleet`/`file-explorer`/`workflows` have one and this app does not.
And a handler HTTP-calling its own gateway is what `routes._slot_state` and
`slack_out.link_thread_to_investigation` already refuse ("needs an auth token and can
deadlock the loop"). So `_push` re-implements the handler's checks **in the handler's
order** — enablement, manifest-declared channel, lazy register, validate-before-limiter,
limiter, push — and consumes the **state-owned** `AppRateLimiter`, not a fresh one, so both
paths share one 30-per-300 s budget rather than two.

**(b) Yes, a posture row was needed — and it was mandatory, not optional.**
`test_security_posture.py` walks every module matching the redactor regex and fails on one
that is neither a registered sink nor allowlisted, so the row became required the moment
the redaction landed. Registered as "Ops Mission Control desktop notifications", beside
the Slack-board and postmortem rows. The alternative (`NON_EGRESS_REDACTION_MODULES`)
would have been a lie: the OS notification centre and the persisted JSONL are an output
boundary.

**A BLOCKER the sketch did not know about.** `apps/discovery._manifest_to_builtin_dict`
had no `notifications` branch, and `notifications` is a `_KNOWN_FIELDS` member so it did
not land in `extra` either. `register_builtin_apps` persists that dict as the app's on-disk
`app.json` and `get_app_manifest` reads that file — so declared channels were dropped for
every consumer, including the manifest check `_push` relies on. Nothing caught it because
no builtin had ever declared a channel. Fixed in the same change, with a test.

**Deliberately NOT the `needs_human` nudge ladder.** An unanswered `needs_human` is an
unchanged condition; `waiting-on-you` fires on the transition INTO that state and not
again. The `group_key` makes a re-block collapse into the same feed row.

**Claim.** New in this pass, not in the supplied gap set. `app.json:33-36` declares the
`notification` event permission and the app never produces a notification: no
`notification_bus`, no `notify(`, no `/api/notifications/push` reference anywhere under
`backend/`, and no `notifications.channels` block in `app.json` (verified: none of the
seven builtin apps declares one). Meanwhile `NotificationBus` is live at
`src/kiro_crew/notifications/bus.py:281`, exposed on `DashboardState` at
`dashboard/state.py:1727`, reachable by **every** app token unconditionally
(`token_auth.py:1110-1113`), with `group_key`/`ttl`/`priority`/4 actions/internal deep
link, a 30-per-300 s limiter, central recursive redaction (`state.py:2371-2374`), and a
desktop toast path (`website/src/hooks/useNativeNotification.ts`).
**Why it matters.** Every operator-facing fact the app computes and nobody reads —
`waiting_on_you`, `coverage` blind spots, a fast-path match, a refused action — currently
requires an open dashboard tab or a Slack workspace. This channel requires neither, needs
no credential, and cannot be a data-exfiltration concern because `url` is constrained to a
dashboard-internal path.
**Sketch.** Declare ≤3 channels in `app.json` (`incident`, `waiting-on-you`, `coverage`;
the cap is 8, `apps/manifest.py:673`). Add a thin `backend/notify_out.py` mirroring
`slack_out`'s posture — `getattr(state, "notification_bus", None)`, never fatal, silence
by default, one notification per *state change* not per tick, `group_key` = incident id so
repeats collapse. Register the channels once. Two things to decide deliberately: the
in-process path bypasses both the manifest-channel check and the rate limiter enforced in
`dashboard/handlers/notifications_push.py:110-176`, so either go through HTTP or replicate
the limiter; and this is a new egress module, so check whether
`src/kiro_crew/security_posture.py` needs an entry beside `slack_out.py` at `:170`.
**Effort.** Small. **Queued?** No. **Unverified:** whether the frontend renders app-source
notes identically to system ones in every surface (I read the store and the toast hook,
not every panel).

### 5.7 `ACTION_COMMENT` is implemented, gated, and has no caller

**STATUS: FIXED (2026-08-01), via the SOP half of the sketch.** `investigate.md` Phase 4
gains a step that POSTs `{action: "comment"}` with a rendered one-paragraph summary, and —
the part that matters — tells the agent that a `403` refusal under `observe`/`propose` or
without a matching rule **is the system working**, not an obstacle: do not post the same
text by another route, and do not ask the operator to widen a rule so it lands. Without that
sentence an agent reads a refusal as a problem to solve.

The same pass wired `provider_keys` into the ledger-POST step of that SOP and the
`/ledger` route, so an investigation's learned entry carries the exact key (§5.3) instead of
only the colliding shape hash — otherwise the new match layer would stay empty for
agent-authored entries.

**Not done, deliberately:** the `Incident.proposed_action` writer from part (a) of the
sketch. The refusal path already audits what would have happened, so it is not load-bearing
for this gap.

**Posture kept intact, and worth restating because it is easy to conflate:** wiring an
automatic caller is *not* the same as exercising the GitHub or Datadog write path against a
real tenant. Those remain unexercised on purpose (`features.md:903`, least privilege).

**Claim.** `VALID_ACTIONS` includes `ACTION_COMMENT` (`models.py:157`), three sinks
implement it (`pagerduty.py:210-214`, `datadog.py:186-196`, `github_issues.py:202-203`),
`_handle_action` authorises correctly at `routes.py:409` — and nothing ever composes or
sends one. `opsApi.action` is declared (`api.ts:274-278`) and referenced nowhere; the only
button in `OpsMissionControlPage.tsx:459-469` is a local `transitionMutation`; no cron in
`app.json:56-89` mentions `/incident/action`; the SOPs only say "draft the precise action
and ask" (`investigate.md:76`, `SKILL.md:22-23`). `Incident.proposed_action` is a
declared-but-never-written field on both sides of the wire.
**Why it matters.** A comment is the safest write-back in the landscape: append-only,
attributed, trivially reversible, zero blast radius. It is also the only way the app's
output reaches a colleague who lives in the ticketing tool rather than in KiroCrew —
without a second credential or a hosted anything. Right now an operator who grants
`act` + `comment` on a scoped pattern still gets nothing written anywhere.
**Sketch.** Two small pieces. (a) Have the investigate SOP write its proposal into
`Incident.proposed_action` through the existing `POST /incident/transition` field
allowlist (`routes.py:290`) — the field is in the model and the TS type and just needs a
writer. (b) Add an SOP step that POSTs `{action: "comment"}` with a rendered one-paragraph
summary; the autonomy gate already decides whether it lands, so under observe/propose it
is refused and audited, which is correct. No new route, no new sink, no vocabulary change.
**Respect the standing posture:** `planning/features.md:903` and
`user_manual.md:611-614` record that the GitHub and Datadog write paths are intentionally
never exercised ("least privilege", "it writes to the tenant"). Wiring an automatic caller
is not the same as verifying it live, and must not be conflated with it. Also note the
scope limit: there are exactly four `ActionSinks` (pagerduty, datadog, github-issues,
noop), so Zabbix/Icinga/Dynatrace comment targets are hypothetical out-of-tree adapters,
not shipped destinations.
**Effort.** Small. **Queued?** No. `features.md:875` "Diagnosis write-back" is about
`/incident/transition` + `/ledger` — write-back to the app's own store, not to the provider.

### 5.8 Nothing renders an incident for a non-KiroCrew reader; the renderer that would is dead

**STATUS: FIXED (2026-08-01), local-file half exactly as sketched.** `store.transition`
calls `write_log` when the resulting status is in `TERMINAL_STATUSES` — outside the index
lock, since that lock is the compare-and-set every `claim` contends on — and sources the
sections from the PERSISTED incident rather than the closing call's kwargs, so an unrelated
later `update_fields` cannot blank a finished record. The write is non-fatal (`OSError`
logged, close still returns) for the same reason the Slack mirror is, and lands 0o600 inside
the already-0o700 incidents dir; the file previously inherited the umask.

**Redaction landed with it, and it was the load-bearing half.** `write_log` interpolated
provider titles/resources and a model-authored diagnosis verbatim; no `redact` call existed
anywhere in `store.py`. It now runs `redact_tokens(core_redact(...))` — BOTH, because core
alone leaves a bare-hex Datadog key and a prefix-less `Bearer` token in place while the app
pass alone leaves an AKIA id, verified live. `store.py` is registered in
`security_posture._REDACTION_SINKS` as "Ops Mission Control incident postmortem": note the
report understated this as optional-unless-you-add-the-route — `test_security_posture.py`
walks EVERY module for redactor call sites and fails on any that is neither a registered
sink nor allowlisted, so registration became mandatory the moment the redaction did.

**Is it safe to hand a colleague? Qualified yes, and the qualification is written into the
spec's Security model.** Credential SHAPES are masked (vendor formats, `Bearer …`/`token=…`
carriers, exfil URLs). Meaning is not: an internal hostname, a customer id or a stack trace
naming private paths survive. It is a credential filter, not a declassification pass.

**Surfaced, so this is not another dead end.** `GET /incident` now also returns `log_path`
(empty unless the file really exists — a fabricated path would be the UI asserting an
artifact the backend does not have), and the Board gained a "Closed — postmortems" card that
is the first caller `opsApi.incident(` and `opsApi.incidents(` have ever had. It renders the
text verbatim in a `<pre>` rather than through `MarkdownRenderer`, because the point is the
exact bytes the colleague receives, with a Copy control and the on-disk path beside it.

**Not done, deliberately: the HTTP download route.** The reasoning in the sketch stands
unchanged — a non-JSON response is a second egress boundary needing its own redaction and
its own posture row, and the JSON field already makes the artifact readable.

**Also decided, not dodged:** `prune_closed` still leaves the files. A flapping alarm now
accumulates one file per flap; that cost is accepted and stated in both the constant's
comment and the spec, because pruning an index row must not destroy the written record.

**Line numbers below predate the last fix pass — follow the code, not the citations.**
`write_log` is at `store.py:416-454` (not 385-423), `read_log` at `:457`, the `/incident`
handler at `routes.py:303-329`, `log` typed at `api.ts:556-559`, and the on-disk-layout line
is `docs/system-specs/modules/ops-mission-control.md:1188` (886 is the Slack section).

**Claim.** `store.write_log()` (`store.py:385-423`) renders the complete per-incident
Markdown postmortem — metadata table, Diagnosis, Actions taken, Next steps, Matched
knowledge — and has exactly one reference in the repo: its own definition
(`incident_log_path` has two callers, `write_log:392` and `read_log:428`). So the file
`spec:886` documents (`incidents/<id>.md`) is never created — the spec describes an
on-disk artifact that cannot exist. `read_log` is served at `routes.py:277`, typed at
`api.ts:259-260`, and `opsApi.incident(` is called by no component. All 26 routes return
`web.json_response`; zero `web.Response(`.
**Why it matters.** Cheapest unshipped capability in the app — the renderer is written.
Without it the incident narrative is trapped in a chat transcript, a JSON index and a
Slack thread: not attachable to a ticket, not pasteable into a review, not handable to a
stakeholder.
**Sketch.** Call `write_log` from `store.transition` when the target status is terminal,
writing under the 0o700 incidents dir. If you also want an HTTP path, `GET /incident/log`
must be wrapped in `_require_enabled` (enforced by
`tests/test_routes.py::test_every_registered_handler_is_gated`) **and** treated as a new
non-JSON egress: redact at that boundary and register in
`src/kiro_crew/security_posture.py`, because `write_log` today renders unredacted provider
text and an unredacted model diagnosis (no `redact` call exists in `store.py` or
`models.py`). Do not claim the git-sync benefit: `ledger_sync.py:193` tracks only
`ledger.jsonl`, `rotation.yaml`, `.gitignore`, deliberately, so incident files never sync.
**Effort.** Small (local file) / small-plus-posture-work (HTTP). **Queued?** No.
`features.md:1007-1008` declines telemetry — data leaving the machine — which a local file
the user chooses to share does not violate.

### 5.9 The fast path has no track record, and confidence never moves down on evidence

**STATUS: FIXED (2026-08-01), together with §5.10.** `entry_unlocks_fast_path` now needs
four conditions: verified, high, `use_count >= MIN_USES_FOR_FAST_PATH` (2), and
`miss_count <= MAX_MISSES_FOR_FAST_PATH` (0). `miss_count` / `last_miss` /
`decayed_at_miss_count` are persisted, default-zero, and take the **max** on every merge
path. `hygiene()` demotes one confidence step at `MISS_RATIO_FOR_DECAY` and reports
`demoted` separately from `decayed`. Spec contract 2b landed in the same change.

**Two things the sketch did not anticipate, both found in the code.**

1. **A floor of 1 would have been vacuous.** `attach_ledger_matches` calls `record_use`
   *before* `is_fast_path`, so at the moment of judgement `use_count` already counts the
   incident being judged — every match whatsoever has `use_count >= 1`, including the
   hand-POSTed first-timer the floor exists to exclude. 2 is the smallest floor that says
   anything.
2. **`decayed_at_miss_count` was mandatory, not optional.** The sketch's ratio test stays
   true once true, and hygiene runs nightly — so a single miss would have walked an entry
   `high → medium → low` across three nights on no new evidence, reaching the bottom of
   the scale for one failure. One piece of evidence, one step.

**And one the sketch got right for a reason worth recording:** re-POSTing an entry is how
`ledger-hygiene.md` promotes `observed → verified`, so accepting `miss_count` from a body
would have made the promotion step double as a one-curl way to erase every recorded
failure — on precisely the entries most likely to have them. `POST /ledger` does not read
the field, and `upsert` takes the max, so neither door lowers it.

**Also changed, and not in the sketch:** the prune order moved from `-use_count` to
`-(use_count - miss_count)`. The report's own "Why it matters" paragraph named this and the
sketch did not fix it — an entry that kept matching the wrong failure climbed the ranking on
every mismatch and was therefore the LAST thing the cap dropped, so the ledger
preferentially kept its most misleading rows.

**The framing correction under §7 stands and was honoured:** nothing was removed from the
agent-authored-corrective-entry path, `find_contradictions` is untouched, and hygiene still
reports rather than deletes.

<details><summary>Original analysis</summary>

**STATUS WAS: OPEN, and §5.3 changed its weighting in both directions.** An exact provider key
makes a fast-path match much more trustworthy than a shape match was, which lowers the
urgency. But the `use_count` floor is now *more* worth having, not less: `record_use` binds
the exact key on first match, so a single hand-POSTed entry that matched once by shape
thereafter matches exactly — a stronger-looking claim on the same single occurrence of
evidence. Pairs with §5.10 as the report says.

**Claim.** `is_fast_path` (`ledger.py:208-215`) is satisfied by any single `verified`+`high`
match with no `use_count` condition, and `POST /ledger` (`routes.py:701-721`) takes
`confidence`/`trust` verbatim. `record_use` increments at claim time before any outcome
exists (`ledger.py:218-240` ← `dispatch.py:267-274`), so the ranking key means "was shown."
The only downward movement anywhere is `hygiene`'s one-step decay after 90 days of
**non-use** (`ledger.py:335-349`); `upsert`/`_reconcile` take the strongest of two values
by design. Nothing correlates "the same fingerprint fired again shortly after an incident
resolved citing entry X."
**Why it matters.** Combined with §5.3's collisions, a false-matching entry climbs the
ranking on every mismatch and survives the hygiene prune (which sorts by `-use_count`).
**Sketch.** Additive: `miss_count: int = 0` and `last_miss: str = ""` on `LedgerEntry`
(defaults keep every existing line valid and union-safe on merge). At claim time, if a
recently-resolved incident with the same fingerprint recorded matched entry ids and the gap
is under a threshold, call a new `ledger.record_miss(entry_id)`. Let the **daily hygiene
pass** — not the hot path — demote one step when `miss_count` exceeds a ratio of
`use_count`, and require `use_count >= MIN` plus `miss_count == 0` for `is_fast_path`.
Detect-don't-decide still holds: hygiene reports, never deletes. An ingested Sentry
`substatus=regressed` or Datadog `regression{}` is a direct `record_miss` with no
inference.
**Effort.** Medium. **Queued?** No. **Do not overstate the framing:** the ledger is *not*
structurally unable to record a failed fix — the shipped mechanism is an agent-authored
corrective entry surfaced by `find_contradictions` (`investigate.md:135-139`,
`ledger.py:252-304`, `ledger-hygiene.md` step 2), which is the same detect-don't-decide
posture `features.md:35-40` chose on purpose. And `handover.py:89`'s `proven` label comes
from trust+confidence, not `use_count`.

</details>

### 5.10 No post-action verification, so `ActionResult.ok` means only "2xx"

**STATUS: FIXED (2026-08-01), together with §5.9.** Five persisted, default-empty fields on
`Incident` (`last_action`, `last_action_at`, `verify_after`, `verification`,
`verification_detail`), a verdict vocabulary in `models.py`, and
`dispatch.verify_pending_actions` running inside the existing cycle. Spec contract 3b
landed in the same change. Both of the report's own warnings were honoured: the recheck
consults `poll_health` and records `unknown` — **not** a success — for a source that did not
answer, and a `silence` is rechecked at the END of its own window rather than on an invented
interval.

**No new cron and no extra provider call.** The recheck reuses the signals and
`poll_health` the cycle already produced, so "re-read the signal after acting" stays inside
the heartbeat's flat cost. The report estimated "a recheck step inside the existing reconcile
cron"; the dispatch cycle turned out to be the better host, because it is the thing that
already holds a fresh poll and its `poll_health` in hand.

**One thing the report understated.** It treats verification as uniform, but only two verbs
are verifiable. An `ack` leaves an alert firing *by design* — `normalize_state` maps
`acknowledged` onto `firing` on purpose — so a verdict derived from firing state would be a
confident wrong answer about an unverifiable write, which is the same defect class as the
2xx it fixes. `VERIFIABLE_ACTIONS = {resolve, silence}`; an ack is stamped `not_checkable`
with no due date and the board says "sent, not confirmed". That leaves a real gap (no adapter
reports acknowledgement state back) which is now named rather than papered over.

**`unknown` is deliberately not terminal.** It is a statement about us, not about the world,
so a later cycle where the source answers replaces it. Freezing it would be the
absence-is-evidence bug in a third place.

<details><summary>Original analysis</summary>

**STATUS WAS: OPEN, and §5.12 makes it cheaper and more valuable.** Cheaper because a
`silence` has a known expiry, so "recheck after the window" has an obvious schedule instead
of an invented one. More valuable because a bounded silence that expires into the same
firing condition is exactly the evidence §5.9's demotion path needs. §5.4's `poll_health` is
also a prerequisite that now exists: a recheck must not read a failed poll as "the action
worked".

**Claim.** `_handle_action` (`routes.py:409-431`) awaits `sink.execute`, audits, returns,
stops. No post-action poll, no scheduled recheck, no field recording what action was taken.
**Why it matters.** Verification is a **read**, so it sits entirely inside the
read-only-by-default posture and does not approach the declined remediation boundary. But
without it the app can report an ack that never landed: Checkmk dispatches commands
asynchronously through Livestatus and its docs warn a 2xx "only indicates whether the
request was successfully transmitted, NOT whether it was in fact successfully executed";
Nagios's command pipe returns nothing at all. Reporting a write that did not land is
exactly the silent lie an ops agent must not tell. It is also the missing input to §5.9 —
it is what would make `use_count` mean "worked."
**Effort.** Medium (needs a field on `Incident`, and a recheck step inside the existing
reconcile cron rather than a new cron). **Queued?** No.

</details>

### 5.11 No representation for provider-side suppression

**STATUS: FIXED (2026-08-01).** `STATE_SUPPRESSED` added to `VALID_STATES`, with
`normalize_state` mapping `suppressed`/`silenced`/`inhibited`/`muted`/`snoozed`/`downtime`/
`in downtime` onto it, plus two persisted, default-empty attribution fields
(`Signal.suppressed_by`, `Signal.suppressed_reason`). Spec contract 1b landed in the same
change.

**A state, not a label**, decided against the two consumers of state: `run_cycle` filters
`state == firing` in ONE place, so "never claim a parked signal" holds by construction with
no second predicate for a later edit to forget; and `/signals` splits buckets BY STATE,
which a label cannot produce — a labelled signal would keep arriving inside `firing`,
exactly where reconcile reads live work.

The webhook adapter now reads BOTH Alertmanager status shapes. The v2 `gettableAlert`
`status` OBJECT was verified broken before this: the scalar-only read stringified the dict,
so `{"state": "suppressed", "silencedBy": [...]}` normalized to `unknown` and the
attribution was dropped entirely — a sender being maximally explicit produced a signal
indistinguishable from garbage. The flat envelope accepts both fields too, since Zabbix
(`suppressed=1`) and Icinga (`downtime_depth`) do not speak Alertmanager's shape.

`CycleResult.suppressed` counts what a cycle saw and left alone, and is deliberately
excluded from `changed` — a suppression is not news, and announcing it would re-notify on
exactly the alarms an operator muted. Without the count a cycle reported a smaller world
than it saw, since `polled` counts firing only.

**UI parity, which was the other half of this gap.** `SignalsPanel` gained a "Parked at the
provider" card (no Claim button — offering one would assert an authority the backend
refuses), a per-source **Parked** column beside Firing, and a footer that says so when
nothing is firing but signals are parked. The Board's dispatch line names the suppressed
count. `reconcile.md` Pass 1 gained step 5: a parked signal must not be resolved, and must
not be left implying the agent is working it either.

**Neither settled decision was reopened**: `acknowledged` still maps to `firing` (pinned by
a test naming `pagerduty.py`'s reason), and no-data stays opt-in and default-off.

**Original claim.** `VALID_STATES` = `{firing, ok, unknown}` (`models.py:43`);
`normalize_state`
falls everything unmatched to `unknown` (`:210-223` — stale; it had moved to `:253-266`).
`run_cycle` claims only `firing`. The `grep -rniE 'suppress|silence|snooze' backend/` line
was also stale by the time this was implemented: §5.12's `ACTION_SILENCE` had landed, which
is the app's own OUTBOUND verb and must not be conflated with this inbound read. So "a human
already parked this" was indistinguishable from "cannot tell."
**Why it matters.** Suppression state is the most important server-side noise filter
available and it exists everywhere (Alertmanager `status.state=suppressed` +
`silencedBy`/`inhibitedBy`, Zabbix `suppressed=1`, Icinga `downtime_depth`, Azure
`Acknowledged`, Sentry `archived_until_*`). Without a representation every new adapter
reimplements the filter privately, and the app will investigate things a human explicitly
parked — the behaviour that destroys trust in an autonomous responder fastest.
**Sketch.** Add `STATE_SUPPRESSED` to `VALID_STATES` with `normalize_state` mapping
`suppressed`/`silenced`/`inhibited`/`in downtime`. `run_cycle` keeps claiming only
`firing`, so default behaviour is unchanged — but "5 firing, 3 suppressed by a human"
becomes a board and handover fact. Persisted-vocabulary change → spec in the same commit.
**Two things NOT to do.** Do not map `acknowledged` → suppressed: `models.py:219` maps it
to `firing` and `pagerduty.py:56-59` says why ("an acknowledged page is still unresolved,
and the whole point is to be working it"). Do not reopen `no-data`: `features.md:979-990`
decides it deliberately (CloudWatch `INSUFFICIENT_DATA` behind an opt-in, default off),
and the truthful state survives in `labels['state']`.
**Effort.** Medium, and note the value mostly lands on adapters this repo does not ship —
among the shipped five only Datadog has any suppression concept (`options.silenced`, which
the poll at `datadog.py:106-149` does not read). **Queued?** No.

### 5.12 No verb with a mandatory expiry — and Datadog's mute is issued indefinitely

**STATUS: FIXED (2026-08-01).** `ACTION_SILENCE` added to `VALID_ACTIONS`, with
`EXPIRING_ACTIONS`, `DEFAULT_SILENCE_SECS` (4 h), `MAX_SILENCE_SECS` (24 h) and
`resolve_silence_secs()`. Unparseable or non-positive input yields the DEFAULT, never "no
expiry" — the one reading that would reintroduce the exact bug this verb replaces.

**The bound is enforced at the authorization boundary** (`routes._handle_action`), not per
adapter. That was a deliberate choice over the simpler per-sink version: a sink must not be
able to opt out by forgetting to check, because an unbounded suppression is the single
outcome the verb exists to prevent. The route also previously dropped everything except
`note`, so `duration_secs` could not have reached a sink at all; it now forwards the clamped
value and echoes it back, since the window applied may be smaller than the one requested.

Datadog's `body={}` is fixed — it always sends `end` now, and `resolve` is **retained as an
alias** onto the same bounded mute rather than removed, because silently dropping it would
revoke a capability an existing act-rule already grants. PagerDuty maps `silence` to its
real `/snooze` endpoint (a genuine time-boxed suppression with a required duration, unlike an
ack, which has no expiry and means something different). `github-issues` deliberately does
**not** advertise it: an issue tracker has no snooze and claiming one would be a lie. `noop`
picks it up automatically by returning `VALID_ACTIONS`.

6 tests in `TestSuppressionIsAlwaysBounded`, asserting the **wire body** rather than the
return value — an `end` in the future on both the `silence` and the `resolve` alias path,
since a passing return value would not have caught the original bug.

**Claim.** `VALID_ACTIONS` is `{ack, resolve, comment}` (`models.py:157`), enforced at
`routes.py:395-398` and mirrored by `AutonomyRule` validation (`rotation.py:127-132`,
`:220`). Every provider's genuinely low-risk write is a suppression with an expiry
(Alertmanager `endsAt` **required**; Datadog mute `end`; Icinga ack `expiry`; Sentry
`ignoreDuration`; Elastic `snooze_schedule`). Concrete shipped consequence:
`datadog.py:177-181` POSTs `/monitor/{id}/mute` with `body={}` — an **indefinite** mute —
because there is no `duration_secs` in the contract to pass as `end`.
**Why it matters.** A time-boxed silence has the safety property nothing else has: a wrong
one self-heals at expiry, is non-destructive (it suppresses notification, never the
underlying condition), is reversible by one DELETE, and records `createdBy`/`comment` so
the audit names the agent. That makes it the correct *first* action to ever let an agent
take, and it is inexpressible. This is not "auto-resolve by default" (declined) — it is a
distinct, weaker verb gated by exactly the same machinery.
**Sketch.** `ACTION_SILENCE = "silence"` with an `execute` payload contract that
**requires** `duration_secs` bounded by a module constant — a silence with no end should
simply be inexpressible. `AutonomyRule.actions` already validates against `VALID_ACTIONS`,
so a rule can grant `silence` without granting `resolve`, which is the point. Pass `end`
in the Datadog mute in the same change.
**Drop the "semantic lie" framing** if you saw it elsewhere: the mapping is disclosed in
the code comment (`datadog.py:160-162`), in the returned `detail` ("datadog monitor {id}
muted (Datadog clears on recovery)", `:185`), and in the adapter blurb (`:92`). The real
defect is the empty body. Separately, `user_manual.md:614` still claims "Datadog is
read/evidence only. No write actions." — a doc bug worth fixing alongside.
**Effort.** Medium. **Queued?** No.

### 5.13 Source reach: three genuine absences, all second-order once push ingest is generic

**STATUS: OPEN — and now genuinely second-order, as the heading predicted.** §5.2 made push
ingest generic, so Alertmanager, Grafana, Sentry, GlitchTip and Datadog Error Tracking are
reachable today through any forwarder that can sign, with no new adapter. What a dedicated
adapter still buys is **polling** (nothing extra to run, and no inbound path at all) and the
**write path**. Both `user_manual.md:614` (claims Datadog is read-only) and the
`datadog-evidence` namespace bug noted in §6 are still unfixed.

- **No Alertmanager poller.** No module exists (`backend/providers/` is base, cloudwatch,
  datadog, github_issues, http, noop, pagerduty, schedule_file, webhook). It is the only
  landscape entry that is simultaneously free, self-hosted, unauthenticated by default,
  and shipping its own stable `fingerprint` plus `status.state`. Blocked on one thing:
  `providers/http.py:39` hardcodes https and `:69-70` raises on anything else, pinned by
  `tests/test_providers.py:675-685`. A loopback-only carve-out is defensible (the call
  carries no credential) but must be argued and tested, not treated as a one-liner. Note
  it overlaps §5.2: a push-configured Alertmanager reaches the app without a poller.
- **No text/plain reader.** `request_json` is the only helper and `json.loads` every
  response (`http.py:92-97`), so Prometheus exposition format — the cheapest read path for
  Netdata and any instrumented process, and the only path for Uptime Kuma monitors the
  operator has not published on a status page — is structurally unreachable. A ~60-line
  parser plus `request_text` beside `request_json`. Same loopback dependency; the parser is
  the easy half.
- **No calendar/HTTP rota reader.** `resolve_shift` has two non-fallback sources:
  `pagerduty` (never exercised against a tenant, `features.md:901`) and `schedule-file`,
  whose path is fixed at `schedule_file.py:93-95` and deliberately non-configurable
  (`:68-71`). **Correct the harm statement**: a team rota *is* expressible today via
  `rotation.yaml` in the synced ledger repo, verified end-to-end against a real private
  repo with three teammates (`features.md:262-300`). The narrower real gap is that an
  operator whose rota already lives in Google/M365/Opsgenie/Grafana OnCall must transcribe
  and maintain a second copy. Design tension to weigh, not fatal: the fixed filename and
  the `leader:` key were both chosen on "one fact in the file everyone reads beats N local
  settings agreeing by convention" (`features.md:250-252`), and a per-install `ics_url`
  reintroduces exactly that class of disagreement unless the URL itself is pinned in the
  synced repo. Opsgenie appears only at `ideation.md:114` and `:144` — considered, never
  decided.
- **No config-driven REST poller.** There is one no-Python ingest path (the webhook, one
  fixed envelope, push-only) and zero config-driven pull paths; no field-mapping machinery
  exists (grep for `field_map|jsonpath|mapping|template` finds only `MappingProxyType`).
  Half the self-hosted landscape differs only in URL, auth header, container path and four
  field names. Worth doing eventually; two sketch corrections: per-source secrets
  `token_<id>` cannot be stored as-is because `routes.py:653-659` rejects any field not in
  a **static** `secret_fields` tuple read via `getattr` (`registry.py:145-148`), so dynamic
  secret fields need that to become a computed property; and an operator-supplied
  arbitrary URL carrying a bearer token is a materially wider egress surface than the
  existing configurable-host cases (`datadog.py:76-77` builds from a `site` field), so it
  needs the same reasoning `http.py:37-39` already encodes plus an explicit statement of
  who may write that config. Distinct from the declined config-path-for-companion-discovery
  (`spec:1018-1024`), which declined a new *code-loading* channel — an inert field map
  interpreted by core Python is not that.

---

## 6. Refuted candidates

| Candidate | Why it is not a gap |
|---|---|
| **Route the pin board through `DashboardState.channel_transports` for Teams/Discord parity** | Wrong about the mechanism. `MessagingTransport` (`src/kiro_crew/messaging/transport.py:79-127`) has five abstract methods — send/resolve/fetch_history/receive/authorize — and **no edit or update primitive** in the contract or any of the seven implementations. `capabilities.edit` is a boolean with no interface behind it. So routing the board there cannot preserve "**Edited in place.** The first post records `slack_thread_ts`; later changes `chat_update` that message" (`spec:780-784`) — it degrades the board into the feed `spec:739` names as the failure mode. Also `rich_blocks=True` only on `slack/transport.py:59`, and the registry deliberately does **not** hold Slack: `dashboard/state.py:1700-1703` "Slack keeps its dedicated ``slack_client`` above (rich streaming mirror), so it is not stored here." A defensible residue: extract an internal `OutputChannel` seam so a future editable channel has somewhere to plug in — but do not promise Teams/Discord parity, because that is a core-messaging change (same shape as the i18n deferral at `features.md:955-968`) |
| **Email / SMTP / ntfy as an output channel** | Needs a credential the module is forbidden to hold (`tests/test_slack_out.py:130-142` fails the build on `slack_token`/`bot_token` strings), and there is no host client to borrow — `smtplib` is on the blocked-import list (`src/kiro_crew/skills_script_validator.py:105`). Requires building a host transport first, i.e. not an ops change. Superseded by the notification bus for the "reach me when I'm not looking" use case |
| **Scheduled handover push** | Declined and reasoned. `sops/handover.md:9-11`: "a handover is read by a person at a moment they choose, and a scheduled one that nobody reads is exactly the noise this app exists to avoid," restated at `spec:837-840`. `HandoverPanel.tsx:70-71` says the same about polling |
| **Nudge/re-page ladder for `needs_human`** | Half-refuted. The sweep half is real (§5.5); the ladder collides with `SKILL.md:133-141` ("Do not re-notify for an unchanged condition") and with the fact that `blocked_reason` already renders on the board line (`slack_out.py:170-178`) and orders first in the handover headline (`handover.py:110-141`). And it would nudge into the same thread nobody can answer (§5.1). Route any such signal through the notification bus with a `group_key` instead |
| **`no-data` / `INSUFFICIENT_DATA` as a new state** | Already decided: `features.md:979-990` — opt-in behind `include_insufficient_data`, default off ("noisy on accounts with idle resources"). `cloudwatch.py:154-188` is that decision's implementation, and the truthful state survives in `labels['state']` |
| **Map provider `acknowledged` onto a suppressed state** | Deliberately the other way: `models.py:219` maps `acknowledged` → `firing`, and `pagerduty.py:56-59` gives the reason — "an acknowledged page is still unresolved, and the whole point is to be working it" |
| **"The ledger cannot learn a fix failed"** | Overstated. The shipped mechanism is an agent-authored corrective entry sharing the fingerprint, surfaced most-used-first by `find_contradictions` (`ledger.py:252-304`), exposed at `GET /ledger/contradictions`, with `ledger-hygiene.md` step 2 telling the agent to split the patterns and explicitly not to delete. That is detect-don't-decide by design (`features.md:35-40`). The narrow real gaps are the missing *mechanical* downward path and the missing `use_count` floor (§5.9) — **both closed 2026-08-01, and the correction held**: `record_miss` was added beside the corrective-entry path, not instead of it, and hygiene still reports its demotions rather than deleting anything |
| **"The handover digest labels entries `proven` from `use_count`"** | False. `handover.py:89` computes `proven` from `trust == FAST_PATH_TRUST and confidence == FAST_PATH_CONFIDENCE`; `use_count` only ranks (`:75`) and gates `MIN_USES_TO_RECUR >= 2` (`:74`) |
| **"Datadog's mute is a semantic lie"** | Disclosed in three places: the code comment (`datadog.py:160-162`), the returned `detail` (`:185`), the adapter blurb (`:92`). The real defect is `body={}` — an indefinite mute (§5.12) |
| **Remediation execution** | Declined, standing: "The app diagnoses and proposes; a human applies the fix" (`features.md:998-1000`). Reinforced structurally — the only write vocabulary is incident-tracker bookkeeping, and `dispatch.py:542-547` appends an unconditional authority reminder to every brief |
| **Auto-resolve by default** | Declined, standing: the source team could reason about which intakes were safe because they built them; a stranger's first install cannot. Autonomy is earned per rule (`features.md:1001-1005`) |
| **Publishing the source workflow's TTR numbers** | Declined: they describe one team's pipeline with caveats that team states itself (`features.md:1006`) |
| **Hosted control plane / telemetry, status page, RSS** | Declined: local-first is the point (`features.md:1007-1008`), and `spec:590` declines hosting webhook ingress or any tunnel. Note this does **not** cover a local file export (§5.8) |
| **Per-provider SLA/freshness schema in the public core** | Declined and reasoned at `features.md:985-990`: a generic SLA schema for a stranger's warehouse would be guessing at their org; it belongs in an adapter or the companion |
| **A filesystem config path for companion discovery** | Declined: "Entry points, not a config path. A filesystem path to import would be a new, unaudited code-loading channel" (`spec:1018-1021`). Does not cover an inert field map (§5.13) |
| **Splunk / SolarWinds / Nagios Core adapters** | Not gaps worth closing. Splunk's only stable identity is a saved-search name over schemaless rows and its only fired-alert mutation is DELETE; SolarWinds has no free tier at all and couples you to the Orion schema through an embedded SWQL string; Nagios Core's write path is a named pipe that returns nothing, so `ActionResult.ok` would be unknowable. Cover the Nagios lineage via Icinga or the webhook |
| **`datadog-evidence` advertises a `site` field nobody reads** | Real, but a known bug class, not a landscape gap: `id = "datadog-evidence"` (`datadog.py:208`) while `configured()`/`_api_base()` read the `datadog` namespace (`:212`, `:75-77`); the guard test that catches this is hardcoded to the cloudwatch module (`tests/test_providers.py:396-415`). Listed here so it is not lost — fix is to generalise that test |

---

## 7. Recommended sequence

> **Steps 1–5, 7 and 9 shipped on 2026-08-01.** The numbered plan below is preserved as
> written, because the ordering reasoning is the useful part and it held up under execution
> — see the note after step 12 for what that looked like in practice, and §0 for status.
> **What remains, in order: step 6** (local notification channel), **step 8** (Markdown
> artifact), **step 10** (post-action verification + `use_count` floor), **step 11**
> (`STATE_SUPPRESSED`), **step 12** (source reach). Step 9 arrived early, with the Datadog
> mute fix it shares a file with.

The ordering principle: **fix what silently lies before adding reach.** Every new source
multiplies the impact of the reconcile-on-failed-poll bug and the fingerprint collisions,
so the correctness items come first even though the reach items are more exciting.

1. **Slack board thread link** (§5.1). Small. First because it is a broken headline claim
   with a silent failure mode, and because it is the delivery substrate everything else
   assumes. Delete the prose instruction in the same commit so there is one mechanism.
2. **Reconcile stops resolving on a failed poll** (§5.4). Small, SOP-only. Second because
   it is the one bug that destroys real work, and because it is a prerequisite for
   accepting `ok` in step 3 — without the `/signals` state-boundary fix, step 3 makes
   reconcile *worse*.
3. **The webhook envelope becomes a generic ingest contract** (§5.2): `alerts[]` fan-out +
   `status`/`state` → `normalize_state` + `provider_key` passthrough + an accepted
   signature-header list. **This is the single change that unlocks the most future
   integrations** — one 176-line file, no new route, no new credential, no tunnel, and it
   reaches Alertmanager, Grafana, Sentry, GlitchTip, Datadog ET and every Alertmanager-shaped
   forwarder. Ship the auth-mode decision with it or the shape work is inert for the two
   largest senders.
4. **Exact provider-identity match layer in the ledger** (§5.3). Medium, additive, no
   re-derivation of anything on disk. Third-party priority because step 3 starts delivering
   provider keys and the ledger has nowhere to put them — and because the verified 4xx/5xx
   collision means every fast-path match today is suspect.
5. **`needs_human` becomes sweepable** (§5.5). Small, plus the sweep test the existing
   guard does not provide.
6. **Local notification channel** (§5.6). Small. Now, not earlier, because steps 1–5 give
   it things worth saying (`waiting_on_you` that will actually resolve, coverage gaps, a
   swept incident) and because it is the right home for any nudge, where `group_key` makes
   "one row per incident" the channel's native behaviour instead of a violation of the
   Slack noise rule.
7. **Wire `ACTION_COMMENT`** (§5.7). Small. After step 6 because the notification is how
   the operator learns the app wrote a comment, and after step 4 because a comment citing a
   collided ledger match would be a confident wrong answer written into someone else's
   ticket. Keep the "never exercised live against a real tenant" posture intact.
8. **Local Markdown artifact from `write_log`** (§5.8). Small; local file only in this
   step. Defer any HTTP download route until you are ready to add redaction at that
   boundary and register it in `security_posture.py`.
9. **`ACTION_SILENCE` with mandatory `duration_secs`, and pass `end` on the Datadog mute**
   (§5.12). Medium. This is the step that makes "let the agent act" a defensible
   proposition rather than an all-or-nothing one — a wrong silence self-heals. Also fix
   `user_manual.md:614`.
10. ~~**Post-action verification** (§5.10) and **`miss_count` + `use_count` floor** (§5.9).
    Medium each, and they pair: verification is what makes `use_count` mean "worked," and
    a floor is what stops one hand-POSTed entry from unlocking the fast path.~~
    **Shipped 2026-08-01, together.** The pairing was real and load-bearing in one
    direction the report did not spell out: `record_miss` needed a producer whose standard
    of evidence was narrow enough to demote on, and the recheck is the only thing in the
    app that produces one. Doing §5.9 alone would have shipped a `miss_count` nothing could
    ever increment — machinery that looks deliberate while doing nothing, which is the
    failure class this whole report exists to catalogue.
11. ~~**`STATE_SUPPRESSED`** (§5.11). Medium. After step 3, because the senders that actually
    carry suppression state are the ones step 3 admits.~~ **Shipped 2026-08-01.** The
    ordering held: the v2 `status` OBJECT that carries `silencedBy` only became worth
    parsing once step 3 admitted Alertmanager-shaped bodies at all.
12. **Source reach, in this order** (§5.13): Sentry/GlitchTip adapter (one adapter, three
    products, plus a 512 MB real backend for the write-path test) → Datadog Error Tracking
    extension (smallest marginal effort in the survey) → the `http.py` loopback carve-out
    and its two consumers (Alertmanager poll, `/metrics` scraper) → declarative REST poller
    → ICS rota reader.

Two things deliberately **not** in the sequence: Teams/Discord parity (refuted — needs a
core messaging edit primitive first) and any nudge ladder into Slack (collides with a
shipped noise stance; route it through step 6 instead).

---

### What executing steps 1–5, 7 and 9 actually taught us (2026-08-01)

Kept because the corrections are more useful than the plan being right.

- **Step 2's "SOP-only, cheaper than it looks" estimate was wrong**, and the reason
  generalises. `errors` answers "did this call fail", but reconcile has to answer "did the
  poll that would have reported *this signal* succeed" — a per-source fact that had to be
  recorded (`poll_health`), not just re-read. An SOP cannot be more correct than the data it
  is given.
- **The dependency chain in the ordering was real, not theoretical.** Accepting `ok`
  (step 3) genuinely does make reconcile worse until `/signals` filters state, exactly as
  step 2 predicted — so they landed together.
- **Two fixes needed a bound the gap did not ask for.** `MAX_KEYS_PER_ENTRY` for
  `provider_keys` (PagerDuty's per-occurrence id would grow a git-synced line forever) and
  `MAX_SILENCE_SECS` for suppression. Both are cases where the additive-field sketch was
  correct but incomplete against a real provider's semantics.
- **Test the sweep, not the grammar.** §5.5 survived because the existing guard asserted
  `needs_human → stale` was *legal* and never ran the sweep — the same
  "assert at the outermost caller" lesson the module spec already records for `run_cycle`'s
  pre-filter. Worth checking the other invariants for the same shape.
- **Two claims in this report were sharpened by implementing them.** §5.7's sketch had an
  `Incident.proposed_action` writer that turned out not to be load-bearing (the audited
  refusal already covers it), and §5.9's framing needed the correction now recorded under
  that heading: an exact key makes the missing `use_count` floor *more* important, not less.
