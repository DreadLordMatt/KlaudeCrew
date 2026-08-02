/**
 * SignalsPanel — the Signals tab.
 *
 * The Signals rail used to be a 280px column beside the Board, which made it a
 * status readout and nothing more: no room to show what a source last returned, no
 * way to see a signal the heartbeat has NOT claimed yet, and no way to act on one.
 * Splitting it out gives signal configuration real space and hands the Board its
 * full width back.
 *
 * What this surface answers that the rail could not:
 *
 * - Which sources are wired, and which are silently unconfigured.
 * - What each source returned on the LAST poll, including its error text. A
 *   provider whose credentials expired reports "ready" from config alone; only a
 *   real poll shows the failure.
 * - Whether a quiet board can be TRUSTED (`all_sources_healthy`). Absence of a signal
 *   means "it cleared" only for a source whose poll succeeded.
 * - Which firing signals are NOT yet incidents. Under the claim cap (3/cycle) an
 *   alarm storm legitimately leaves a queue, and before this the remainder was a
 *   number with no detail — you could see "12 remaining" but not what they were.
 * - Claim one now, rather than waiting for the next heartbeat.
 */
import { useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  RefreshCw,
  Radio,
  AlertTriangle,
  Inbox,
  CheckCircle2,
  ShieldCheck,
  BellOff,
} from 'lucide-react'
import { Badge, Btn, Card, CardTitle, EmptyState } from '../../components/ui'
import {
  describeSourceHealth,
  opsApi,
  SIGNALS_QUERY_KEY,
  WEBHOOK_QUEUE_LIMIT,
  type ProviderInfo,
  type Signal,
} from './api'

/** Module-level frozen empty so the render-time fallback is referentially stable. */
const EMPTY_PROVIDERS: readonly ProviderInfo[] = Object.freeze([])

function severityVariant(severity: string): 'err' | 'warn' | 'muted' {
  if (severity === 'critical') return 'err'
  if (severity === 'warning') return 'warn'
  return 'muted'
}

/** Compact relative age, so a stale failure reads as stale. */
function age(iso: string): string {
  if (!iso) return ''
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return ''
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000))
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return `${Math.floor(secs / 86400)}d ago`
}

export default function SignalsPanel() {
  const queryClient = useQueryClient()

  const providersQuery = useQuery({
    queryKey: ['ops-mission-control', 'providers'],
    queryFn: () => opsApi.providers(),
  })

  // No refetchInterval: polling every configured source is a real cost (rate
  // limits, paid API calls), so this is an explicit user action. The dispatch cron
  // is what polls continuously.
  const signalsQuery = useQuery({
    queryKey: SIGNALS_QUERY_KEY,
    queryFn: () => opsApi.signals(),
    enabled: false,
  })

  // Read-only view of the board's `/state`, for `webhook_queue` alone. `enabled: false`
  // with the SAME key the page owns: `OpsMissionControlPage` mounts that query
  // unconditionally, before it branches on the visible tab, so the cache is always warm
  // and populated by its poll rather than by a second one from here. There is no cheaper
  // source — the depth is live process state, not config, so `/providers` cannot carry it.
  const boardStateQuery = useQuery({
    queryKey: ['ops-mission-control', 'state'],
    queryFn: () => opsApi.state(),
    enabled: false,
  })

  const claimMutation = useMutation({
    mutationFn: (signal: Signal) => opsApi.claim(signal),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'state'] })
      queryClient.invalidateQueries({ queryKey: SIGNALS_QUERY_KEY })
    },
  })

  // Stable empty fallback: a `?? []` inline allocates a fresh array each render, so
  // the memo below would never hit its cache.
  const providers = providersQuery.data?.providers ?? EMPTY_PROVIDERS
  const signalSources = useMemo(
    () => providers.filter((p) => p.roles.includes('signal')),
    [providers],
  )
  // Left possibly-undefined rather than defaulted to `{}`: an inline `?? {}` allocates a
  // fresh object each render, which would bust the memo below on every pass.
  // `describeSourceHealth` accepts undefined and treats it as "no reason recorded".
  const errors = signalsQuery.data?.errors
  const health = signalsQuery.data?.poll_health
  const unclaimed = signalsQuery.data?.unclaimed ?? []
  // `firing`, not the raw `signals` list. The old code counted `signals` — every signal the
  // poll returned, ANY state — under a column headed "Firing", so a provider that reports
  // recoveries inflated the very number an operator uses to judge blast radius. `firing` is
  // filtered exactly the way dispatch claims.
  const firing = signalsQuery.data?.firing ?? []
  const cleared = signalsQuery.data?.cleared ?? []
  // Its own bucket, deliberately never merged into `firing`. A signal a human parked at
  // the provider is absent from `firing` for a third reason — not "it cleared" and not "we
  // could not look" — so the source table must count it apart from both.
  const suppressed = signalsQuery.data?.suppressed ?? []
  // Depth of the inbound webhook spool. `?? 0` rather than a "no data" branch: the number
  // only ever states "this many are waiting", and an absent `/state` means the same thing
  // to a reader as a zero — nothing to go look at yet.
  const webhookQueued = boardStateQuery.data?.webhook_queue ?? 0

  // `all_sources_healthy` is `bool(health) and all(ok)`, so a fresh install with nothing
  // configured yields FALSE. Branch on three cases: reporting "a source is failing" to
  // someone who has configured nothing is a scary lie about their own setup.
  const anySignalSourceConfigured = signalSources.some((p) => p.configured)
  const unhealthySources = useMemo(
    () =>
      signalSources
        .filter((p) => {
          const state = describeSourceHealth(p.id, health, errors, p.configured).state
          return state === 'failed' || state === 'backing_off'
        })
        .map((p) => p.display_name),
    [signalSources, health, errors],
  )
  // Sources that answered but whose absence still proves nothing — a push spool, whose poll
  // drains the queue. Named separately from `unhealthySources` because nothing is WRONG with
  // them: the banner below must not paint them as a fault, and must not let
  // `all_sources_healthy` promise a blanket "absence means recovery" over them either.
  const pushSources = useMemo(
    () =>
      signalSources
        .filter((p) => !describeSourceHealth(p.id, health, errors, p.configured).absenceIsEvidence)
        .filter((p) => health?.[p.id]?.ok)
        .map((p) => p.display_name),
    [signalSources, health, errors],
  )

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardTitle>
          <Radio className="lucide-inline" /> Signal sources
        </CardTitle>
        <p className="text-[13px] text-muted mb-3">
          Where work comes from. Configure credentials and scope in Settings; poll
          here to see what a source actually returns.
        </p>

        {/* Whether a quiet board can be trusted. Only shown once a poll has actually run —
            before that we have no evidence either way, and claiming health we have not
            observed is the same defect in the other direction. */}
        {signalsQuery.data ? (
          !anySignalSourceConfigured ? (
            <p className="text-[13px] text-muted mb-3 flex items-start gap-1.5">
              <AlertTriangle className="lucide-inline" />
              <span>
                No signal source is configured, so nothing was polled. A quiet board means
                nothing yet — connect a source in Settings.
              </span>
            </p>
          ) : signalsQuery.data.all_sources_healthy ? (
            <p className="text-[13px] text-ok mb-3 flex items-start gap-1.5">
              <ShieldCheck className="lucide-inline" />
              <span>
                Every configured source answered.{' '}
                {/* The unqualified version of this sentence was an overstated claim as soon
                    as a push source was configured: `all_sources_healthy` is `all(ok)`, and
                    `ok` says we looked, not that we saw everything. A spool that each poll
                    empties cannot report a signal it already delivered, so "absent means
                    recovered" is exactly wrong for it — and this banner is the line an
                    operator reads before trusting a quiet board. */}
                {pushSources.length > 0 ? (
                  <>
                    A signal absent from this poll can be read as recovered, EXCEPT for{' '}
                    {pushSources.join(', ')} — {pushSources.length === 1 ? 'it delivers' : 'they deliver'}{' '}
                    by push into a spool that each poll empties, so an already-delivered
                    signal will not appear again whether or not it is still firing.
                  </>
                ) : (
                  <>A signal that is absent from this poll can be read as recovered.</>
                )}
              </span>
            </p>
          ) : (
            <p className="text-[13px] text-warn mb-3 flex items-start gap-1.5">
              <AlertTriangle className="lucide-inline" />
              <span>
                {unhealthySources.length > 0
                  ? `${unhealthySources.join(', ')} did not answer this poll.`
                  : 'At least one source did not answer this poll.'}{' '}
                While that is true, absence of a signal does NOT mean recovery — it may just
                mean we could not look.
              </span>
            </p>
          )
        ) : null}

        {providersQuery.isLoading ? (
          <p className="text-sm text-muted">Loading…</p>
        ) : signalSources.length === 0 ? (
          <p className="text-sm text-muted">No signal sources registered.</p>
        ) : (
          <table className="w-full text-[13px]">
            <thead>
              {/* pr-* on the numeric column: `text-right` alone butts "Firing"
                  straight against "Last poll" with no gutter. */}
              <tr className="text-muted text-left">
                <th className="font-normal py-1">Source</th>
                <th className="font-normal py-1 w-28">State</th>
                <th className="font-normal py-1 w-20 text-right pr-4">Firing</th>
                {/* Its own column rather than a footnote on Firing: these two numbers
                    answer opposite questions ("what needs work" vs "what did a human
                    already park"), and a source whose only signals are parked otherwise
                    reads as an idle source. */}
                <th className="font-normal py-1 w-20 text-right pr-4">Parked</th>
                <th className="font-normal py-1 pl-2">Last poll</th>
              </tr>
            </thead>
            <tbody>
              {/* One row per source, derived from poll_health — NOT from `configured`.
                  This row used to read `configured ? 'ready' : ...` plus the literal word
                  'ok' whenever any poll response existed, so a source in backoff, and a
                  source that had never been polled at all, both rendered "ready / ok"
                  while contributing nothing. That is the failure this whole panel exists
                  to prevent, reintroduced one layer up: the operator trusted a green row
                  and read silence as health. */}
              {signalSources.map((p) => {
                const status = describeSourceHealth(p.id, health, errors, p.configured)
                const count = firing.filter((s) => s.source === p.id).length
                const parked = suppressed.filter((s) => s.source === p.id).length
                return (
                  <tr key={p.id} className="border-t border-border align-top">
                    <td className="py-2 pr-2">
                      <span title={p.detail}>{p.display_name}</span>
                    </td>
                    <td className="py-2">
                      <Badge variant={status.variant}>{status.label}</Badge>
                    </td>
                    {/* An em dash, not 0, whenever the source did not contribute: a real
                        zero means "looked, found nothing", which is a different fact. */}
                    <td className="py-2 text-right pr-4">
                      {status.contributing ? count : '—'}
                    </td>
                    {/* Same em-dash rule as Firing: a source that did not contribute
                        cannot honestly report a zero here either. */}
                    <td className="py-2 text-right pr-4">
                      {!status.contributing ? (
                        '—'
                      ) : parked > 0 ? (
                        <span className="text-warn">{parked}</span>
                      ) : (
                        0
                      )}
                    </td>
                    <td className="py-2 pl-2 text-muted">
                      {/* The reason verbatim, plus WHEN. A provider that reports "ready"
                          from config alone can still fail on expired credentials, and the
                          message is what tells the two apart; the age is what tells a
                          failure we just observed from one recorded minutes ago. */}
                      {status.state === 'failed' || status.state === 'backing_off' ? (
                        <span
                          className={`flex items-start gap-1 ${
                            status.state === 'failed' ? 'text-danger' : 'text-warn'
                          }`}
                        >
                          <AlertTriangle className="lucide-inline" />
                          <span className="break-all">
                            {status.detail}
                            {status.at ? ` (${age(status.at)})` : ''}
                          </span>
                        </span>
                      ) : status.state === 'ok' ? (
                        <span>
                          {typeof health?.[p.id]?.signals === 'number'
                            ? `${health[p.id].signals} signal(s)`
                            : 'answered'}
                          {status.at ? ` · ${age(status.at)}` : ''}
                          {/* A successful poll of a DRAINED QUEUE is not the same claim as a
                              successful poll of an API, and the row read identically for
                              both. This source's `poll` empties its spool, so a signal that
                              is still firing at the sender is absent from every cycle after
                              the one that delivered it — "0 signal(s), answered" on the
                              webhook row therefore said "all clear" about a source that
                              structurally cannot report one. */}
                          {!status.absenceIsEvidence ? (
                            <span
                              className="text-warn"
                              title="This source pushes to a spool that each poll empties, so a signal it already delivered will not appear again even if the fault is still live."
                            >
                              {' '}
                              · delivered by push, so an empty poll means nothing
                            </span>
                          ) : null}
                        </span>
                      ) : status.state === 'not_polled' ? (
                        'not polled yet — this is not the same as healthy'
                      ) : (
                        '—'
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}

        <div className="flex items-center gap-2 mt-3">
          <Btn
            disabled={signalsQuery.isFetching}
            onClick={() => signalsQuery.refetch()}
            title="Poll every configured source now (read-only)"
          >
            <RefreshCw className="lucide-inline" />{' '}
            {signalsQuery.isFetching ? 'Polling…' : 'Poll now'}
          </Btn>
          {signalsQuery.data ? (
            <span className="text-[12px] text-muted">
              {firing.length} firing · {suppressed.length} parked at provider ·{' '}
              {cleared.length} cleared · {unclaimed.length} not yet claimed
              {/* The one state where "nothing is firing" on its own would be a lie: the
                  estate is not quiet, it is muted, and the operator needs to know which
                  before they conclude everything is fine. */}
              {firing.length === 0 && suppressed.length > 0
                ? ' — nothing is firing, but the parked signals above are not being investigated'
                : ''}
            </span>
          ) : null}
          {signalsQuery.isError ? (
            <span className="text-[12px] text-danger">
              {(signalsQuery.error as Error).message}
            </span>
          ) : null}
        </div>

        {/* Signals a sender has already delivered that no cycle has drained yet.
            `/state` has reported this number since the webhook adapter shipped and NO
            surface read it, which made the one adapter whose signals arrive by push the
            hardest one to confirm: the spool is drained BY the poll, so "Poll now" empties
            it and the source row then reports the count as ordinary signals. A sender
            debugging their integration had nothing to look at between a 200 from the
            webhook route and a heartbeat up to two minutes later.

            Shown only when non-empty, and worded as pending rather than as a fault — a
            queue at 0 is the steady state and a permanent "0 queued" row would be noise.
            The ceiling matters though: the spool is a bounded deque (200), so a depth at
            the cap means deliveries are being dropped silently, which is the one case an
            operator must not have to infer. */}
        {webhookQueued > 0 ? (
          <p className="text-[12px] text-muted mt-2 flex items-start gap-1.5">
            <Inbox className="lucide-inline" />
            <span>
              {webhookQueued} inbound webhook signal(s) delivered and waiting for the next
              cycle to pick them up — they are not on the board yet.
              {webhookQueued >= WEBHOOK_QUEUE_LIMIT ? (
                <span className="text-warn">
                  {' '}
                  The spool is full at {WEBHOOK_QUEUE_LIMIT}, so the oldest deliveries are
                  being discarded as new ones arrive. Run a cycle from the Board
                  (&quot;Poll &amp; claim&quot;).
                </span>
              ) : null}
            </span>
          </p>
        ) : null}
      </Card>

      {/* Parked signals, rendered ABOVE the unclaimed queue on purpose: this is the card
          that explains why the queue below may be empty while sources are reporting.

          There is deliberately NO Claim button here. The app respects the park — dispatch
          claims only `firing`, so a Claim control would be the UI offering an authority the
          backend refuses, and a 403 after a click is worse than no button. */}
      {signalsQuery.data && suppressed.length > 0 ? (
        <Card>
          <CardTitle>
            <BellOff className="lucide-inline" /> Parked at the provider
          </CardTitle>
          <p className="text-[13px] text-muted mb-2">
            Someone silenced these where they came from, so they are not being investigated
            and no incident was opened. They are listed because "parked by a person" and
            "the app ignored it" would otherwise look identical.
          </p>
          <ul className="flex flex-col divide-y divide-border">
            {suppressed.map((s) => (
              <li key={s.id} className="flex flex-col gap-0.5 py-2 text-[13px]">
                <div className="flex items-center gap-2">
                  <Badge variant={severityVariant(s.severity)}>{s.severity}</Badge>
                  <Badge variant="warn">parked</Badge>
                  <span className="truncate flex-1" title={`${s.title}\n${s.resource}`}>
                    {s.title}
                  </span>
                  <span className="text-[12px] text-muted shrink-0 hidden lg:inline">
                    {s.source}
                  </span>
                  {s.url ? (
                    <a
                      href={s.url}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="text-[12px] text-accent hover:underline shrink-0"
                    >
                      Provider
                    </a>
                  ) : null}
                </div>
                {/* Attribution, and an explicit admission when there is none — never imply
                    we know who parked it. The reason wording differs because the next move
                    differs: a silence is a person's decision to review or let expire, an
                    inhibition means the thing to look at is the OTHER alert. */}
                <span className="text-[12px] text-muted pl-1">
                  {s.suppressed_reason === 'inhibited'
                    ? s.suppressed_by
                      ? `Inhibited by ${s.suppressed_by} — a higher-ranked alert is masking this one; go look at that alert.`
                      : 'Inhibited by another alert — a higher-ranked alert is masking this one.'
                    : s.suppressed_by
                      ? `Silenced by ${s.suppressed_by} — review or let the silence expire at the provider.`
                      : 'This provider published no attribution, so we cannot say who parked it or when it comes back.'}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      <Card>
        <CardTitle>
          <Inbox className="lucide-inline" /> Firing, not yet claimed
        </CardTitle>
        <p className="text-[13px] text-muted mb-2">
          The heartbeat claims a few per cycle, so a burst leaves a queue. Claim one
          now to start its investigation without waiting.
        </p>

        {!signalsQuery.data ? (
          <p className="text-sm text-muted">Poll to see what is firing.</p>
        ) : unclaimed.length === 0 ? (
          <EmptyState
            icon={<Radio className="lucide-inline" />}
            title="Nothing unclaimed"
            subtitle={
              firing.length > 0
                ? 'Everything currently firing already has an incident.'
                : suppressed.length > 0
                  ? // "No sources are reporting a firing signal" is true here but reads as
                    // "all quiet", which is the misreading this whole item exists to stop.
                    `Nothing is firing — but ${suppressed.length} signal(s) are parked at the provider (see above) and will not be picked up.`
                  : 'No sources are reporting a firing signal.'
            }
          />
        ) : (
          <ul className="flex flex-col divide-y divide-border">
            {unclaimed.map((s) => (
              <li key={s.id} className="flex items-center gap-2 py-2 text-[13px]">
                <Badge variant={severityVariant(s.severity)}>{s.severity}</Badge>
                <span className="truncate flex-1" title={`${s.title}\n${s.resource}`}>
                  {s.title}
                </span>
                <span className="text-[12px] text-muted shrink-0 hidden lg:inline">
                  {s.source}
                </span>
                {s.url ? (
                  <a
                    href={s.url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-[12px] text-accent hover:underline shrink-0"
                  >
                    Provider
                  </a>
                ) : null}
                <Btn
                  disabled={claimMutation.isPending}
                  onClick={() => claimMutation.mutate(s)}
                >
                  Claim
                </Btn>
              </li>
            ))}
          </ul>
        )}

        {/* What the claim FOUND, on the one surface that can say it.
            `exact_match_ids` and `fast_path` are properties of a lookup, not of the stored
            incident, so `/state` and `/incident` do not carry them and the Board correctly
            refuses to re-derive them (`record_use` binds the provider key on match, so from
            occurrence two onward every shape match would render as exact). This claim
            response is the ONLY place either is observable, which is exactly why both were
            declared in api.ts and read by nothing.

            The distinction is the operator's, not a detail: an exact match came from the
            provider's own identity, while a shape match came from a hash over rendered text
            with bare digits stripped — which provably over-merges a 4xx and a 5xx alarm on
            one resource. `fast_path` says whether the agent will propose the remembered fix
            directly, so it is the difference between reading the incident and checking it. */}
        {claimMutation.data ? (
          <p className="text-[12px] mt-2">
            <span className="text-ok">
              Claimed {claimMutation.data.incident.incident_id}.
            </span>{' '}
            <span className="text-muted">
              {claimMutation.data.matches.length === 0
                ? 'Nothing in the ledger matched, so the investigation starts from scratch.'
                : `${claimMutation.data.matches.length} ledger entr${
                    claimMutation.data.matches.length === 1 ? 'y' : 'ies'
                  } matched — ` +
                  (claimMutation.data.exact_match_ids.length > 0
                    ? `${claimMutation.data.exact_match_ids.length} on this provider's own identity, which is an exact match.`
                    : "all on our shape hash, which merges alarms differing only in a number. Weigh the remembered fix accordingly.") +
                  (claimMutation.data.fast_path
                    ? ' A proven pattern matched, so the agent may propose its fix directly.'
                    : ' No pattern cleared the fast-path bar, so the agent must confirm before proposing.')}
              {claimMutation.data.similar.length > 0
                ? ` ${claimMutation.data.similar.length} similar lesson(s) attached as context — a near-miss, not a match.`
                : ''}
            </span>
          </p>
        ) : null}

        {claimMutation.isError ? (
          <p className="text-[12px] text-danger mt-2">
            {(claimMutation.error as Error).message}
          </p>
        ) : null}
      </Card>

      {/* Positively recovered, which is NOT the same as "gone". A caller may resolve on
          these without consulting poll_health, because an explicit provider `ok` is
          evidence while an absence is only the absence of a look.

          Only rendered after a poll, and legitimately empty on most installs: the webhook
          adapter is currently the only one that can emit a recovered state (every other
          adapter hardcodes firing), so a CloudWatch-only install will never populate this.
          That is correct, not dead UI — the alternative is silently discarding the one
          state that lets an operator close work with evidence. */}
      {signalsQuery.data && cleared.length > 0 ? (
        <Card>
          <CardTitle>
            <CheckCircle2 className="lucide-inline" /> Reported recovered
          </CardTitle>
          <p className="text-[13px] text-muted mb-2">
            The provider said these are back to normal. Unlike a signal that merely stopped
            appearing, this is evidence you can resolve on.
          </p>
          <ul className="flex flex-col divide-y divide-border">
            {cleared.map((s) => (
              <li key={s.id} className="flex items-center gap-2 py-2 text-[13px]">
                <Badge variant="ok">recovered</Badge>
                <span className="truncate flex-1" title={`${s.title}\n${s.resource}`}>
                  {s.title}
                </span>
                <span className="text-[12px] text-muted shrink-0 hidden lg:inline">
                  {s.source}
                </span>
                {s.url ? (
                  <a
                    href={s.url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="text-[12px] text-accent hover:underline shrink-0"
                  >
                    Provider
                  </a>
                ) : null}
              </li>
            ))}
          </ul>
        </Card>
      ) : null}
    </div>
  )
}
