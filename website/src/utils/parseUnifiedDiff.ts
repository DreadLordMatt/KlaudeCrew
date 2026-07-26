/** Parse a unified diff patch into renderable rows with line numbers.
 *
 * Produces GitHub-style rows: context/add/del lines carry old/new line
 * numbers (null on the side that doesn't exist), and `hunk-gap` rows mark
 * the unmodified stretches between hunks with how many lines were skipped.
 */

export type DiffRow =
  | { kind: 'hunk-gap'; hiddenCount: number }
  | { kind: 'context' | 'add' | 'del'; oldLine: number | null; newLine: number | null; text: string }

const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/

/** Yield lines without materializing the whole split array.
 *
 * `patch.split('\n')` on a 2MB newline-dense patch allocates hundreds of
 * thousands of strings up front, which defeats any cap applied afterwards.
 * Walking with indexOf lets a bounded parse stop early and allocate only the
 * lines it actually consumes. */
function* iterLines(patch: string): Generator<string> {
  let start = 0
  for (;;) {
    const nl = patch.indexOf('\n', start)
    if (nl === -1) {
      if (start < patch.length) yield patch.slice(start)
      return
    }
    yield patch.slice(start, nl)
    start = nl + 1
  }
}

/** Parse at most `maxRows` rows (unbounded when omitted). */
export function parseUnifiedDiff(patch: string, maxRows?: number): DiffRow[] {
  const rows: DiffRow[] = []
  if (!patch) return rows
  let oldLine = 0
  let newLine = 0
  // Old-file line number just past the previous hunk, for gap sizing.
  let prevHunkOldEnd: number | null = null

  for (const line of iterLines(patch)) {
    if (maxRows !== undefined && rows.length >= maxRows) break
    const hunk = line.match(HUNK_RE)
    if (hunk) {
      const oldStart = Number(hunk[1])
      if (prevHunkOldEnd === null) {
        if (oldStart > 1) rows.push({ kind: 'hunk-gap', hiddenCount: oldStart - 1 })
      } else {
        rows.push({ kind: 'hunk-gap', hiddenCount: Math.max(oldStart - prevHunkOldEnd, 0) })
      }
      oldLine = oldStart
      newLine = Number(hunk[3])
      prevHunkOldEnd = null
      continue
    }
    if (line.startsWith('\\')) continue // "\ No newline at end of file"
    if (oldLine === 0 && newLine === 0) continue // preamble before any hunk
    if (line.startsWith('+')) {
      rows.push({ kind: 'add', oldLine: null, newLine, text: line.slice(1) })
      newLine += 1
    } else if (line.startsWith('-')) {
      rows.push({ kind: 'del', oldLine, newLine: null, text: line.slice(1) })
      oldLine += 1
    } else {
      // Context lines are prefixed with a space; a bare empty string is an
      // empty context line.
      rows.push({ kind: 'context', oldLine, newLine, text: line.slice(1) })
      oldLine += 1
      newLine += 1
    }
    prevHunkOldEnd = oldLine
  }
  // NOTE: no trailing-row cleanup. `split('\n')` used to yield a phantom ''
  // element for a patch ending in a newline, which was popped here; `iterLines`
  // does not emit it, so popping would delete a GENUINE final blank context
  // line instead.
  return rows
}

/** Parse for rendering: at most `maxRows` rows, plus whether more existed.
 *
 * Parses one row past the limit so `truncated` is exact — a patch with exactly
 * `maxRows` rows is complete, not capped — while still never walking the whole
 * patch. */
export function parseUnifiedDiffCapped(
  patch: string,
  maxRows: number,
): { rows: DiffRow[]; truncated: boolean } {
  const rows = parseUnifiedDiff(patch, maxRows + 1)
  if (rows.length > maxRows) return { rows: rows.slice(0, maxRows), truncated: true }
  return { rows, truncated: false }
}
