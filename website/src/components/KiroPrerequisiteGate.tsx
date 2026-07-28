import { useEffect, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { motion, useReducedMotion } from 'framer-motion'
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Copy,
  ExternalLink,
  Loader2,
  LogIn,
  Package,
  RefreshCw,
  ShieldCheck,
  Terminal,
} from 'lucide-react'
import {
  ApiError,
  api,
  type KiroPrerequisiteStatus,
} from '../api/client'
import { KiroReadinessProvider } from '../providers/KiroReadinessContext'
import { KiroGhost } from './KiroGhost'
import { Badge, Btn, Card, SendBtn, Skeleton } from './ui'

const QUERY_KEY = ['kiro-prerequisite'] as const

// The last SUCCESSFUL verdict is mirrored here so a cold reload during a
// gateway restart re-seeds the query cache and behaves like the already-loaded
// case, instead of falling through to the (children-rendering) fail-open path.
export const VERDICT_STORAGE_KEY = 'kirocrew:kiro-prerequisite:last-verdict'

// Structural guard: a persisted value is trusted only if it still looks like a
// KiroPrerequisiteStatus. A corrupt or shape-drifted blob must never crash the
// gate nor fabricate a `ready` verdict — it is simply ignored.
//
// The field lists below are EXHAUSTIVE against KiroPrerequisiteStatus /
// KiroPrerequisiteOperation (api/client.ts) on purpose. Every field is rendered
// somewhere downstream — `operation.error`/`message`/`detail` land in
// ReauthenticationBanner and the setup panels, `operation.url` reaches
// trustedLoginUrl(), `docs_url` becomes an href — and React throws outright when
// asked to render an object. Spot-checking a few fields left the rest as a hole
// that a hand-edited localStorage blob walks straight through, so the guard
// enumerates the whole contract instead. Keep these lists in sync with the
// interfaces; a field present in the type but absent here is a live crash path.
const STATUS_STRING_FIELDS = ['platform', 'docs_url'] as const
const STATUS_BOOLEAN_FIELDS = [
  'installed', 'authenticated', 'ready', 'initial_setup_complete',
  'can_auto_install', 'can_login', 'repair_required', 'setup_allowed',
] as const
const OPERATION_STRING_FIELDS = ['kind', 'status', 'message', 'detail', 'url', 'error'] as const

function isKiroPrerequisiteStatus(value: unknown): value is KiroPrerequisiteStatus {
  if (typeof value !== 'object' || value === null) return false
  const v = value as Record<string, unknown>
  if (!STATUS_STRING_FIELDS.every((k) => typeof v[k] === 'string')) return false
  if (!STATUS_BOOLEAN_FIELDS.every((k) => typeof v[k] === 'boolean')) return false
  const op = v.operation
  if (typeof op !== 'object' || op === null) return false
  const opRec = op as Record<string, unknown>
  return OPERATION_STRING_FIELDS.every((k) => typeof opRec[k] === 'string')
}

// Read the last persisted verdict, defensively. Any failure — storage
// unavailable (private mode / disabled), an absent key, invalid JSON, or a
// shape mismatch — yields `undefined`, so the caller treats it as "no known
// verdict" rather than trusting garbage.
// The persisted verdict exists for ONE purpose: on a cold load during a gateway
// blip, answer "render the app or the wall?" without waiting for a probe. Only
// the booleans (plus the two operation ENUMS that drive the refetch cadence)
// feed that decision. Every free-text/URL field — `operation.url` is a
// device-flow sign-in URL, and message/detail/error/docs_url/platform carry host
// and operation output — is irrelevant to it and is re-fetched within seconds.
//
// So none of it is stored. localStorage is origin-scoped, NOT subject-scoped: an
// identity switch in the same browser would otherwise render the previous
// subject's host details and sign-in URL for the moment before the refetch lands.
// Redacting at the WRITE side means there is no sensitive payload to leak and
// therefore nothing to bind to an authenticated subject. Redaction is applied
// again on READ so a blob written by an older build — which stored the full
// object — is neutralised on upgrade instead of being trusted as-is.
export function redactVerdictForStorage(status: KiroPrerequisiteStatus): KiroPrerequisiteStatus {
  return {
    ...status,
    platform: '',
    docs_url: '',
    operation: {
      // Enums only: `status` drives the 1s running-poll cadence, `kind` selects
      // which operation is in flight. Neither carries host or user data.
      kind: status.operation.kind,
      status: status.operation.status,
      message: '',
      detail: '',
      url: '',
      error: '',
    },
  }
}

export function readPersistedVerdict(): KiroPrerequisiteStatus | undefined {
  if (typeof window === 'undefined') return undefined
  let raw: string | null
  try {
    raw = window.localStorage.getItem(VERDICT_STORAGE_KEY)
  } catch {
    return undefined
  }
  if (!raw) return undefined
  try {
    const parsed: unknown = JSON.parse(raw)
    if (isKiroPrerequisiteStatus(parsed)) return redactVerdictForStorage(parsed)
  } catch {
    // Corrupt JSON — ignore.
  }
  return undefined
}

// Persist a verdict. Callers pass ONLY real, successfully-fetched verdicts here
// (never an error/unknown state). Best-effort: a storage failure is swallowed.
function writePersistedVerdict(status: KiroPrerequisiteStatus): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(
      VERDICT_STORAGE_KEY,
      JSON.stringify(redactVerdictForStorage(status)),
    )
  } catch {
    // Storage full/unavailable — persistence is an optimization, not required.
  }
}

// A failed probe either carries a real HTTP verdict from a reachable gateway,
// or none at all:
//   - 'missing-endpoint' (HTTP 404): an older gateway with no prerequisite API.
//     Fail open — unchanged from before.
//   - 'gateway-error' (any other real HTTP status, e.g. the 5xx from
//     handlers/kiro_prerequisite.py): the gateway is reachable and reports it
//     could not run the check. This is the ONLY case that keeps the wall.
//   - 'unreachable' (a genuine transport failure — `fetch` rejects with a bare
//     TypeError): the SPA never reached the gateway, so the failure says NOTHING
//     about Kiro setup. Treat as UNKNOWN — fail open and let the app's own
//     offline layer own the message.
// `ApiError` (api/client.ts) is the discriminator for a real HTTP response: it
// carries a numeric `status`. Below that, only a `TypeError` proves the request
// never completed — `fetch` rejects with a TypeError for DNS/connection/CORS
// failures. Anything else (most importantly the `SyntaxError` that
// `kiroPrerequisite`'s `.then(j)` throws when a 200 carries a non-JSON body,
// e.g. an HTML error page from a proxy) means we DID get a response and simply
// could not understand it. That is not evidence the gateway is unreachable, so
// it must NOT fail open: a user who genuinely needs setup would be dropped into
// a broken app. Those classify as 'gateway-error' and keep the wall + retry.
export type PrerequisiteProbeFailure = 'missing-endpoint' | 'gateway-error' | 'unreachable'

export function classifyPrerequisiteError(error: unknown): PrerequisiteProbeFailure {
  if (error instanceof ApiError) {
    return error.status === 404 ? 'missing-endpoint' : 'gateway-error'
  }
  return error instanceof TypeError ? 'unreachable' : 'gateway-error'
}

// Backoff for the UNKNOWN/unreachable state: ~2s, then doubling, capped at 10s
// (2s, 2s, 4s, 8s, 10s, 10s…). Replaces the old flat 30s wait so recovery is
// automatic within seconds of the gateway port returning.
export function kiroUnknownBackoff(failureCount: number): number {
  const base = 2_000
  const max = 10_000
  const steps = Math.max(0, failureCount - 1)
  return Math.min(max, base * 2 ** steps)
}

export function kiroPrerequisiteRefetchInterval(
  status: KiroPrerequisiteStatus | undefined,
  failureCount = 0,
): number | false {
  if (status?.operation.status === 'running') return 1_000
  if (status?.ready) return 30_000
  if (status && status.setup_allowed === false) return 3_000
  // Any other KNOWN verdict keeps the existing 30s cadence.
  if (status) return 30_000
  // No known verdict (unknown / gateway unreachable): back off 2s → 10s.
  return kiroUnknownBackoff(failureCount)
}

function trustedLoginUrl(value: string): string | null {
  if (!value || value.includes('\\')) return null
  for (const character of value) {
    const code = character.charCodeAt(0)
    if (code < 32 || code === 127) return null
  }
  try {
    const parsed = new URL(value)
    const host = parsed.hostname.toLowerCase()
    const trustedPath = host === 'app.kiro.dev'
      || (host === 'view.awsapps.com'
        && (parsed.pathname === '/start' || parsed.pathname.startsWith('/start/')))
    if (
      parsed.protocol !== 'https:'
      || (parsed.port !== '' && parsed.port !== '443')
      || parsed.username !== ''
      || parsed.password !== ''
      || !trustedPath
    ) {
      return null
    }
    return parsed.href
  } catch {
    return null
  }
}

function FloatingGhost({
  className,
  delay,
  rotate = 0,
}: {
  className: string
  delay: number
  rotate?: number
}) {
  const reduceMotion = useReducedMotion()
  return (
    <motion.div
      aria-hidden="true"
      className={`pointer-events-none absolute z-0 text-white drop-shadow-[0_12px_20px_rgba(24,20,38,0.26)] ${className}`}
      initial={reduceMotion ? false : { opacity: 0, scale: 0.72 }}
      animate={{
        opacity: 1,
        scale: 1,
        y: reduceMotion ? 0 : [-5, 5, -5],
        rotate,
      }}
      transition={{
        opacity: { delay, duration: 0.35 },
        scale: { delay, duration: 0.45, type: 'spring', bounce: 0.45 },
        y: { delay, duration: 3.8, ease: 'easeInOut', repeat: Infinity },
      }}
    >
      <KiroGhost size={160} className="h-full w-full" />
    </motion.div>
  )
}

function SetupStage() {
  const reduceMotion = useReducedMotion()
  return (
    <section className="relative min-h-[250px] overflow-hidden bg-[radial-gradient(circle_at_50%_42%,color-mix(in_srgb,var(--accent)_78%,white_22%),var(--accent)_76%)] px-8 py-9 text-white lg:min-h-[680px] lg:px-10 lg:py-12">
      <div
        aria-hidden="true"
        className="absolute inset-0 opacity-30 [background-image:linear-gradient(rgba(255,255,255,.08)_1px,transparent_1px),linear-gradient(90deg,rgba(255,255,255,.08)_1px,transparent_1px)] [background-size:34px_34px] [mask-image:linear-gradient(to_bottom,black,transparent_88%)]"
      />
      <FloatingGhost className="-left-8 top-[24%] h-24 w-20 rotate-90 lg:h-28 lg:w-24" delay={0.15} rotate={90} />
      <FloatingGhost className="-right-5 top-5 h-28 w-20 -rotate-12 lg:h-36 lg:w-28" delay={0.35} rotate={-12} />
      <FloatingGhost className="bottom-[-5.5rem] right-[-12%] hidden h-64 w-48 lg:block" delay={0.55} />
      <FloatingGhost className="-top-20 left-[40%] hidden h-48 w-36 rotate-180 lg:block" delay={0.75} rotate={180} />

      <motion.div
        className="relative z-20 flex h-full flex-col"
        initial={reduceMotion ? false : { opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.15, duration: 0.55 }}
      >
        <div className="flex items-center gap-2.5 text-sm font-semibold tracking-wide">
          <span aria-hidden="true">
            <KiroGhost size={28} className="h-8 w-7" />
          </span>
          Kiro Crew
        </div>
        <div className="mt-12 max-w-sm lg:my-auto">
          <p className="text-[12px] font-bold uppercase tracking-[0.18em] text-white/75">
            One quick setup
          </p>
          <h2 className="mt-3 text-4xl font-semibold leading-[1.05] tracking-tight lg:text-5xl">
            Your crew is almost ready.
          </h2>
          <p className="mt-5 max-w-xs text-sm leading-relaxed text-white/80">
            Install Kiro CLI, sign in once, and Kiro Crew will take it from here.
          </p>
        </div>
        <div className="mt-8 flex items-center gap-2 text-[12px] font-semibold text-white/75">
          <span className="h-1.5 w-1.5 rounded-full bg-white shadow-[0_0_12px_rgba(255,255,255,.9)]" />
          Secure setup on your gateway host
        </div>
      </motion.div>
    </section>
  )
}

// Shared full-screen chrome for every gate state: the ambient-blur backdrop,
// the centered card, and the branded SetupStage panel. Only the right-hand
// content differs between states. `scroll` switches the tall two-step setup to
// vertical overflow (and adds a second blur); `cardLabel` sets the card's
// aria-label for the loading state.
function SetupShell({
  children,
  scroll = false,
  cardLabel,
}: {
  children: ReactNode
  scroll?: boolean
  cardLabel?: string
}) {
  return (
    <main
      className={`relative flex min-h-screen items-center justify-center ${
        scroll ? 'overflow-y-auto' : 'overflow-hidden'
      } bg-bg px-4 py-8 sm:px-8`}
    >
      <div
        aria-hidden="true"
        className="absolute left-[-12rem] top-[-12rem] h-[32rem] w-[32rem] rounded-full bg-accent/10 blur-[100px]"
      />
      {scroll && (
        <div
          aria-hidden="true"
          className="absolute bottom-[-15rem] right-[-10rem] h-[34rem] w-[34rem] rounded-full bg-accent/5 blur-[120px]"
        />
      )}
      <div
        className="relative grid w-full max-w-5xl overflow-hidden rounded-2xl border border-border bg-card shadow-[0_30px_90px_rgba(0,0,0,.2)] lg:grid-cols-[.82fr_1.18fr]"
        aria-label={cardLabel}
      >
        <SetupStage />
        {children}
      </div>
    </main>
  )
}

function StepStatus({
  complete,
  current,
}: {
  complete: boolean
  current: boolean
}) {
  if (complete) {
    return <Badge variant="ok"><CheckCircle2 className="lucide-inline" /> Complete</Badge>
  }
  return <Badge variant={current ? 'aim' : 'muted'}>{current ? 'Required' : 'Waiting'}</Badge>
}

function OperationProgress({ status }: { status: KiroPrerequisiteStatus }) {
  const operation = status.operation
  if (operation.status === 'idle' && !operation.message) return null
  const isRunning = operation.status === 'running'
  const isFailure = operation.status === 'failed'
  const loginUrl = trustedLoginUrl(operation.url)

  return (
    <div
      className={`mt-4 rounded-lg border p-3 ${
        isFailure
          ? 'border-danger/20 bg-danger/10'
          : 'border-border bg-bg-elevated'
      }`}
      aria-live="polite"
    >
      <div className={`flex items-center gap-2 text-sm ${isFailure ? 'text-danger' : 'text-text'}`}>
        {isRunning && <Loader2 className="lucide-inline animate-spin" />}
        {isFailure && <AlertTriangle className="lucide-inline" />}
        <span>{operation.error || operation.message}</span>
      </div>
      {loginUrl && (
        <a
          className="mt-3 inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline focus-ring"
          href={loginUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          Open Kiro sign-in page <ExternalLink className="lucide-inline" />
        </a>
      )}
      {operation.detail && (
        <pre className="mt-3 max-h-36 overflow-auto whitespace-pre-wrap break-words rounded-md bg-bg p-3 font-mono text-[12px] leading-relaxed text-muted">
          {operation.detail}
        </pre>
      )}
    </div>
  )
}

const TERMINAL_LOGIN_COMMAND = 'kiro-cli login'

// kiro-cli owns the whole sign-in flow. The in-app button starts it on the
// gateway host, but a terminal is always available and is the only path when the
// gateway runs headless or over SSH — so surface the exact command too.
function TerminalLoginCommand() {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(TERMINAL_LOGIN_COMMAND)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2_000)
    } catch {
      setCopied(false)
    }
  }
  return (
    <div className="pointer-events-auto mt-2 flex flex-wrap items-center gap-2">
      <span className="text-[12px] text-muted">
        <Terminal className="lucide-inline" /> or run in terminal:
      </span>
      <code className="rounded-md bg-bg px-2 py-1 font-mono text-[12px] text-text">
        {TERMINAL_LOGIN_COMMAND}
      </code>
      <Btn type="button" onClick={copy} aria-label="Copy sign-in command">
        <Copy className="lucide-inline" />
        {copied ? 'Copied' : 'Copy'}
      </Btn>
    </div>
  )
}

function ReauthenticationBanner({
  status,
  busy,
  retrying,
  mutationError,
  onInstall,
  onLogin,
  onRetry,
}: {
  status: KiroPrerequisiteStatus
  busy: boolean
  retrying: boolean
  mutationError: Error | null
  onInstall: () => void
  onLogin: () => void
  onRetry: () => void
}) {
  const owner = status.setup_allowed !== false
  const loginUrl = trustedLoginUrl(status.operation.url)
  const needsInstall = !status.installed
  return (
    <aside
      className="pointer-events-none fixed inset-x-3 top-3 z-[100] mx-auto max-w-4xl rounded-xl border border-warn/30 bg-card/95 p-4 shadow-[0_18px_55px_rgba(0,0,0,.24)] backdrop-blur-md"
      role="status"
      aria-live="polite"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-warn/10 text-warn">
            <AlertTriangle className="lucide-inline" />
          </span>
          <div className="min-w-0">
            <p className="font-semibold text-text-strong">
              {needsInstall ? 'Kiro CLI needs attention.' : 'Kiro Crew needs Kiro sign-in.'}
            </p>
            <p className="mt-1 text-[13px] leading-relaxed text-muted">
              {owner
                ? needsInstall
                  ? 'Sessions are paused until Kiro CLI is restored on this gateway. Other dashboard areas remain available.'
                  : 'Sign in again to start sessions. Your artifacts, settings, and history remain available.'
                : 'The gateway owner needs to restore Kiro access. This page will update automatically.'}
            </p>
            {(status.operation.message || status.operation.error) && (
              <p className={`mt-2 text-[13px] ${status.operation.error ? 'text-danger' : 'text-muted'}`}>
                {status.operation.error || status.operation.message}
              </p>
            )}
            {loginUrl && (
              <a
                className="pointer-events-auto mt-2 inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline focus-ring"
                href={loginUrl}
                target="_blank"
                rel="noopener noreferrer"
              >
                Open Kiro sign-in page <ExternalLink className="lucide-inline" />
              </a>
            )}
            {!needsInstall && owner && <TerminalLoginCommand />}
            {status.operation.detail && (
              <pre className="mt-2 max-h-24 overflow-auto whitespace-pre-wrap break-words rounded-md bg-bg p-2 font-mono text-[12px] text-muted">
                {status.operation.detail}
              </pre>
            )}
            {mutationError && <p className="mt-2 text-[13px] text-danger">{mutationError.message}</p>}
          </div>
        </div>
        <div className="pointer-events-auto flex shrink-0 flex-wrap items-center gap-2 sm:justify-end">
          {owner && needsInstall && status.can_auto_install && (
            <SendBtn type="button" disabled={busy} onClick={onInstall}>
              {busy
                ? <Loader2 className="lucide-inline animate-spin" />
                : <Package className="lucide-inline" />}
              Install Kiro CLI
            </SendBtn>
          )}
          {owner && status.installed && (
            <SendBtn type="button" disabled={busy} onClick={onLogin}>
              {busy
                ? <Loader2 className="lucide-inline animate-spin" />
                : <LogIn className="lucide-inline" />}
              Sign in to Kiro
            </SendBtn>
          )}
          <Btn type="button" disabled={busy || retrying} onClick={onRetry}>
            <RefreshCw className={`lucide-inline ${retrying ? 'animate-spin' : ''}`} />
            Refresh
          </Btn>
          {owner && needsInstall && !status.can_auto_install && (
            <a
              className="text-[13px] font-medium text-accent hover:underline focus-ring"
              href={status.docs_url}
              target="_blank"
              rel="noopener noreferrer"
            >
              Installation guide <ExternalLink className="lucide-inline" />
            </a>
          )}
        </div>
      </div>
    </aside>
  )
}

function OwnerSetupRequired({
  retrying,
  onRetry,
}: {
  retrying: boolean
  onRetry: () => void
}) {
  return (
    <SetupShell>
      <section className="flex flex-col justify-center p-7 sm:p-10 lg:p-12">
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-accent-subtle text-accent">
          <ShieldCheck className="lucide-inline" />
        </div>
        <p className="mt-6 text-[12px] font-bold uppercase tracking-[0.16em] text-accent">
          Gateway setup required
        </p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
          The gateway owner needs to finish setup.
        </h1>
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">
          Ask the Kiro Crew owner to install Kiro CLI and sign in on this gateway. This dashboard
          will open for you as soon as the gateway is ready.
        </p>
        <div className="mt-6">
          <Btn type="button" disabled={retrying} onClick={onRetry}>
            <RefreshCw className={`lucide-inline ${retrying ? 'animate-spin' : ''}`} />
            Check again
          </Btn>
        </div>
      </section>
    </SetupShell>
  )
}

function LoadingGate() {
  return (
    <SetupShell cardLabel="Checking Kiro CLI">
      <div className="space-y-4 p-7 sm:p-10 lg:p-12">
        <Skeleton className="h-4 w-28" />
        <Skeleton className="h-10 w-64 max-w-full" />
        <Skeleton className="h-5 w-full max-w-lg" />
        <Skeleton className="mt-8 h-44 w-full" />
        <Skeleton className="h-44 w-full" />
      </div>
    </SetupShell>
  )
}

function SetupStatusError({
  message,
  retrying,
  onRetry,
}: {
  message: string
  retrying: boolean
  onRetry: () => void
}) {
  return (
    <SetupShell>
      <section className="flex flex-col justify-center p-7 sm:p-10 lg:p-12">
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-danger/10 text-danger">
          <AlertTriangle className="lucide-inline" />
        </div>
        <p className="mt-6 text-[12px] font-bold uppercase tracking-[0.16em] text-danger">
          Setup check unavailable
        </p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
          We could not check Kiro CLI.
        </h1>
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">
          {message} Retry the gateway check before starting a session.
        </p>
        <div className="mt-6">
          <SendBtn type="button" disabled={retrying} onClick={onRetry}>
            <RefreshCw className={`lucide-inline ${retrying ? 'animate-spin' : ''}`} />
            Try again
          </SendBtn>
        </div>
      </section>
    </SetupShell>
  )
}

export default function KiroPrerequisiteGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const statusQuery = useQuery({
    queryKey: QUERY_KEY,
    queryFn: api.kiroPrerequisite,
    // Seed from the last persisted verdict so a reload during a gateway restart
    // renders like the already-loaded case instead of the fail-open path.
    // `initialDataUpdatedAt: 0` marks the seed stale so a fresh probe still
    // fires immediately on mount (the app default staleTime is 30s).
    initialData: readPersistedVerdict,
    initialDataUpdatedAt: 0,
    refetchInterval: (query) =>
      kiroPrerequisiteRefetchInterval(query.state.data, query.state.fetchFailureCount),
  })
  // Mirror only real, successful verdicts to storage (never error/unknown):
  // statusQuery.data is only ever a fetched/seeded status, and a failed
  // background refetch leaves that data untouched — so this never overwrites a
  // good verdict with an error state.
  useEffect(() => {
    if (statusQuery.isSuccess && statusQuery.data) {
      writePersistedVerdict(statusQuery.data)
    }
  }, [statusQuery.isSuccess, statusQuery.data])
  const updateStatus = (status: KiroPrerequisiteStatus) => {
    queryClient.setQueryData(QUERY_KEY, status)
  }
  const installMutation = useMutation({
    mutationFn: api.installKiroPrerequisite,
    onSuccess: updateStatus,
  })
  const loginMutation = useMutation({
    mutationFn: api.loginKiroPrerequisite,
    onSuccess: updateStatus,
  })

  if (statusQuery.isPending) return <LoadingGate />
  const retrying = statusQuery.isFetching
  const retryStatus = () => { void statusQuery.refetch() }
  const prerequisite = statusQuery.data

  // A 404 says this gateway has no prerequisite API at all. That is a property
  // of the GATEWAY, not of anything we cached, so it must fail open even when a
  // persisted verdict exists — otherwise a stale `ready:false` verdict would
  // make the `&& !prerequisite` guard below skip this branch and pin the setup
  // wall up against a gateway that was rolled back to a version predating the
  // API. Classified before the cached-data guard for that reason.
  if (
    statusQuery.isError
    && classifyPrerequisiteError(statusQuery.error) === 'missing-endpoint'
  ) {
    return <KiroReadinessProvider ready>{children}</KiroReadinessProvider>
  }

  // An error with no prior verdict: keep the full-screen wall ONLY for a real
  // HTTP error from a reachable gateway. A 404 (old gateway without the API)
  // and an unreachable gateway (network-layer failure, no HTTP status) both
  // fail open into the app, where the existing offline layer — not this gate —
  // owns the "gateway offline" messaging. `ready` matches the 404 branch on
  // purpose (see the PR's KiroReadinessProvider rationale): asserting a distinct
  // not-ready value here would make consumers such as SideChat falsely claim
  // "Finish Kiro CLI setup" when the truth is only that the gateway is offline.
  if (statusQuery.isError && !prerequisite) {
    if (classifyPrerequisiteError(statusQuery.error) === 'gateway-error') {
      return (
        <SetupStatusError
          message={statusQuery.error.message || 'The gateway returned an unexpected error.'}
          retrying={retrying}
          onRetry={retryStatus}
        />
      )
    }
    return <KiroReadinessProvider ready>{children}</KiroReadinessProvider>
  }
  if (!prerequisite) {
    return (
      <SetupStatusError
        message="The gateway returned no prerequisite status."
        retrying={retrying}
        onRetry={retryStatus}
      />
    )
  }
  if (prerequisite.ready) {
    return <KiroReadinessProvider ready>{children}</KiroReadinessProvider>
  }
  const status = prerequisite
  const busy = status.operation.status === 'running'
    || installMutation.isPending
    || loginMutation.isPending
  const mutationError = installMutation.error || loginMutation.error
  const platform = status.platform || 'local'
  if (status.initial_setup_complete) {
    return (
      <>
        <KiroReadinessProvider ready={false}>{children}</KiroReadinessProvider>
        <ReauthenticationBanner
          status={status}
          busy={busy}
          retrying={retrying}
          mutationError={mutationError}
          onInstall={() => installMutation.mutate()}
          onLogin={() => loginMutation.mutate()}
          onRetry={retryStatus}
        />
      </>
    )
  }
  if (prerequisite.setup_allowed === false) {
    return <OwnerSetupRequired retrying={retrying} onRetry={retryStatus} />
  }

  return (
    <SetupShell scroll>
        <section className="p-7 sm:p-10 lg:p-12">
          <div className="mb-7">
            <div className="mb-3 flex items-center gap-2 text-[12px] font-semibold tracking-[0.14em] text-accent">
              <span className="uppercase">Setup</span>
              <ArrowRight className="lucide-inline" />
              <span>{platform} gateway</span>
            </div>
            <h1 className="text-3xl font-bold tracking-tight text-text-strong">Set up Kiro</h1>
            <p className="mt-2 max-w-xl text-sm leading-relaxed text-muted">
              Kiro Crew uses Kiro CLI as its agent engine. Complete these two steps on the{' '}
              <strong className="font-semibold text-text">{platform} gateway host</strong>, then
              the dashboard will open automatically.
            </p>
          </div>

          <Card className={!status.installed ? 'border-accent/60 shadow-[0_10px_35px_var(--accent-glow)]' : ''}>
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="flex items-center gap-2 text-base font-semibold text-text-strong">
                  <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent-subtle text-accent">
                    <Package className="lucide-inline" />
                  </span>
                  Install Kiro CLI
                </h2>
                <p className="mt-2 text-sm leading-relaxed text-muted">
                  Kiro Crew downloads the official Kiro installer over HTTPS. Installation starts
                  only when you choose the button below.
                </p>
              </div>
              <StepStatus complete={status.installed} current={!status.installed} />
            </div>
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <SendBtn
                type="button"
                disabled={busy || status.installed || !status.can_auto_install}
                onClick={() => installMutation.mutate()}
              >
                {busy && status.operation.kind === 'install'
                  ? <><Loader2 className="lucide-inline animate-spin" /> Installing…</>
                  : status.installed
                    ? <><CheckCircle2 className="lucide-inline" /> Installed</>
                    : <><Package className="lucide-inline" /> Install Kiro CLI</>}
              </SendBtn>
              <a
                className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline focus-ring"
                href={status.docs_url}
                target="_blank"
                rel="noopener noreferrer"
              >
                Installation guide <ExternalLink className="lucide-inline" />
              </a>
            </div>
            {!status.installed && !status.can_auto_install && (
              <p className="mt-3 text-[13px] leading-relaxed text-muted">
                Automatic installation is unavailable here. Install Kiro CLI from the official
                guide on the gateway host, then choose Check again.
              </p>
            )}
            {status.operation.kind === 'install' && <OperationProgress status={status} />}
          </Card>

          <Card className={status.installed && !status.authenticated ? 'border-accent/60 shadow-[0_10px_35px_var(--accent-glow)]' : ''}>
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="flex items-center gap-2 text-base font-semibold text-text-strong">
                  <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent-subtle text-accent">
                    <LogIn className="lucide-inline" />
                  </span>
                  Sign in to Kiro
                </h2>
                <p className="mt-2 text-sm leading-relaxed text-muted">
                  Start Kiro&apos;s device sign-in, open the secure page, and enter the code shown
                  here. Credentials remain managed by Kiro CLI on the gateway host.
                </p>
              </div>
              <StepStatus
                complete={status.authenticated}
                current={status.installed && !status.authenticated}
              />
            </div>
            <div className="mt-4">
              <SendBtn
                type="button"
                disabled={
                  busy
                  || !status.installed
                  || status.authenticated
                }
                onClick={() => loginMutation.mutate()}
              >
                {busy && status.operation.kind === 'login'
                  ? <><Loader2 className="lucide-inline animate-spin" /> Waiting for sign-in…</>
                  : status.authenticated
                    ? <><CheckCircle2 className="lucide-inline" /> Signed in</>
                    : <><LogIn className="lucide-inline" /> Sign in to Kiro</>}
              </SendBtn>
            </div>
            {status.operation.kind === 'login' && <OperationProgress status={status} />}
          </Card>

          {mutationError && (
            <div
              className="mb-4 flex items-start gap-2 rounded-lg border border-danger/20 bg-danger/10 p-3 text-sm text-danger"
              role="alert"
            >
              <AlertTriangle className="lucide-inline" />
              {mutationError.message || 'Kiro setup could not start.'}
            </div>
          )}

          <div className="flex items-center justify-between gap-4 border-t border-border pt-5">
            <p className="text-[13px] text-muted" aria-live="polite">
              {status.installed
                ? 'Kiro CLI is installed. Finish signing in to continue.'
                : `Kiro CLI is required on the ${platform} gateway host.`}
            </p>
            <Btn
              type="button"
              disabled={busy || statusQuery.isFetching}
              onClick={() => statusQuery.refetch()}
            >
              <RefreshCw className={`lucide-inline ${statusQuery.isFetching ? 'animate-spin' : ''}`} />
              Check again
            </Btn>
          </div>
        </section>
    </SetupShell>
  )
}
