import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { KiroPrerequisiteStatus } from '../api/client'
import KiroPrerequisiteGate, {
  classifyPrerequisiteError,
  kiroPrerequisiteRefetchInterval,
  readPersistedVerdict,
  redactVerdictForStorage,
  VERDICT_STORAGE_KEY,
} from '../components/KiroPrerequisiteGate'
import { useKiroSessionReady } from '../providers/KiroReadinessContext'
import { renderWithProviders } from './helpers'

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    body: string

    constructor(status: number, message: string, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  },
  api: {
    kiroPrerequisite: vi.fn(),
    installKiroPrerequisite: vi.fn(),
    loginKiroPrerequisite: vi.fn(),
  },
}))

import { api, ApiError } from '../api/client'

function status(overrides: Partial<KiroPrerequisiteStatus> = {}): KiroPrerequisiteStatus {
  return {
    platform: 'Linux',
    installed: false,
    authenticated: false,
    ready: false,
    initial_setup_complete: false,
    can_auto_install: true,
    can_login: true,
    repair_required: false,
    docs_url: 'https://kiro.dev/docs/cli/installation/',
    setup_allowed: true,
    operation: {
      kind: '',
      status: 'idle',
      message: '',
      detail: '',
      url: '',
      error: '',
    },
    ...overrides,
  }
}

function SessionReadinessProbe() {
  const ready = useKiroSessionReady()
  return <div>{ready ? 'Sessions ready' : 'Sessions paused'}</div>
}

describe('KiroPrerequisiteGate', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // Persisted verdicts must not leak between cases — a stale ready verdict
    // would seed the gate and mask the wall an error/cold-load test expects.
    window.localStorage.clear()
  })

  it('keeps a slow readiness poll after setup so later sign-out is detected', () => {
    expect(kiroPrerequisiteRefetchInterval(status({ ready: true }))).toBe(30_000)
    expect(kiroPrerequisiteRefetchInterval(status({
      operation: {
        kind: 'login',
        status: 'running',
        message: '',
        detail: '',
        url: '',
        error: '',
      },
    }))).toBe(1_000)
    // Known-but-not-ready states keep their existing cadence unchanged.
    expect(kiroPrerequisiteRefetchInterval(status({ installed: true }))).toBe(30_000)
    expect(kiroPrerequisiteRefetchInterval(status({ setup_allowed: false }))).toBe(3_000)
  })

  it('backs off 2s→10s while the verdict is unknown instead of a flat 30s', () => {
    // status === undefined is the "no known verdict / gateway unreachable" case.
    expect(kiroPrerequisiteRefetchInterval(undefined, 0)).toBe(2_000)
    expect(kiroPrerequisiteRefetchInterval(undefined, 1)).toBe(2_000)
    expect(kiroPrerequisiteRefetchInterval(undefined, 2)).toBe(4_000)
    expect(kiroPrerequisiteRefetchInterval(undefined, 3)).toBe(8_000)
    // Capped at 10s, never the old 30s.
    expect(kiroPrerequisiteRefetchInterval(undefined, 4)).toBe(10_000)
    expect(kiroPrerequisiteRefetchInterval(undefined, 9)).toBe(10_000)
  })

  it('classifies probe errors by whether a reachable gateway answered', () => {
    // A genuine transport failure (fetch rejects with TypeError) => unknown =>
    // fail open, never the wall.
    expect(classifyPrerequisiteError(new TypeError('Failed to fetch'))).toBe('unreachable')
    // Anything else non-HTTP means a response DID arrive and we could not parse
    // it — no evidence the gateway is unreachable, so it must NOT fail open.
    // Regression: a 200 carrying a non-JSON body (proxy HTML error page) makes
    // `.then(j)` throw SyntaxError; classifying that as 'unreachable' dropped a
    // setup-required user straight into a broken app.
    expect(classifyPrerequisiteError(new SyntaxError('Unexpected token <'))).toBe('gateway-error')
    expect(classifyPrerequisiteError(new Error('boom'))).toBe('gateway-error')
    expect(classifyPrerequisiteError('nope')).toBe('gateway-error')
    // A real HTTP response from a reachable gateway.
    expect(classifyPrerequisiteError(new ApiError(404, 'HTTP 404'))).toBe('missing-endpoint')
    expect(classifyPrerequisiteError(new ApiError(500, 'Probe failed'))).toBe('gateway-error')
    expect(classifyPrerequisiteError(new ApiError(503, 'unavailable'))).toBe('gateway-error')
  })

  it('keeps the wall when a 200 response carries an unparseable body', async () => {
    // End-to-end form of the regression above: the gate must not fail open for a
    // malformed-but-delivered response.
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new SyntaxError('Unexpected token <'))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('We could not check Kiro CLI.')).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('reads a persisted verdict defensively', () => {
    // Nothing stored.
    expect(readPersistedVerdict()).toBeUndefined()
    // Corrupt JSON is ignored, never thrown.
    window.localStorage.setItem(VERDICT_STORAGE_KEY, '{ not valid json')
    expect(readPersistedVerdict()).toBeUndefined()
    // A structurally invalid blob does not fabricate a verdict.
    window.localStorage.setItem(VERDICT_STORAGE_KEY, JSON.stringify({ ready: true }))
    expect(readPersistedVerdict()).toBeUndefined()
    // A well-formed verdict round-trips.
    const verdict = status({ installed: true, authenticated: true, ready: true })
    window.localStorage.setItem(VERDICT_STORAGE_KEY, JSON.stringify(verdict))
    // Read redacts, so a legacy blob written by an older build is neutralised.
    expect(readPersistedVerdict()).toEqual(redactVerdictForStorage(verdict))
  })

  it('rejects a persisted verdict whose operation.url is not a string', () => {
    // Regression: `operation.url` reaches trustedLoginUrl(), which calls string
    // methods on it. A non-string url must be rejected by the structural guard
    // rather than reaching that code and crashing the gate.
    const verdict = status({ installed: true, authenticated: true, ready: true })
    const drifted = { ...verdict, operation: { ...verdict.operation, url: 1 } }
    window.localStorage.setItem(VERDICT_STORAGE_KEY, JSON.stringify(drifted))
    expect(readPersistedVerdict()).toBeUndefined()
  })

  it('rejects a persisted verdict with ANY type-drifted field', () => {
    // Regression (third round of the same class): spot-checking a few fields left
    // the rest as a crash path — every field is rendered somewhere downstream and
    // React throws when handed an object. The guard must enumerate the whole
    // contract, so this walks EVERY field and asserts each one alone is enough to
    // reject the blob. A field added to the type but not to the guard fails here.
    const good = status({ installed: true, authenticated: true, ready: true })
    // Baseline: the well-formed fixture is still accepted by the stricter guard.
    window.localStorage.setItem(VERDICT_STORAGE_KEY, JSON.stringify(good))
    expect(readPersistedVerdict()).toEqual(redactVerdictForStorage(good))

    const topLevel = Object.keys(good) as (keyof typeof good)[]
    for (const key of topLevel) {
      if (key === 'operation') continue
      // An object is the dangerous value: React crashes rendering it.
      const drifted = { ...good, [key]: { nested: 'boom' } }
      window.localStorage.setItem(VERDICT_STORAGE_KEY, JSON.stringify(drifted))
      expect(readPersistedVerdict(), `top-level field ${String(key)} must be validated`)
        .toBeUndefined()
    }
    for (const key of Object.keys(good.operation) as (keyof typeof good.operation)[]) {
      const drifted = { ...good, operation: { ...good.operation, [key]: { nested: 'boom' } } }
      window.localStorage.setItem(VERDICT_STORAGE_KEY, JSON.stringify(drifted))
      expect(readPersistedVerdict(), `operation.${String(key)} must be validated`)
        .toBeUndefined()
    }
    // And a missing operation sub-field is drift too, not a partial accept.
    const { error: _dropped, ...partialOp } = good.operation
    window.localStorage.setItem(
      VERDICT_STORAGE_KEY,
      JSON.stringify({ ...good, operation: partialOp }),
    )
    expect(readPersistedVerdict()).toBeUndefined()
  })

  it('never persists host details, sign-in URL, or operation output', async () => {
    // Regression: localStorage is origin-scoped, not subject-scoped. Persisting
    // the full verdict meant an identity switch in the same browser could render
    // the previous subject's host details and device-flow sign-in URL for the
    // window before the refetch landed. Nothing sensitive may reach storage.
    const sensitive = status({
      installed: true,
      authenticated: false,
      ready: false,
      platform: 'macOS-15.1-arm64-somehost',
      docs_url: 'https://kiro.dev/docs/cli/installation/',
      operation: {
        kind: 'login',
        status: 'running',
        message: 'Visit the URL to finish signing in',
        detail: 'device flow started on somehost',
        url: 'https://view.awsapps.com/start/#/device?user_code=SECRET-CODE',
        error: '',
      },
    })
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(sensitive)

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )
    await waitFor(() => expect(window.localStorage.getItem(VERDICT_STORAGE_KEY)).toBeTruthy())

    const raw = window.localStorage.getItem(VERDICT_STORAGE_KEY) as string
    // Assert on the raw serialized payload, not the parsed object: any leak of
    // these substrings anywhere in the blob is a failure.
    expect(raw).not.toContain('SECRET-CODE')
    expect(raw).not.toContain('somehost')
    expect(raw).not.toContain('finish signing in')
    expect(raw).not.toContain('kiro.dev')

    // The decision-relevant fields DO survive, or persistence would be pointless.
    const stored = JSON.parse(raw) as KiroPrerequisiteStatus
    expect(stored.installed).toBe(true)
    expect(stored.authenticated).toBe(false)
    expect(stored.ready).toBe(false)
    expect(stored.operation.status).toBe('running')
    expect(stored.operation.kind).toBe('login')
    // …and the redacted blob still satisfies the structural guard, so it is
    // actually usable as a seed rather than being rejected on read.
    expect(readPersistedVerdict()).toBeDefined()
  })

  it('renders the application immediately when Kiro is ready', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      ready: true,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate>
        <div>Dashboard loaded</div>
        <SessionReadinessProbe />
      </KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.getByText('Sessions ready')).toBeInTheDocument()
    expect(screen.queryByText('Set up Kiro')).not.toBeInTheDocument()
  })

  it('installs on the named gateway host and unlocks device login', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ platform: 'Windows' }))
    vi.mocked(api.installKiroPrerequisite).mockResolvedValue(status({
      platform: 'Windows',
      installed: true,
      operation: {
        kind: 'install',
        status: 'succeeded',
        message: 'Kiro CLI is installed.',
        detail: '',
        url: '',
        error: '',
      },
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText(/Kiro Crew uses Kiro CLI/)).toBeInTheDocument()
    expect((await screen.findAllByText(/Windows gateway host/)).length).toBeGreaterThan(0)
    fireEvent.click(screen.getByRole('button', { name: 'Install Kiro CLI' }))
    await waitFor(() => expect(api.installKiroPrerequisite).toHaveBeenCalledOnce())
    expect(await screen.findByRole('button', { name: 'Sign in to Kiro' })).toBeEnabled()
  })

  it('offers sign-in for an already-installed CLI regardless of install source', async () => {
    // A user-owned / self-updated / toolbox Kiro CLI that runs is installed and
    // sign-in ready — no "unverified executable" dead end, no repair prompt.
    // The mock reproduces the exact OLD rejected-provenance status
    // (can_login:false + repair_required:true): under the pre-change gate this
    // rendered a button-less "Reinstall" dead end; the new "runs" contract must
    // ignore both fields and still offer an enabled Sign-in — so this fails on
    // revert of the can_login/repair_required gate removals.
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: false,
      can_auto_install: false,
      can_login: false,
      repair_required: true,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    const loginButton = await screen.findByRole('button', { name: 'Sign in to Kiro' })
    expect(loginButton).toBeEnabled()
    expect(screen.queryByText(/unverified executable/)).not.toBeInTheDocument()
    expect(screen.queryByText('rm -- ~/.local/bin/kiro-cli')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Installed' })).toBeDisabled()
  })

  it('shows the secure device URL and advances when login becomes ready', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({ installed: true }))
    vi.mocked(api.loginKiroPrerequisite).mockResolvedValue(status({
      installed: true,
      operation: {
        kind: 'login',
        status: 'running',
        message: 'Open the sign-in page.',
        detail: 'Enter code ABCD-EFGH',
        url: 'https://view.awsapps.com/start/',
        error: '',
      },
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Sign in to Kiro' }))
    const link = await screen.findByRole('link', { name: /Open Kiro sign-in page/ })
    expect(link).toHaveAttribute('href', 'https://view.awsapps.com/start/')
    expect(screen.getByText(/ABCD-EFGH/)).toBeInTheDocument()
  })

  it('does not render a login link when browser URL parsing rejects it', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      operation: {
        kind: 'login',
        status: 'running',
        message: 'Open the sign-in page.',
        detail: 'Enter code ABCD-EFGH',
        url: 'https://evil.example\\@view.awsapps.com/start',
        error: '',
      },
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText(/ABCD-EFGH/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Open Kiro sign-in page/ })).not.toBeInTheDocument()
  })

  it('shows non-owners a redacted owner-setup state', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      platform: 'gateway',
      can_auto_install: false,
      setup_allowed: false,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText(/gateway owner needs to finish setup/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Install Kiro CLI' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check again' })).toBeEnabled()
  })

  it('lets a non-owner observe owner completion without reloading', async () => {
    vi.mocked(api.kiroPrerequisite)
      .mockResolvedValueOnce(status({
        platform: 'gateway',
        can_auto_install: false,
        setup_allowed: false,
      }))
      .mockResolvedValueOnce(status({
        platform: 'gateway',
        installed: true,
        authenticated: true,
        ready: true,
        initial_setup_complete: true,
        setup_allowed: false,
      }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Check again' }))
    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
  })

  it('keeps cached readiness mounted after a transient refetch failure', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      ready: true,
    }))
    const rendered = renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )
    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()

    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new ApiError(500, 'Probe failed'))
    await rendered.queryClient.invalidateQueries({ queryKey: ['kiro-prerequisite'] })

    expect(screen.getByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('We could not check Kiro CLI.')).not.toBeInTheDocument()
  })

  it('keeps an established dashboard navigable during Kiro reauthentication', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      initial_setup_complete: true,
    }))
    vi.mocked(api.loginKiroPrerequisite).mockResolvedValue(status({
      installed: true,
      initial_setup_complete: true,
      operation: {
        kind: 'login',
        status: 'running',
        message: 'Open the sign-in page.',
        detail: 'Enter code ABCD-EFGH',
        url: 'https://view.awsapps.com/start/',
        error: '',
      },
    }))

    renderWithProviders(
      <KiroPrerequisiteGate>
        <div>Dashboard loaded</div>
        <SessionReadinessProbe />
      </KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.getByText('Sessions paused')).toBeInTheDocument()
    expect(screen.getByText('Kiro Crew needs Kiro sign-in.')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveClass('pointer-events-none')
    fireEvent.click(screen.getByRole('button', { name: 'Sign in to Kiro' }))
    await waitFor(() => expect(api.loginKiroPrerequisite).toHaveBeenCalledOnce())
    expect(await screen.findByText(/ABCD-EFGH/)).toBeInTheDocument()
  })

  it('offers a copyable terminal sign-in command in the re-auth banner', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    // happy-dom's navigator.clipboard is getter-only; defineProperty replaces it.
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: false,
      ready: false,
      initial_setup_complete: true,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('kiro-cli login')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /copy sign-in command/i }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('kiro-cli login'))
    // The retry control reads as a post-sign-in re-check, not a failed-probe retry.
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Check again' })).not.toBeInTheDocument()
  })

  it('keeps an established non-owner dashboard open while the owner reconnects', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      platform: 'gateway',
      initial_setup_complete: true,
      setup_allowed: false,
    }))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.getByText(/gateway owner needs to restore Kiro access/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in to Kiro' })).not.toBeInTheDocument()
  })

  it('fails open when connected to a gateway without the new endpoint', async () => {
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new ApiError(404, 'HTTP 404'))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
  })

  it('fails open on 404 even when a stale not-ready verdict is persisted', async () => {
    // Regression: a 404 means the GATEWAY has no prerequisite API, which is not
    // something a cached verdict can override. A persisted `ready:false` used to
    // satisfy the `&& !prerequisite` guard and pin the wall up against a gateway
    // rolled back to a version predating the API.
    window.localStorage.setItem(
      VERDICT_STORAGE_KEY,
      JSON.stringify(status({ installed: false, authenticated: false, ready: false })),
    )
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new ApiError(404, 'HTTP 404'))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('We could not check Kiro CLI.')).not.toBeInTheDocument()
  })

  it('keeps setup visible and offers retry for a live gateway error', async () => {
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new ApiError(500, 'Probe failed'))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('We could not check Kiro CLI.')).toBeInTheDocument()
    expect(screen.getByText(/Probe failed/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })

  it('fails open on a network-layer failure with no cached verdict', async () => {
    // A fetch that never reached the gateway throws a bare TypeError (no HTTP
    // status). That says nothing about Kiro setup, so the app renders and its
    // own offline layer owns the message — the setup wall must NOT appear.
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new TypeError('Failed to fetch'))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    expect(screen.queryByText('We could not check Kiro CLI.')).not.toBeInTheDocument()
    expect(screen.queryByText('Setup check unavailable')).not.toBeInTheDocument()
  })

  it('reuses a persisted verdict across a remount during a gateway restart', async () => {
    vi.mocked(api.kiroPrerequisite).mockResolvedValue(status({
      installed: true,
      authenticated: true,
      ready: true,
    }))
    const first = renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )
    expect(await screen.findByText('Dashboard loaded')).toBeInTheDocument()
    // The successful verdict is mirrored to storage.
    await waitFor(() =>
      expect(window.localStorage.getItem(VERDICT_STORAGE_KEY)).toBeTruthy(),
    )
    first.unmount()

    // Cold remount (fresh query cache) while the gateway is unreachable: the
    // persisted ready verdict seeds the gate so it renders the app immediately
    // instead of the wall, exactly like the already-loaded case.
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new TypeError('Failed to fetch'))
    renderWithProviders(
      <KiroPrerequisiteGate><div>Reloaded dashboard</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('Reloaded dashboard')).toBeInTheDocument()
    expect(screen.queryByText('We could not check Kiro CLI.')).not.toBeInTheDocument()
  })

  it('ignores a corrupt persisted verdict without fabricating readiness', async () => {
    // A corrupt stored blob must be ignored, not trusted: with no valid seed the
    // gate cold-loads and a real 5xx still produces the wall (proving the corrupt
    // value neither crashed the gate nor faked a ready verdict).
    window.localStorage.setItem(VERDICT_STORAGE_KEY, '{ corrupt')
    vi.mocked(api.kiroPrerequisite).mockRejectedValue(new ApiError(500, 'Probe failed'))

    renderWithProviders(
      <KiroPrerequisiteGate><div>Dashboard loaded</div></KiroPrerequisiteGate>,
    )

    expect(await screen.findByText('We could not check Kiro CLI.')).toBeInTheDocument()
    expect(screen.queryByText('Dashboard loaded')).not.toBeInTheDocument()
  })
})
