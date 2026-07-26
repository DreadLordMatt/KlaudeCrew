/**
 * useWebSocket `steer_push` -> attachment metadata handoff.
 *
 * A mid-turn steer is broadcast to every other open tab as a `steer_push` frame.
 * Marker number N in the content is a positional index into `meta.files` /
 * `meta.dirs`, so a tab that renders the content without those ordered lists
 * falls back to a whitespace-bounded scan and truncates a path containing a
 * space (`/repo/my docs` -> `/repo/my`).
 *
 * The transport forwards `data.meta` into the appended message. That wiring had
 * no test: the reducer tests construct the message directly and bypass the hook,
 * so deleting the forward left the whole suite green while second-tab
 * attachments silently truncated. This locks the hook-level handoff.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createElement } from 'react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import { useWebSocket } from '../hooks/useWebSocket'

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    voiceConfig: vi.fn().mockResolvedValue({ autoSpeak: false }),
    approvals: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [], unread: 0 }),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] }),
  },
}))

const WS_INSTANCES: MockWebSocket[] = []

class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  readyState = MockWebSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send = vi.fn()
  close = vi.fn()

  constructor() {
    WS_INSTANCES.push(this)
  }

  simulateOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.(new Event('open'))
  }

  simulateMessage(data: object) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(data) }))
  }
}

const SLOT = 'chat-1'
const SPACED_DIR = '/repo/my docs'
const SPACED_FILE = '/repo/my notes/todo.md'

describe('useWebSocket steer_push forwards attachment metadata', () => {
  let queryClient: QueryClient
  let store: ReturnType<typeof createTestStore>

  beforeEach(() => {
    vi.clearAllMocks()
    WS_INSTANCES.length = 0
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    store = createTestStore()
    vi.stubGlobal('WebSocket', MockWebSocket)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function wrapper({ children }: { children: React.ReactNode }) {
    return createElement(Provider, { store },
      createElement(QueryClientProvider, { client: queryClient }, children))
  }

  function steered() {
    const s = store.getState().chat
    const msgs = s.slotMessages[SLOT] ?? s.messages
    return msgs.filter(m => m.role === 'user').at(-1)
  }

  it('carries meta.files and meta.dirs onto the appended bubble', () => {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({
        type: 'steer_push',
        data: {
          slot: SLOT,
          content: `check [attached_file 1] ${SPACED_FILE} and [attached_dir 1] ${SPACED_DIR}`,
          ts: '2026-07-26T10:00:00.000Z',
          meta: { files: [SPACED_FILE], dirs: [SPACED_DIR] },
        },
      })
    })

    const msg = steered()
    expect(msg, 'steer_push did not append a user bubble').toBeTruthy()
    // The ordered lists must survive verbatim — they are the index space the
    // markers resolve against.
    expect(msg!.meta?.files).toEqual([SPACED_FILE])
    expect(msg!.meta?.dirs).toEqual([SPACED_DIR])
  })

  it('re-asserts steer: true so the bubble styles correctly', () => {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({
        type: 'steer_push',
        data: { slot: SLOT, content: 'plain steer', ts: '2026-07-26T10:00:00.000Z' },
      })
    })

    expect(steered()!.meta?.steer).toBe(true)
  })

  it('still marks a steer against an older backend that sends no meta', () => {
    renderHook(() => useWebSocket(), { wrapper })
    const ws = WS_INSTANCES[0]
    act(() => { ws.simulateOpen() })

    act(() => {
      ws.simulateMessage({
        type: 'steer_push',
        data: { slot: SLOT, content: 'no meta at all', ts: '2026-07-26T10:00:00.000Z' },
      })
    })

    const msg = steered()!
    expect(msg.meta?.steer).toBe(true)
    expect(msg.meta?.dirs).toBeUndefined()
  })
})
