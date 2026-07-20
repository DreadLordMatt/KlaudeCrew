import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobLogsView from '../components/JobLogsView'

// Regression: a failed Cancel on a running job sets panelError in SchedulePage,
// but on the Logs tab that error had no DOM anchor (panelError only rendered in
// the Details branch), so the failure was silent — the run kept showing
// "Currently running…" with no feedback. JobLogsView now takes a cancelError
// prop and renders it under the running banner.

vi.mock('../api/client', () => ({
  api: {
    // No history rows — keeps the view simple; the banner + cancelError are
    // what we assert.
    cronHistory: vi.fn().mockResolvedValue({ runs: [], total: 0 }),
  },
}))

describe('JobLogsView cancel error rendering', () => {
  beforeEach(() => vi.clearAllMocks())

  it('renders the cancelError text on the Logs view (was silent before)', async () => {
    renderWithProviders(
      <JobLogsView
        jobId="job-1"
        isRunning
        runningSince={Math.floor(Date.now() / 1000)}
        onCancel={() => {}}
        cancelError="Cancel failed: 503 backend unavailable"
      />,
    )
    // The running banner is shown...
    expect(await screen.findByText('Currently running…')).toBeInTheDocument()
    // ...and the cancel error is now visible (pre-fix: absent).
    expect(
      screen.getByText('Cancel failed: 503 backend unavailable'),
    ).toBeInTheDocument()
  })

  it('does not render an error node when cancelError is null', async () => {
    renderWithProviders(
      <JobLogsView
        jobId="job-1"
        isRunning
        runningSince={Math.floor(Date.now() / 1000)}
        onCancel={() => {}}
        cancelError={null}
      />,
    )
    expect(await screen.findByText('Currently running…')).toBeInTheDocument()
    expect(screen.queryByText(/Cancel failed/)).not.toBeInTheDocument()
  })
})
