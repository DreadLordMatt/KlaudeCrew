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
 * - Which firing signals are NOT yet incidents. Under the claim cap (3/cycle) an
 *   alarm storm legitimately leaves a queue, and before this the remainder was a
 *   number with no detail — you could see "12 remaining" but not what they were.
 * - Claim one now, rather than waiting for the next heartbeat.
 */
import { useMemo } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Radio, AlertTriangle, Inbox } from 'lucide-react'
import { Badge, Btn, Card, CardTitle, EmptyState } from '../../components/ui'
import { opsApi, type ProviderInfo, type Signal } from './api'

/** Signals are polled on demand, not on a timer: each poll hits a paid provider API. */
const SIGNALS_QUERY_KEY = ['ops-mission-control', 'signals'] as const

/** Module-level frozen empty so the render-time fallback is referentially stable. */
const EMPTY_PROVIDERS: readonly ProviderInfo[] = Object.freeze([])

function severityVariant(severity: string): 'err' | 'warn' | 'muted' {
  if (severity === 'critical') return 'err'
  if (severity === 'warning') return 'warn'
  return 'muted'
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
  const errors = signalsQuery.data?.errors ?? {}
  const unclaimed = signalsQuery.data?.unclaimed ?? []
  const polled = signalsQuery.data?.signals ?? []

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
                <th className="font-normal py-1 pl-2">Last poll</th>
              </tr>
            </thead>
            <tbody>
              {signalSources.map((p) => {
                const err = errors[p.id]
                const count = polled.filter((s) => s.source === p.id).length
                return (
                  <tr key={p.id} className="border-t border-border align-top">
                    <td className="py-2 pr-2">
                      <span title={p.detail}>{p.display_name}</span>
                    </td>
                    <td className="py-2">
                      <Badge variant={err ? 'err' : p.configured ? 'ok' : 'muted'}>
                        {err ? 'error' : p.configured ? 'ready' : 'not set up'}
                      </Badge>
                    </td>
                    <td className="py-2 text-right pr-4">{p.configured ? count : '—'}</td>
                    <td className="py-2 pl-2 text-muted">
                      {/* The error text verbatim: a provider that reports "ready"
                          from config alone can still fail on expired credentials,
                          and the message is what tells the two apart. */}
                      {err ? (
                        <span className="text-danger flex items-start gap-1">
                          <AlertTriangle className="lucide-inline" />
                          <span className="break-all">{err}</span>
                        </span>
                      ) : signalsQuery.data ? (
                        p.configured ? 'ok' : 'skipped'
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
              {polled.length} firing · {unclaimed.length} not yet claimed
            </span>
          ) : null}
          {signalsQuery.isError ? (
            <span className="text-[12px] text-danger">
              {(signalsQuery.error as Error).message}
            </span>
          ) : null}
        </div>
      </Card>

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
              polled.length > 0
                ? 'Everything currently firing already has an incident.'
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

        {claimMutation.isError ? (
          <p className="text-[12px] text-danger mt-2">
            {(claimMutation.error as Error).message}
          </p>
        ) : null}
      </Card>
    </div>
  )
}
