/**
 * Tests for the governance-aware channel tabs on the Settings page.
 *
 * When the `channels` governance policy DENIES a channel (endpoint returns
 * false), its Settings tab is greyed + disabled with an "Off by admin" badge
 * (NOT hidden), and its panel body shows the disabled-by-policy state instead
 * of the editable config form. With all-true (the standard build default),
 * nothing is greyed and the editable panels render — UI unchanged from today.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mocks = vi.hoisted(() => ({ getGovernanceChannels: vi.fn() }))

vi.mock('../api/client', () => ({ api: { getGovernanceChannels: mocks.getGovernanceChannels } }))

// Stub the heavy panels — we test the tab greying + disabled-panel swap, not internals.
vi.mock('../pages/settings/OverviewPanel', () => ({ OverviewPanel: () => <div data-testid="overview-panel" /> }))
vi.mock('../pages/settings/ChatPanel', () => ({ ChatPanel: () => <div data-testid="chat-panel" /> }))
vi.mock('../pages/settings/DisplayPanel', () => ({ DisplayPanel: () => <div data-testid="display-panel" /> }))
vi.mock('../pages/settings/BrowserPanel', () => ({ BrowserPanel: () => <div data-testid="browser-panel" /> }))
vi.mock('../pages/settings/InstancesPanel', () => ({ InstancesPanel: () => <div data-testid="instances-panel" /> }))
vi.mock('../pages/settings/SecurityPanel', () => ({ SecurityPanel: () => <div data-testid="security-panel" /> }))
vi.mock('../pages/settings/NotificationsPanel', () => ({ NotificationsPanel: () => <div data-testid="notifications-panel" /> }))
vi.mock('../pages/settings/SlackPanel', () => ({ SlackPanel: () => <div data-testid="slack-panel" /> }))
vi.mock('../pages/settings/DiscordPanel', () => ({ DiscordPanel: () => <div data-testid="discord-panel" /> }))
vi.mock('../pages/settings/TelegramPanel', () => ({ TelegramPanel: () => <div data-testid="telegram-panel" /> }))
vi.mock('../pages/settings/WebexPanel', () => ({ WebexPanel: () => <div data-testid="webex-panel" /> }))
vi.mock('../pages/settings/WeComPanel', () => ({ WeComPanel: () => <div data-testid="wecom-panel" /> }))
vi.mock('../pages/settings/GeneralPanel', () => ({ GeneralPanel: () => <div data-testid="general-panel" /> }))

vi.mock('../store', () => ({ useAppSelector: () => undefined }))

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

import SettingsPage from '../pages/SettingsPage'

function renderAt(route: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={[route]}>
      <QueryClientProvider client={queryClient}>
        <SettingsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('SettingsPage channel governance', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('greys + badges a policy-denied channel tab and shows the disabled panel', async () => {
    mocks.getGovernanceChannels.mockResolvedValue({
      slack: true, discord: false, telegram: true, webex: true, wecom: true,
    })
    renderAt('/settings?tab=discord')

    // Badge appears next to the denied tab's label.
    await waitFor(() => expect(screen.getByText('Off by admin')).toBeInTheDocument())

    // The Discord tab button is greyed (opacity-50 + cursor-not-allowed).
    const discordBtn = screen.getByRole('button', { name: /Discord/ })
    expect(discordBtn.className).toContain('opacity-50')
    expect(discordBtn.className).toContain('cursor-not-allowed')

    // The editable DiscordPanel must NOT render; the disabled state shows instead.
    expect(screen.queryByTestId('discord-panel')).not.toBeInTheDocument()
    expect(screen.getByText(/turned off by your administrator/)).toBeInTheDocument()
  })

  it('renders the editable panel and no greying when all channels are permitted', async () => {
    mocks.getGovernanceChannels.mockResolvedValue({
      slack: true, discord: true, telegram: true, webex: true, wecom: true,
    })
    renderAt('/settings?tab=discord')

    // Editable panel renders; no badge, no disabled state.
    await waitFor(() => expect(screen.getByTestId('discord-panel')).toBeInTheDocument())
    expect(screen.queryByText('Off by admin')).not.toBeInTheDocument()
    expect(screen.queryByText(/turned off by your administrator/)).not.toBeInTheDocument()

    const discordBtn = screen.getByRole('button', { name: /Discord/ })
    expect(discordBtn.className).not.toContain('opacity-50')
  })
})
