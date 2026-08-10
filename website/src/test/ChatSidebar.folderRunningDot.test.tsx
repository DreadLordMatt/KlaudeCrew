/**
 * A folder row says when work is in flight inside it.
 *
 * A collapsed folder hides its session rows, and the row's numeric column only
 * moves when sessions are added or removed — so before this, a folder holding a
 * turn mid-flight rendered identically to a wholly idle one. The row now carries
 * a hollow accent ring whenever its subtree holds a running session.
 *
 * Five load-bearing assertions:
 *   (1) a collapsed folder whose session is running shows the ring;
 *   (2) the signal propagates up the ancestor chain, so a collapsed parent
 *       speaks for a running session nested arbitrarily deep;
 *   (3) an idle folder shows no ring (the mark means running, not "has sessions");
 *   (4) the ring and the unread dot COEXIST — they describe different sessions,
 *       so suppressing either would hide real state; ring vs solid is what keeps
 *       them apart, which is also what makes them legible without animation;
 *   (5) the count reaches assistive tech, not just the mouse (role + aria-label).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'

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
  loadChatConfig: () => ({ tagColumnsEnabled: false, confirmCloseSession: false }),
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

function renderSidebar(slots: any[], folders: any[], unread: string[] = []) {
  const store = createTestStore({
    dashboard: {
      status: {}, connected: true, slots, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: unread, updateProgress: null,
      slotsLoaded: true,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    } as any,
    chat: { activeSlot: null, slotStatusDetail: {}, subagents: {}, slotActivity: {}, workflowRuns: {} } as any,
  })
  // staleTime keeps the seeded folder list authoritative: the blanket api mock
  // resolves every call to [], so an on-mount refetch would wipe the folders out.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false }, mutations: { retry: false } } })
  qc.setQueryData(['chat-folders'], folders)
  return render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatSidebar
              slots={slots} activeSlot={null} unreadSlots={unread}
              history={[]} historyHasMore={false} defaultAgent="" installedAgents={[]}
            />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
}

// Tree view (the default) — the flat lane has no folder rows to mark.
beforeEach(() => { localStorage.clear() })
afterEach(() => vi.clearAllMocks())

describe('chat sidebar — folder row marks a running subtree', () => {
  const FOLDER = [{ id: 'wt', name: 'worktrees', collapsed: true, order: 0 }]
  const NESTED = [
    { id: 'top', name: 'kirocrew', collapsed: true, order: 0 },
    { id: 'deep', name: 'fix-stale-turn', collapsed: true, order: 1, parent_id: 'top' },
  ]

  it('shows the ring on a collapsed folder whose session is running', () => {
    const running = { key: 'chat-1-100', title: 'busy chat', running: true, messages: 4, folder_id: 'wt' }
    const { getByTestId } = renderSidebar([running], FOLDER)
    const ring = getByTestId('folder-running-wt')
    // The count rides in the label; the ring itself is the glanceable signal.
    expect(ring.getAttribute('title')).toContain('1')
    // Reachable by AT, not only on mouse hover.
    expect(ring.getAttribute('role')).toBe('img')
    expect(ring.getAttribute('aria-label')).toBe(ring.getAttribute('title'))
    // Form, not motion, is what distinguishes it from the solid unread dot — so
    // a reduced-motion user (and a still screenshot) can still tell them apart.
    expect(ring.className).toContain('border-accent')
    expect(ring.className).not.toContain('animate-pulse')
  })

  it('rolls a nested running session up to every collapsed ancestor', () => {
    const running = { key: 'chat-2-200', title: 'busy chat', running: true, messages: 4, folder_id: 'deep' }
    const { getByTestId } = renderSidebar([running], NESTED)
    expect(getByTestId('folder-running-deep')).toBeTruthy()
    expect(getByTestId('folder-running-top')).toBeTruthy()
  })

  it('counts every running session in the subtree, not just one', () => {
    const slots = [
      { key: 'chat-3-300', title: 'deep one', running: true, messages: 4, folder_id: 'deep' },
      { key: 'chat-4-400', title: 'deep two', running: true, messages: 4, folder_id: 'deep' },
      { key: 'chat-5-500', title: 'top one', running: true, messages: 4, folder_id: 'top' },
    ]
    const { getByTestId } = renderSidebar(slots, NESTED)
    expect(getByTestId('folder-running-top').getAttribute('title')).toContain('3')
    expect(getByTestId('folder-running-deep').getAttribute('title')).toContain('2')
    // The label must say the number is a SUBTREE total: `top` holds one session
    // directly but reports 3, so a bare "in this folder" would misdescribe it.
    expect(getByTestId('folder-running-top').getAttribute('aria-label')).toContain('subfolders')
  })

  it('leaves an idle folder unmarked', () => {
    // The mark means "running", not "holds sessions" — an idle folder with a
    // session in it must stay bare, otherwise the ring carries no information.
    const idle = { key: 'chat-6-600', title: 'idle chat', running: false, messages: 4, folder_id: 'wt' }
    const { queryByTestId } = renderSidebar([idle], FOLDER)
    expect(queryByTestId('folder-running-wt')).toBeNull()
  })

  it('shows the running ring AND the unread dot together', () => {
    // Different sessions: one finished and is waiting on the user, another is
    // still working. Dropping the unread dot while any sibling runs would hide
    // "your turn" for the duration of unrelated work — the session row's
    // `s.unread && !s.running` rule only ever masks a session with itself.
    const slots = [
      { key: 'chat-7-700', title: 'finished chat', running: false, messages: 4, folder_id: 'wt' },
      { key: 'chat-8-800', title: 'busy chat', running: true, messages: 4, folder_id: 'wt' },
    ]
    const { getByTestId } = renderSidebar(slots, FOLDER, ['chat-7-700'])
    expect(getByTestId('folder-running-wt')).toBeTruthy()
    expect(getByTestId('folder-unread-wt')).toBeTruthy()
  })

  it('still shows the unread dot when nothing is running', () => {
    const done = { key: 'chat-9-900', title: 'finished chat', running: false, messages: 4, folder_id: 'wt' }
    const { getByTestId, queryByTestId } = renderSidebar([done], FOLDER, ['chat-9-900'])
    expect(getByTestId('folder-unread-wt')).toBeTruthy()
    expect(queryByTestId('folder-running-wt')).toBeNull()
  })
})
