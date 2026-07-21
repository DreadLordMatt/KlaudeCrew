import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  meshclawImportStatus: vi.fn(),
  meshclawImportStart: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import { MeshclawImportCard, formatBytes } from '../components/MeshclawImportCard'

function renderCard(props: { suppressed?: boolean; stalledAfterMs?: number } = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <QueryClientProvider client={qc}>
      <MeshclawImportCard {...props} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  mockApi.meshclawImportStatus.mockReset()
  mockApi.meshclawImportStart.mockReset()
})

describe('formatBytes', () => {
  it('formats byte sizes with units', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(1536)).toBe('1.5 KB')
    expect(formatBytes(1572864)).toBe('1.5 MB')
  })
})

describe('MeshclawImportCard', () => {
  it('renders nothing for a fresh install (available: false)', async () => {
    mockApi.meshclawImportStatus.mockResolvedValue({
      available: false,
      sourcePath: '',
      sizeEstimateBytes: 0,
      sessionCount: 0,
    })
    renderCard()
    // Give the query a tick to resolve, then confirm the card never appears.
    await waitFor(() => expect(mockApi.meshclawImportStatus).toHaveBeenCalled())
    expect(screen.queryByText(/found data from a previous MeshClaw install/i)).toBeNull()
  })

  it('does not fetch or render while suppressed (theme picker open)', () => {
    renderCard({ suppressed: true })
    expect(mockApi.meshclawImportStatus).not.toHaveBeenCalled()
    expect(screen.queryByText(/found data from a previous MeshClaw install/i)).toBeNull()
  })

  it('shows size + session count and switches to the restarting state on Import', async () => {
    mockApi.meshclawImportStatus.mockResolvedValue({
      available: true,
      sourcePath: '/Users/tester/.meshclaw',
      sizeEstimateBytes: 1572864, // 1.5 MB
      sessionCount: 42,
    })
    mockApi.meshclawImportStart.mockResolvedValue({ status: 'restarting' })
    renderCard()

    expect(await screen.findByText(/found data from a previous MeshClaw install/i)).toBeTruthy()
    expect(screen.getByText('42')).toBeTruthy()
    expect(screen.getByText('1.5 MB')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /import/i }))

    await waitFor(() => expect(mockApi.meshclawImportStart).toHaveBeenCalledTimes(1))
    // Restarting / reconnecting state replaces the card.
    expect(await screen.findByText(/This page will reconnect shortly/i)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /import/i })).toBeNull()
  })

  it('Skip dismisses the card and persists the choice', async () => {
    mockApi.meshclawImportStatus.mockResolvedValue({
      available: true,
      sourcePath: '/Users/tester/.meshclaw',
      sizeEstimateBytes: 2048,
      sessionCount: 3,
    })
    renderCard()

    await screen.findByText(/found data from a previous MeshClaw install/i)
    fireEvent.click(screen.getByRole('button', { name: /skip/i }))

    await waitFor(() =>
      expect(screen.queryByText(/found data from a previous MeshClaw install/i)).toBeNull(),
    )
    expect(localStorage.getItem('mc-meshclaw-import-dismissed')).toBe('1')
    expect(mockApi.meshclawImportStart).not.toHaveBeenCalled()
  })

  it('treats a network-level start failure as importing (restart drops the socket)', async () => {
    mockApi.meshclawImportStatus.mockResolvedValue({
      available: true,
      sourcePath: '/Users/tester/.meshclaw',
      sizeEstimateBytes: 2048,
      sessionCount: 3,
    })
    // fetch rejects with a TypeError (no HTTP status) when the gateway kills
    // the connection before the response flushes -- the import still runs.
    mockApi.meshclawImportStart.mockRejectedValue(new TypeError('Failed to fetch'))
    renderCard()

    fireEvent.click(await screen.findByRole('button', { name: /import/i }))

    expect(await screen.findByText(/This page will reconnect shortly/i)).toBeTruthy()
    expect(screen.queryByText(/Import failed to start/i)).toBeNull()
    expect(screen.queryByRole('button', { name: /import/i })).toBeNull()
  })

  it('shows the error and re-enables Import on a real HTTP failure (409)', async () => {
    mockApi.meshclawImportStatus.mockResolvedValue({
      available: true,
      sourcePath: '/Users/tester/.meshclaw',
      sizeEstimateBytes: 2048,
      sessionCount: 3,
    })
    // Shape of api/client.ts ApiError: an Error carrying a numeric .status.
    mockApi.meshclawImportStart.mockRejectedValue(
      Object.assign(new Error('import not available'), { status: 409 }),
    )
    renderCard()

    fireEvent.click(await screen.findByRole('button', { name: /import/i }))

    expect(await screen.findByText(/Import failed to start: import not available/i)).toBeTruthy()
    // Card stays up (no restarting overlay) and Import is clickable again.
    expect(screen.queryByText(/This page will reconnect shortly/i)).toBeNull()
    const importBtn = screen.getByRole('button', { name: /import/i }) as HTMLButtonElement
    expect(importBtn.disabled).toBe(false)
    // A11y: the error is announced as an alert...
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain('import not available')
    // ...and focus returns to the Import button (the disabled pending button
    // had ejected focus to <body>).
    await waitFor(() => expect(document.activeElement).toBe(importBtn))
  })

  it('renders nothing while the status query is still in flight', () => {
    mockApi.meshclawImportStatus.mockReturnValue(new Promise(() => {}))
    renderCard()
    expect(screen.queryByText(/found data from a previous MeshClaw install/i)).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('fires the status query once unsuppressed', async () => {
    mockApi.meshclawImportStatus.mockResolvedValue({
      available: true,
      sourcePath: '/Users/tester/.meshclaw',
      sizeEstimateBytes: 2048,
      sessionCount: 3,
    })
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    })
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <MeshclawImportCard suppressed />
      </QueryClientProvider>,
    )
    expect(mockApi.meshclawImportStatus).not.toHaveBeenCalled()

    rerender(
      <QueryClientProvider client={qc}>
        <MeshclawImportCard suppressed={false} />
      </QueryClientProvider>,
    )

    await waitFor(() => expect(mockApi.meshclawImportStatus).toHaveBeenCalled())
    expect(await screen.findByText(/found data from a previous MeshClaw install/i)).toBeTruthy()
  })

  it('Escape transiently closes the card without persisting the skip', async () => {
    mockApi.meshclawImportStatus.mockResolvedValue({
      available: true,
      sourcePath: '/Users/tester/.meshclaw',
      sizeEstimateBytes: 2048,
      sessionCount: 3,
    })
    renderCard()
    await screen.findByText(/found data from a previous MeshClaw install/i)

    fireEvent.keyDown(window, { key: 'Escape' })

    await waitFor(() =>
      expect(screen.queryByText(/found data from a previous MeshClaw install/i)).toBeNull(),
    )
    // Transient close only: the permanent skip flag is reserved for the
    // explicit Skip button, so the offer returns on the next launch.
    expect(localStorage.getItem('mc-meshclaw-import-dismissed')).toBeNull()
  })

  it('ignores Escape that another handler already consumed (defaultPrevented)', async () => {
    mockApi.meshclawImportStatus.mockResolvedValue({
      available: true,
      sourcePath: '/Users/tester/.meshclaw',
      sizeEstimateBytes: 2048,
      sessionCount: 3,
    })
    renderCard()
    await screen.findByText(/found data from a previous MeshClaw install/i)

    // A document-level listener (running before the card's window listener on
    // the bubble path) preventDefaults, as a stacked component would.
    const consume = (e: Event) => e.preventDefault()
    document.addEventListener('keydown', consume)
    try {
      fireEvent.keyDown(document.body, { key: 'Escape' })
    } finally {
      document.removeEventListener('keydown', consume)
    }

    expect(screen.getByText(/found data from a previous MeshClaw install/i)).toBeTruthy()
    expect(localStorage.getItem('mc-meshclaw-import-dismissed')).toBeNull()
  })

  it('does not steal Escape from a dialog stacked above the card', async () => {
    mockApi.meshclawImportStatus.mockResolvedValue({
      available: true,
      sourcePath: '/Users/tester/.meshclaw',
      sizeEstimateBytes: 2048,
      sessionCount: 3,
    })
    renderCard()
    await screen.findByText(/found data from a previous MeshClaw install/i)

    // Simulate a modal (e.g. UpdateModal) mounted after — and therefore
    // stacked above — the card.
    const stacked = document.createElement('div')
    stacked.setAttribute('role', 'dialog')
    stacked.setAttribute('aria-modal', 'true')
    document.body.appendChild(stacked)
    try {
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(screen.getByText(/found data from a previous MeshClaw install/i)).toBeTruthy()
    } finally {
      stacked.remove()
    }

    // With the stacked dialog gone, Escape reaches the card again.
    fireEvent.keyDown(window, { key: 'Escape' })
    await waitFor(() =>
      expect(screen.queryByText(/found data from a previous MeshClaw install/i)).toBeNull(),
    )
    expect(localStorage.getItem('mc-meshclaw-import-dismissed')).toBeNull()
  })

  it('traps Tab focus inside the dialog', async () => {
    mockApi.meshclawImportStatus.mockResolvedValue({
      available: true,
      sourcePath: '/Users/tester/.meshclaw',
      sizeEstimateBytes: 2048,
      sessionCount: 3,
    })
    renderCard()
    await screen.findByText(/found data from a previous MeshClaw install/i)

    const dialog = screen.getByRole('dialog')
    const skipBtn = screen.getByRole('button', { name: /skip/i })
    const importBtn = screen.getByRole('button', { name: /import/i })

    // Initial focus lands on Import (the last focusable); Tab wraps to Skip.
    await waitFor(() => expect(document.activeElement).toBe(importBtn))
    fireEvent.keyDown(dialog, { key: 'Tab' })
    expect(document.activeElement).toBe(skipBtn)
    // Shift-Tab from the first focusable wraps back to the last.
    fireEvent.keyDown(dialog, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(importBtn)
  })

  it('marks background content inert/aria-hidden while the overlay is open', async () => {
    const bystander = document.createElement('div')
    document.body.appendChild(bystander)
    try {
      mockApi.meshclawImportStatus.mockResolvedValue({
        available: true,
        sourcePath: '/Users/tester/.meshclaw',
        sizeEstimateBytes: 2048,
        sessionCount: 3,
      })
      renderCard()
      await screen.findByText(/found data from a previous MeshClaw install/i)

      expect(bystander.getAttribute('aria-hidden')).toBe('true')
      expect(bystander.hasAttribute('inert')).toBe(true)

      // Closing the overlay restores the background.
      fireEvent.click(screen.getByRole('button', { name: /skip/i }))
      await waitFor(() => expect(bystander.hasAttribute('inert')).toBe(false))
      expect(bystander.getAttribute('aria-hidden')).toBeNull()
    } finally {
      bystander.remove()
    }
  })

  it('offers a reload escape hatch when the restart takes too long', async () => {
    mockApi.meshclawImportStatus.mockResolvedValue({
      available: true,
      sourcePath: '/Users/tester/.meshclaw',
      sizeEstimateBytes: 2048,
      sessionCount: 3,
    })
    mockApi.meshclawImportStart.mockResolvedValue({ status: 'restarting' })
    renderCard({ stalledAfterMs: 25 })

    fireEvent.click(await screen.findByRole('button', { name: /import/i }))
    await screen.findByText(/This page will reconnect shortly/i)

    expect(await screen.findByRole('button', { name: /reload/i })).toBeTruthy()
    expect(screen.getByText(/Taking longer than expected/i)).toBeTruthy()
  })
})
