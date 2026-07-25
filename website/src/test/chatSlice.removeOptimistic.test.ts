/**
 * Tests for removeOptimisticMessage — rollback of an optimistic user bubble
 * whose write was rejected.
 *
 * A mid-turn steer renders its bubble immediately and POSTs fire-and-forget. If
 * the POST fails, that bubble is a lie: it claims the agent received text and
 * attachments it never got. Rollback must be surgical — it may only ever remove
 * a still-unconfirmed bubble, never one the server has already echoed (the
 * steer echo reconciles in place and drops the `optimistic` marker), and it must
 * reach the per-slot list because a steer can target a background slot.
 */
import { describe, it, expect } from 'vitest'
import reducer, { removeOptimisticMessage } from '../store/chatSlice'
import type { ChatMessage } from '../types'

const SLOT = 'slot-a'
const TS = '2026-07-26T09:00:00.000Z'

function stateWith(messages: ChatMessage[], slotMessages?: ChatMessage[]) {
  const base = reducer(undefined, { type: '@@INIT' })
  return {
    ...base,
    activeSlot: SLOT,
    messages,
    slotMessages: slotMessages ? { [SLOT]: slotMessages } : base.slotMessages,
  }
}

const optimistic: ChatMessage = {
  role: 'user', content: 'steered text', cls: 'msg msg-u', ts: TS,
  meta: { steer: true, optimistic: true, dirs: ['/repo/my docs'] },
}

describe('removeOptimisticMessage', () => {
  it('removes the optimistic bubble matching the ts', () => {
    const next = reducer(stateWith([optimistic]), removeOptimisticMessage({ slot: SLOT, ts: TS }))
    expect(next.messages).toHaveLength(0)
  })

  it('also removes it from the per-slot list (steer into a background slot)', () => {
    const next = reducer(stateWith([], [optimistic]), removeOptimisticMessage({ slot: SLOT, ts: TS }))
    expect(next.slotMessages[SLOT]).toHaveLength(0)
  })

  it('leaves a CONFIRMED message with the same ts untouched', () => {
    // The steer echo reconciles the bubble in place and clears `optimistic`.
    // A late-arriving rejection must not then delete the real, server-persisted
    // message — that would silently erase history the agent actually received.
    const confirmed: ChatMessage = { ...optimistic, meta: { steer: true, dirs: ['/repo/my docs'] } }
    const next = reducer(stateWith([confirmed]), removeOptimisticMessage({ slot: SLOT, ts: TS }))
    expect(next.messages).toHaveLength(1)
  })

  it('leaves other optimistic messages with a different ts untouched', () => {
    const other: ChatMessage = { ...optimistic, ts: '2026-07-26T09:00:01.000Z', content: 'a later steer' }
    const next = reducer(stateWith([optimistic, other]), removeOptimisticMessage({ slot: SLOT, ts: TS }))
    expect(next.messages).toHaveLength(1)
    expect(next.messages[0].content).toBe('a later steer')
  })

  it('is a no-op for an unknown slot', () => {
    const next = reducer(stateWith([], [optimistic]), removeOptimisticMessage({ slot: 'nope', ts: TS }))
    expect(next.slotMessages[SLOT]).toHaveLength(1)
  })
})
