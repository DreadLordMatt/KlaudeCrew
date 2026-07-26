/**
 * Regression tests for MarkdownPanel's `effectiveDiffMode` gating: a
 * truncated, refused (filters_unsafe), incomplete (diff_unavailable), or
 * failed /api/file-diff original must make diff mode UNAVAILABLE — toggle
 * disabled, restored diff mode force-reset — so Monaco never diffs the
 * current content against an empty base (which would render every line as
 * newly added). Reverting the gating in MarkdownPanel.tsx fails these tests.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import MarkdownPanel from '../components/MarkdownPanel'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    fileDiff: vi.fn(),
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    artifact: vi.fn(),
    createArtifact: vi.fn(),
  },
}))

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  </MemoryRouter>
)

function renderPanel(props: { initialDiffMode?: boolean; onDiffModeChange?: (v: boolean) => void } = {}) {
  return render(
    <MarkdownPanel
      filePath="/tmp/project/notes.txt"
      content={'current line one\ncurrent line two\n'}
      onContentChange={() => {}}
      onSave={async () => {}}
      onClose={() => {}}
      embedded
      {...props}
    />,
    { wrapper },
  )
}

/** Both diff toggles (editor toolbar + header bar) share the label. */
async function diffToggles() {
  await waitFor(() => expect(screen.getAllByLabelText('Toggle diff view').length).toBeGreaterThan(0))
  return screen.getAllByLabelText('Toggle diff view')
}

beforeEach(() => {
  queryClient.clear()
  vi.mocked(api.fileDiff).mockReset()
})

describe('MarkdownPanel diff-mode gating (effectiveDiffMode)', () => {
  it('disables the diff toggle when the original is truncated', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({
      diff: '@@ -1 +1 @@\n-a\n+b',
      original: 'partial base…',
      status: 'modified',
      original_truncated: true,
      truncated: true,
    })
    renderPanel()
    for (const btn of await diffToggles()) {
      await waitFor(() => expect(btn).toBeDisabled())
      expect(btn).toHaveAttribute('title', expect.stringContaining('Diff unavailable'))
    }
  })

  it('force-resets a restored diff-mode tab when the original is truncated', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({
      diff: '@@ -1 +1 @@\n-a\n+b',
      original: '',
      status: 'modified',
      original_truncated: true,
    })
    const onDiffModeChange = vi.fn()
    renderPanel({ initialDiffMode: true, onDiffModeChange })
    // The restored diff preference is reset OFF rather than diffing against a
    // base we do not have.
    await waitFor(() => expect(onDiffModeChange).toHaveBeenCalledWith(false))
    for (const btn of await diffToggles()) {
      expect(btn).toBeDisabled()
      expect(btn).toHaveAttribute('aria-pressed', 'false')
    }
  })

  it('disables the toggle with the backend reason on a filters_unsafe refusal', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({
      diff: '',
      original: '',
      status: 'filters_unsafe',
      error: 'this repository has a local info/attributes entry',
    })
    renderPanel()
    for (const btn of await diffToggles()) {
      await waitFor(() => expect(btn).toBeDisabled())
      expect(btn).toHaveAttribute('title', expect.stringContaining('info/attributes'))
    }
  })

  it('disables the toggle on an explicit diff_unavailable result', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({
      diff: '',
      original: 'base',
      status: 'modified',
      diff_unavailable: true,
      error: 'one side could not be read completely',
    })
    renderPanel()
    for (const btn of await diffToggles()) {
      await waitFor(() => expect(btn).toBeDisabled())
      expect(btn).toHaveAttribute('title', expect.stringContaining('could not be read'))
    }
  })

  it('treats a rejected diff query as diff-unavailable (no empty-base diff)', async () => {
    vi.mocked(api.fileDiff).mockRejectedValue(new Error('boom'))
    const onDiffModeChange = vi.fn()
    renderPanel({ initialDiffMode: true, onDiffModeChange })
    // Rendering is gated (toggle disabled, effective mode off) but the STORED
    // preference is NOT cleared — a transient error must not permanently flip
    // a restored tab out of diff mode.
    for (const btn of await diffToggles()) {
      await waitFor(() => expect(btn).toBeDisabled())
      expect(btn).toHaveAttribute('aria-pressed', 'false')
      expect(btn).toHaveAttribute('title', expect.stringContaining('could not be loaded'))
    }
    expect(onDiffModeChange).not.toHaveBeenCalledWith(false)
  })

  it('keeps the stored preference when a BACKGROUND refetch fails on retained data', async () => {
    // The subtle case: react-query keeps the previous successful `diffData`
    // AND reports isError. Gating the persisted reset on "diffData present"
    // alone would fire here and clear the user's preference permanently.
    vi.mocked(api.fileDiff)
      .mockResolvedValueOnce({ diff: '@@ -1 +1 @@\n-a\n+b', original: 'a\n', status: 'modified' })
      .mockRejectedValue(new Error('network blip'))
    const onDiffModeChange = vi.fn()
    const { rerender } = renderPanel({ initialDiffMode: true, onDiffModeChange })
    await waitFor(() => expect(api.fileDiff).toHaveBeenCalled())
    // Force a refetch that fails while the successful data is still cached.
    await queryClient.refetchQueries({ queryKey: ['file-diff', '/tmp/project/notes.txt'] })
    rerender(<div />)
    expect(onDiffModeChange).not.toHaveBeenCalledWith(false)
  })

  it('treats not_git as diff-unavailable (no baseline exists)', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({ diff: '', original: '', status: 'not_git' })
    renderPanel()
    for (const btn of await diffToggles()) {
      await waitFor(() => expect(btn).toBeDisabled())
    }
  })

  it('treats a binary baseline as diff-unavailable', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({
      diff: 'Binary files a/blob.bin and b/blob.bin differ',
      original: '',
      status: 'modified',
      diff_unavailable: true,
      error: 'The committed version of this file is binary',
    })
    renderPanel()
    for (const btn of await diffToggles()) {
      await waitFor(() => expect(btn).toBeDisabled())
      expect(btn).toHaveAttribute('title', expect.stringContaining('binary'))
    }
  })

  it('keeps the toggle enabled for a complete modified diff (gate does not over-block)', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({
      diff: '@@ -1 +1 @@\n-a\n+b',
      original: 'a\n',
      status: 'modified',
    })
    renderPanel({ initialDiffMode: false })
    for (const btn of await diffToggles()) {
      await waitFor(() => expect(btn).not.toBeDisabled())
      expect(btn).toHaveAttribute('title', 'Toggle diff view')
    }
  })
})
