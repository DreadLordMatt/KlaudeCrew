/**
 * A queued message's card must keep the server's attachment metadata.
 *
 * The `queue_push` WS event is what an open client renders a queued card from.
 * The ordered `files` / `dirs` lists are what let `[attached_file N]` /
 * `[attached_dir N]` resolve a path containing a space; without them the card
 * falls back to the whitespace-bounded content scan and displays a path
 * truncated at its first space until the page is reloaded.
 *
 * The reducer previously hardcoded `meta: { queueId }`, discarding whatever the
 * server sent.
 */
import { describe, it, expect } from 'vitest'
import reducer, { appendQueuedMessage, editQueuedMessage, removeQueuedMessage, setActiveSlot } from '../store/chatSlice'

const SLOT = 'slot-q'
const SPACED = '/repo/my docs'

function base() {
  const s = reducer(undefined, { type: '@@INIT' })
  return reducer(s, setActiveSlot(SLOT))
}

describe('appendQueuedMessage preserves attachment metadata', () => {
  it('keeps meta.dirs from the queue_push payload', () => {
    const next = reducer(base(), appendQueuedMessage({
      slot: SLOT,
      content: `review [attached_dir 1] ${SPACED}`,
      ts: '2026-07-26T10:00:00.000Z',
      queue_id: 'q1',
      meta: { dirs: [SPACED] },
    }))
    const card = next.messages.at(-1)!
    expect(card.role).toBe('queued')
    expect(card.meta?.dirs).toEqual([SPACED])
  })

  it('keeps meta.files alongside the queueId', () => {
    const next = reducer(base(), appendQueuedMessage({
      slot: SLOT,
      content: '[attached_file 1] /repo/my notes.txt',
      ts: '2026-07-26T10:00:00.000Z',
      queue_id: 'q2',
      meta: { files: ['/repo/my notes.txt'] },
    }))
    const card = next.messages.at(-1)!
    expect(card.meta?.files).toEqual(['/repo/my notes.txt'])
    expect(card.meta?.queueId, 'queueId must survive the merge').toBe('q2')
  })

  it('queueId wins over a spoofed one in meta', () => {
    // queueId is the client's handle for cancel/edit; a server meta key must not
    // be able to displace it.
    const next = reducer(base(), appendQueuedMessage({
      slot: SLOT,
      content: 'x',
      ts: '2026-07-26T10:00:00.000Z',
      queue_id: 'real',
      meta: { queueId: 'spoofed' },
    }))
    expect(next.messages.at(-1)!.meta?.queueId).toBe('real')
  })

  it('still works with no meta (older backend)', () => {
    const next = reducer(base(), appendQueuedMessage({
      slot: SLOT,
      content: 'plain',
      ts: '2026-07-26T10:00:00.000Z',
      queue_id: 'q3',
    }))
    const card = next.messages.at(-1)!
    expect(card.meta?.queueId).toBe('q3')
    expect(card.meta?.dirs).toBeUndefined()
  })
})

describe('hydrated queue cards keep server metadata', () => {
  // Live queue_push already carried the lists, so a queued attachment looked
  // correct until RELOAD: hydration rebuilt the card with only `queueId`, so the
  // markers had no index space and a spaced path truncated.
  it('carries meta from the slot-detail payload onto the card', () => {
    const next = reducer(base(), {
      type: 'chat/switchSlot/fulfilled',
      payload: {
        key: SLOT,
        messages: [],
        running: false,
        stopping: false,
        hasMore: false,
        total: 0,
        queue: [{
          content: `review [attached_dir 1] ${SPACED}`,
          queueId: 'q1',
          ts: '2026-07-26T10:00:00.000Z',
          meta: { dirs: [SPACED] },
        }],
      },
    })
    const all = [...next.messages, ...Object.values(next.slotMessages).flat()]
    const card = all.find(m => m.role === 'queued')
    expect(card, 'no queued card was hydrated').toBeTruthy()
    expect(card!.meta?.dirs).toEqual([SPACED])
    expect(card!.meta?.queueId).toBe('q1')
  })
})

describe('editQueuedMessage drops attachment metadata', () => {
  // Must match the server (`queue_edit_by_id`): the edited text owns its own
  // attachments. Keeping the old ordered path lists would render an attachment
  // card for a marker the new content no longer contains.
  function queued() {
    return reducer(base(), appendQueuedMessage({
      slot: SLOT,
      content: `review [attached_dir 1] ${SPACED}`,
      ts: '2026-07-26T10:00:00.000Z',
      queue_id: 'q1',
      meta: { dirs: [SPACED] },
    }))
  }

  it('clears meta.dirs but keeps queueId', () => {
    const after = reducer(queued(), editQueuedMessage({
      slot: SLOT,
      queue_id: 'q1',
      content: 'never mind, just say hi',
    }))
    const card = after.messages.at(-1)!
    expect(card.content).toBe('never mind, just say hi')
    expect(card.meta?.dirs).toBeUndefined()
    // queueId is this card's identity for later edit/cancel lookups.
    expect(card.meta?.queueId).toBe('q1')
  })

  it('leaves a non-matching card untouched', () => {
    const after = reducer(queued(), editQueuedMessage({
      slot: SLOT,
      queue_id: 'nope',
      content: 'x',
    }))
    expect(after.messages.at(-1)!.meta?.dirs).toEqual([SPACED])
  })
})

describe('removeQueuedMessage carries metadata onto the drained message', () => {
  // Preserving meta on the CARD is pointless if the message it becomes drops it:
  // `queue_pop` is what runs the turn, and that is the bubble the user ends up
  // looking at. Without the ordered lists it falls back to the whitespace scan
  // and truncates a spaced path.
  function queued(meta?: Record<string, unknown>) {
    return reducer(base(), appendQueuedMessage({
      slot: SLOT,
      content: `review [attached_dir 1] ${SPACED}`,
      ts: '2026-07-26T10:00:00.000Z',
      queue_id: 'q1',
      ...(meta ? { meta } : {}),
    }))
  }

  it('keeps meta.dirs when the queued card is popped', () => {
    const after = reducer(queued({ dirs: [SPACED] }), removeQueuedMessage({
      slot: SLOT,
      content: `review [attached_dir 1] ${SPACED}`,
      queue_id: 'q1',
    }))
    const msg = after.messages.at(-1)!
    expect(msg.role).toBe('user')
    expect(msg.meta?.dirs).toEqual([SPACED])
  })

  it('drops queueId so a drained message cannot be matched as still queued', () => {
    const after = reducer(queued({ dirs: [SPACED] }), removeQueuedMessage({
      slot: SLOT,
      content: `review [attached_dir 1] ${SPACED}`,
      queue_id: 'q1',
    }))
    expect(after.messages.at(-1)!.meta?.queueId).toBeUndefined()
  })

  it('leaves meta absent when the card had none beyond queueId', () => {
    const after = reducer(queued(), removeQueuedMessage({
      slot: SLOT,
      content: `review [attached_dir 1] ${SPACED}`,
      queue_id: 'q1',
    }))
    expect(after.messages.at(-1)!.meta).toBeUndefined()
  })
})
