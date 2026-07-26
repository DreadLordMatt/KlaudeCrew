import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, GitBranch, Pen } from 'lucide-react'
import { api } from '../api/client'
import DiffView from './UnifiedDiffView'
import { colorForExt, fileIcon } from '../utils/fileIcons'

/** Poll cadence for the local worktree scan while the view is visible. Cheap
 *  on the backend (bounded `git status` on one repo) but not free — lazy. */
const LOCAL_CHANGES_POLL_MS = 10_000

type GitChangeFile = {
  path: string
  rel: string
  status: string
  staged: boolean
  additions?: number
  deletions?: number
  kind?: string
}
/** Bare file name followed by its de-emphasized directory path (VS Code
 *  style: "Deep.tsx  src/components"). The dir shrinks/truncates first so the
 *  name stays readable in narrow panels. */
export function FileNameWithPath({ rel, title }: { rel: string; title?: string }) {
  const slash = rel.lastIndexOf('/')
  const dir = slash === -1 ? '' : rel.slice(0, slash)
  const name = slash === -1 ? rel : rel.slice(slash + 1)
  return (
    <span className="flex items-baseline gap-1.5 min-w-0 text-[13px]" title={title ?? rel}>
      <span className="text-text truncate max-w-full">{name}</span>
      {dir && <span className="text-muted/70 text-[11px] truncate shrink min-w-0">{dir}</span>}
    </span>
  )
}

/** Compact per-status letter badge (VS Code style): letter + color instead of
 *  the long porcelain word. The full word stays on hover (title) and for
 *  screen readers (aria-label). `!` marks conflicts — `C` is taken by copied. */
const STATUS_BADGE: Record<string, { letter: string; tone: string }> = {
  modified: { letter: 'M', tone: 'text-accent' },
  added: { letter: 'A', tone: 'text-ok' },
  untracked: { letter: 'U', tone: 'text-ok' },
  deleted: { letter: 'D', tone: 'text-danger' },
  renamed: { letter: 'R', tone: 'text-accent' },
  copied: { letter: 'C', tone: 'text-accent' },
  conflicted: { letter: '!', tone: 'text-danger' },
}

/** Working-tree changes for the git repo at the chat's project directory —
 *  the body of the Changes panel's ever-present Local tab. Single-repo by
 *  design: GET /api/git-changes resolves the repo containing the project dir
 *  (no child-directory sweep); per-file diffs are fetched lazily on expand
 *  through /api/file-diff and rendered with the same DiffView the PR file
 *  rows use. Each row carries hover-revealed actions: a chevron reflecting
 *  the inline-diff state and an open-in-editor button that opens the file as
 *  a native document tab; clicking anywhere on the row toggles the diff. */
export default function LocalChangesView({ projectDir, onFileOpen }: {
  projectDir?: string
  /** Open a path as a native file document tab (threaded from ChatPage). */
  onFileOpen?: (path: string) => void
}) {
  const query = useQuery({
    queryKey: ['git-changes', projectDir],
    queryFn: () => api.gitChanges(projectDir!),
    enabled: !!projectDir,
    refetchInterval: LOCAL_CHANGES_POLL_MS,
    refetchOnWindowFocus: false,
  })

  if (!projectDir) {
    return <LocalChangesEmpty>Pick a project directory for this chat to see its uncommitted git changes.</LocalChangesEmpty>
  }
  if (query.isLoading) {
    return <LocalChangesEmpty>Checking for git changes…</LocalChangesEmpty>
  }
  // Only surface the error state when there is NO data to show. React Query
  // retains the last successful payload across a failed background poll, and
  // replacing a valid file list with an error every 10s whenever the network
  // blips would destroy the view the user is reading.
  if (query.isError && !query.data) {
    return <LocalChangesEmpty>{`Could not scan ${projectDir} for changes.`}</LocalChangesEmpty>
  }
  const repo = query.data?.repo ?? null
  // The scan could not complete (budget expiry, unreadable repo, dropped
  // rows): clean/empty must not read as authoritative.
  const incomplete = query.data?.truncated === true || repo?.truncated === true
  // The repo was refused because its attributes cannot be isolated — the
  // endpoint will not read content from it. Explains the gap the partial
  // banner would otherwise leave unexplained.
  const filtersUnsafe = query.data?.filters_unsafe

  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex-1 min-h-0 overflow-y-auto scrollbar-overlay">
        {incomplete && (
          <div className="px-4 py-1.5 text-[11px] text-warn border-b border-border">
            Partial scan — the file or time budget was reached; some changes may not be listed.
          </div>
        )}
        {filtersUnsafe && (
          <div className="px-4 py-1.5 text-[11px] text-warn border-b border-border">
            {`This repository was skipped for safety: ${filtersUnsafe}.`}
          </div>
        )}
        {!repo && !filtersUnsafe && !incomplete && (
          <LocalChangesEmpty>No git repository at this project directory. Point the chat's project directory at a git repo (or any folder inside one) to see its changes.</LocalChangesEmpty>
        )}
        {repo && repo.files.length === 0 && !incomplete && (
          <LocalChangesEmpty>
            {`Working tree clean — no local changes${repo.branch ? ` on ${repo.branch}` : ''}.`}
          </LocalChangesEmpty>
        )}
        {repo && repo.files.length > 0 && (
          <div>
            {/* Repo header row: an inset pill naming the repo, its branch and
                the retained file count. Single repo — no collapse state. */}
            <div className="mx-1.5 mt-1.5 flex items-center gap-2 px-2.5 py-2 rounded-md">
              <span className="text-[13px] font-semibold text-muted-strong truncate">{repo.name}</span>
              {repo.branch && (
                <span className="flex items-center gap-1 text-[11px] text-muted shrink-0 min-w-0">
                  <GitBranch size={11} className="shrink-0" />
                  <span className="truncate max-w-[140px]">{repo.branch}</span>
                </span>
              )}
              <span className="ml-auto text-[11px] text-muted shrink-0">
                {repo.truncated && (
                  <span
                    className="text-warn mr-1.5"
                    title="The scan hit its file or output limit — more changes exist than are listed here."
                  >
                    partial
                  </span>
                )}
                {repo.files.length}{repo.truncated ? '+' : ''} {repo.files.length === 1 && !repo.truncated ? 'file' : 'files'}
              </span>
            </div>
            {repo.files.map(file => <LocalChangeRow key={file.path} file={file} onFileOpen={onFileOpen} />)}
          </div>
        )}
      </div>
    </div>
  )
}

function LocalChangesEmpty({ children }: { children: string }) {
  return <div className="px-4 py-6 text-[12px] text-muted text-center">{children}</div>
}

function LocalChangeRow({ file, onFileOpen }: { file: GitChangeFile; onFileOpen?: (path: string) => void }) {
  const [open, setOpen] = useState(false)
  // Same file-type icon map the Files tab uses (utils/fileIcons).
  const Icon = fileIcon(file.path)
  const iconColor = colorForExt(file.path)
  // Lazy: the diff is only fetched when the row is expanded. The queryKey
  // carries a change FINGERPRINT (status/staged/counts from the 10s list
  // poll) so a changed file gets a fresh cache entry, and refetchInterval
  // covers what the fingerprint can't see: an edit that leaves ±counts
  // identical, or an untracked file (no counts at all). Deleted files fetch
  // too — the endpoint serves their deletion patch from HEAD.
  const diffQuery = useQuery({
    queryKey: ['file-diff', file.path, file.status, file.staged, file.additions, file.deletions],
    // lexical: this view's paths identify the changed ENTRY (a modified
    // symlink must diff as the link, not its target) — other fileDiff
    // consumers keep the endpoint's default canonical-path behavior.
    queryFn: () => api.fileDiff(file.path, { lexical: true }),
    enabled: open,
    staleTime: LOCAL_CHANGES_POLL_MS,
    refetchInterval: open ? LOCAL_CHANGES_POLL_MS : false,
    refetchOnWindowFocus: false,
  })
  return (
    <div>
      {/* The row is a div-with-button-role (not a <button>) so the
          open-in-editor action can be its own nested real button: it opens the
          file as a native document tab, everything else toggles the inline
          diff. Keyboard: Enter/Space on the row toggles; the guard on e.target
          keeps the nested button's own activation from double-firing. */}
      {/* The row looks IDENTICAL open or closed (inset rounded hover pill).
          Stickiness comes from this wrapper: full-width and opaque (bg-bg) so
          diff text can't scroll through the row or its side gutters while
          pinned. The wrapper's containing block is the row+diff div, so the
          row un-sticks exactly when its own diff scrolls past. -top-px hides
          the sub-pixel hairline that peeks above a pinned sticky element. */}
      <div className={open ? 'sticky -top-px z-10 bg-bg' : undefined}>
        <div
          role="button"
          tabIndex={0}
          aria-expanded={open}
          onClick={() => setOpen(value => !value)}
          onKeyDown={e => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); setOpen(value => !value) } }}
          className="group flex items-center gap-2 px-3 py-2 mx-1.5 rounded-md text-left cursor-pointer hover:bg-bg-elevated transition-colors"
        >
          <Icon size={13} className={`${iconColor} shrink-0`} />
          {/* Name + path are plain text; the two ACTION icons sit right after
              them: a chevron reflecting the inline-diff state (row click
              toggles) and an explicit open-in-editor button. */}
          <FileNameWithPath rel={file.rel} title={file.path} />
          {/* Action icons reveal on row hover, or on KEYBOARD focus only
              (focus-visible): plain focus-within kept them lit after a mouse
              click expanded/collapsed the row, since the click leaves DOM
              focus on it. The chevron stays visible while expanded so open
              state remains legible. */}
          {open
            ? <ChevronDown size={13} className="shrink-0 text-muted" />
            : <ChevronRight size={13} className="shrink-0 text-muted opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100 transition-opacity" />}
          {/* No open-in-editor for deleted files (the path no longer exists),
              directory entries (modified submodules/gitlinks — file-read would
              reject the directory), symlinks (the editor would open the
              TARGET), or hard links (the endpoint refuses multi-link inodes,
              so the editor could only fail); the row's status is the useful
              signal for those. */}
          {onFileOpen && file.status !== 'deleted' && file.kind !== 'dir' && file.kind !== 'symlink' && file.kind !== 'hardlink' && (
            <button
              type="button"
              onClick={e => { e.stopPropagation(); onFileOpen(file.path) }}
              className="shrink-0 flex items-center justify-center w-[18px] h-[18px] rounded bg-transparent border-none p-0 cursor-pointer text-muted hover:text-accent opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100 focus-visible:opacity-100 transition-opacity"
              title={`Open ${file.rel} in editor`}
              aria-label={`Open ${file.rel} in editor`}
            >
              <Pen size={12} />
            </button>
          )}
          <span className="flex-1 min-w-0" />
          {/* `staged` is intentionally not surfaced — no staging/commit
              actions exist in this view; counts + status letter carry the
              signal. */}
          {(file.additions !== undefined || file.deletions !== undefined) && (
            <span className="text-[11px] font-mono shrink-0">
              <span className="text-ok">+{file.additions ?? 0}</span> <span className="text-danger">-{file.deletions ?? 0}</span>
            </span>
          )}
          <span
            title={file.status}
            aria-label={file.status}
            className={`text-[11px] font-mono font-bold w-3 text-center shrink-0 ${STATUS_BADGE[file.status]?.tone || 'text-muted'}`}
          >
            {STATUS_BADGE[file.status]?.letter || '?'}
          </span>
        </div>
      </div>
      {open && (
        // Frame the diff with dividers on BOTH sides so the row above and the
        // next file's row below both stand apart from the diff body.
        <div className="overflow-x-auto border-t border-b border-border">
          {diffQuery.isLoading ? (
            <div className="px-3 py-3 text-[11px] text-muted">Loading diff…</div>
          ) : diffQuery.isError && !diffQuery.data ? (
            // A failed request is NOT "no diff exists" — say so and offer retry.
            <div className="px-3 py-3 text-[12px] text-muted flex items-center gap-2">
              <span>Couldn't load the diff.</span>
              <button
                type="button"
                onClick={() => diffQuery.refetch()}
                className="text-[11px] px-2 py-0.5 rounded-md border border-border bg-transparent text-muted hover:text-text hover:bg-bg-hover cursor-pointer"
              >
                Retry
              </button>
            </div>
          ) : diffQuery.data?.status === 'filters_unsafe' ? (
            // A 200 that REFUSED to read content. Falling through to "No diff
            // available" would present a security refusal as an empty diff —
            // and this can happen after the list scan, when attributes change
            // between the scan and the expand.
            <div className="px-3 py-3 text-[12px] text-warn">
              {`Diff not shown for safety: ${diffQuery.data.error || 'this repository was refused a safe read'}.`}
            </div>
          ) : diffQuery.data?.diff ? (
            // A NON-EMPTY patch renders even when `diff_unavailable` is set.
            // That flag means there is no usable two-way BASELINE (binary or
            // unreadable original) — which is exactly the case where git's own
            // marker patch ("Binary files ... differ", "old mode ...") carries
            // the whole story. Testing the flag first hid those patches behind
            // a warning; the flag still gates two-pane consumers (MarkdownPanel),
            // which is where an absent baseline actually matters.
            <>
              {diffQuery.data.diff_truncated && (
                <div className="px-3 py-1.5 text-[11px] text-warn border-b border-border">
                  Diff truncated — the file's content exceeds the 2MB response limit; showing the beginning only.
                </div>
              )}
              <DiffView patch={diffQuery.data.diff} path={file.path} />
            </>
          ) : diffQuery.data?.diff_unavailable ? (
            <div className="px-3 py-3 text-[12px] text-warn">
              {diffQuery.data.error || 'Diff unavailable because the file could not be read completely.'}
            </div>
          ) : (
            <div className="px-3 py-3 text-[12px] text-muted">No diff available for this file.</div>
          )}
        </div>
      )}
    </div>
  )
}
