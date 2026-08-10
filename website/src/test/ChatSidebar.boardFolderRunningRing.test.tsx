/**
 * Board view (tag columns) carries the same running mark as the list view, and
 * its column scoping must not leak the session filters.
 *
 * A board column is a STRUCTURAL partition — a session belongs to it because of
 * its tags. The row's session count is filter-scoped (it counts what is on
 * screen), but the running mark answers "is work happening in here", which a
 * search or a filter toggle must not be able to answer wrongly. Deriving the
 * mark's column membership from the filtered slot set would empty the ring while
 * the work carried on, so membership is recomputed over the unfiltered list.
 *
 * Three load-bearing assertions:
 *   (1) the ring renders on a column's folder row, and its label names BOTH
 *       scopes — the folder subtree it counts AND the column it is limited to.
 *       Naming only the column would make two sibling folder rows in one column
 *       look like contradictory column totals;
 *   (2) an active session filter that hides the running session does NOT clear
 *       the ring;
 *   (3) a column whose tags do not match the running session stays unmarked —
 *       so the scoping is real and not "mark every column".
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { ChatTag, TagColumn, ChatFolder } from '../types'

// Render framer-motion elements as plain DOM (jsdom cannot run projection).
vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'layoutScroll', 'initial', 'animate', 'exit',
    'transition', 'variants', 'whileHover', 'whileTap', 'whileInView',
    'drag', 'dragConstraints', 'dragElastic', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: any, ref: any) => {
      const clean: any = {}
      for (const k of Object.keys(props)) {
        if (k === 'children') continue
        if (k === 'layoutId') { clean['data-layout-id'] = props[k]; continue }
        if (FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children)
    })
  const motion = new Proxy({}, { get: (_t, tag: string) => make(tag) })
  return {
    motion,
    AnimatePresence: ({ children }: any) => React.createElement(React.Fragment, null, children),
    LayoutGroup: ({ children }: any) => React.createElement(React.Fragment, null, children),
  }
})

vi.mock('../components/ProjectPicker', () => ({ default: () => null }))
vi.mock('../pages/chat/ChatSettings', () => ({
  loadChatConfig: () => ({ tagColumnsEnabled: true, confirmCloseSession: false }),
  saveChatConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy({} as Record<string, unknown>, { get: () => vi.fn().mockResolvedValue([]) }),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})

import ChatSidebar from '../pages/ChatSidebar'

const BLOCKED = '11111111-1111-1111-1111-111111111111'
const REVIEW = '22222222-2222-2222-2222-222222222222'
const COL_A = 'col-aaaa'
const COL_B = 'col-bbbb'
const FOLDER = 'folder-wt'

const tags: ChatTag[] = [
  { id: BLOCKED, name: 'Blocked', color: '#e11', order: 0, status: true },
  { id: REVIEW, name: 'Review', color: '#1a1', order: 1, status: true },
]
const columns: TagColumn[] = [
  { id: COL_A, name: 'Blocked', tag_ids: [BLOCKED], mode: 'any', order: 0 },
  { id: COL_B, name: 'Review', tag_ids: [REVIEW], mode: 'any', order: 1 },
]
const folders: ChatFolder[] = [{ id: FOLDER, name: 'worktrees', order: 0, collapsed: true }]

function renderSidebar(slots: any[]) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as any,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-tags'], tags)
  qc.setQueryData(['tag-columns'], columns)
  qc.setQueryData(['chat-folders'], folders)
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={[]}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => { localStorage.clear() })
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — board column folder marks a running subtree', () => {
  // Running, tagged Blocked (so it lives in COL_A), filed in the folder, NOT pinned.
  const running = {
    key: 'chat-1-100', title: 'busy chat', running: true, messages: 4,
    folder_id: FOLDER, tags: [BLOCKED], pinned: false,
  }

  it('names both the folder subtree and the column in the label', () => {
    const { getByTestId } = renderSidebar([running])
    const label = getByTestId(`folder-running-col-${COL_A}-${FOLDER}`).getAttribute('aria-label') ?? ''
    expect(label).toContain('1')
    // Both scopes, because the number is subtree ∩ column. A label naming only
    // the column would read as a column total, and two sibling folder rows in
    // one column would then look like they disagree.
    expect(label).toContain('subfolders')
    expect(label).toContain('column')
  })

  it('does not mark a column whose tags exclude the running session', () => {
    // Proves the scoping is real: COL_B filters on a tag this session lacks.
    const { queryByTestId } = renderSidebar([running])
    expect(queryByTestId(`folder-running-col-${COL_B}-${FOLDER}`)).toBeNull()
  })

  it('keeps the mark when a session filter hides the running session', () => {
    // Pinned-only is on and the running session is not pinned, so it drops out
    // of the filtered list the row's COUNT is built from. The mark must survive:
    // deriving its column membership from that filtered set is exactly the bug
    // this guards — the ring would go dark while the turn kept running.
    localStorage.setItem('mc-session-pinned-only', '1')
    const pinnedOther = {
      key: 'chat-2-200', title: 'pinned idle', running: false, messages: 4,
      folder_id: FOLDER, tags: [BLOCKED], pinned: true,
    }
    const { getByTestId } = renderSidebar([running, pinnedOther])
    const ring = getByTestId(`folder-running-col-${COL_A}-${FOLDER}`)
    expect(ring.getAttribute('aria-label')).toContain('1')
  })
})
