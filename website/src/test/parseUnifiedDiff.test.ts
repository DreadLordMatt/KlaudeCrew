import { describe, expect, it } from 'vitest'
import { parseUnifiedDiff, parseUnifiedDiffCapped } from '../utils/parseUnifiedDiff'

describe('parseUnifiedDiff', () => {
  it('numbers add, del, and context rows from the hunk header', () => {
    const rows = parseUnifiedDiff('@@ -1,3 +1,3 @@\n context\n-old\n+new\n')
    expect(rows).toEqual([
      { kind: 'context', oldLine: 1, newLine: 1, text: 'context' },
      { kind: 'del', oldLine: 2, newLine: null, text: 'old' },
      { kind: 'add', oldLine: null, newLine: 2, text: 'new' },
    ])
  })

  it('emits a leading gap when the first hunk starts past line 1', () => {
    const rows = parseUnifiedDiff('@@ -10,2 +10,2 @@\n a\n b\n')
    expect(rows[0]).toEqual({ kind: 'hunk-gap', hiddenCount: 9 })
  })

  it('sizes gaps between hunks from old-file line numbers', () => {
    const rows = parseUnifiedDiff('@@ -1,2 +1,2 @@\n a\n b\n@@ -150,2 +150,2 @@\n c\n d\n')
    const gap = rows.find(row => row.kind === 'hunk-gap')
    expect(gap).toEqual({ kind: 'hunk-gap', hiddenCount: 147 })
  })

  it('skips no-newline markers', () => {
    const rows = parseUnifiedDiff('@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n')
    expect(rows.map(row => row.kind)).toEqual(['del', 'add'])
  })

  it('returns no rows for an empty patch', () => {
    expect(parseUnifiedDiff('')).toEqual([])
  })

  it('preserves a genuine trailing blank context line', () => {
    const rows = parseUnifiedDiff('@@ -1,2 +1,2 @@\n-old\n+new\n \n')
    expect(rows[rows.length - 1]).toEqual({ kind: 'context', oldLine: 2, newLine: 2, text: '' })
  })
})

describe('parseUnifiedDiffCapped', () => {
  const bigPatch = '@@ -0,0 +1,50 @@\n' + Array.from({ length: 50 }, (_, i) => `+l${i}`).join('\n')

  it('caps rows and reports truncation', () => {
    const { rows, truncated } = parseUnifiedDiffCapped(bigPatch, 10)
    expect(rows).toHaveLength(10)
    expect(truncated).toBe(true)
  })

  it('does not report truncation for an exactly-cap-sized patch', () => {
    const { rows, truncated } = parseUnifiedDiffCapped(bigPatch, 50)
    expect(rows).toHaveLength(50)
    expect(truncated).toBe(false)
  })

  it('matches the uncapped parse for small patches', () => {
    const patch = '@@ -1,2 +1,2 @@\n context\n-old\n+new\n'
    expect(parseUnifiedDiffCapped(patch, 100)).toEqual({ rows: parseUnifiedDiff(patch), truncated: false })
  })
})
