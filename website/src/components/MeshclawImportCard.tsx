import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { Package, Download, Loader2, RefreshCw, X } from 'lucide-react'
import { api } from '../api/client'

// Persist a skip so the card does not nag on every reconnect within this
// browser (mirrors the `mc-onboarded` theme-onboarding flag). A successful
// import renames ~/.meshclaw -> ~/.meshclaw.bak server-side, so after the
// restart the status query reports available:false and the card stays gone
// regardless of this flag.
const DISMISS_KEY = 'mc-meshclaw-import-dismissed'

/** Human-readable byte size, e.g. 1536 -> "1.5 KB", 0 -> "0 B". */
export function formatBytes(n: number): string {
  if (!n || n <= 0 || !Number.isFinite(n)) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  const rounded = i === 0 || v >= 10 ? Math.round(v).toString() : v.toFixed(1)
  return `${rounded} ${units[i]}`
}

/**
 * True when the start-mutation failure is a real HTTP error (ApiError carries
 * a numeric `.status`). Duck-typed on `.status` — same convention as
 * api/queryClient.ts — so it needs no import of the ApiError class itself.
 *
 * A network-level rejection (fetch TypeError, no HTTP status) is NOT a
 * confirmed failure here: POST /start triggers a gateway restart that can
 * drop the socket before the response flushes, so the import may well be
 * running even though the fetch rejected.
 */
const isHttpError = (err: unknown): boolean =>
  typeof (err as { status?: unknown } | null)?.status === 'number'

/** Elements the Tab trap cycles between. Disabled controls are skipped. */
const FOCUSABLE_SELECTOR = [
  'button:not([disabled])',
  '[href]',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ')

/**
 * A11y: hide everything OUTSIDE `el` from assistive tech and the tab order
 * while a modal overlay is open. Walks up to <body>, marking each ancestor's
 * siblings `aria-hidden` + `inert` (the same "hide others" approach Radix
 * uses), and returns an undo function that restores the previous attribute
 * values. Used together with the keyboard Tab trap below: `inert` keeps
 * pointer/sequential focus out of the background, the trap keeps Tab cycling
 * inside the dialog.
 */
function hideOutside(el: HTMLElement): () => void {
  const undos: Array<() => void> = []
  let node: HTMLElement | null = el
  while (node && node !== document.body) {
    const parent: HTMLElement | null = node.parentElement
    if (!parent) break
    for (const sib of Array.from(parent.children)) {
      if (sib === node || !(sib instanceof HTMLElement)) continue
      const prevAria = sib.getAttribute('aria-hidden')
      const hadInert = sib.hasAttribute('inert')
      sib.setAttribute('aria-hidden', 'true')
      sib.setAttribute('inert', '')
      undos.push(() => {
        if (prevAria === null) sib.removeAttribute('aria-hidden')
        else sib.setAttribute('aria-hidden', prevAria)
        if (!hadInert) sib.removeAttribute('inert')
      })
    }
    node = parent
  }
  return () => undos.forEach((undo) => undo())
}

interface MeshclawImportCardProps {
  /** When true (e.g. the theme picker is still open), render nothing so the
   *  two first-run overlays never stack. */
  suppressed?: boolean
  /** How long the restarting overlay waits before offering a manual reload
   *  escape hatch (a dropped POST that never restarts the gateway would
   *  otherwise leave a permanent spinner). Overridable for tests. */
  stalledAfterMs?: number
}

/**
 * First-run onboarding step: if the backend reports an importable legacy
 * MeshClaw data dir, offer a one-click import. Import triggers a gateway
 * restart on the backend; the card then shows a reconnecting state and the
 * app's existing socket-reconnect logic re-establishes the session.
 *
 * Gated on `status.available === true`, so it NEVER renders for fresh installs
 * with no legacy data.
 */
export function MeshclawImportCard({
  suppressed = false,
  stalledAfterMs = 30_000,
}: MeshclawImportCardProps) {
  // `dismissed` hides the card for this mount. Only the explicit Skip button
  // persists the choice to localStorage; Escape sets the transient state, so
  // the offer returns on the next launch.
  const [dismissed, setDismissed] = useState(() => !!localStorage.getItem(DISMISS_KEY))
  const [importing, setImporting] = useState(false)
  const [stalled, setStalled] = useState(false)

  const { data } = useQuery({
    queryKey: ['meshclaw-import-status'],
    queryFn: () => api.meshclawImportStatus(),
    staleTime: Infinity,
    retry: false,
    enabled: !dismissed && !suppressed,
  })

  const startMutation = useMutation({
    mutationFn: () => api.meshclawImportStart(),
    onSuccess: () => setImporting(true),
    onError: (err) => {
      // Lost-response race: the restart can kill the connection before the
      // 200 arrives. Treat a network-level failure as "import started" and
      // show the reconnecting overlay; only a confirmed HTTP error (e.g. 409
      // not-available) keeps the card up with the error message.
      if (!isHttpError(err)) setImporting(true)
    },
  })

  const starting = startMutation.isPending
  const startFailed = startMutation.isError && isHttpError(startMutation.error)

  const handleSkip = useCallback(() => {
    localStorage.setItem(DISMISS_KEY, '1')
    setDismissed(true)
  }, [])

  const cardVisible = !importing && !dismissed && !suppressed && !!data?.available

  const cardDialogRef = useRef<HTMLDivElement>(null)
  const importingDialogRef = useRef<HTMLDivElement>(null)
  // Outer fixed wrapper of whichever overlay is currently rendered — the
  // background-inert effect hides everything outside this subtree.
  const overlayRootRef = useRef<HTMLDivElement>(null)
  const importButtonRef = useRef<HTMLButtonElement>(null)

  // A11y: Escape closes the offer card TRANSIENTLY (card returns on next
  // launch); the permanent, localStorage-backed skip is reserved for the
  // explicit Skip button. Not wired while a start request is in flight, and
  // never for the importing overlay (nothing to cancel).
  useEffect(() => {
    if (!cardVisible || starting) return
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      // Only act when this card is the top-most open dialog: a dialog stacked
      // above (e.g. UpdateModal) owns Escape. Modals mount when they open, so
      // document order is a faithful proxy for stacking order.
      const dialogs = document.querySelectorAll('[role="dialog"][aria-modal="true"]')
      if (dialogs[dialogs.length - 1] !== cardDialogRef.current) return
      setDismissed(true)
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [cardVisible, starting])

  // A11y: initial focus. The offer card focuses its primary action; the
  // importing overlay focuses the dialog container itself.
  useEffect(() => {
    if (cardVisible) importButtonRef.current?.focus()
  }, [cardVisible])

  useEffect(() => {
    if (importing) importingDialogRef.current?.focus()
  }, [importing])

  // A11y: on an HTTP failure the pending (disabled) button ejected focus to
  // <body>; return it to the re-enabled Import button so keyboard users can
  // retry, pairing the role=alert announcement with a sane focus point.
  useEffect(() => {
    if (startFailed) importButtonRef.current?.focus()
  }, [startFailed])

  // A11y: while either overlay is open, everything behind it is aria-hidden +
  // inert so screen readers and sequential focus cannot reach the background.
  useEffect(() => {
    if (!importing && !cardVisible) return
    const root = overlayRootRef.current
    if (!root) return
    return hideOutside(root)
  }, [importing, cardVisible])

  // Stuck-spinner watchdog: if the gateway restart never comes back (the
  // POST was dropped before the backend acted), surface a manual reload so
  // the overlay is not a dead end.
  useEffect(() => {
    if (!importing) return
    const t = window.setTimeout(() => setStalled(true), stalledAfterMs)
    return () => window.clearTimeout(t)
  }, [importing, stalledAfterMs])

  // A11y: keyboard half of the focus trap — Tab/Shift-Tab wrap within the
  // dialog. With zero focusables (importing overlay before the reload
  // affordance appears) focus is parked on the container itself.
  const trapTabKey = useCallback((e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== 'Tab') return
    const container = e.currentTarget
    const focusables = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
    if (focusables.length === 0) {
      e.preventDefault()
      container.focus()
      return
    }
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    const active = document.activeElement
    if (e.shiftKey) {
      if (active === first || !container.contains(active)) {
        e.preventDefault()
        last.focus()
      }
    } else if (active === last || !container.contains(active)) {
      e.preventDefault()
      first.focus()
    }
  }, [])

  // Restarting state: the gateway is performing the import and will drop the
  // socket. Keep this overlay up (even if `suppressed` flips or the card was
  // dismissed mid-flight); the reconnect logic reloads the app.
  if (importing) {
    return (
      <div
        ref={overlayRootRef}
        className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/80 backdrop-blur-sm animate-rise"
      >
        <div
          ref={importingDialogRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="mc-import-restarting-title"
          tabIndex={-1}
          onKeyDown={trapTabKey}
          className="bg-card border border-border rounded-xl p-8 max-w-md w-full mx-4 shadow-xl text-center outline-none"
        >
          <div className="flex justify-center mb-4">
            <Loader2 aria-hidden="true" className="lucide-inline animate-spin" size={32} />
          </div>
          <div id="mc-import-restarting-title" className="text-lg font-bold text-text-strong mb-2">
            Importing your data…
          </div>
          <div className="text-sm text-muted mb-1">
            KiroCrew is restarting to import your previous MeshClaw data.
          </div>
          <div className="text-[13px] text-muted">This page will reconnect shortly…</div>
          {stalled && (
            <div className="mt-5 pt-4 border-t border-border">
              <div className="text-[13px] text-muted mb-2">
                Taking longer than expected. If this screen does not reconnect, reload the page.
              </div>
              <button
                className="px-3.5 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer bg-transparent border border-border text-text hover:bg-bg-hover transition-colors inline-flex items-center gap-1.5"
                onClick={() => window.location.reload()}
              >
                <RefreshCw aria-hidden="true" className="lucide-inline" size={14} /> Reload
              </button>
            </div>
          )}
        </div>
      </div>
    )
  }

  if (dismissed || suppressed) return null

  // Gate: never render for fresh installs (available !== true) or while status
  // is still loading.
  if (!data?.available) return null

  return (
    <div
      ref={overlayRootRef}
      className="fixed inset-0 z-[100] flex items-center justify-center bg-bg/80 backdrop-blur-sm animate-rise"
    >
      <div
        ref={cardDialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="mc-import-card-title"
        onKeyDown={trapTabKey}
        className="bg-card border border-border rounded-xl p-6 max-w-md w-full mx-4 shadow-xl"
      >
        <div className="flex items-start gap-3 mb-4">
          <div className="shrink-0 mt-0.5 text-accent">
            <Package aria-hidden="true" className="lucide-inline" size={22} />
          </div>
          <div className="min-w-0">
            <div id="mc-import-card-title" className="text-lg font-bold text-text-strong">
              We found data from a previous MeshClaw install
            </div>
            <div className="text-[13px] text-muted mt-1">
              Import your sessions, memory, workspace, and settings into KiroCrew. Your original
              data is preserved and renamed as a backup.
            </div>
          </div>
        </div>

        <div className="rounded-lg border border-border bg-bg/40 p-3 mb-4 text-[13px]">
          <div className="flex justify-between gap-3 py-0.5">
            <span className="text-muted">Sessions</span>
            <span className="text-text font-medium tabular-nums">{data.sessionCount}</span>
          </div>
          <div className="flex justify-between gap-3 py-0.5">
            <span className="text-muted">Size</span>
            <span className="text-text font-medium tabular-nums">
              {formatBytes(data.sizeEstimateBytes)}
            </span>
          </div>
          {data.sourcePath && (
            <div className="flex justify-between gap-3 py-0.5">
              <span className="text-muted shrink-0">Source</span>
              <span className="text-text/80 font-mono text-[11px] truncate" title={data.sourcePath}>
                {data.sourcePath}
              </span>
            </div>
          )}
        </div>

        {startFailed && (
          <div role="alert" className="text-[13px] text-danger mb-3">
            Import failed to start: {(startMutation.error as Error)?.message || 'Unknown error'}
          </div>
        )}

        <div className="flex items-center justify-end gap-2">
          <button
            className="px-3.5 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer bg-transparent border border-border text-muted hover:text-text hover:bg-bg-hover transition-colors flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
            onClick={handleSkip}
            disabled={starting}
          >
            <X aria-hidden="true" className="lucide-inline" size={14} /> Skip
          </button>
          <button
            ref={importButtonRef}
            className="px-4 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer bg-accent text-accent-fg border-none hover:opacity-90 transition-opacity flex items-center gap-1.5 disabled:opacity-60 disabled:cursor-not-allowed"
            onClick={() => startMutation.mutate()}
            disabled={starting}
          >
            {starting ? (
              <>
                <Loader2 aria-hidden="true" className="lucide-inline animate-spin" size={14} />{' '}
                Starting…
              </>
            ) : (
              <>
                <Download aria-hidden="true" className="lucide-inline" size={14} /> Import
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  )
}

export default MeshclawImportCard
