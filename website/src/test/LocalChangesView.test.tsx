import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({
  gitChanges: vi.fn(),
  fileDiff: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import LocalChangesView, { FileNameWithPath } from '../components/LocalChangesView'

function renderView(props: { projectDir?: string; onFileOpen?: (path: string) => void } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <LocalChangesView {...props} />
    </QueryClientProvider>,
  )
}

const repoResponse = (overrides: Record<string, unknown> = {}, fileOverrides: Record<string, unknown>[] | null = null) => ({
  dir: '/work/project',
  repo: {
    root: '/work/project',
    name: 'project',
    branch: 'main',
    files: fileOverrides ?? [
      { path: '/work/project/src/app.ts', rel: 'src/app.ts', status: 'modified', staged: false, additions: 3, deletions: 1 },
      { path: '/work/project/new.txt', rel: 'new.txt', status: 'untracked', staged: false },
    ],
    ...overrides,
  },
})

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.fileDiff.mockResolvedValue({ diff: '', original: '', status: 'clean' })
})

describe('LocalChangesView', () => {
  it('prompts for a project directory when none is set', () => {
    renderView({})
    expect(screen.getByText(/Pick a project directory/)).toBeInTheDocument()
    expect(mockApi.gitChanges).not.toHaveBeenCalled()
  })

  it('shows the no-repo state when the endpoint returns repo: null', async () => {
    mockApi.gitChanges.mockResolvedValue({ dir: '/work/notrepo', repo: null })
    renderView({ projectDir: '/work/notrepo' })
    await waitFor(() => expect(screen.getByText(/No git repository at this project directory/)).toBeInTheDocument())
  })

  it('shows a clean-tree message naming the branch', async () => {
    mockApi.gitChanges.mockResolvedValue(repoResponse({}, []))
    renderView({ projectDir: '/work/project' })
    await waitFor(() => expect(screen.getByText(/Working tree clean — no local changes on main/)).toBeInTheDocument())
  })

  it('renders repo header with branch, count, and file rows with status badges', async () => {
    mockApi.gitChanges.mockResolvedValue(repoResponse())
    renderView({ projectDir: '/work/project' })
    await waitFor(() => expect(screen.getByText('project')).toBeInTheDocument())
    expect(screen.getByText('main')).toBeInTheDocument()
    expect(screen.getByText(/2 files/)).toBeInTheDocument()
    expect(screen.getByText('app.ts')).toBeInTheDocument()
    expect(screen.getByText('src')).toBeInTheDocument()
    expect(screen.getByText('new.txt')).toBeInTheDocument()
    // +/- counts for the tracked change only.
    expect(screen.getByText('+3')).toBeInTheDocument()
    expect(screen.getByText('-1')).toBeInTheDocument()
    // Letter badges with accessible full-word labels.
    expect(screen.getByLabelText('modified')).toHaveTextContent('M')
    expect(screen.getByLabelText('untracked')).toHaveTextContent('U')
  })

  it('expands a row to fetch and render the lexical diff lazily', async () => {
    mockApi.gitChanges.mockResolvedValue(repoResponse())
    mockApi.fileDiff.mockResolvedValue({
      diff: '@@ -1 +1,2 @@\n-alphaline\n+betaline\n+gammaline',
      original: 'alphaline',
      status: 'modified',
    })
    renderView({ projectDir: '/work/project' })
    await waitFor(() => expect(screen.getByText('app.ts')).toBeInTheDocument())
    expect(mockApi.fileDiff).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('app.ts'))
    await waitFor(() => expect(mockApi.fileDiff).toHaveBeenCalledWith('/work/project/src/app.ts', { lexical: true }))
    await waitFor(() => expect(screen.getByText('betaline')).toBeInTheDocument())
  })

  it('offers the open-in-editor action except for deleted/dir/symlink/hardlink rows', async () => {
    mockApi.gitChanges.mockResolvedValue(repoResponse({}, [
      { path: '/work/project/ok.ts', rel: 'ok.ts', status: 'modified', staged: false },
      { path: '/work/project/gone.ts', rel: 'gone.ts', status: 'deleted', staged: false },
      { path: '/work/project/sub', rel: 'sub', status: 'modified', staged: false, kind: 'dir' },
      { path: '/work/project/link', rel: 'link', status: 'modified', staged: false, kind: 'symlink' },
      { path: '/work/project/hard.ts', rel: 'hard.ts', status: 'modified', staged: false, kind: 'hardlink' },
    ]))
    const onFileOpen = vi.fn()
    renderView({ projectDir: '/work/project', onFileOpen })
    await waitFor(() => expect(screen.getByText('ok.ts')).toBeInTheDocument())
    expect(screen.getByLabelText('Open ok.ts in editor')).toBeInTheDocument()
    expect(screen.queryByLabelText('Open gone.ts in editor')).toBeNull()
    expect(screen.queryByLabelText('Open sub in editor')).toBeNull()
    expect(screen.queryByLabelText('Open link in editor')).toBeNull()
    // A multi-link inode: /api/file-diff refuses its content, so the editor
    // action could only fail.
    expect(screen.queryByLabelText('Open hard.ts in editor')).toBeNull()
    fireEvent.click(screen.getByLabelText('Open ok.ts in editor'))
    expect(onFileOpen).toHaveBeenCalledWith('/work/project/ok.ts')
    // The open action must not also toggle the row's inline diff.
    expect(mockApi.fileDiff).not.toHaveBeenCalled()
  })

  it('shows the partial banner when the scan is truncated', async () => {
    mockApi.gitChanges.mockResolvedValue({ ...repoResponse({ truncated: true }), truncated: true })
    renderView({ projectDir: '/work/project' })
    await waitFor(() => expect(screen.getByText(/Partial scan/)).toBeInTheDocument())
    expect(screen.getByText('partial')).toBeInTheDocument()
    expect(screen.getByText(/2\+ files/)).toBeInTheDocument()
  })

  it('explains a filters_unsafe refusal instead of showing an empty tree', async () => {
    mockApi.gitChanges.mockResolvedValue({
      dir: '/work/project',
      repo: null,
      filters_unsafe: 'this repository has a local info/attributes entry',
    })
    renderView({ projectDir: '/work/project' })
    await waitFor(() => expect(screen.getByText(/skipped for safety: this repository has a local info\/attributes entry/)).toBeInTheDocument())
    expect(screen.queryByText(/No git repository/)).toBeNull()
  })

  it('surfaces a per-file safety refusal on expand', async () => {
    mockApi.gitChanges.mockResolvedValue(repoResponse())
    mockApi.fileDiff.mockResolvedValue({ diff: '', original: '', status: 'filters_unsafe', error: 'refused reason' })
    renderView({ projectDir: '/work/project' })
    await waitFor(() => expect(screen.getByText('app.ts')).toBeInTheDocument())
    fireEvent.click(screen.getByText('app.ts'))
    await waitFor(() => expect(screen.getByText(/Diff not shown for safety: refused reason/)).toBeInTheDocument())
  })

  it('surfaces diff_unavailable with the backend reason', async () => {
    mockApi.gitChanges.mockResolvedValue(repoResponse())
    mockApi.fileDiff.mockResolvedValue({ diff: '', original: 'x', status: 'modified', diff_unavailable: true, error: 'one side could not be read' })
    renderView({ projectDir: '/work/project' })
    await waitFor(() => expect(screen.getByText('app.ts')).toBeInTheDocument())
    fireEvent.click(screen.getByText('app.ts'))
    await waitFor(() => expect(screen.getByText(/one side could not be read/)).toBeInTheDocument())
  })

  it('offers retry when the diff fetch fails', async () => {
    mockApi.gitChanges.mockResolvedValue(repoResponse())
    mockApi.fileDiff.mockRejectedValueOnce(new Error('boom')).mockResolvedValue({ diff: '@@ -1 +1 @@\n-a\n+b', original: 'a', status: 'modified' })
    renderView({ projectDir: '/work/project' })
    await waitFor(() => expect(screen.getByText('app.ts')).toBeInTheDocument())
    fireEvent.click(screen.getByText('app.ts'))
    await waitFor(() => expect(screen.getByText(/Couldn't load the diff/)).toBeInTheDocument())
    fireEvent.click(screen.getByText('Retry'))
    await waitFor(() => expect(screen.getByText('b')).toBeInTheDocument())
  })

  it('notes a truncated diff above the patch', async () => {
    mockApi.gitChanges.mockResolvedValue(repoResponse())
    mockApi.fileDiff.mockResolvedValue({ diff: '@@ -1 +1 @@\n-a\n+b', original: 'a', status: 'modified', diff_truncated: true })
    renderView({ projectDir: '/work/project' })
    await waitFor(() => expect(screen.getByText('app.ts')).toBeInTheDocument())
    fireEvent.click(screen.getByText('app.ts'))
    await waitFor(() => expect(screen.getByText(/Diff truncated/)).toBeInTheDocument())
  })

  it('renders a binary marker patch even when diff_unavailable is set', async () => {
    // Regression: `diff_unavailable` means there is no two-way BASELINE, not
    // that there is nothing to show. git's own marker patch carries the story.
    mockApi.gitChanges.mockResolvedValue(repoResponse())
    mockApi.fileDiff.mockResolvedValue({
      diff: 'Binary files a/blob.bin and b/blob.bin differ',
      original: '',
      status: 'modified',
      diff_unavailable: true,
      error: 'The committed version of this file is binary',
    })
    renderView({ projectDir: '/work/project' })
    await waitFor(() => expect(screen.getByText('app.ts')).toBeInTheDocument())
    fireEvent.click(screen.getByText('app.ts'))
    await waitFor(() => expect(screen.getByText(/Binary files .* differ/)).toBeInTheDocument())
  })

  it('shows the diff_unavailable notice only when there is no patch', async () => {
    mockApi.gitChanges.mockResolvedValue(repoResponse())
    mockApi.fileDiff.mockResolvedValue({
      diff: '',
      original: 'x',
      status: 'modified',
      diff_unavailable: true,
      error: 'one side could not be read',
    })
    renderView({ projectDir: '/work/project' })
    await waitFor(() => expect(screen.getByText('app.ts')).toBeInTheDocument())
    fireEvent.click(screen.getByText('app.ts'))
    await waitFor(() => expect(screen.getByText(/one side could not be read/)).toBeInTheDocument())
  })

  it('shows an error state when the scan request itself fails', async () => {
    mockApi.gitChanges.mockRejectedValue(new Error('nope'))
    renderView({ projectDir: '/work/project' })
    await waitFor(() => expect(screen.getByText(/Could not scan \/work\/project for changes/)).toBeInTheDocument())
  })
})

describe('FileNameWithPath', () => {
  it('splits the name from its de-emphasized directory', () => {
    render(<FileNameWithPath rel="src/components/Deep.tsx" />)
    expect(screen.getByText('Deep.tsx')).toBeInTheDocument()
    expect(screen.getByText('src/components')).toBeInTheDocument()
  })

  it('renders a bare name without a directory span', () => {
    render(<FileNameWithPath rel="README.md" />)
    expect(screen.getByText('README.md')).toBeInTheDocument()
  })
})
