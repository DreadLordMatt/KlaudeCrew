import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { SkillSourcesSection } from './SkillSourcesSection'
import type { SkillSource } from '../../types'

vi.mock('../../api/client', () => ({
  api: {
    skillSources: vi.fn(),
    addSkillSource: vi.fn(),
    syncSkillSource: vi.fn(),
    deleteSkillSource: vi.fn(),
  },
}))

const { api } = await import('../../api/client')

function source(over: Partial<SkillSource> = {}): SkillSource {
  return {
    name: 'team-skills',
    repo: 'https://github.com/org/team-skills.git',
    branch: 'main',
    subdir: 'skills',
    enabled: true,
    cloned: true,
    head: 'abcdef1234567890abcdef1234567890abcdef12',
    skill_count: 3,
    synced_at: 1_700_000_000,
    last_success_at: 1_700_000_000,
    last_ok: true,
    last_error: '',
    ...over,
  }
}

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <SkillSourcesSection />
    </QueryClientProvider>,
  )
}

describe('SkillSourcesSection', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.skillSources).mockResolvedValue({ sources: [] })
  })

  it('shows the empty state when no repos are linked', async () => {
    renderSection()
    expect(await screen.findByText(/No repos linked yet/i)).toBeInTheDocument()
  })

  it('lists a linked repo with its skill count and short SHA', async () => {
    vi.mocked(api.skillSources).mockResolvedValue({ sources: [source()] })
    renderSection()
    // The repo URL also contains "team-skills", so match the name node exactly.
    expect(
      await screen.findByText((_t, el) => el?.textContent === 'team-skills · 3 skills'),
    ).toBeInTheDocument()
    // Short SHA, not the full 40 chars.
    expect(screen.getByText(/abcdef1 *$/)).toBeInTheDocument()
  })

  it('states that a failed sync is still serving the previous commit', async () => {
    vi.mocked(api.skillSources).mockResolvedValue({
      sources: [source({ last_ok: false, last_error: 'fetch_failed' })],
    })
    renderSection()
    expect(await screen.findByText(/Last sync failed/i)).toBeInTheDocument()
    expect(screen.getByText(/still serving the previous commit/i)).toBeInTheDocument()
  })

  it('keeps Link repo disabled until both name and repo are filled', async () => {
    const user = userEvent.setup()
    renderSection()
    const button = await screen.findByRole('button', { name: /Link repo/i })
    expect(button).toBeDisabled()
    await user.type(screen.getByLabelText(/Repository URL/i), 'https://github.com/o/r.git')
    expect(button).toBeDisabled()
    await user.type(screen.getByLabelText(/^Name$/i), 'team-skills')
    expect(button).toBeEnabled()
  })

  it('submits the add form and reports the resulting skill count', async () => {
    const user = userEvent.setup()
    vi.mocked(api.addSkillSource).mockResolvedValue({ ok: true, source: source() })
    renderSection()
    await user.type(screen.getByLabelText(/Repository URL/i), 'https://github.com/org/team-skills.git')
    await user.type(screen.getByLabelText(/^Name$/i), 'team-skills')
    await user.type(screen.getByLabelText(/Subdirectory/i), 'skills')
    await user.click(screen.getByRole('button', { name: /Link repo/i }))
    await waitFor(() =>
      expect(api.addSkillSource).toHaveBeenCalledWith({
        name: 'team-skills',
        repo: 'https://github.com/org/team-skills.git',
        branch: 'main',
        subdir: 'skills',
      }),
    )
    expect(await screen.findByText(/3 skill\(s\) available/i)).toBeInTheDocument()
  })

  it('surfaces an add failure instead of clearing the form', async () => {
    const user = userEvent.setup()
    vi.mocked(api.addSkillSource).mockRejectedValue(new Error('git clone failed (exit 128)'))
    renderSection()
    const repoInput = screen.getByLabelText(/Repository URL/i)
    await user.type(repoInput, 'https://github.com/org/nope.git')
    await user.type(screen.getByLabelText(/^Name$/i), 'team-skills')
    await user.click(screen.getByRole('button', { name: /Link repo/i }))
    expect(await screen.findByText(/git clone failed/i)).toBeInTheDocument()
    // The URL is still there so the user can correct it.
    expect(repoInput).toHaveValue('https://github.com/org/nope.git')
  })

  it('reports a failed sync as an error, not a success', async () => {
    const user = userEvent.setup()
    vi.mocked(api.skillSources).mockResolvedValue({ sources: [source()] })
    vi.mocked(api.syncSkillSource).mockResolvedValue({
      ok: false,
      message: 'git fetch failed (exit 128)',
      error: 'fetch_failed',
      result: { message: 'git fetch failed (exit 128)' },
      source: source(),
    })
    renderSection()
    await user.click(await screen.findByRole('button', { name: /^Sync$/i }))
    expect(await screen.findByText(/git fetch failed/i)).toBeInTheDocument()
  })

  it('refetches the rows even when the sync request itself rejects', async () => {
    // Otherwise the row keeps claiming the previous outcome after a failure.
    const user = userEvent.setup()
    vi.mocked(api.skillSources).mockResolvedValue({ sources: [source()] })
    vi.mocked(api.syncSkillSource).mockRejectedValue(new Error('network down'))
    renderSection()
    await waitFor(() => expect(api.skillSources).toHaveBeenCalledTimes(1))
    await user.click(await screen.findByRole('button', { name: /^Sync$/i }))
    expect(await screen.findByText(/network down/i)).toBeInTheDocument()
    await waitFor(() => expect(vi.mocked(api.skillSources).mock.calls.length).toBeGreaterThan(1))
  })

  it('requires a second click to unlink', async () => {
    const user = userEvent.setup()
    vi.mocked(api.skillSources).mockResolvedValue({ sources: [source()] })
    vi.mocked(api.deleteSkillSource).mockResolvedValue({ ok: true, mirror_removed: true })
    renderSection()
    await user.click(await screen.findByRole('button', { name: /Unlink/i }))
    expect(api.deleteSkillSource).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: /Confirm/i }))
    await waitFor(() => expect(api.deleteSkillSource).toHaveBeenCalledWith('team-skills'))
  })

  it('renders without crashing when the api method is missing from a partial mock', async () => {
    // Mirrors the real hazard: suites that mock ../../api/client partially leave
    // newly added methods undefined, and this query runs on mount.
    vi.mocked(api.skillSources).mockImplementation(
      undefined as unknown as typeof api.skillSources,
    )
    renderSection()
    expect(await screen.findByText(/No repos linked yet/i)).toBeInTheDocument()
  })
})
