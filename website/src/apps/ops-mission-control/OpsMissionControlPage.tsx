/**
 * Ops Mission Control — the board.
 *
 * Three tabs: **Board** (claimed incidents, their status, and the embedded
 * investigation chat), **Signals** (source health, what the last poll returned, and
 * firing signals not yet claimed — see `SignalsPanel`), and **Settings** (providers,
 * autonomy, instance). The Knowledge ledger sits under the Board because it is read
 * while working an incident, not while configuring one.
 *
 * This is a BUILTIN dashboard page (rendered by BuiltinAppRoute inside the main
 * React tree), so it uses same-origin `fetch` with the dashboard's session
 * cookie — NOT the app-sdk hooks, which require <AppApiProvider> and only wrap
 * standalone/installed apps via AppHost.
 *
 * Backend contract: kiro_crew/apps/builtins/ops_mission_control/backend/routes.py
 * Design: docs/task-specs/2026/07/ops-mission-control/spec.md
 */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Radio,
  ShieldCheck,
  ShieldAlert,
  Activity,
  BookOpen,
  Users,
  CircleDot,
  UserCheck,
  CheckCircle2,
  Clock,
  AlertTriangle,
  RefreshCw,
} from 'lucide-react'
import { Badge, Btn, Card, CardTitle, EmptyState, PageHeader, StatCard } from '../../components/ui'
import SegmentedControl from '../../components/SegmentedControl'
import SettingsPanel from './SettingsPanel'
import SignalsPanel from './SignalsPanel'
import HandoverPanel from './HandoverPanel'
import IncidentChat from './IncidentChat'
import {
  opsApi,
  type Incident,
  type IncidentStatus,
  type LedgerEntry,
  type OperatingMode,
  type ProviderInfo,
} from './api'

/** Poll fast while work is live, slowly when idle — no SSE (it clobbers state on connect). */
const POLL_ACTIVE_MS = 5000
const POLL_IDLE_MS = 30000

// Module-level frozen empties so the render-time fallbacks are referentially
// stable across renders (see the memos in the component body).
const EMPTY_INCIDENTS: readonly Incident[] = Object.freeze([])
const EMPTY_PROVIDERS: readonly ProviderInfo[] = Object.freeze([])
const EMPTY_LEDGER: readonly LedgerEntry[] = Object.freeze([])

const STATUS_LABEL: Record<IncidentStatus, string> = {
  unclaimed: 'Unclaimed',
  dispatched: 'Dispatched',
  investigating: 'Investigating',
  needs_human: 'Needs human',
  resolved: 'Resolved',
  escalated: 'Escalated',
  stale: 'Stale',
}

/**
 * What the incident is waiting for. Shown INSTEAD of the bare status, because
 * "Needs human" reads identically whether the agent wants one click of approval or
 * has run out of ideas — and the operator's next action is completely different.
 */
const BLOCKED_LABEL: Record<string, string> = {
  awaiting_approval: 'Approve to continue',
  awaiting_input: 'Waiting on you',
  awaiting_diagnosis: 'Stopped, no diagnosis',
}

/** The row's status text: the blocked reason when blocked, else the status. */
function statusText(inc: Incident): string {
  const reason = inc.blocked_reason
  if (reason && BLOCKED_LABEL[reason]) return BLOCKED_LABEL[reason]
  return STATUS_LABEL[inc.status]
}

function StatusIcon({ status }: { status: IncidentStatus }) {
  switch (status) {
    case 'investigating':
    case 'dispatched':
      return <Activity className="lucide-inline text-accent" />
    case 'needs_human':
      return <UserCheck className="lucide-inline text-warn" />
    case 'resolved':
      return <CheckCircle2 className="lucide-inline text-ok" />
    case 'escalated':
      return <AlertTriangle className="lucide-inline text-danger" />
    case 'stale':
      return <Clock className="lucide-inline text-muted" />
    default:
      return <CircleDot className="lucide-inline text-muted" />
  }
}

function severityVariant(severity: string): 'err' | 'warn' | 'muted' {
  if (severity === 'critical') return 'err'
  if (severity === 'warning') return 'warn'
  return 'muted'
}

/** Compact relative age, e.g. "12m", "3h". */
function age(iso: string): string {
  if (!iso) return '—'
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return '—'
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000))
  if (secs < 60) return `${secs}s`
  if (secs < 3600) return `${Math.floor(secs / 60)}m`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`
  return `${Math.floor(secs / 86400)}d`
}

function ModeBadge({ mode }: { mode: OperatingMode }) {
  // Observe is the safe default and should read as reassuring, not as a warning.
  if (mode === 'observe') {
    return (
      <Badge variant="ok" title="Read-only — nothing is written to any provider">
        <ShieldCheck className="lucide-inline" /> Observe
      </Badge>
    )
  }
  if (mode === 'propose') {
    return (
      <Badge variant="muted" title="Drafts actions and asks — nothing executes unapproved">
        <ShieldCheck className="lucide-inline" /> Propose
      </Badge>
    )
  }
  return (
    <Badge variant="warn" title="Executes actions granted by your rules — every one is audited">
      <ShieldAlert className="lucide-inline" /> Act
    </Badge>
  )
}

type MainView = 'board' | 'signals' | 'handover' | 'settings'

const VIEW_SEGMENTS: { key: MainView; label: string }[] = [
  { key: 'board', label: 'Board' },
  { key: 'signals', label: 'Signals' },
  { key: 'handover', label: 'Handover' },
  { key: 'settings', label: 'Settings' },
]

export default function OpsMissionControlPage() {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<string | null>(null)
  const [view, setView] = useState<MainView>('board')

  const stateQuery = useQuery({
    queryKey: ['ops-mission-control', 'state'],
    queryFn: () => opsApi.state(),
    refetchInterval: (query) => {
      const incidents = query.state.data?.incidents ?? []
      const live = incidents.some(
        (i) => i.status === 'investigating' || i.status === 'dispatched',
      )
      return live ? POLL_ACTIVE_MS : POLL_IDLE_MS
    },
  })

  const ledgerQuery = useQuery({
    queryKey: ['ops-mission-control', 'ledger'],
    queryFn: () => opsApi.ledger(),
    refetchInterval: POLL_IDLE_MS,
  })

  const transitionMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: IncidentStatus }) =>
      opsApi.transition(id, status),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'state'] })
    },
  })

  // Manual trigger for the same cycle the dispatch cron runs. Present because a
  // user who has just connected a provider should be able to prove it works now
  // rather than waiting up to a heartbeat to find out they mistyped a region.
  const dispatchMutation = useMutation({
    mutationFn: () => opsApi.dispatch(),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'state'] })
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'ledger'] })
    },
  })

  const state = stateQuery.data
  const rotation = state?.rotation
  const ledgerEntries = ledgerQuery.data?.entries ?? EMPTY_LEDGER

  // Stable empty-array fallbacks: a `?? []` inline would allocate a fresh array
  // on every render, so the memos below would never hit their cache.
  const incidents = state?.incidents ?? EMPTY_INCIDENTS
  const providers = state?.providers ?? EMPTY_PROVIDERS

  const configuredProviders = useMemo(() => providers.filter((p) => p.configured), [providers])

  const selectedIncident: Incident | null = useMemo(
    () => incidents.find((i) => i.incident_id === selected) ?? null,
    [incidents, selected],
  )

  // Entry id -> entry, so an incident's `ledger_matches` (ids only) can render the actual
  // remembered pattern and fix. The board previously said just "2 matched", which is the
  // compounding-memory payoff reduced to a number: a responder could not see WHAT was
  // remembered without reading the agent's chat transcript. No new endpoint needed — the
  // page already fetches the whole ledger for the Ledger tab.
  const ledgerById = useMemo(() => {
    const map = new Map<string, LedgerEntry>()
    for (const entry of ledgerEntries) map.set(entry.entry_id, entry)
    return map
  }, [ledgerEntries])

  // `unknown` no longer implies "armed". Under strict gating a schedule that cannot say
  // whether this operator is on call DISARMS the dispatch tier, so the label must report
  // what actually happened — a badge reading "tier armed" beside an instance that has
  // stopped picking up work is the most misleading thing this header could say.
  const shiftLabel = rotation?.unknown
    ? rotation.on_shift
      ? 'rotation unknown — tier armed'
      : 'rotation unknown — not picking up work'
    : rotation?.on_shift
      ? rotation.who
        ? `on shift: ${rotation.who}`
        : 'on shift'
      : rotation?.who
        ? `off shift — ${rotation.who} is on call`
        : 'off shift'

  return (
    <>
      <PageHeader
        title="Mission Control"
        subtitle="Autonomous first responder for your alarms, pages, and monitors"
        actions={
          <div className="flex items-center gap-2">
            {view === 'board' ? (
              <Btn
                disabled={dispatchMutation.isPending}
                onClick={() => dispatchMutation.mutate()}
                title="Poll every configured source now and claim anything new"
              >
                <RefreshCw className="lucide-inline" />{' '}
                {dispatchMutation.isPending ? 'Checking…' : 'Check now'}
              </Btn>
            ) : null}
            {rotation ? <ModeBadge mode={rotation.mode} /> : null}
            <Badge variant={rotation?.on_shift || rotation?.unknown ? 'ok' : 'muted'}>
              <Radio className="lucide-inline" /> {shiftLabel}
            </Badge>
          </div>
        }
      />

      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        <div className="mb-4">
          <SegmentedControl segments={VIEW_SEGMENTS} value={view} onChange={setView} layoutId="omc-view" />
        </div>

        {view === 'settings' ? (
          <SettingsPanel />
        ) : view === 'signals' ? (
          <SignalsPanel />
        ) : view === 'handover' ? (
          <HandoverPanel />
        ) : (
        <>
        {dispatchMutation.data && !dispatchMutation.data.changed ? (
          <p className="text-[13px] text-muted mb-4">
            {dispatchMutation.data.skipped_reason ||
              (configuredProviders.length === 0
                ? 'No providers are set up yet — open Settings to connect one.'
                : `Polled ${dispatchMutation.data.polled} firing signal(s); nothing new to claim.`)}
          </p>
        ) : null}
        {dispatchMutation.isError ? (
          <p className="text-[13px] text-danger mb-4">
            {(dispatchMutation.error as Error).message}
          </p>
        ) : null}

        <div className="grid gap-3.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))] mb-6">
          <StatCard
            label="Active"
            value={incidents.filter((i) => i.status !== 'resolved').length}
            accent
          />
          {/* Count blocked incidents from the live board rather than the status
              tally: an incident parked on an approval is what the operator needs
              to act on, and the tally lags a same-request reconcile. */}
          <StatCard
            label="Waiting on you"
            value={incidents.filter((i) => i.blocked_reason).length}
          />
          <StatCard label="Sources wired" value={configuredProviders.length} />
          <StatCard label="Patterns known" value={state?.ledger?.total ?? 0} />
        </div>

        {/* Board gets the full width now that source health lives in its own tab —
            an incident title plus its status and age was being truncated into a
            280px-narrower column for a rail that only showed ready/not-set-up. */}
        <div className="mb-6">
          <Card>
            <CardTitle>Board</CardTitle>
            {stateQuery.isLoading ? (
              <p className="text-sm text-muted">Loading…</p>
            ) : incidents.length === 0 ? (
              <EmptyState
                icon={<ShieldCheck className="lucide-inline" />}
                title="Nothing is firing"
                subtitle={
                  configuredProviders.length === 0
                    ? 'Connect a provider in Settings to start watching.'
                    : 'Signals that fire will be claimed and investigated here.'
                }
              />
            ) : (
              <ul className="flex flex-col divide-y divide-border mt-1">
                {incidents.map((inc) => (
                  <li key={inc.incident_id}>
                    <button
                      type="button"
                      // Stable hook for the browser spec. The row has no accessible name
                      // of its own (its content is the incident id plus a title that
                      // varies per signal), so a text selector would be pinned to seeded
                      // fixture data and break the moment the fixture changes.
                      data-testid="omc-incident-row"
                      onClick={() =>
                        setSelected(selected === inc.incident_id ? null : inc.incident_id)
                      }
                      className="w-full flex items-center gap-2 py-2 text-left text-sm hover:bg-card-hover"
                    >
                      <StatusIcon status={inc.status} />
                      <span className="font-mono text-xs text-muted shrink-0">
                        {inc.incident_id}
                      </span>
                      <span className="truncate flex-1" title={inc.signal.title}>
                        {inc.signal.title}
                      </span>
                      <Badge variant={severityVariant(inc.signal.severity)}>
                        {inc.signal.severity}
                      </Badge>
                      {/* A blocked incident is emphasised: the operator scans this
                          column to find what needs them, so "waiting on you" must
                          not look the same as "progressing". */}
                      <span
                        className={`text-xs shrink-0 w-32 text-right ${
                          inc.blocked_reason ? 'text-warn font-medium' : 'text-muted'
                        }`}
                      >
                        {statusText(inc)}
                      </span>
                      <span className="text-xs text-muted shrink-0 w-10 text-right">
                        {age(inc.updated_at || inc.claimed_at)}
                      </span>
                    </button>

                    {selected === inc.incident_id ? (
                      <div className="pb-3 pl-6 pr-2 flex flex-col gap-2">
                        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                          <dt className="text-muted">Source</dt>
                          <dd>{inc.signal.source}</dd>
                          <dt className="text-muted">Resource</dt>
                          <dd className="truncate">{inc.signal.resource || '—'}</dd>
                          <dt className="text-muted">Mode</dt>
                          <dd>
                            <ModeBadge mode={inc.operating_mode} />
                          </dd>
                          <dt className="text-muted">Known patterns</dt>
                          <dd>
                            {inc.ledger_matches.length > 0
                              ? `${inc.ledger_matches.length} matched`
                              : 'none matched'}
                          </dd>
                        </dl>

                        {/* The remembered fix, not just a count. This is the whole point
                            of the ledger: on a second occurrence the responder should be
                            able to read what worked last time without opening the agent's
                            transcript. Trust/confidence and use count are shown BECAUSE
                            an unproven entry must not read like a proven one. */}
                        {inc.ledger_matches.length > 0 ? (
                          <div className="flex flex-col gap-1.5">
                            {inc.ledger_matches.map((entryId) => {
                              const entry = ledgerById.get(entryId)
                              if (!entry) {
                                // Hygiene may have pruned it, or the ledger query is still
                                // in flight. Say which id, rather than rendering nothing —
                                // a silently missing match reads as "no prior knowledge".
                                return (
                                  <p key={entryId} className="text-xs text-muted">
                                    Matched entry {entryId.slice(0, 8)} is no longer in the
                                    ledger.
                                  </p>
                                )
                              }
                              return (
                                <div
                                  key={entryId}
                                  className="rounded border border-subtle px-2 py-1.5 text-xs"
                                >
                                  <div className="flex items-center gap-1.5 flex-wrap">
                                    <span
                                      className={
                                        entry.trust === 'verified'
                                          ? 'text-success'
                                          : 'text-warning'
                                      }
                                    >
                                      {entry.trust}
                                    </span>
                                    <span className="text-muted">·</span>
                                    <span className="text-muted">
                                      {entry.confidence} confidence
                                    </span>
                                    <span className="text-muted">·</span>
                                    <span className="text-muted">
                                      used {entry.use_count}&times;
                                    </span>
                                  </div>
                                  <p className="mt-1">{entry.pattern}</p>
                                  <p className="mt-0.5 text-muted">
                                    <span className="text-text">Fix:</span> {entry.fix}
                                  </p>
                                </div>
                              )
                            })}
                          </div>
                        ) : null}

                        {inc.diagnosis ? (
                          <p className="text-sm whitespace-pre-wrap">{inc.diagnosis}</p>
                        ) : (
                          <p className="text-sm text-muted">No diagnosis recorded yet.</p>
                        )}

                        <div className="flex items-center gap-2 flex-wrap">
                          {inc.signal.url ? (
                            <a
                              href={inc.signal.url}
                              target="_blank"
                              rel="noreferrer noopener"
                              className="text-xs text-accent hover:underline"
                            >
                              Open in provider
                            </a>
                          ) : null}
                          {inc.status === 'investigating' || inc.status === 'needs_human' ? (
                            <Btn
                              disabled={transitionMutation.isPending}
                              onClick={() =>
                                transitionMutation.mutate({
                                  id: inc.incident_id,
                                  status: 'resolved',
                                })
                              }
                            >
                              Mark resolved
                            </Btn>
                          ) : null}
                        </div>

                        {transitionMutation.isError && selectedIncident?.incident_id === inc.incident_id ? (
                          <p className="text-xs text-danger">
                            {(transitionMutation.error as Error).message}
                          </p>
                        ) : null}

                        {/* The live investigation. Mounted only for the expanded
                            row: each embed polls its own slot, so rendering one
                            per incident would multiply the poll traffic by the
                            board's length for conversations nobody is reading.
                            IncidentChat owns its own bounded height (that bound is
                            what makes the transcript scroll), so no wrapper here. */}
                        <IncidentChat
                          incidentId={inc.incident_id}
                          title={inc.signal.title}
                        />
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>

        {/* Team composition. Only rendered when a committed rotation.yaml is the source —
            a solo install has no team and an empty panel would just be noise. Placed
            above the ledger because "who is handling this" is the question an operator
            asks BEFORE "what do we know about it", and because a disarmed instance needs
            an explanation near the top rather than buried. */}
        {rotation?.roster?.members?.length ? (
          <Card className="mb-4">
            <CardTitle>
              <Users className="lucide-inline" /> On-call team
              <span className="text-[12px] text-muted font-normal ml-2">
                {rotation.roster.timezone}
                {rotation.roster.strict_gating ? ' · only the on-call instance picks up work' : ''}
              </span>
            </CardTitle>

            <ul className="flex flex-col gap-1 mt-2">
              {rotation.roster.members.map((m) => {
                const isMe = !!rotation.roster?.me && m.login === rotation.roster.me
                return (
                  <li key={m.login} className="flex items-center gap-2 text-sm py-1">
                    <span className={m.on_call_now ? 'text-success' : 'text-muted'}>
                      <Radio className="lucide-inline" />
                    </span>
                    <span className={isMe ? 'font-semibold text-text-strong' : ''}>
                      {m.login}
                      {isMe ? ' (this instance)' : ''}
                    </span>
                    {m.on_call_now ? (
                      <Badge variant="ok">on call now</Badge>
                    ) : null}
                    <span className="text-[12px] text-muted ml-auto">
                      {m.shifts} shift{m.shifts === 1 ? '' : 's'}
                    </span>
                  </li>
                )
              })}
            </ul>

            {/* The two states that look identical from the board but mean very different
                things: a normal off-shift instance vs. one that will never pick up work
                because it is not on the rotation at all. Saying so here is the difference
                between "waiting my turn" and a setup mistake nobody notices. */}
            {rotation.roster.me && !rotation.roster.me_on_roster ? (
              <p className="text-xs text-warning mt-2">
                This instance ({rotation.roster.me}) is not named in the schedule, so it
                will never pick up work. Add it to rotation.yaml, or turn off strict
                gating.
              </p>
            ) : null}
            {!rotation.roster.me ? (
              <p className="text-xs text-warning mt-2">
                No GitHub login resolved for this instance, so it cannot tell whether it is
                on call. Set the schedule-file provider&apos;s github_login, or authenticate
                the gh CLI.
              </p>
            ) : null}
            {rotation.roster.error ? (
              <p className="text-xs text-danger mt-2">
                Schedule problem: {rotation.roster.error}
              </p>
            ) : null}
          </Card>
        ) : null}

        <Card>
          <CardTitle>
            <BookOpen className="lucide-inline" /> Knowledge ledger
          </CardTitle>
          {ledgerEntries.length === 0 ? (
            <p className="text-sm text-muted mt-2">
              Empty. Each investigation that finds a reusable fix records it here, so a
              repeat failure resolves from what you already know.
            </p>
          ) : (
            <table className="w-full text-xs mt-2">
              <thead>
                <tr className="text-muted text-left">
                  <th className="font-normal py-1">Pattern</th>
                  <th className="font-normal py-1 w-24">Confidence</th>
                  <th className="font-normal py-1 w-20">Trust</th>
                  <th className="font-normal py-1 w-16 text-right">Used</th>
                </tr>
              </thead>
              <tbody>
                {ledgerEntries.slice(0, 25).map((entry) => (
                  <tr key={entry.entry_id} className="border-t border-border">
                    <td className="py-1.5 pr-2">
                      <span title={entry.fix}>{entry.pattern}</span>
                    </td>
                    <td className="py-1.5">{entry.confidence}</td>
                    <td className="py-1.5">
                      <Badge variant={entry.trust === 'verified' ? 'ok' : 'muted'}>
                        {entry.trust}
                      </Badge>
                    </td>
                    <td className="py-1.5 text-right">{entry.use_count}×</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
        </>
        )}
      </div>
    </>
  )
}
