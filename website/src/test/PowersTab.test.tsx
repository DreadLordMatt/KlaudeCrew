import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { Power, RegistryPower } from '../types'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  powers: vi.fn(),
  powersRegistry: vi.fn(),
  powerRegistryDetail: vi.fn(),
  installPower: vi.fn(),
  removePower: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import PowersTab from '../pages/overview/PowersTab'

const power = (over: Partial<Power> = {}): Power => ({
  name: 'supabase',
  displayName: 'Supabase',
  description: 'Local Postgres with the Supabase CLI.',
  keywords: ['database', 'postgres'],
  author: 'Supabase',
  kind: 'mcp',
  steeringFiles: [],
  source: { kind: 'github', ref: 'https://github.com/supabase/supabase-power' },
  installedAt: '2026-01-01T00:00:00Z',
  path: '/home/u/.kiro/powers/supabase',
  ...over,
})

const reg = (over: Partial<RegistryPower> = {}): RegistryPower => ({
  id: 'stripe',
  displayName: 'Stripe',
  description: 'Accept payments with Stripe.',
  author: 'Stripe',
  category: 'Backend & APIs',
  scope: 'official',
  githubUrl: 'https://github.com/kirodotdev/powers/tree/main/stripe',
  keywords: ['payments'],
  provider: 'official',
  ...over,
})

const REGISTRY = {
  items: [
    reg(),
    reg({ id: 'aws-lambda', displayName: 'AWS Lambda', scope: 'aws', category: 'Cloud & Infrastructure', keywords: ['serverless'], githubUrl: 'https://github.com/kirodotdev/powers/tree/main/aws-lambda' }),
    reg({ id: 'widget', displayName: 'Community Widget', scope: 'community', category: 'Frontend & UI', keywords: ['ui'], githubUrl: 'https://github.com/acme/widget-power', provider: 'marketplace' }),
  ],
  providers: [{ name: 'official', displayName: 'Official', available: true }],
  stale: false,
}

function renderTab() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}><PowersTab /></QueryClientProvider>)
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.powers.mockResolvedValue({ installed: [] })
  mockApi.powersRegistry.mockResolvedValue(REGISTRY)
})

describe('PowersTab — installed view', () => {
  it('renders each installed power with its declared kind badge', async () => {
    mockApi.powers.mockResolvedValue({ installed: [power()] })
    renderTab()
    await waitFor(() => expect(screen.getByText('Powers (1)')).toBeInTheDocument())
    expect(screen.getByText('Supabase')).toBeInTheDocument()
    expect(screen.getByText('MCP')).toBeInTheDocument()
  })

  it('renders the empty state when nothing is installed', async () => {
    mockApi.powers.mockResolvedValue({ installed: [] })
    renderTab()
    // With nothing installed the tab opens on Browse; switch to Installed.
    const installedTab = await screen.findByRole('tab', { name: 'Installed' })
    fireEvent.click(installedTab)
    await waitFor(() => expect(screen.getByText('No powers installed')).toBeInTheDocument())
  })

  it('exposes NO activation affordance for an installed power', async () => {
    // The security claim this release makes is that an installed Power is
    // inert. A toggle or trust button would contradict it, so their ABSENCE is
    // the contract — asserted directly rather than left implicit.
    mockApi.powers.mockResolvedValue({ installed: [power()] })
    renderTab()
    await waitFor(() => expect(screen.getByText('Supabase')).toBeInTheDocument())
    expect(screen.queryByRole('switch')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /trust/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /enable/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/Review before trusting/)).not.toBeInTheDocument()
  })

  it('states plainly that an installed power is not active', async () => {
    mockApi.powers.mockResolvedValue({ installed: [power()] })
    renderTab()
    await waitFor(() => expect(screen.getByText('Inactive')).toBeInTheDocument())
    expect(screen.getByText(/no MCP server is registered/)).toBeInTheDocument()
  })

  it('remove hits the remove endpoint', async () => {
    mockApi.powers.mockResolvedValue({ installed: [power()] })
    mockApi.removePower.mockResolvedValue({ ok: true })
    renderTab()
    const btn = await screen.findByRole('button', { name: 'Remove Supabase' })
    fireEvent.click(btn)
    await waitFor(() => expect(mockApi.removePower).toHaveBeenCalledWith('supabase'))
  })
})

describe('PowersTab — browse view', () => {
  it('renders the registry grid', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('Stripe')).toBeInTheDocument())
    expect(screen.getByText('AWS Lambda')).toBeInTheDocument()
    expect(screen.getByText('Community Widget')).toBeInTheDocument()
  })

  it('search narrows the grid', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('Stripe')).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Search powers'), { target: { value: 'lambda' } })
    await waitFor(() => expect(screen.queryByText('Stripe')).not.toBeInTheDocument())
    expect(screen.getByText('AWS Lambda')).toBeInTheDocument()
  })

  it('the scope filter narrows the grid', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('Stripe')).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'AWS' }))
    await waitFor(() => expect(screen.queryByText('Stripe')).not.toBeInTheDocument())
    expect(screen.getByText('AWS Lambda')).toBeInTheDocument()
    expect(screen.queryByText('Community Widget')).not.toBeInTheDocument()
  })

  it('the category filter narrows the grid', async () => {
    renderTab()
    await waitFor(() => expect(screen.getByText('Stripe')).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Filter by category'), { target: { value: 'Frontend & UI' } })
    await waitFor(() => expect(screen.queryByText('Stripe')).not.toBeInTheDocument())
    expect(screen.getByText('Community Widget')).toBeInTheDocument()
  })

  it('install fires the mutation with the correct source payload', async () => {
    mockApi.installPower.mockResolvedValue({ power: power() })
    renderTab()
    const btn = await screen.findByRole('button', { name: 'Install Stripe' })
    fireEvent.click(btn)
    await waitFor(() => expect(mockApi.installPower).toHaveBeenCalledWith({ kind: 'registry', ref: 'stripe', provider: 'official' }))
  })

  it('a registry 503 renders the unavailable state without crashing', async () => {
    mockApi.powersRegistry.mockRejectedValue(Object.assign(new Error('service unavailable'), { status: 503 }))
    renderTab()
    await waitFor(() => expect(screen.getByText('Powers registry unavailable')).toBeInTheDocument())
    expect(screen.getByText(/warming up/i)).toBeInTheDocument()
    // No crash, no infinite spinner — a Retry affordance is offered.
    expect(screen.getByRole('button', { name: /Retry/ })).toBeInTheDocument()
  })
})

describe('PowersTab — provider-aware installed matching', () => {
  // Two registry cards share the slug "foo": one from the official provider, one
  // from the marketplace, pointing at DIFFERENT repositories. Only the official
  // repo is actually installed.
  const collideRegistry = {
    items: [
      reg({ id: 'foo', displayName: 'Foo Official', scope: 'official', provider: 'official', githubUrl: 'https://github.com/kirodotdev/powers/tree/main/foo' }),
      reg({ id: 'foo', displayName: 'Foo Marketplace', scope: 'community', provider: 'marketplace', githubUrl: 'https://github.com/acme/foo-power' }),
    ],
    providers: [{ name: 'official', displayName: 'Official', available: true }],
    stale: false,
  }

  const openBrowse = async () => {
    // `view` useState-initialises to 'installed', so the Installed tab reads
    // selected from the FIRST render — waiting on that races the post-load
    // auto-pick effect, which can fire after the click and revert to Installed.
    // Wait for the loaded count instead: it only updates once the query resolves
    // (and thus after the auto-pick effect has committed).
    await screen.findByText('Powers (1)')
    fireEvent.click(screen.getByRole('tab', { name: /Browse/ }))
    await screen.findByRole('tab', { name: /Browse/, selected: true })
  }

  it('does NOT mark a same-named marketplace power installed when only the official one is installed', async () => {
    mockApi.powers.mockResolvedValue({ installed: [power({ name: 'foo', displayName: 'Foo', source: { kind: 'github', ref: 'https://github.com/kirodotdev/powers/tree/main/foo' } })] })
    mockApi.powersRegistry.mockResolvedValue(collideRegistry)
    renderTab()
    await openBrowse()
    await waitFor(() => expect(screen.getByText('Foo Marketplace')).toBeInTheDocument())

    // Official "foo" matches on the resolved repo URL → Installed, no install button.
    expect(screen.queryByRole('button', { name: 'Install Foo Official' })).not.toBeInTheDocument()
    // Marketplace "foo" points at a different repo; the bare-slug fallback must
    // NOT apply to a non-official provider, so it is still installable.
    expect(screen.getByRole('button', { name: 'Install Foo Marketplace' })).toBeInTheDocument()
    // Exactly one card is still installable (the marketplace one); the official
    // card is installed, so it shows no Install button.
    expect(screen.getAllByRole('button', { name: /^Install / })).toHaveLength(1)
  })

  it('marks a marketplace power installed via its resolved repo URL, independent of name', async () => {
    // Installed record has a name that matches NO registry slug, so only the
    // URL comparison can mark anything installed — proving the reliable path
    // works for a marketplace card (whose slug fallback is disabled).
    mockApi.powers.mockResolvedValue({ installed: [power({ name: 'local-widget', displayName: 'Widget', source: { kind: 'github', ref: 'https://github.com/acme/widget-power' } })] })
    mockApi.powersRegistry.mockResolvedValue(REGISTRY)
    renderTab()
    await openBrowse()
    await waitFor(() => expect(screen.getByText('Community Widget')).toBeInTheDocument())

    // The marketplace card sharing the repo URL is Installed…
    expect(screen.queryByRole('button', { name: 'Install Community Widget' })).not.toBeInTheDocument()
    // …while the unrelated official cards remain installable.
    expect(screen.getByRole('button', { name: 'Install Stripe' })).toBeInTheDocument()
    // Only the Community Widget card matched (by URL); the two official cards
    // (Stripe, AWS Lambda) are still installable.
    expect(screen.getAllByRole('button', { name: /^Install / })).toHaveLength(2)
  })
})

describe('PowersTab — round 12 regressions', () => {
  it('does NOT switch to Browse when the installed query fails', async () => {
    // A failed query also stops loading. Treating that as "nothing installed"
    // switched to Browse and offered already-installed Powers as installable.
    // Asserted via a CONSEQUENCE rather than tab state: the registry query is
    // `enabled: view === 'browse'`, so if the view ever flips to Browse the
    // registry is fetched. A tab-attribute assertion is satisfied by the initial
    // render before the effect runs, so it passes even when the bug is present.
    mockApi.powers.mockRejectedValue(new Error('backend down'))
    renderTab()
    await waitFor(() => expect(mockApi.powers).toHaveBeenCalled())
    // Give the effect every chance to run before concluding it did not switch.
    await new Promise(r => setTimeout(r, 50))
    expect(mockApi.powersRegistry).not.toHaveBeenCalled()
    expect(screen.queryByText('No powers installed')).not.toBeInTheDocument()
  })

  it('still switches to Browse when the installed query succeeds empty', async () => {
    mockApi.powers.mockResolvedValue({ installed: [] })
    renderTab()
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Browse' })).toHaveAttribute('aria-selected', 'true'),
    )
  })

  it('describes a stale registry as incomplete, not as cached data', async () => {
    mockApi.powersRegistry.mockResolvedValue({ ...REGISTRY, stale: true })
    renderTab()
    await waitFor(() => expect(screen.getByText('Stripe')).toBeInTheDocument())
    expect(screen.getByText(/may be incomplete or out of date/i)).toBeInTheDocument()
    expect(screen.queryByText(/cached copy/i)).not.toBeInTheDocument()
  })

  it('says nothing is cached when a stale registry is also empty', async () => {
    // The backend sets `stale` on provider failure whether or not a cache
    // existed, so an empty result must not claim to be showing cached content.
    mockApi.powersRegistry.mockResolvedValue({
      items: [],
      providers: [{ name: 'official', displayName: 'Official', available: false }],
      stale: true,
    })
    renderTab()
    await waitFor(() =>
      expect(screen.getByText(/every registry provider is currently unavailable/i)).toBeInTheDocument(),
    )
    expect(screen.queryByText(/cached copy/i)).not.toBeInTheDocument()
  })

  // ── publisher icons ──────────────────────────────────────────────────────
  // The URL is host-validated server-side, so these cover the presentational
  // contract: use the icon when there is one, fall back to the Kiro Powers mark
  // when there is not, and fall back again when a third-party host fails to
  // serve it (a broken-image glyph in every card is worse than no icon).

  it('renders the publisher icon when the registry supplies one', async () => {
    mockApi.powers.mockResolvedValue({ installed: [] })
    mockApi.powersRegistry.mockResolvedValue({
      items: [reg({ iconUrl: 'https://prod.download.desktop.kiro.dev/powers/icons/stripe.png' })],
      providers: [],
      stale: false,
    })
    renderTab()

    const icon = await screen.findByTestId('power-icon')
    expect(icon).toHaveAttribute(
      'src',
      'https://prod.download.desktop.kiro.dev/powers/icons/stripe.png',
    )
    // Decorative: the card title already names the Power, so the image must not
    // repeat it to screen readers.
    expect(icon).toHaveAttribute('alt', '')
    expect(icon).toHaveAttribute('aria-hidden', 'true')
    expect(screen.queryByTestId('power-icon-fallback')).not.toBeInTheDocument()
  })

  it('falls back to the Powers mark when no icon is supplied', async () => {
    mockApi.powers.mockResolvedValue({ installed: [] })
    mockApi.powersRegistry.mockResolvedValue({
      items: [reg({ iconUrl: undefined })],
      providers: [],
      stale: false,
    })
    renderTab()

    expect(await screen.findByTestId('power-icon-fallback')).toBeInTheDocument()
    expect(screen.queryByTestId('power-icon')).not.toBeInTheDocument()
  })

  it('falls back when the icon host fails to serve the image', async () => {
    mockApi.powers.mockResolvedValue({ installed: [] })
    mockApi.powersRegistry.mockResolvedValue({
      items: [reg({ iconUrl: 'https://prod.download.desktop.kiro.dev/powers/icons/gone.png' })],
      providers: [],
      stale: false,
    })
    renderTab()

    const icon = await screen.findByTestId('power-icon')
    fireEvent.error(icon)

    expect(await screen.findByTestId('power-icon-fallback')).toBeInTheDocument()
    expect(screen.queryByTestId('power-icon')).not.toBeInTheDocument()
  })

  it('omits the Source link when the repository URL is not a safe http(s) URL', async () => {
    // The value is validated server-side, but it also survives in a disk cache,
    // so the render must not be the only thing standing between a poisoned entry
    // and a clickable `javascript:` href.
    mockApi.powers.mockResolvedValue({ installed: [] })
    mockApi.powersRegistry.mockResolvedValue({
      items: [reg({ githubUrl: 'javascript:alert(1)' })],
      providers: [],
      stale: false,
    })
    renderTab()

    expect(await screen.findByText('Source unavailable')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /View Stripe source on GitHub/ })).not.toBeInTheDocument()
  })

  it('renders the Source link for a normal https URL', async () => {
    mockApi.powers.mockResolvedValue({ installed: [] })
    mockApi.powersRegistry.mockResolvedValue({
      items: [reg({ githubUrl: 'https://github.com/kirodotdev/powers/tree/main/stripe' })],
      providers: [],
      stale: false,
    })
    renderTab()

    const link = await screen.findByRole('link', { name: /View Stripe source on GitHub/ })
    expect(link).toHaveAttribute('href', 'https://github.com/kirodotdev/powers/tree/main/stripe')
    expect(screen.queryByText('Source unavailable')).not.toBeInTheDocument()
  })
})
