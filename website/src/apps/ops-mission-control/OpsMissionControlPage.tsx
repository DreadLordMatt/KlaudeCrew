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
  Check,
  ClipboardCopy,
  CircleDot,
  FileText,
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
  BLOCKED_LABEL,
  describeSourceHealth,
  describeVerification,
  entryIsProven,
  MIN_USES_FOR_FAST_PATH,
  opsApi,
  SIGNALS_QUERY_KEY,
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

/** The row's status text: the blocked reason when blocked, else the status. */
function statusText(inc: Incident): string {
  // `BLOCKED_LABEL` is imported from ./api rather than defined here: the handover digest
  // renders the same field, and when each surface owned its own wording they drifted.
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

/**
 * A shift end as a local time, or '' when it cannot be parsed.
 *
 * Returns EMPTY rather than the raw string on a parse failure, and the caller then omits the
 * clause entirely. `ShiftStatus.until` comes from a hand-edited `rotation.yaml` by way of
 * `datetime.isoformat()`, so a malformed schedule can put anything here — and a shift badge
 * reading "on shift: octocat until 2026-13-45" is worse than one that simply does not say,
 * because the rest of the badge is true and the reader has no way to tell which part broke.
 * The roster card already reports a schedule parse error in full.
 */
function shiftEnd(iso: string): string {
  const at = Date.parse(iso)
  if (Number.isNaN(at)) return ''
  return new Date(at).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
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

/**
 * Cache key for the closed-history list. Module-level so `transitionMutation.onSettled`
 * can invalidate the exact entry the section below reads — that invalidation is what closes
 * the loop: "Mark resolved" drops a row off the Board (`/state` returns only open
 * incidents) and it has to reappear here, with its postmortem, in the same beat.
 */
const CLOSED_QUERY_KEY = ['ops-mission-control', 'incidents', 'closed'] as const

/**
 * Ledger rows the table renders. Named because the footer has to cite it: entries are sorted
 * `-use_count`, which is exactly the order that pushes a REFUTED entry down, so a reader who
 * sees no red rows has not seen the whole ledger and the footer says so by number.
 */
const LEDGER_ROWS_SHOWN = 25

/** The two terminal statuses, per the status grammar (`models.TERMINAL_STATUSES`). */
const CLOSED_STATUSES: readonly IncidentStatus[] = Object.freeze(['resolved', 'escalated'])

/**
 * The postmortem for one closed incident, fetched on expand.
 *
 * Rendered VERBATIM in a `<pre>`, not through MarkdownRenderer. The point of this file is
 * that it goes to somebody who does not run KiroCrew — a ticket, a review, a colleague — so
 * what the operator needs to see is the exact bytes that person will receive. A rendered
 * view would quietly hide the metadata table's pipes and, worse, would make a redaction
 * marker easy to miss.
 *
 * Fetched per expanded row rather than with the list: a closed-history list is long by
 * definition and reading every artifact to show a summary nobody asked for would turn one
 * request into 200 file reads.
 */
function ClosedPostmortem({ incidentId }: { incidentId: string }) {
  const [copied, setCopied] = useState(false)
  const query = useQuery({
    queryKey: ['ops-mission-control', 'incident', incidentId],
    queryFn: () => opsApi.incident(incidentId),
  })

  const log = query.data?.log ?? ''
  const logPath = query.data?.log_path ?? ''

  const copy = async () => {
    // Same handling as the handover digest's copy button, including the silent failure
    // branch: a blocked clipboard (insecure context, denied permission) needs no error,
    // because the text it would have copied is already on screen to select by hand.
    if (!log || typeof navigator === 'undefined' || !navigator.clipboard) return
    try {
      await navigator.clipboard.writeText(log)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      /* clipboard blocked — the text is on screen */
    }
  }

  if (query.isLoading) {
    return <p className="text-xs text-muted">Reading the postmortem…</p>
  }
  if (query.isError) {
    return <p className="text-xs text-danger">{(query.error as Error).message}</p>
  }
  if (!log) {
    // Two different empties, and neither is "the investigation found nothing". Say which,
    // and do NOT imply a file exists — `log_path` is empty here by construction.
    return (
      <p className="text-xs text-muted">
        No postmortem was written for this incident. Incidents that closed before
        postmortems were written do not have one, and nothing back-fills them — the record
        would have to be invented.
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2 flex-wrap">
        <Btn onClick={copy} title="Copy the postmortem exactly as written, for a ticket or a review">
          {copied ? <Check className="lucide-inline" /> : <ClipboardCopy className="lucide-inline" />}{' '}
          {copied ? 'Copied' : 'Copy postmortem'}
        </Btn>
        {/* Only when the backend says the file is really there. A path is a promise. */}
        {logPath ? (
          <span className="font-mono text-[11px] text-muted break-all" title={logPath}>
            {logPath}
          </span>
        ) : null}
      </div>
      <pre className="font-mono text-[11px] whitespace-pre-wrap break-all max-h-80 overflow-auto rounded border border-subtle px-2 py-1.5">
        {log}
      </pre>
    </div>
  )
}

/**
 * Closed incidents and the artifact each one left behind.
 *
 * Exists because the renderer that writes `incidents/<id>.md` had no reader: the file is
 * this app's only output for someone who does not run KiroCrew, and until now nothing in
 * the UI called the route that returns it. `/state` deliberately carries open work only, so
 * a resolved incident vanished from every surface the moment it was resolved.
 */
function ClosedIncidents() {
  const [expanded, setExpanded] = useState<string | null>(null)

  // No refetchInterval: closed history does not change on its own. It is invalidated by
  // the transition that closes an incident, which is the only thing that can add a row.
  const query = useQuery({
    queryKey: CLOSED_QUERY_KEY,
    queryFn: () => opsApi.incidents(),
  })

  // Filtered client-side because `/incidents` takes ONE status and "closed" is two.
  const closed = useMemo(
    () => (query.data?.incidents ?? EMPTY_INCIDENTS).filter((i) => CLOSED_STATUSES.includes(i.status)),
    [query.data],
  )

  return (
    <Card className="mb-6">
      <CardTitle>
        <FileText className="lucide-inline" /> Closed — postmortems
      </CardTitle>
      {query.isLoading ? (
        <p className="text-sm text-muted mt-2">Loading…</p>
      ) : query.isError ? (
        <p className="text-[13px] text-danger mt-2">{(query.error as Error).message}</p>
      ) : closed.length === 0 ? (
        <p className="text-sm text-muted mt-2">
          Nothing has closed yet. When an incident is resolved or escalated, its postmortem
          is written here — one Markdown file per incident, which is the form to attach to a
          ticket or hand to a colleague.
        </p>
      ) : (
        <>
          {/* The server caps the list at 200 across ALL statuses, so say so rather than
              implying this is the whole history. */}
          {query.data?.truncated ? (
            <p className="text-xs text-warn mt-1">
              Showing the {closed.length} closed incident(s) in the most recent{' '}
              {query.data.incidents.length} of {query.data.total} — older ones are on disk
              but not in this list.
            </p>
          ) : null}
          <ul className="flex flex-col divide-y divide-border mt-1">
            {closed.map((inc) => (
              <li key={inc.incident_id}>
                <button
                  type="button"
                  data-testid="omc-closed-row"
                  onClick={() =>
                    setExpanded(expanded === inc.incident_id ? null : inc.incident_id)
                  }
                  className="w-full flex items-center gap-2 py-2 text-left text-sm hover:bg-card-hover"
                >
                  <StatusIcon status={inc.status} />
                  <span className="font-mono text-xs text-muted shrink-0">{inc.incident_id}</span>
                  <span className="truncate flex-1" title={inc.signal.title}>
                    {inc.signal.title}
                  </span>
                  <Badge variant={severityVariant(inc.signal.severity)}>
                    {inc.signal.severity}
                  </Badge>
                  <span className="text-xs text-muted shrink-0 w-24 text-right">
                    {STATUS_LABEL[inc.status]}
                  </span>
                  <span className="text-xs text-muted shrink-0 w-10 text-right">
                    {age(inc.updated_at || inc.claimed_at)}
                  </span>
                </button>
                {expanded === inc.incident_id ? (
                  <div className="pb-3 pl-6 pr-2">
                    <ClosedPostmortem incidentId={inc.incident_id} />
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
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

  // Read-only view of the Signals tab's cached poll. `enabled: false` and the SAME query
  // key, deliberately: /signals hits every configured provider, which costs rate-limit
  // budget and in some cases money, so merely looking at the board must never trigger one.
  // The board therefore knows exactly as much as the operator's last explicit poll — and
  // when that is nothing, it says so rather than assuming health. /state carries neither
  // poll_health nor all_sources_healthy, so there is no cheaper source for this.
  const cachedSignalsQuery = useQuery({
    queryKey: SIGNALS_QUERY_KEY,
    queryFn: () => opsApi.signals(),
    enabled: false,
  })

  const transitionMutation = useMutation({
    mutationFn: ({ id, status }: { id: string; status: IncidentStatus }) =>
      opsApi.transition(id, status),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['ops-mission-control', 'state'] })
      // Closing an incident REMOVES it from /state, so without this the row would simply
      // disappear: the closed-history section is where it (and the postmortem the close
      // just wrote) reappears, and it has no polling of its own.
      queryClient.invalidateQueries({ queryKey: CLOSED_QUERY_KEY })
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

  // Which sources the last explicit poll could not read. Named, not counted: "1 source
  // unhealthy" does not tell an operator whether their blind spot is the alarm feed that
  // matters or a monitor they barely use.
  const cachedSignals = cachedSignalsQuery.data
  const unhealthySources = useMemo(() => {
    if (!cachedSignals) return [] as string[]
    return providers
      .filter((p) => p.roles.includes('signal'))
      .filter((p) => {
        const state = describeSourceHealth(
          p.id,
          cachedSignals.poll_health,
          cachedSignals.errors,
          p.configured,
        ).state
        return state === 'failed' || state === 'backing_off'
      })
      .map((p) => p.display_name)
  }, [providers, cachedSignals])

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
  //
  // `until` is appended when the rotation source published a shift end. Only the
  // schedule-file provider does (`ShiftStatus.until`), so it is blank on a solo install and
  // the label degrades to what it always said. When it IS present it answers the question
  // the badge otherwise leaves open — "how long does this state hold" — which is the whole
  // reason a responder looks at a shift badge before starting something long.
  const shiftEnds = rotation?.until ? ` until ${shiftEnd(rotation.until)}` : ''
  const shiftLabel = rotation?.unknown
    ? rotation.on_shift
      ? 'rotation unknown — tier armed'
      : 'rotation unknown — not picking up work'
    : rotation?.on_shift
      ? rotation.who
        ? `on shift: ${rotation.who}${shiftEnds}`
        : `on shift${shiftEnds}`
      : rotation?.who
        ? `off shift — ${rotation.who} is on call${shiftEnds}`
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
                {/* "Poll & claim", not "Check now". The Signals tab's neighbouring button
                    is "Poll now" and is strictly READ-ONLY, while this one starts a
                    dispatch cycle that CLAIMS incidents and spends agent turns. As
                    similarly-named siblings the only thing carrying that difference was a
                    tooltip, which a hurried operator does not read — so the label names
                    the consequence instead. */}
                <RefreshCw className="lucide-inline" />{' '}
                {dispatchMutation.isPending ? 'Claiming…' : 'Poll & claim'}
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
                : // `polled` counts FIRING signals only, so on its own this sentence reports a
                  // smaller world than the cycle saw. A parked signal is one the cycle looked
                  // at and deliberately left alone, and saying so here is the difference
                  // between "all quiet" and "muted" on the surface an operator lands on.
                  `Polled ${dispatchMutation.data.polled} firing signal(s)` +
                  (dispatchMutation.data.suppressed > 0
                    ? `, plus ${dispatchMutation.data.suppressed} parked at the provider and left alone`
                    : '') +
                  '; nothing new to claim.')}
          </p>
        ) : null}
        {/* A cycle that DISCOVERED an action did not land, surfaced on the surface the
            operator lands on by default.

            This is the one verification outcome worth interrupting for, and the reason is
            asymmetric on purpose: the app previously reported these actions as applied, so
            this line is it retracting its own claim. `cleared` deliberately says nothing —
            announcing the expected outcome would make the button congratulate itself — and
            `unknown` says nothing either, because "we could not look" is not a finding and
            a later cycle retries it by itself. */}
        {(() => {
          const map = dispatchMutation.data?.verifications
          if (!map) return null
          const failed = Object.entries(map).filter(([, v]) => v === 'still_firing')
          if (failed.length === 0) return null
          return (
            <p className="text-[13px] text-danger mb-4 flex items-start gap-1.5">
              <AlertTriangle className="lucide-inline shrink-0 mt-0.5" />
              <span>
                {failed.length === 1
                  ? `${failed[0][0]} was still firing when we re-read it after the action this app reported as applied.`
                  : `${failed.length} incidents were still firing when we re-read them after actions this app reported as applied (${failed
                      .map(([id]) => id)
                      .join(', ')}).`}{' '}
                Expand the row below for what was attempted. The ledger entries cited have
                been marked as having failed.
              </span>
            </p>
          )
        })()}
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
          {/* "Known" counts everything the ledger holds, including guesses nobody has
              ever applied. This second card is the one that answers "how much of it would
              an agent propose without checking" — it clears the whole fast-path bar
              (verified, high, used ≥2×, never observed to fail), so it is normally much
              smaller than "known" and that gap is the honest picture. */}
          <StatCard label="Patterns proven" value={state?.ledger?.proven ?? 0} />
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
              /* An empty board is a claim about the world, and this used to make it
                 unconditionally: "Nothing is firing" was rendered whenever the incident
                 list was empty, with no reference to whether any source had answered. A
                 source failing every poll produces exactly that empty list. Three honest
                 branches instead, keyed on the Signals tab's cached poll (never a fresh
                 one — see cachedSignalsQuery). */
              <>
                {!cachedSignals ? (
                  <EmptyState
                    icon={<Radio className="lucide-inline" />}
                    title="No incidents claimed"
                    subtitle={
                      configuredProviders.length === 0
                        ? 'Connect a provider in Settings to start watching.'
                        : 'Source health has not been verified this session — poll from the Signals tab to confirm the quiet is real.'
                    }
                  />
                ) : cachedSignals.all_sources_healthy ? (
                  <EmptyState
                    icon={<ShieldCheck className="lucide-inline" />}
                    title="Nothing is firing"
                    subtitle="Every configured source answered the last poll, so this quiet is real."
                  />
                ) : (
                  <EmptyState
                    icon={<AlertTriangle className="lucide-inline" />}
                    title="Nothing claimed — but the board is not verified"
                    subtitle={
                      configuredProviders.length === 0
                        ? 'No source is configured, so nothing was polled. Connect a provider in Settings.'
                        : unhealthySources.length > 0
                          ? `${unhealthySources.join(', ')} did not answer the last poll, so absence of a signal does not mean recovery.`
                          : 'At least one source did not answer the last poll, so absence of a signal does not mean recovery.'
                    }
                  />
                )}
              </>
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
                          {/* HOW the ledger can identify this failure, which decides how
                              much a match is worth. Our own fingerprint is a hash over
                              rendered text with bare digits stripped, so "4xx error rate
                              above 5" and "5xx error rate above 1" on one resource hash
                              identically — a match on it can hand a responder a fix learned
                              from a different failure.

                              Stated one-directionally on purpose. An empty provider_key
                              PROVES this could only have been a shape match. A non-empty one
                              does NOT prove this particular match was exact, and the board
                              cannot find out: exactness is computed per lookup and lives on
                              the dispatch/claim response, not on the incident. The tempting
                              `provider_key in entry.provider_keys` check is forbidden here
                              because record_use BINDS the key on match, so from the second
                              occurrence onward every shape match would render as exact. */}
                          <dt className="text-muted">Match basis</dt>
                          <dd>
                            {inc.signal.provider_key ? (
                              <span>
                                {inc.signal.source} publishes an exact identity for this
                                failure (
                                <span className="font-mono">
                                  {inc.signal.provider_key.slice(0, 24)}
                                </span>
                                ), which the ledger prefers over our shape hash.
                              </span>
                            ) : (
                              <span className="text-warn">
                                This provider published no exact identity, so any ledger match
                                is our shape hash — a heuristic that merges alarms differing
                                only in a number, such as a 4xx and a 5xx rate on one
                                resource. Weigh a remembered fix accordingly.
                              </span>
                            )}
                          </dd>
                        </dl>

                        {/* WHETHER ANYTHING CHECKED that an action this app took landed.
                            A provider's 2xx means only "transmitted" — Checkmk dispatches
                            commands asynchronously through Livestatus and documents that
                            gap explicitly, and Nagios's command pipe returns nothing at
                            all — so the board used to be able to show an applied fix that
                            never took effect, with no code anywhere in a position to
                            notice.

                            Rendered only when an action was actually executed. A
                            "not applicable" row on every incident would bury the handful
                            where it matters, and an EMPTY `verification` genuinely means
                            "nothing was attempted", not "verified fine". Five states, all
                            worded by `describeVerification` in api.ts so no two surfaces
                            can phrase them differently — `unknown` above all, which is
                            neither a success nor a failure and is easy to word as
                            either. */}
                        {(() => {
                          const view = describeVerification(inc)
                          if (!view) return null
                          const tone =
                            view.variant === 'err'
                              ? 'text-danger'
                              : view.variant === 'warn'
                                ? 'text-warn'
                                : view.variant === 'ok'
                                  ? 'text-ok'
                                  : 'text-muted'
                          return (
                            <div className="rounded border border-subtle px-2 py-1.5 text-xs">
                              <div className="flex items-center gap-1.5 flex-wrap">
                                {view.variant === 'err' ? (
                                  <AlertTriangle className="lucide-inline text-danger" />
                                ) : view.variant === 'ok' ? (
                                  <CheckCircle2 className="lucide-inline text-ok" />
                                ) : (
                                  <Clock className="lucide-inline text-muted" />
                                )}
                                <span className="text-muted">
                                  {inc.last_action || 'action'} sent
                                  {/* `age` returns an em dash for a missing timestamp, so
                                      the "N ago" wording is conditional — "sent — ago"
                                      would be a sentence assembled around a hole. */}
                                  {inc.last_action_at
                                    ? ` ${age(inc.last_action_at)} ago`
                                    : ''}{' '}
                                  &mdash;
                                </span>
                                <span className={tone}>{view.label}</span>
                              </div>
                              <p className="mt-1 text-muted">{view.meaning}</p>
                              {/* The backend's own sentence, verbatim, because for
                                  `unknown` it names WHICH source did not answer — the fact
                                  an operator needs and the one a re-worded summary
                                  drops. */}
                              {inc.verification_detail ? (
                                <p className="mt-1">{inc.verification_detail}</p>
                              ) : null}
                            </div>
                          )
                        })()}

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
                                          ? 'text-ok'
                                          : 'text-warn'
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
                                    {/* The MISS count, beside the use count and never
                                        without it. `use_count` increments at claim time,
                                        before any outcome exists — so on its own it means
                                        "was shown to somebody", and an operator reading
                                        "used 4×" as corroboration is reading the wrong
                                        number. */}
                                    {entry.miss_count > 0 ? (
                                      <>
                                        <span className="text-muted">·</span>
                                        <span className="text-danger">
                                          failed {entry.miss_count}&times;
                                        </span>
                                      </>
                                    ) : null}
                                    {/* Whether the FAST PATH is unlocked, computed by the
                                        same predicate the engine uses (`entryIsProven`)
                                        rather than re-derived here — a panel that
                                        disagreed with the brief the agent was handed
                                        would leave the operator no way to tell which of
                                        the two was lying. */}
                                    <Badge variant={entryIsProven(entry) ? 'ok' : 'muted'}>
                                      {entryIsProven(entry)
                                        ? 'proven — agent may propose directly'
                                        : 'hypothesis — agent must confirm first'}
                                    </Badge>
                                  </div>
                                  <p className="mt-1">{entry.pattern}</p>
                                  <p className="mt-0.5 text-muted">
                                    <span className="text-text">Fix:</span> {entry.fix}
                                  </p>
                                  {/* Declared as demoted, in words. "failed 2×" above is
                                      the number; this is what it MEANS, and the
                                      distinction matters because a refuted fix is worth
                                      strictly less than an untested one — the opposite of
                                      how a high use count reads. */}
                                  {entry.miss_count > 0 ? (
                                    <p className="mt-1 text-warn">
                                      Demoted: this fix was applied and the signal was still
                                      firing afterwards
                                      {entry.last_miss ? ` (most recently ${entry.last_miss})` : ''}
                                      . It is kept because a fix that works sometimes is
                                      worth more than none, but treat it as already tried
                                      rather than as the answer.
                                    </p>
                                  ) : null}
                                  {/* Why it is only a hypothesis, when the reason is the
                                      new track-record floor rather than trust or
                                      confidence. Without this the badge is a verdict with
                                      no visible cause on an entry that reads
                                      verified/high. */}
                                  {!entryIsProven(entry) &&
                                  entry.miss_count === 0 &&
                                  entry.trust === 'verified' &&
                                  entry.confidence === 'high' ? (
                                    <p className="mt-1 text-muted">
                                      Marked verified and high confidence, but used only{' '}
                                      {entry.use_count}&times; — anyone can record those two
                                      values, so the fast path also needs{' '}
                                      {MIN_USES_FOR_FAST_PATH} uses before an agent proposes
                                      it without checking.
                                    </p>
                                  ) : null}
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

                        {/* Whether a reply typed into the Slack thread will actually reach
                            this investigation. Transient by nature: the backend computes it
                            while handling the transition and no GET returns it, so it is
                            rendered from the mutation result and deliberately neither
                            persisted nor re-fetched. `variables.id` scopes it to the row that
                            triggered it, since one mutation object serves the whole board.

                            `slack_thread_ts` was rejected as a proxy: linking also requires
                            Slack output to be configured AND a live investigation slot, so a
                            linked ts with no slot is precisely the false positive
                            test_slack_out.py pins — it would tell an operator their reply
                            lands when it will not. */}
                        {transitionMutation.data &&
                        transitionMutation.variables?.id === inc.incident_id ? (
                          transitionMutation.data.slack_thread_replyable ? (
                            <p className="text-xs text-ok">
                              Replies in the Slack thread reach this investigation.
                            </p>
                          ) : (
                            <p className="text-xs text-warn">
                              Replies in the Slack thread will NOT reach this investigation —
                              answer here in the chat below instead.
                            </p>
                          )
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

        {/* Closed work and its artifacts, directly under the live board: an operator who
            has just resolved something looks here next, and this is the only place the
            written record is readable. */}
        <ClosedIncidents />

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
                const rosterLeader = rotation.roster?.leader ?? ''
                return (
                  <li key={m.login} className="flex items-center gap-2 text-sm py-1">
                    <span className={m.on_call_now ? 'text-ok' : 'text-muted'}>
                      <Radio className="lucide-inline" />
                    </span>
                    <span className={isMe ? 'font-semibold text-text-strong' : ''}>
                      {m.login}
                      {isMe ? ' (this instance)' : ''}
                    </span>
                    {m.on_call_now ? (
                      <Badge variant="ok">on call now</Badge>
                    ) : null}
                    {/* Who owns nightly ledger hygiene. Shown because that job PRUNES the
                        shared ledger: before the schedule's `leader:` key existed, every
                        instance claimed it by default and N agents pruned one ledger.
                        Displaying the owner makes "exactly one" visible, not assumed. */}
                    {rosterLeader && m.login.toLowerCase() === rosterLeader.toLowerCase() ? (
                      <Badge variant="muted" title="Runs nightly ledger hygiene for the team">
                        leader
                      </Badge>
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
            {/* Two things this sentence used to get wrong, both of them the app claiming
                more than the code does.

                It asserted "will never pick up work" UNCONDITIONALLY, but that only holds
                under strict gating: `schedule_file._indeterminate` returns
                `on_shift=True, unknown=True` when strict gating is off, so an unnamed
                instance keeps working normally. Told that its work had stopped, an operator
                would go hunting for a fault it does not have.

                And it offered "or turn off strict gating" as a remedy the UI cannot
                deliver. `strict_gating` is read through `config_value` but is NOT in
                `ScheduleFileRotationSource.config_fields`, so `PUT /providers/schedule-file/
                config` 400s it — there is no toggle here, in Settings, or anywhere else.
                Advice that earns a rejection is worse than no advice, so the only remedies
                named are the two that work. */}
            {rotation.roster.me &&
            !rotation.roster.me_on_roster &&
            rotation.roster.strict_gating ? (
              <p className="text-xs text-warn mt-2">
                This instance ({rotation.roster.me}) is not named in the schedule, so under
                strict gating it will never pick up work. Add it to a{' '}
                <span className="font-mono">who:</span> list in{' '}
                <span className="font-mono">rotation.yaml</span>, or correct its GitHub login
                in Settings.
              </p>
            ) : null}
            {/* Not on the roster, but strict gating is off — so this instance DOES pick up
                work, from an indeterminate schedule. Worth saying rather than staying quiet:
                every instance in that state arms, which is the duplicate-claim shape the
                shared schedule exists to avoid, and it looks like a correctly-configured
                team from here. */}
            {rotation.roster.me &&
            !rotation.roster.me_on_roster &&
            !rotation.roster.strict_gating ? (
              <p className="text-xs text-muted mt-2">
                This instance ({rotation.roster.me}) is not named in the schedule. Strict
                gating is off, so it still picks up work — which means it does not wait for
                any teammate. Add it to a <span className="font-mono">who:</span> list if the
                rotation is meant to decide.
              </p>
            ) : null}
            {!rotation.roster.me ? (
              <p className="text-xs text-warn mt-2">
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
                  {/* Beside "Used" and not folded into it. A single "score" column would
                      hide which half of the record is which, and the whole point is that
                      "used 4×" was previously the only number on screen — it counts times
                      the entry was SHOWN, so an entry that kept matching the wrong failure
                      climbed this column on every mismatch. */}
                  <th className="font-normal py-1 w-16 text-right">Failed</th>
                  <th className="font-normal py-1 w-28">Fast path</th>
                </tr>
              </thead>
              <tbody>
                {ledgerEntries.slice(0, LEDGER_ROWS_SHOWN).map((entry) => (
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
                    {/* An em dash for zero, not "0×": a real 0 here is the ordinary,
                        unremarkable case for almost every row, and a column of zeroes
                        would make the handful of non-zero ones harder to spot rather
                        than easier. */}
                    <td
                      className={`py-1.5 text-right ${
                        entry.miss_count > 0 ? 'text-danger' : 'text-muted'
                      }`}
                    >
                      {entry.miss_count > 0 ? `${entry.miss_count}×` : '—'}
                    </td>
                    <td className="py-1.5">
                      {entry.miss_count > 0 ? (
                        <Badge variant="warn" title="This fix was applied and the signal kept firing">
                          demoted
                        </Badge>
                      ) : (
                        <Badge variant={entryIsProven(entry) ? 'ok' : 'muted'}>
                          {entryIsProven(entry) ? 'unlocked' : 'locked'}
                        </Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {/* What the "Fast path" column actually gates, stated once under the table
              rather than in a tooltip per row. Before the track-record floor landed, an
              agent proposed a remembered fix on the strength of two hand-settable fields;
              an operator reading this table had no way to know that. */}
          {ledgerEntries.length > 0 ? (
            <p className="text-xs text-muted mt-2">
              &quot;Unlocked&quot; means an investigation may propose this fix directly
              instead of re-deriving it — which needs all four of verified trust, high
              confidence, at least {MIN_USES_FOR_FAST_PATH} uses, and no recorded failure.
              A locked entry is still handed to the agent in full; it is told to confirm it
              first. A demoted entry was tried and the signal kept firing, which is a
              stronger warning than an untested guess.
            </p>
          ) : null}
          {/* The ledger-wide refuted count, which the table above cannot give: it renders
              the top 25 rows, and a demoted entry is exactly the row hygiene's `-use_count`
              sort pushes down — so "no red rows visible" is not "nothing has been refuted".
              This is the only render of `stats.demoted`, and it is here rather than as a
              sixth stat card because a card would have to be conditional (a permanent
              "Refuted 0" trains the operator to stop reading it) and the browser gate
              asserts every card unconditionally — `POST /ledger` deliberately refuses
              `miss_count`, so a demoted entry cannot be seeded through the API and a
              conditional card could never be proven to render.

              `total_misses` is deliberately not shown beside it: two numbers where one is
              "entries" and the other is "events" invites reading the larger as the count of
              broken lessons. */}
          {state?.ledger?.demoted ? (
            <p className="text-xs text-warn mt-1">
              {state.ledger.demoted} of {state.ledger.total} entr
              {state.ledger.demoted === 1 ? 'y has' : 'ies have'} been refuted — the fix was
              applied and the signal kept firing. They may not all be in the{' '}
              {LEDGER_ROWS_SHOWN} rows above.
            </p>
          ) : null}
        </Card>
        </>
        )}
      </div>
    </>
  )
}
