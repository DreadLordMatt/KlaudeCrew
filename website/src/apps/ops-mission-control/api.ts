// Thin fetch wrapper for the Ops Mission Control backend.
//
// The backend registers its routes directly on the main gateway's aiohttp
// Application (backend/routes.py:register_routes), so the base path is
// /api/apps/ops-mission-control — the same convention as issue-radar and
// code-review-sage, NOT the /apps/{name}/api reverse-proxy prefix used by apps
// that run as a separate child process.
const API = '/api/apps/ops-mission-control'

export type Severity = 'critical' | 'warning' | 'info'
export type SignalState = 'firing' | 'ok' | 'unknown'
export type IncidentStatus =
  | 'unclaimed'
  | 'dispatched'
  | 'investigating'
  | 'needs_human'
  | 'resolved'
  | 'escalated'
  | 'stale'
export type OperatingMode = 'observe' | 'propose' | 'act'
export type ActionKind = 'ack' | 'resolve' | 'comment'

export interface Signal {
  id: string
  source: string
  title: string
  severity: Severity
  state: SignalState
  fired_at: string
  resource: string
  url: string
  labels: Record<string, string>
  /** Stable identity for the KIND of failure — what the ledger matches on. */
  fingerprint: string
}

export interface Incident {
  incident_id: string
  signal: Signal
  status: IncidentStatus
  operating_mode: OperatingMode
  claimed_at: string
  updated_at: string
  slot_key: string
  slack_thread_ts: string
  ledger_matches: string[]
  diagnosis: string
  proposed_action: Record<string, unknown> | null
  resolution: string
  /**
   * Why this incident is waiting on a person, derived from its investigation slot
   * (empty when it is not blocked). `needs_human` alone reads the same whether the
   * agent wants a decision or gave up, so the board shows this instead.
   */
  blocked_reason?: 'awaiting_approval' | 'awaiting_input' | 'awaiting_diagnosis' | ''
}

export interface ProviderInfo {
  id: string
  display_name: string
  roles: string[]
  configured: boolean
  config_fields: string[]
  secret_fields: string[]
  detail: string
  config: Record<string, unknown>
  /** Set/unset only — the API never returns a stored secret value. */
  secrets: Record<string, string>
}

export interface RotationInfo {
  on_shift: boolean
  who: string
  until: string
  /** True when no rotation source could answer. The on-shift tier stays ARMED. */
  unknown: boolean
  tiers: Record<string, boolean>
  armed_crons: string[]
  mode: OperatingMode
  rules: number
  primary: boolean
  modes_available: OperatingMode[]
}

export interface LedgerEntry {
  entry_id: string
  pattern: string
  fix: string
  fingerprints: string[]
  confidence: 'high' | 'medium' | 'low'
  trust: 'verified' | 'observed'
  use_count: number
  first_seen: string
  last_used: string
  source: string
}

export interface LedgerStats {
  total: number
  verified: number
  high_confidence: number
  total_uses: number
}

/**
 * Slack output-channel state. `ready` is the only field the UI should gate on;
 * the three booleans exist so Settings can name WHICH half is missing, since the
 * fixes differ (flip a toggle / enter a channel / configure KiroCrew's Slack).
 */
export interface SlackOutStatus {
  enabled: boolean
  channel: string
  /** Whether KiroCrew's OWN Slack client exists — this app stores no token. */
  slack_available: boolean
  ready: boolean
  detail: string
}

/**
 * An installed companion adapter package. Reported from what is *installed*, which
 * is a different question from what was admitted at boot — so "none installed" is
 * distinguishable from "installed but rejected", which need different fixes.
 */
export interface CompanionInfo {
  name: string
  target: string
}

/** Shift handover digest — a read-only projection, computed fresh per request. */
export interface HandoverDigest {
  /** One sentence for someone who reads nothing else. */
  headline: string
  open_work: {
    total_open: number
    waiting_on_you: HandoverIncident[]
    escalated: HandoverIncident[]
    stalled_without_diagnosis: HandoverIncident[]
    progressing: number
  }
  recurring_patterns: {
    pattern: string
    fix: string
    uses: number
    confidence: string
    trust: string
    /** Matches the ledger's fast-path bar; anything else is a hypothesis. */
    proven: boolean
  }[]
  coverage: { watching: string[]; not_configured: string[]; any_watching: boolean }
  autonomy: { mode: string; rules: number; on_shift?: boolean | null }
  /** Pre-rendered text, so a Slack paste and this UI cannot word things differently. */
  text: string
}

export interface HandoverIncident {
  id: string
  title: string
  status: IncidentStatus
  blocked_reason: string
  severity: string
  source: string
  has_diagnosis: boolean
}

export interface BoardState {
  incidents: Incident[]
  counts: Record<string, number>
  /** Count of incidents waiting on a person, keyed by blocked_reason. */
  blocked?: Record<string, number>
  providers: ProviderInfo[]
  rotation: RotationInfo
  ledger: LedgerStats
  slack?: SlackOutStatus
  companions?: CompanionInfo[]
  webhook_queue: number
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${API}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!resp.ok) {
    // Surface the backend's reason — a 403 here is usually the autonomy gate
    // explaining that no rule grants this action, which the user needs to read.
    let detail = `HTTP ${resp.status}`
    try {
      const body = (await resp.json()) as { error?: string }
      if (body?.error) detail = body.error
    } catch {
      /* non-JSON error body — keep the status text */
    }
    throw new Error(detail)
  }
  return (await resp.json()) as T
}

export const opsApi = {
  state: () => req<BoardState>('/state'),

  /**
   * The board's incident list. Server-capped at 200 — `truncated` and `total` are
   * present ONLY when there was more, so the UI can say "showing 200 of 640" instead
   * of implying the list is complete. Typed rather than ignored: a silently clipped
   * board is how someone concludes an incident vanished.
   */
  incidents: (status?: IncidentStatus) =>
    req<{ incidents: Incident[]; truncated?: boolean; total?: number }>(
      `/incidents${status ? `?status=${status}` : ''}`,
    ),

  incident: (id: string) =>
    req<{ incident: Incident; log: string }>(`/incident?id=${encodeURIComponent(id)}`),

  transition: (id: string, status: IncidentStatus, extra?: Record<string, string>) =>
    req<{ incident: Incident }>('/incident/transition', {
      method: 'POST',
      body: JSON.stringify({ id, status, ...extra }),
    }),

  claim: (signal: Signal) =>
    req<{ incident: Incident }>('/incident/claim', {
      method: 'POST',
      body: JSON.stringify({ signal }),
    }),

  action: (id: string, action: ActionKind, opts?: { sink?: string; note?: string }) =>
    req<{ ok: boolean; action: string; detail: string; error: string }>('/incident/action', {
      method: 'POST',
      body: JSON.stringify({ id, action, ...opts }),
    }),

  /** Fresh each call: a cached handover goes stale between shifts. */
  handover: () => req<HandoverDigest>('/handover'),

  signals: () =>
    req<{ signals: Signal[]; unclaimed: Signal[]; errors: Record<string, string> }>('/signals'),

  providers: () => req<{ providers: ProviderInfo[] }>('/providers'),

  /**
   * Run one dispatch cycle: poll, claim, match the ledger, release stale work.
   *
   * `matches` and `similar` are deliberately separate and must stay that way in any UI
   * that renders them. A `matches` entry means this exact failure fingerprint recurred;
   * a `similar` entry is a semantic near-miss whose fingerprint does NOT match. Showing
   * them together would let a near-miss inherit the "verified, used 4x" authority it has
   * not earned — the backend keeps them apart for the same reason (see
   * `dispatch.attach_similar_lessons`).
   */
  dispatch: () =>
    req<{
      claimed: {
        incident: Incident
        matches: LedgerEntry[]
        similar: LedgerEntry[]
        fast_path: boolean
      }[]
      released: string[]
      polled: number
      unclaimed_remaining: number
      errors: Record<string, string>
      changed: boolean
      skipped_reason: string
      briefs: Record<string, string>
    }>('/dispatch', { method: 'POST' }),

  /** Non-secret provider config. Secrets are refused here by the backend. */
  putProviderConfig: (providerId: string, updates: Record<string, unknown>) =>
    req<{ ok: boolean; provider: string; config: Record<string, unknown> }>(
      `/providers/${encodeURIComponent(providerId)}/config`,
      { method: 'PUT', body: JSON.stringify(updates) },
    ),

  /** App-level settings: autonomy mode, primary flag, cycle tuning. */
  putSettings: (updates: Record<string, unknown>) =>
    req<{ ok: boolean; applied: Record<string, unknown> }>('/settings', {
      method: 'PUT',
      body: JSON.stringify(updates),
    }),

  putSecret: (providerId: string, field: string, value: string) =>
    req<{ ok: boolean }>(`/providers/${encodeURIComponent(providerId)}/secret`, {
      method: 'PUT',
      body: JSON.stringify({ field, value }),
    }),

  deleteSecret: (providerId: string) =>
    req<{ ok: boolean; removed: boolean }>(
      `/providers/${encodeURIComponent(providerId)}/secret`,
      { method: 'DELETE' },
    ),

  rotation: () => req<RotationInfo>('/rotation'),

  ledger: () => req<{ entries: LedgerEntry[]; stats: LedgerStats }>('/ledger'),

  /** Deterministic dedupe / decay / prune pass over the ledger. */
  ledgerHygiene: () =>
    req<{ summary: Record<string, number>; changed: boolean }>('/ledger/hygiene', {
      method: 'POST',
    }),

  addLedgerEntry: (entry: {
    pattern: string
    fix: string
    fingerprints?: string[]
    confidence?: string
    trust?: string
  }) =>
    req<{ entry: LedgerEntry }>('/ledger', {
      method: 'POST',
      body: JSON.stringify(entry),
    }),

  removeLedgerEntry: (id: string) =>
    req<{ ok: boolean; removed: boolean }>(`/ledger?id=${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
}
