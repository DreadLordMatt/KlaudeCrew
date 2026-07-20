import { useState, useCallback, useMemo } from 'react'
import type { Artifact } from '../types'

/** Singleton "view" tabs (opened from the + menu, one instance each). */
export type ViewKind = 'files' | 'artifacts' | 'subagents' | 'workflows' | 'logs' | 'side' | 'terminal'
/** All tab kinds: singleton views + on-demand document tabs. */
export type TabKind = ViewKind | 'file' | 'diff' | 'artifact'

export interface PanelTab {
  id: string
  kind: TabKind
  title: string
  /** Origin chat slot — comment submission routes to the session the tab was
   *  opened from, not whatever session is active later. */
  slot?: string | null
  // ── document fields ──
  path?: string
  content?: string
  original?: string
  modified?: string
  artifactSlug?: string
  artifactKind?: Artifact['kind']
}

const VIEW_TITLES: Record<ViewKind, string> = {
  files: 'Files', artifacts: 'Artifacts', subagents: 'Subagents', workflows: 'Workflows',
  logs: 'Logs', side: 'Side', terminal: 'Terminal',
}

const basename = (p: string) => p.split('/').pop() || p

type Bucket = { tabs: PanelTab[]; activeId: string | null }
/** Module-level so an empty strip yields STABLE tabs/activeId identities
 *  (a per-render fallback object would churn the hook's memoized return). */
const EMPTY_BUCKET: Bucket = { tabs: [], activeId: null }

/**
 * Tabbed side panel state. Replaces the old mutually-exclusive
 * usePanelState + useDiffPanel + activityTab model: every view (category views,
 * terminal) and every opened document (file / diff / artifact) is a tab in one
 * strip. Opening a document that's already open focuses its tab instead of
 * duplicating it. Content is held here rather than redux to keep large file
 * bodies out of the store.
 *
 * State is bucketed PER CHAT SLOT (`slotKey`): each chat has its own strip
 * (tabs, order, focused tab), and switching chats swaps the whole strip —
 * switching back restores it exactly. Tabs opened with no active slot live in
 * a shared fallback bucket. Buckets for deleted slots linger until reload
 * (bounded by session count; large file bodies are the only real weight).
 */
export function usePanelTabs(slotKey: string | null = null) {
  const key = slotKey ?? '__no_slot__'
  const [bySlot, setBySlot] = useState<Record<string, Bucket>>({})
  const { tabs, activeId } = bySlot[key] ?? EMPTY_BUCKET

  /** Apply a bucket transform to the CURRENT slot's strip. */
  const update = useCallback((fn: (b: Bucket) => Bucket) => {
    setBySlot(prev => ({ ...prev, [key]: fn(prev[key] ?? { tabs: [], activeId: null }) }))
  }, [key])

  /** Add tab if its id is absent, otherwise merge patch into the existing tab;
   *  either way focus it. When `replaceId` is given (e.g. a file opened FROM
   *  the Files tab replaces that Files tab), the new tab takes the replaced
   *  tab's strip position; if the new tab already exists elsewhere, the
   *  replaced tab is simply closed. */
  const upsert = useCallback((tab: PanelTab, replaceId?: string) => {
    update(b => {
      const i = b.tabs.findIndex(t => t.id === tab.id)
      if (i !== -1) {
        const next = b.tabs.slice()
        next[i] = { ...next[i], ...tab }
        return { tabs: replaceId && replaceId !== tab.id ? next.filter(t => t.id !== replaceId) : next, activeId: tab.id }
      }
      if (replaceId) {
        const r = b.tabs.findIndex(t => t.id === replaceId)
        if (r !== -1) {
          const next = b.tabs.slice()
          next[r] = tab
          return { tabs: next, activeId: tab.id }
        }
      }
      return { tabs: [...b.tabs, tab], activeId: tab.id }
    })
  }, [update])

  const openView = useCallback((kind: ViewKind) => {
    upsert({ id: kind, kind, title: VIEW_TITLES[kind] })
  }, [upsert])

  const openFile = useCallback((path: string, content: string, slot: string | null = null, opts?: { replaceId?: string }) => {
    upsert({ id: `file:${path}`, kind: 'file', title: basename(path), path, content, slot }, opts?.replaceId)
  }, [upsert])

  const openDiff = useCallback((path: string, modified: string, original = '') => {
    upsert({ id: `diff:${path}`, kind: 'diff', title: `${basename(path)} - Diff`, path, modified, original })
  }, [upsert])

  const openArtifact = useCallback((art: { slug: string; kind: Artifact['kind'] }, content: string, slot: string | null = null) => {
    upsert({ id: `artifact:${art.slug}`, kind: 'artifact', title: art.slug, artifactSlug: art.slug, artifactKind: art.kind, content, slot })
  }, [upsert])

  /** Patch fields on an existing tab WITHOUT focusing it (live content/query
   *  hydration — e.g. MarkdownPanel edits, artifact query resolving). */
  const patchTab = useCallback((id: string, patch: Partial<PanelTab>) => {
    update(b => {
      const i = b.tabs.findIndex(t => t.id === id)
      if (i === -1) return b
      const next = b.tabs.slice()
      next[i] = { ...next[i], ...patch }
      return { ...b, tabs: next }
    })
  }, [update])

  const closeTab = useCallback((id: string) => {
    update(b => {
      const i = b.tabs.findIndex(t => t.id === id)
      if (i === -1) return b
      const next = b.tabs.filter(t => t.id !== id)
      // Refocus a neighbor when closing the active tab (prefer the left one).
      const activeId = b.activeId !== id
        ? b.activeId
        : next.length === 0 ? null : (next[i - 1] ?? next[i] ?? next[next.length - 1]).id
      return { tabs: next, activeId }
    })
  }, [update])

  const closeAll = useCallback(() => { update(() => ({ tabs: [], activeId: null })) }, [update])

  const setActive = useCallback((id: string | null) => { update(b => ({ ...b, activeId: id })) }, [update])

  /** Replace the tab order wholesale (drag-to-reorder in the strip). */
  const setOrder = useCallback((next: PanelTab[]) => { update(b => ({ ...b, tabs: next })) }, [update])

  const activeTab = useMemo(() => tabs.find(t => t.id === activeId) ?? null, [tabs, activeId])

  return useMemo(() => ({
    tabs, activeId, activeTab,
    openView, openFile, openDiff, openArtifact,
    patchTab, closeTab, closeAll, setActive, setOrder,
    hasTabs: tabs.length > 0,
  }), [tabs, activeId, activeTab, openView, openFile, openDiff, openArtifact, patchTab, closeTab, closeAll, setActive, setOrder])
}
