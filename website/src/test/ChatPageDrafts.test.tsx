import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer, { setActiveSlot, switchSlot, createSlot, appendQueuedMessage } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [{ role: 'assistant', content: 'hi', cls: '' }], running: false, has_more: false, total: 1 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    slackChannels: vi.fn().mockResolvedValue([]),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    createChatSlot: vi.fn().mockResolvedValue({ key: 'new-slot', title: 'new-slot', messages: 0, running: false }),
    setSlotColor: vi.fn().mockResolvedValue({ ok: true }),
    setSlotFolder: vi.fn().mockResolvedValue({ ok: true }),
    chatSlotProject: vi.fn().mockResolvedValue({ ok: true }),
    cancelQueuedMessage: vi.fn().mockResolvedValue({ ok: true }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPage from '../pages/ChatPage'

function makeStore(activeSlot: string, slots: { key: string; mode?: string }[]) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        // connected: true is required for any test that exercises ChatPage.send().
        // The offline-UX feature added a defense-in-depth `if (!connected) return` at
        // the top of send() (covers all 5 call sites: keyboard, follow-up option,
        // reconnect auto-send, widget event, question card). Tests that submit
        // a draft and assert on api.sendChat must opt in explicitly here —
        // dashboardSlice initial state defaults connected to false, which is
        // also the value during a fresh page load before the WS handshake.
        status: null, connected: true, slots: slots.map(s => ({ key: s.key, messages: 1, running: false, mode: s.mode || '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined })),
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
      chat: {
        activeSlot, messages: [{ role: 'assistant', content: 'hi', cls: '' }],
        slotRunning: false, slotStopping: false, slotState: 'idle',
        history: [], historyHasMore: false, pendingInput: null,
        subagents: {}, toolLog: [], activityOpen: false, activityTab: 'tools',
        slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
        slotStatusDetail: {}, slotContextPct: {}, slotActivity: {}, slotHistory: [],
        historyOffset: 0, _wsChunkedDuringFetch: false,
        slotMessages: {}, slotLoading: false,
      } as unknown as RootState['chat'],
      notifications: { items: [] } as unknown as RootState['notifications'],
    },
  })
}

async function renderPage(store: ReturnType<typeof makeStore>, mode?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  let result: ReturnType<typeof render>
  await act(async () => {
    result = render(
      <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter><ChatPage mode={mode} /></MemoryRouter>
        </ThemeProvider>
      </Provider>
      </QueryClientProvider>,
    )
  })
  return result!
}

async function renderAndWaitForInput(store: ReturnType<typeof makeStore>, mode?: string) {
  const result = await renderPage(store, mode)
  await waitFor(() => expect(screen.getByLabelText('Message input')).toBeTruthy())
  return result
}

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
})

// the per-slot draft fix relies on a load-bearing effect ORDER --
// ALL THREE per-composer persist effects (text, files, pastes) must be declared
// before the effect that advances composerSlotRef.current. React runs effects
// in declaration order, so if the advance ran first a persist effect batched
// with a slot switch would see the already-advanced ref and smear the outgoing
// slot's value onto the incoming one. A behavioral test can't reach this (RTL
// flushes effects between a keystroke and a dispatch, so the two never share a
// commit); this static source-order assertion does, and goes red the instant
// someone reorders the effects or moves the advance up. All three persist
// writes are asserted (not just text) because the advance now guards all three.
describe('ChatPage composerSlotRef effect ordering', () => {
  it('declares all three composer-persist effects before advancing composerSlotRef', () => {
    // Deliberately brittle: this matches exact code substrings from ChatPage.tsx
    // to lock a load-bearing effect-declaration order. An innocuous rename/reformat
    // will trip it. The fix is to UPDATE the substrings below to the new form,
    // never to delete the guard (the ordering invariant it protects is real).
    const here = dirname(fileURLToPath(import.meta.url))
    const src = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
    const textIdx = src.indexOf('setDraft(drafts.current, s, input)')
    const fileIdx = src.indexOf('setFileDraft(fileDrafts.current, s, pendingFiles)')
    const pasteIdx = src.indexOf('setPasteDraft(pasteDrafts.current, s, pasteBlocks)')
    const advanceIdx = src.indexOf('composerSlotRef.current = activeSlot')
    expect(textIdx, 'text-persist effect (setDraft off composerSlotRef) not found').toBeGreaterThan(-1)
    expect(fileIdx, 'file-persist effect (setFileDraft off composerSlotRef) not found').toBeGreaterThan(-1)
    expect(pasteIdx, 'paste-persist effect (setPasteDraft off composerSlotRef) not found').toBeGreaterThan(-1)
    expect(advanceIdx, 'composerSlotRef advance not found').toBeGreaterThan(-1)
    const order = 'persist effect must be declared BEFORE the composerSlotRef advance (draft-smear guard). If effects moved, UPDATE the substrings; do not delete this guard.'
    expect(textIdx, order).toBeLessThan(advanceIdx)
    expect(fileIdx, order).toBeLessThan(advanceIdx)
    expect(pasteIdx, order).toBeLessThan(advanceIdx)
  })

  // Symptom B (send routing to the slot the user already left) can't be covered
  // behaviorally: the ref-vs-closure divergence it fixes is a same-tick race
  // between the reducer's activeSlot flip and send()'s re-memoization, and RTL
  // flushes a render between any dispatch and the Enter event, so the closure
  // and activeSlotRef never disagree in a test. Guard the fix statically
  // instead: send() must resolve its target from uiSlot (= activeSlotRef.current),
  // never the bare closure activeSlot. Goes red if someone reverts `?? uiSlot`.
  it('sends to uiSlot (activeSlotRef), not the stale closure activeSlot (Symptom B)', () => {
    // Same brittle-by-design string match: if the send-target lines are renamed,
    // UPDATE the substrings to the new form; do not delete this guard.
    const here = dirname(fileURLToPath(import.meta.url))
    const src = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
    expect(src, 'uiSlot must be read from the activeSlot ref').toContain('const uiSlot = activeSlotRef.current')
    expect(src, 'send target must resolve from uiSlot').toContain('let slot = targetSlot ?? uiSlot')
    expect(src, 'send target must NOT fall back to the stale closure activeSlot').not.toContain('let slot = targetSlot ?? activeSlot')
  })
})

describe('ChatPage draft persistence', { timeout: 15_000 }, () => {
  it('preserves draft when switching sessions', async () => {
    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'draft for A' } })

    act(() => { store.dispatch(setActiveSlot('slot-b')) })

    const saved = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
    expect(saved['slot-a']).toBe('draft for A')

    act(() => { store.dispatch(setActiveSlot('slot-a')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('draft for A')
  })

  it('persists draft to localStorage on every keystroke', async () => {
    const store = makeStore('slot-x', [{ key: 'slot-x' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'live' } })

    await waitFor(() => {
      const saved = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      expect(saved['slot-x']).toBe('live')
    })
  })

  it('removes draft when input is cleared', async () => {
    const store = makeStore('slot-x', [{ key: 'slot-x' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'temp' } })
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('mc-chat-drafts')!)['slot-x']).toBe('temp')
    })

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: '' } })
    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('mc-chat-drafts')!)['slot-x']).toBeUndefined()
    })
  })

  it('keeps drafts for multiple sessions independently', async () => {
    const store = makeStore('s1', [{ key: 's1' }, { key: 's2' }, { key: 's3' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'one' } })

    act(() => { store.dispatch(setActiveSlot('s2')) })
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'two' } })

    act(() => { store.dispatch(setActiveSlot('s3')) })
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'three' } })

    const saved = await waitFor(() => {
      const s = JSON.parse(localStorage.getItem('mc-chat-drafts')!)
      expect(s['s3']).toBe('three')
      return s
    })
    expect(saved['s1']).toBe('one')
    expect(saved['s2']).toBe('two')
    expect(saved['s3']).toBe('three')

    act(() => { store.dispatch(setActiveSlot('s1')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('one')
  })

  it('does not overwrite target draft with source input on slot switch (race condition)', async () => {
    // Pre-seed a draft for slot-b
    localStorage.setItem('mc-chat-drafts', JSON.stringify({ 'slot-b': 'B draft' }))

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'A text' } })

    // Switch to slot-b — should restore "B draft", NOT "A text"
    act(() => { store.dispatch(setActiveSlot('slot-b')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('B draft')

    // Verify slot-a draft was saved correctly
    const saved = JSON.parse(localStorage.getItem('mc-chat-drafts')!)
    expect(saved['slot-a']).toBe('A text')
  })

  it('localStorage rehydration does not clobber in-memory draft (regression)', async () => {
    // Scenario: type in slot-a, localStorage is stale (doesn't have the draft yet),
    // switch to slot-b — the in-memory draft for slot-a must survive rehydration.
    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'fresh text' } })

    // Simulate stale localStorage (e.g. another tab wrote an older version)
    localStorage.setItem('mc-chat-drafts', JSON.stringify({ 'slot-a': 'stale' }))

    // Switch to slot-b
    act(() => { store.dispatch(setActiveSlot('slot-b')) })

    // Switch back to slot-a — should have 'fresh text', not 'stale'
    act(() => { store.dispatch(setActiveSlot('slot-a')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('fresh text')
  })

  it('draft survives round-trip through three slots', async () => {
    const store = makeStore('a', [{ key: 'a' }, { key: 'b' }, { key: 'c' }])
    await renderAndWaitForInput(store)

    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'alpha' } })

    act(() => { store.dispatch(setActiveSlot('b')) })
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'beta' } })

    act(() => { store.dispatch(setActiveSlot('c')) })
    // Don't type anything in c

    act(() => { store.dispatch(setActiveSlot('a')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('alpha')

    act(() => { store.dispatch(setActiveSlot('b')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('beta')

    act(() => { store.dispatch(setActiveSlot('c')) })
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('')
  })

  it('pre-seeded per-slot file drafts survive slot switches without cross-leak', async () => {
    // Regression guard for screenshot-leak bug: pendingFiles was a single shared
    // useState, so files attached in slot-a appeared in slot-b's compose box
    // when the user switched tabs before sending.
    sessionStorage.setItem('mc-chat-file-drafts', JSON.stringify({
      'slot-a': ['/tmp/screenshot-a.png'],
      'slot-b': ['/tmp/screenshot-b1.png', '/tmp/screenshot-b2.png'],
    }))

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    // Switch to slot-b, then back to slot-a. The slot-switch effect flushes
    // fileDrafts on each transition; the pre-seeded per-slot entries must
    // round-trip unchanged (no cross-leak, no reset-to-empty).
    act(() => { store.dispatch(setActiveSlot('slot-b')) })
    act(() => { store.dispatch(setActiveSlot('slot-a')) })

    await waitFor(() => {
      const saved = JSON.parse(sessionStorage.getItem('mc-chat-file-drafts')!)
      expect(saved['slot-a']).toEqual(['/tmp/screenshot-a.png'])
      expect(saved['slot-b']).toEqual(['/tmp/screenshot-b1.png', '/tmp/screenshot-b2.png'])
    })
  })

  it('async upload resolving after slot switch lands in the request slot', async () => {
    // Regression guard for the async-upload race flagged in review:
    // user starts an upload in slot-a, switches to slot-b before the promise
    // resolves, and the uploaded file must land in slot-a's persisted draft —
    // not silently appear in slot-b's live state.
    const { api } = await import('../api/client')
    let resolveUpload!: (v: { paths: string[] }) => void
    const deferred = new Promise<{ paths: string[] }>(r => { resolveUpload = r })
    vi.mocked(api.uploadFiles).mockReturnValueOnce(deferred)

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    // Fire a drop event on the chat input area to trigger uploadFiles.
    const input = screen.getByLabelText('Message input')
    const dropTarget = input.closest('div') as HTMLElement
    const file = new File(['x'], 'test.png', { type: 'image/png' })
    await act(async () => {
      fireEvent.drop(dropTarget, { dataTransfer: { files: [file], types: ['Files'] } })
    })

    // Switch to slot-b while the upload is still pending.
    act(() => { store.dispatch(setActiveSlot('slot-b')) })

    // Now resolve the upload — the file must be diverted to slot-a.
    await act(async () => {
      resolveUpload({ paths: ['/tmp/uploaded.png'] })
      await deferred
    })

    await waitFor(() => {
      const saved = JSON.parse(sessionStorage.getItem('mc-chat-file-drafts') || '{}')
      expect(saved['slot-a']).toEqual(['/tmp/uploaded.png'])
      expect(saved['slot-b']).toBeUndefined()
    })
  })

  it('collapsed paste survives slot switch and sends expanded, not literal token', async () => {
    // Regression for the dead-token bug: a collapsed paste becomes a
    // `[ Paste #N · M lines ]` chip backed by an in-memory PasteBlock. Switching
    // slots used to clear the blocks while the token text was restored from the
    // text draft, so the chip went dead and the literal token was sent.
    const { api } = await import('../api/client')
    vi.mocked(api.sendChat).mockClear()

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    const pasted = 'line1\nline2\nline3\nline4\nline5'  // >= PASTE_THRESHOLD_LINES

    // Fire a text paste — ChatInput collapses it into a token + PasteBlock.
    await act(async () => {
      fireEvent.paste(input, {
        clipboardData: { items: [], getData: (t: string) => (t === 'text' ? pasted : '') },
      })
    })
    // The textarea now holds the token, not the raw content.
    await waitFor(() => expect(input.value).toMatch(/\[ Paste #1 · 5 lines \]/))

    // Switch away and back WITHOUT sending.
    act(() => { store.dispatch(setActiveSlot('slot-b')) })
    act(() => { store.dispatch(setActiveSlot('slot-a')) })

    // Token text is restored AND still backed by its block.
    await waitFor(() => expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toMatch(/\[ Paste #1 · 5 lines \]/))

    // Send — the LLM must receive the EXPANDED content, never the literal token.
    await act(async () => { fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' }) })

    await waitFor(() => expect(api.sendChat).toHaveBeenCalled())
    const llmText = vi.mocked(api.sendChat).mock.calls[0][0] as string
    expect(llmText).toContain('line1\nline2\nline3\nline4\nline5')
    expect(llmText).not.toContain('[ Paste #1 · 5 lines ]')
  })

  it('restores paste blocks to the active slot on connection error', async () => {
    // The restore path puts the token text back in the input; the
    // backing blocks must come back too, or the restored draft shows a dead token.
    const { api } = await import('../api/client')
    vi.mocked(api.sendChat).mockRejectedValueOnce(new Error('Network error'))

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    const pasted = 'alpha\nbeta\ngamma\ndelta'
    await act(async () => {
      fireEvent.paste(input, {
        clipboardData: { items: [], getData: (t: string) => (t === 'text' ? pasted : '') },
      })
    })
    await waitFor(() => expect(input.value).toMatch(/\[ Paste #1 · 4 lines \]/))

    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })

    // After the failed send, the paste draft must be persisted for the slot so a
    // subsequent reload/switch can re-pair the token (not just left in the text).
    await waitFor(() => {
      const pasteDrafts = JSON.parse(localStorage.getItem('mc-chat-paste-drafts') || '{}')
      expect(pasteDrafts['slot-a']).toBeTruthy()
      expect(pasteDrafts['slot-a'][0].content).toBe(pasted)
    })
  })

  it('preserves the user content when slot creation is rejected mid-send', async () => {
    // Two-phase commit contract: a send PAUSES draft persistence for the origin
    // slot before clearing the live composer, and destroys nothing until the
    // send commits. A rejected createSlot therefore needs no restore logic --
    // the draft store was never touched -- and the live composer is re-filled
    // as a courtesy when the user is still there.
    //
    // Driven through the chat-launch intent (useChatLauncher writes
    // window.__mc_chat_launch; ChatPage consumes it on mount and sets
    // newSessionRef), the app's own seam for "create a slot and send this".
    const { api } = await import('../api/client')
    const createMock = vi.mocked(api.createChatSlot)
    createMock.mockRejectedValueOnce(new Error('slot creation failed'))

    const launchWindow = window as Window & {
      __mc_chat_launch?: { ts: number; message: string }
    }
    launchWindow.__mc_chat_launch = { ts: Date.now(), message: 'text I do not want to lose' }

    // A file staged in the origin slot's draft BEFORE the send. An uncommitted
    // send must leave it untouched (no clear runs for an optionText send, and
    // commit never runs on rejection).
    sessionStorage.setItem('mc-chat-file-drafts', JSON.stringify({ 'slot-a': ['/staged/keep.png'] }))

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)
    await waitFor(() => expect(createMock).toHaveBeenCalled())

    // The rejection leaves the text visible in the live composer (the courtesy
    // re-fill: same slot, nothing typed since).
    await waitFor(() => {
      expect(
        (screen.getByLabelText('Message input') as HTMLTextAreaElement).value,
        'a rejected slot creation must leave the text visible for retry',
      ).toBe('text I do not want to lose')
    })
    // ...and the file draft was never destroyed: no commit ran, so the draft
    // store was never touched. Under snapshot-and-restore this assertion was
    // impossible to make meaningful -- restores raced the persist effects.
    const fd = JSON.parse(sessionStorage.getItem('mc-chat-file-drafts') || '{}')
    expect(
      fd['slot-a'],
      'the staged file draft must survive an uncommitted send',
    ).toEqual(['/staged/keep.png'])
  })

  it('a rejection landing after a slot switch leaves the origin draft intact', async () => {
    // Send from slot-a, switch to slot-b while the create is pending, THEN the
    // rejection lands. The courtesy re-fill is correctly suppressed (user is on
    // slot-b), and slot-a's drafts must be byte-identical to pre-send.
    //
    // COVERAGE NOTE, stated honestly: this drives an optionText (auto-send)
    // send, which never clears the composer, so the persistence PAUSE is a
    // no-op on this path and deleting it does not fail this test. The pause
    // protects normal composer sends, whose createSlot branch requires the New
    // Chat intent -- not reachable from this harness. What this test DOES lock
    // in: an uncommitted send destroys nothing, even when the rejection lands
    // on a different slot than it started from.
    const { api } = await import('../api/client')
    let rejectCreate!: (e: Error) => void
    const deferred = new Promise<{ key: string; title: string; messages: number; running: boolean }>(
      (_r, rej) => { rejectCreate = rej },
    )
    vi.mocked(api.createChatSlot).mockReturnValueOnce(deferred as ReturnType<typeof api.createChatSlot>)

    const launchWindow = window as Window & {
      __mc_chat_launch?: { ts: number; message: string }
    }
    launchWindow.__mc_chat_launch = { ts: Date.now(), message: 'origin slot text' }
    sessionStorage.setItem('mc-chat-file-drafts', JSON.stringify({ 'slot-a': ['/origin/file.png'] }))

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)
    await waitFor(() => expect(api.createChatSlot).toHaveBeenCalled())

    // User gives up and switches to slot-b while the create is still pending.
    await act(async () => { await store.dispatch(switchSlot('slot-b')) })

    // Now the rejection lands.
    await act(async () => {
      rejectCreate(new Error('slot creation failed'))
      await Promise.resolve()
      await Promise.resolve()
    })

    // slot-a's text and file drafts must be exactly what they were pre-send.
    await waitFor(() => {
      const fd = JSON.parse(sessionStorage.getItem('mc-chat-file-drafts') || '{}')
      expect(
        fd['slot-a'],
        'the origin file draft must survive a rejection that lands after a switch',
      ).toEqual(['/origin/file.png'])
    })
    const td = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
    expect(
      td['slot-a'] ?? 'origin slot text',
      'the origin text draft must not have been overwritten with the empty clear',
    ).toBe('origin slot text')
  })

  it('a rejected COMPOSER send preserves the persisted text, files and pastes', async () => {
    // This is the path the persistence PAUSE actually protects, and the one the
    // first two rejection tests could not reach: an ordinary composer send
    // (no optionText) CLEARS the composer, so without the pause the persist
    // effects — and the slot-switch flush — write that empty state straight
    // into the draft store and the user's text/files/pastes are gone for good.
    //
    // Reaching it needs `newSessionRef` armed on a composer send. The seam is
    // the app's own behavior: a REJECTED new-session send leaves the one-shot
    // intent armed on purpose (that is the two-phase-commit contract), so the
    // user's next keystroke-driven send still takes the createSlot branch.
    // First rejection arms it; the second (deferred) rejection is under test.
    //
    // The user then switches AWAY before the rejection lands, which suppresses
    // the courtesy live re-fill. That matters for falsifiability: with the
    // re-fill in play the restored text is re-persisted, masking the loss. With
    // the user on another slot, only the pause stands between the composer
    // clear and the draft store.
    //
    // Revert-verified: removing the pause guard from the slot-switch /
    // unmount / beforeunload flushes fails this test, as does removing all six
    // guards. Removing ONLY the live text-persist-effect guard still passes —
    // on this path the outgoing-slot flush is the write that destroys the
    // draft. The live-effect guards are defence for other orderings and are
    // not claimed as covered here.
    const { api } = await import('../api/client')
    const createMock = vi.mocked(api.createChatSlot)
    // Call counts are cumulative across this file (no global clearMocks), so
    // assert on the delta from where this test starts.
    const baseCalls = createMock.mock.calls.length
    createMock.mockRejectedValueOnce(new Error('slot creation failed'))
    let rejectSecond!: (e: Error) => void
    const deferred = new Promise<{ key: string; title: string; messages: number; running: boolean }>(
      (_r, rej) => { rejectSecond = rej },
    )
    createMock.mockReturnValueOnce(deferred as ReturnType<typeof api.createChatSlot>)

    const launchWindow = window as Window & {
      __mc_chat_launch?: { ts: number; message: string }
    }
    launchWindow.__mc_chat_launch = { ts: Date.now(), message: 'arming send' }

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)
    await waitFor(() => expect(createMock.mock.calls.length).toBe(baseCalls + 1))

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    // Clear the courtesy re-fill from the arming send, then compose fresh:
    // typed text plus a pasted block.
    fireEvent.change(input, { target: { value: '' } })
    await act(async () => {
      fireEvent.paste(input, {
        clipboardData: { items: [], getData: (t: string) => (t === 'text' ? 'p1\np2\np3\np4' : '') },
      })
    })
    await waitFor(() => expect(input.value).toMatch(/\[ Paste #1 · 4 lines \]/))
    fireEvent.change(input, { target: { value: `keep this text ${input.value}` } })
    // Let the debounced persist land the composed draft before the send.
    await waitFor(() => {
      const td = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      expect(td['slot-a'] ?? '').toContain('keep this text')
    }, { timeout: 3000 })

    // Second send: keystroke-driven, so the composer clear runs.
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })
    await waitFor(() => expect(createMock.mock.calls.length).toBe(baseCalls + 2))
    expect(
      (screen.getByLabelText('Message input') as HTMLTextAreaElement).value,
      'a composer send must clear the live composer (this is the destructive path)',
    ).toBe('')

    // User switches away while the create is still pending, then it rejects.
    await act(async () => { await store.dispatch(switchSlot('slot-b')) })
    await act(async () => {
      rejectSecond(new Error('slot creation failed again'))
      await Promise.resolve()
      await Promise.resolve()
    })

    // slot-a's persisted text and paste drafts must be exactly what they were
    // before the send: the clear was never allowed to reach the draft store.
    const td = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
    expect(
      td['slot-a'] ?? '',
      'the pause must stop the composer clear from erasing the persisted text draft',
    ).toContain('keep this text')
    const pd = JSON.parse(localStorage.getItem('mc-chat-paste-drafts') || '{}')
    expect(
      pd['slot-a']?.[0]?.content,
      'the pasted block backing the preserved token must survive',
    ).toBe('p1\np2\np3\np4')
  })

  it('a rejection keeps a file staged DURING the slow create', async () => {
    // The composer stays usable while a create is pending, so the user can
    // attach another file. Re-filling with the payload snapshot wholesale
    // replaced the live list and that newer attachment vanished — silently, with
    // no undo. The re-fill must MERGE: snapshot order first, then anything
    // staged since.
    const { api } = await import('../api/client')
    const createMock = vi.mocked(api.createChatSlot)
    const baseCalls = createMock.mock.calls.length
    let rejectCreate!: (e: Error) => void
    const deferred = new Promise<{ key: string; title: string; messages: number; running: boolean }>(
      (_r, rej) => { rejectCreate = rej },
    )
    createMock.mockReturnValueOnce(deferred as ReturnType<typeof api.createChatSlot>)
    vi.mocked(api.uploadFiles).mockResolvedValueOnce({ paths: ['/uploaded/during.png'] } as Awaited<ReturnType<typeof api.uploadFiles>>)

    const launchWindow = window as Window & {
      __mc_chat_launch?: { ts: number; message: string }
    }
    launchWindow.__mc_chat_launch = { ts: Date.now(), message: 'send with attachment' }
    // A file staged before the send — part of this send's payload.
    sessionStorage.setItem('mc-chat-file-drafts', JSON.stringify({ 'slot-a': ['/staged/before.png'] }))

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)
    await waitFor(() => expect(createMock.mock.calls.length).toBe(baseCalls + 1))

    // While the create hangs, the user attaches another file.
    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement
    expect(fileInput, 'the attach control must exist to stage a file mid-create').toBeTruthy()
    await act(async () => {
      fireEvent.change(fileInput, {
        target: { files: [new File(['x'], 'during.png', { type: 'image/png' })] },
      })
    })
    await waitFor(() => {
      const fd = JSON.parse(sessionStorage.getItem('mc-chat-file-drafts') || '{}')
      expect(fd['slot-a']).toContain('/uploaded/during.png')
    }, { timeout: 3000 })

    // Now the create rejects.
    await act(async () => {
      rejectCreate(new Error('slot creation failed'))
      await Promise.resolve()
      await Promise.resolve()
    })

    // Live composer chips are the falsifiable assertion: the re-fill writes
    // pendingFiles directly, and the draft store would keep the mid-create file
    // either way (nothing destroys it). Both chips must be present, snapshot
    // first.
    await waitFor(() => {
      const alts = Array.from(document.querySelectorAll('img[data-lightbox-image]'))
        .map(el => el.getAttribute('alt'))
      expect(
        alts,
        'the mid-create attachment must not be replaced by the older snapshot',
      ).toEqual(['/staged/before.png', '/uploaded/during.png'])
    }, { timeout: 3000 })
  })

  it('a second send during a slow create does not spawn a hidden second slot', async () => {
    // The new-session intent is only consumed at COMMIT, so while a slow create
    // awaits it is still armed: a second send took the create branch again and
    // made a SECOND slot. Only the first is activated, so the second prompt ran
    // in a session the user never sees. The send is rejected instead — and,
    // because it bails before the composer clear, it loses nothing.
    const { api } = await import('../api/client')
    const createMock = vi.mocked(api.createChatSlot)
    const baseCalls = createMock.mock.calls.length
    let resolveCreate!: (v: { key: string; title: string; messages: number; running: boolean }) => void
    const deferred = new Promise<{ key: string; title: string; messages: number; running: boolean }>(
      r => { resolveCreate = r },
    )
    createMock.mockReturnValueOnce(deferred as ReturnType<typeof api.createChatSlot>)

    const launchWindow = window as Window & {
      __mc_chat_launch?: { ts: number; message: string }
    }
    launchWindow.__mc_chat_launch = { ts: Date.now(), message: 'first send' }

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)
    await waitFor(() => expect(createMock.mock.calls.length).toBe(baseCalls + 1))

    // Second send while the first create is still pending.
    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    fireEvent.change(input, { target: { value: 'second send' } })
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })

    expect(
      createMock.mock.calls.length,
      'the overlapping send must not start a second slot creation',
    ).toBe(baseCalls + 1)
    expect(
      (screen.getByLabelText('Message input') as HTMLTextAreaElement).value,
      'the rejected send must leave the typed text in place',
    ).toBe('second send')

    // Let the first create finish so the component settles.
    await act(async () => {
      resolveCreate({ key: 'new-slot', title: 'new-slot', messages: 0, running: false })
      await Promise.resolve()
    })
  })

  it('cancelling a card WITHOUT ordered files leaves the content verbatim', async () => {
    // An edited card intentionally has no ordered list (the retyped text's
    // markers no longer line up with the old paths). Falling back to a
    // whitespace-bounded scan would mangle the text and stage a phantom
    // attachment, so the raw marker is left visible for the user to fix by hand.
    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)

    const content = 'review [attached_file 1] /docs/quarterly report.pdf please'
    act(() => {
      store.dispatch(appendQueuedMessage({
        slot: 'slot-a', content, ts: 't1', queue_id: 'q1',
      }))
    })

    const cancelBtn = await waitFor(() => screen.getByLabelText('Cancel queued message'))
    await act(async () => { fireEvent.click(cancelBtn) })

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await waitFor(() => expect(input.value).toContain('review'))
    expect(
      input.value,
      'without a trustworthy ordered list the content is restored verbatim',
    ).toBe(content)
  })

  it('slow New Chat that resolves after a slot switch does not steal the typed text', async () => {
    // Symptom A: memory is high, user clicks New Chat, the create backend call
    // hangs. User switches to slot-b and types. When the slow create finally
    // resolves it must NOT hijack the view and drag slot-b's text into the new
    // chat. The text stays in slot-b, and the new chat opens empty.
    const { api } = await import('../api/client')
    let resolveCreate!: (v: { key: string; title: string; messages: number; running: boolean }) => void
    const deferred = new Promise<{ key: string; title: string; messages: number; running: boolean }>(r => { resolveCreate = r })
    vi.mocked(api.createChatSlot).mockReturnValueOnce(deferred as any)

    const store = makeStore('slot-a', [{ key: 'slot-a' }, { key: 'slot-b' }])
    await renderAndWaitForInput(store)

    // Kick off a slow New Chat (stays pending).
    let createPromise: Promise<unknown>
    act(() => { createPromise = store.dispatch(createSlot(undefined)) })

    // User gives up waiting, switches to slot-b, and types there.
    await act(async () => { await store.dispatch(switchSlot('slot-b')) })
    fireEvent.change(screen.getByLabelText('Message input'), { target: { value: 'text meant for slot-b' } })

    // The slow create finally resolves.
    await act(async () => {
      resolveCreate({ key: 'new-slot', title: 'new-slot', messages: 0, running: false })
      await createPromise
    })

    // The view must still be on slot-b with the typed text intact...
    expect(store.getState().chat.activeSlot).toBe('slot-b')
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('text meant for slot-b')

    // ...and once the debounced draft save flushes, the text must be keyed to
    // slot-b, never leaked into the new chat's draft.
    const saved = await waitFor(() => {
      const s = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      expect(s['slot-b']).toBe('text meant for slot-b')
      return s
    })
    expect(saved['new-slot']).toBeUndefined()
  })

  it('restores draft to localStorage on connection error', async () => {
    // Override sendChat to simulate network failure for this test only
    const { api } = await import('../api/client')
    vi.mocked(api.sendChat).mockRejectedValueOnce(new Error('Network error'))

    const store = makeStore('slot-a', [{ key: 'slot-a' }])
    await renderAndWaitForInput(store)

    const input = screen.getByLabelText('Message input') as HTMLTextAreaElement
    await act(async () => { fireEvent.change(input, { target: { value: 'precious prompt' } }) })

    // Send triggers connection error (sendChat rejects)
    await act(async () => { fireEvent.keyDown(input, { key: 'Enter' }) })

    // Draft should be restored to localStorage after error
    await waitFor(() => {
      const drafts = JSON.parse(localStorage.getItem('mc-chat-drafts') || '{}')
      expect(drafts['slot-a']).toBe('precious prompt')
    })
  })
})
