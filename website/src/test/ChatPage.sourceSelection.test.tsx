/**
 * Regression test: the Changes panel's source selection must be per-session,
 * not page-global.
 *
 * `selectedSourceUrl` is a single `useState` in ChatPage, and ChatPage never
 * remounts on a slot switch (only `activeSlot` changes). The Local tab's
 * sentinel is deliberately exempt from the "reconcile to the first PR"
 * effect — so without a reset, selecting Local in session A left every
 * subsequent session opening Local instead of its own PR. The fix clears the
 * selection when `activeSlot` changes (in-memory only; selection is never
 * persisted).
 *
 * Same shape as the per-slot browse-mode regression next door.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'
import { LOCAL_CHANGES_SOURCE_URL } from '../utils/pullRequestLinks'

const PR_URL = 'https://github.com/kirodotdev/KiroCrew/pull/480'

vi.mock('react-virtuoso', () => ({ Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (i: number, d: unknown) => React.ReactNode }) => <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div> }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    // Hydration REPLACES the preloaded messages, so the PR link has to be
    // here too or no source is ever detected.
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'see https://github.com/kirodotdev/KiroCrew/pull/480', cls: '' }], running: false, has_more: false, total: 1 }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
// ActivityViewer is NOT mocked: it hosts the Changes view that renders
// PullRequestPanel (mocked below).
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
// Stand in for the real panel: surface the selection and let the test drive
// onSelectSource the way a user clicking the Local chip would.
vi.mock('../components/PullRequestPanel', () => ({
  default: ({ sources, selectedUrl, onSelect }: { sources: Array<{ url: string }>; selectedUrl: string; onSelect: (u: string) => void }) => (
    <div>
      <div data-testid="selected-source">{selectedUrl}</div>
      <div data-testid="source-count">{sources.length}</div>

      <button onClick={() => onSelect(LOCAL_CHANGES_SOURCE_URL)}>pick-local</button>
    </div>
  ),
}))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

// A message carrying a PR link, so each slot has a real source to reconcile to.
const MSG = [{ role: 'assistant', content: `see ${PR_URL}`, cls: '' }]

function makeStore(activeSlot: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null,
        slots: [
          { key: 'slot-a', messages: 1, running: false, stopping: false, stop_state: 'idle', mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
          { key: 'slot-b', messages: 1, running: false, stopping: false, stop_state: 'idle', mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined },
        ],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: MSG,
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        // Panel pre-opened: ChatPage auto-focuses the Changes TAB on source
        // discovery but deliberately leaves panel VISIBILITY to the user.
        subagents: {}, toolLog: [], activityOpen: true, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderChat(activeSlot: string) {
  const store = makeStore(activeSlot)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(
      <QueryClientProvider client={qc}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter><ChatPage /></MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>,
    )
  })
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
  return store
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
})

describe('ChatPage source selection is per-slot', () => {
  it('does not carry the Local selection into the next slot', async () => {
    const store = await renderChat('slot-a')
    // The panel auto-opens on source discovery and reconciles to the PR.
    // Focus the Changes tab (it is pinned in the strip; ChatPage auto-focuses
    // it on discovery, but the test drives it explicitly to stay independent
    // of that behavior).
    await act(async () => { fireEvent.click(screen.getByLabelText('Changes')) })
    await waitFor(() => expect(screen.getByTestId('selected-source').textContent).toBe(PR_URL))

    // User switches this session to the Local tab.
    await act(async () => { fireEvent.click(screen.getByText('pick-local')) })
    await waitFor(() => expect(screen.getByTestId('selected-source').textContent).toBe(LOCAL_CHANGES_SOURCE_URL))

    // Switching sessions must not inherit it: slot-b reconciles to its own PR.
    await act(async () => { store.dispatch({ type: 'chat/setActiveSlot', payload: 'slot-b' }) })
    await waitFor(() => expect(screen.getByTestId('selected-source').textContent).toBe(PR_URL))
  })
})
