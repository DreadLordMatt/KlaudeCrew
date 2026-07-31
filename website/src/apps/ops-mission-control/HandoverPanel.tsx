/**
 * HandoverPanel — the shift handover digest.
 *
 * The workflow this app is modeled on kept a hand-maintained handover document, and it
 * was one of the team's most-used artifacts: at shift change nobody wants a list of
 * every incident, they want the few things that will actually page them and what to do
 * about each. That document cost a human hours and went stale between edits; everything
 * generic in it is already data this app owns.
 *
 * Design choices worth keeping:
 *
 * - **The headline is the product.** Someone reading one line must get the right
 *   priority, so the backend composes it (no coverage → work waiting on you → the
 *   ordinary case) and this renders it prominently rather than re-deriving it.
 * - **Blind spots are shown, not just coverage.** An unconfigured source is silence
 *   that looks like health, and the incoming responder inherits it.
 * - **Unproven patterns are visibly unproven.** Flattening `observed/medium` into "the
 *   fix" is how a digest gets someone to apply the wrong thing confidently.
 * - **Copy-as-text is first-class.** The backend also returns a rendered text form, so
 *   pasting into a handover thread and reading it here cannot word things differently.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  Check,
  ClipboardCopy,
  Clock,
  Radio,
  RefreshCw,
  ShieldCheck,
  UserCheck,
} from 'lucide-react'
import { Badge, Btn, Card, CardTitle, EmptyState } from '../../components/ui'
import { opsApi, type HandoverIncident } from './api'

/** Module-level frozen empty so render-time fallbacks stay referentially stable. */
const EMPTY_ROWS: readonly HandoverIncident[] = Object.freeze([])

function IncidentList({
  rows,
  emphasis,
}: {
  rows: readonly HandoverIncident[]
  emphasis?: boolean
}) {
  return (
    <ul className="flex flex-col divide-y divide-border">
      {rows.map((row) => (
        <li key={row.id} className="flex items-center gap-2 py-2 text-[13px]">
          <span className="font-mono text-[12px] text-muted shrink-0">{row.id}</span>
          <span className="truncate flex-1" title={row.title}>
            {row.title}
          </span>
          {row.blocked_reason ? (
            <span className={`text-[12px] shrink-0 ${emphasis ? 'text-warn font-medium' : 'text-muted'}`}>
              {row.blocked_reason.replace(/_/g, ' ')}
            </span>
          ) : null}
          <span className="text-[12px] text-muted shrink-0 hidden lg:inline">{row.source}</span>
        </li>
      ))}
    </ul>
  )
}

export default function HandoverPanel() {
  const [copied, setCopied] = useState(false)

  // No refetchInterval: a handover is read at shift change, not watched. It is
  // computed fresh per request, so the Refresh button is the honest control.
  const query = useQuery({
    queryKey: ['ops-mission-control', 'handover'],
    queryFn: () => opsApi.handover(),
  })

  const digest = query.data
  const work = digest?.open_work
  const waiting = work?.waiting_on_you ?? EMPTY_ROWS
  const stalled = work?.stalled_without_diagnosis ?? EMPTY_ROWS
  const escalated = work?.escalated ?? EMPTY_ROWS
  const patterns = digest?.recurring_patterns ?? []

  const copy = async () => {
    if (!digest?.text || typeof navigator === 'undefined' || !navigator.clipboard) return
    try {
      await navigator.clipboard.writeText(digest.text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      /* clipboard blocked (permissions, insecure context) — the text is on screen */
    }
  }

  if (query.isLoading) {
    return (
      <Card>
        <p className="text-sm text-muted">Building the digest…</p>
      </Card>
    )
  }
  if (query.isError) {
    return (
      <Card>
        <p className="text-[13px] text-danger">{(query.error as Error).message}</p>
      </Card>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardTitle>
          <Clock className="lucide-inline" /> Handover
        </CardTitle>
        {/* The one line someone gets if they read nothing else. */}
        <p className="text-sm mb-3">{digest?.headline}</p>
        <div className="flex items-center gap-2">
          <Btn disabled={query.isFetching} onClick={() => query.refetch()}>
            <RefreshCw className="lucide-inline" />{' '}
            {query.isFetching ? 'Refreshing…' : 'Refresh'}
          </Btn>
          <Btn onClick={copy} title="Copy the digest as text, for a handover thread">
            {copied ? <Check className="lucide-inline" /> : <ClipboardCopy className="lucide-inline" />}{' '}
            {copied ? 'Copied' : 'Copy as text'}
          </Btn>
          {digest?.autonomy?.mode ? (
            <Badge variant={digest.autonomy.mode === 'observe' ? 'ok' : 'warn'}>
              <ShieldCheck className="lucide-inline" /> {digest.autonomy.mode}
            </Badge>
          ) : null}
        </div>
      </Card>

      {waiting.length > 0 ? (
        <Card>
          <CardTitle>
            <UserCheck className="lucide-inline" /> Waiting on you
          </CardTitle>
          <p className="text-[13px] text-muted mb-2">
            These do not progress on their own — an unanswered prompt from the last shift
            is the first job of this one.
          </p>
          <IncidentList rows={waiting} emphasis />
        </Card>
      ) : null}

      {stalled.length > 0 ? (
        <Card>
          <CardTitle>
            <AlertTriangle className="lucide-inline" /> Stopped with no diagnosis
          </CardTitle>
          <p className="text-[13px] text-muted mb-2">
            The investigation ended without recording anything, so there is no thread to
            pick up. These need restarting rather than answering.
          </p>
          <IncidentList rows={stalled} />
        </Card>
      ) : null}

      {escalated.length > 0 ? (
        <Card>
          <CardTitle>Escalated to someone else</CardTitle>
          <p className="text-[13px] text-muted mb-2">
            Handed to another owner, so no longer this app&apos;s work — but you may be the
            one who has to chase it.
          </p>
          <IncidentList rows={escalated} />
        </Card>
      ) : null}

      <Card>
        <CardTitle>What keeps happening</CardTitle>
        {patterns.length === 0 ? (
          <EmptyState
            icon={<Radio className="lucide-inline" />}
            title="No recurring patterns yet"
            subtitle="A pattern appears here once the same failure has matched twice — one occurrence is an incident, not a pattern."
          />
        ) : (
          <ul className="flex flex-col divide-y divide-border mt-1">
            {patterns.map((pat) => (
              <li key={pat.pattern} className="py-2">
                <div className="flex items-start gap-2 text-[13px]">
                  <span className="font-mono text-[12px] text-muted shrink-0 w-8 text-right">
                    {pat.uses}×
                  </span>
                  <span className="flex-1">{pat.pattern}</span>
                  {/* An unproven entry must not look like an answer. */}
                  <Badge variant={pat.proven ? 'ok' : 'muted'}>
                    {pat.proven ? 'proven' : `${pat.confidence}/${pat.trust}`}
                  </Badge>
                </div>
                <p className="text-[12px] text-muted mt-1 pl-10">{pat.fix}</p>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <CardTitle>Coverage</CardTitle>
        <p className="text-[13px]">
          Watching: {digest?.coverage?.watching?.join(', ') || 'nothing'}
        </p>
        {digest?.coverage?.not_configured?.length ? (
          <p className="text-[13px] text-muted mt-1">
            Not configured (blind spots): {digest.coverage.not_configured.join(', ')}
          </p>
        ) : null}
        {!digest?.coverage?.any_watching ? (
          <p className="text-[13px] text-warn mt-2 flex items-start gap-1.5">
            <AlertTriangle className="lucide-inline" />
            <span>
              Nothing is being watched, so a quiet board means nothing. Connect a source in
              Settings.
            </span>
          </p>
        ) : null}
      </Card>
    </div>
  )
}
