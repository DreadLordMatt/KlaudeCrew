import { describe, it, expect } from 'vitest'
import { computeActiveSubtree, countRunningSubtree, folderIsHidden, folderOffersHide } from '../utils/folderVisibility'
import type { ChatFolder } from '../types'

const folders: ChatFolder[] = [
  { id: 'root', name: 'Root', order: 0, parent_id: '' },
  { id: 'child', name: 'Child', order: 1, parent_id: 'root' },
  { id: 'grandchild', name: 'Grandchild', order: 2, parent_id: 'child' },
  { id: 'other', name: 'Other', order: 3, parent_id: '' },
]

describe('computeActiveSubtree', () => {
  it('returns empty set when no folders hold slots', () => {
    expect(computeActiveSubtree(folders, []).size).toBe(0)
  })

  it('includes a folder with a direct slot', () => {
    expect([...computeActiveSubtree(folders, ['other'])]).toEqual(['other'])
  })

  it('propagates active membership up the whole ancestor chain', () => {
    const active = computeActiveSubtree(folders, ['grandchild'])
    expect(active.has('grandchild')).toBe(true)
    expect(active.has('child')).toBe(true)
    expect(active.has('root')).toBe(true)
    expect(active.has('other')).toBe(false)
  })

  it('does not loop or duplicate on shared ancestors', () => {
    const active = computeActiveSubtree(folders, ['grandchild', 'child'])
    expect([...active].sort()).toEqual(['child', 'grandchild', 'root'])
  })
})

describe('folderIsHidden', () => {
  const active = computeActiveSubtree(folders, ['other'])

  it('hides a hidden folder with no active session', () => {
    expect(folderIsHidden({ ...folders[0], hidden: true }, active)).toBe(true)
  })

  it('keeps a hidden folder visible while its subtree has an active session', () => {
    expect(folderIsHidden({ ...folders[3], hidden: true }, active)).toBe(false)
  })

  it('never hides a folder that was not hidden', () => {
    expect(folderIsHidden({ ...folders[0], hidden: false }, active)).toBe(false)
    expect(folderIsHidden(folders[0], active)).toBe(false)
  })
})

describe('folderOffersHide', () => {
  const active = computeActiveSubtree(folders, ['other'])

  it('offers hide for an empty folder that still has archived sessions (A=0, H>0)', () => {
    expect(folderOffersHide({ ...folders[0], history_count: 2 }, active)).toBe(true)
  })

  it('does not offer hide while the subtree has an active session (A>0)', () => {
    // `other` is in the active set; even with archived sessions, no hide is offered.
    expect(folderOffersHide({ ...folders[3], history_count: 5 }, active)).toBe(false)
  })

  it('offers Delete only for a truly empty folder — no active, no history (A=0, H=0)', () => {
    expect(folderOffersHide({ ...folders[0], history_count: 0 }, active)).toBe(false)
    // history_count absent is treated as 0.
    expect(folderOffersHide(folders[0], active)).toBe(false)
  })
})

describe('countRunningSubtree', () => {
  it('counts nothing when no slot is running', () => {
    expect(countRunningSubtree(folders, []).size).toBe(0)
  })

  it('counts a running slot for its own folder and every ancestor', () => {
    const counts = countRunningSubtree(folders, ['grandchild'])
    expect(counts.get('grandchild')).toBe(1)
    expect(counts.get('child')).toBe(1)
    expect(counts.get('root')).toBe(1)
    // A sibling subtree stays untouched.
    expect(counts.get('other')).toBeUndefined()
  })

  it('sums a subtree rather than collapsing it to a boolean', () => {
    // Two running slots in the same folder plus one in its parent: the root's
    // number is the total for the whole subtree, not "1 = something runs".
    // This is the behaviour computeActiveSubtree cannot express — it would
    // short-circuit on the second walk as soon as it saw `root` again.
    const counts = countRunningSubtree(folders, ['grandchild', 'grandchild', 'child'])
    expect(counts.get('grandchild')).toBe(2)
    expect(counts.get('child')).toBe(3)
    expect(counts.get('root')).toBe(3)
  })

  it('ignores a folder id that is not in the tree', () => {
    const counts = countRunningSubtree(folders, ['ghost'])
    // The unknown id still counts for itself (it has no parent to walk to), but
    // it must not fabricate a count on any real folder.
    expect(counts.get('ghost')).toBe(1)
    expect(counts.get('root')).toBeUndefined()
  })

  it('terminates on a cyclic parent chain and counts each folder once', () => {
    // Corrupt data: a→b→a. Without the per-slot `seen` guard this loops forever.
    const cyclic: ChatFolder[] = [
      { id: 'a', name: 'A', order: 0, parent_id: 'b' },
      { id: 'b', name: 'B', order: 1, parent_id: 'a' },
    ]
    const counts = countRunningSubtree(cyclic, ['a'])
    expect(counts.get('a')).toBe(1)
    expect(counts.get('b')).toBe(1)
  })
})
