import { describe, it, expect } from 'vitest'
import { prepareSendPayload, buildFileLabels, resolveFileSegment, stripAttachmentMarkers, metaFileList, parseFilesChecked, mergeStagedFiles } from '../utils/fileTokens'

describe('stripAttachmentMarkers', () => {
  it('removes a marker and its path, closing the gap', () => {
    expect(
      stripAttachmentMarkers('look at [attached_file 1] /repo/my notes.txt please', ['/repo/my notes.txt']),
    ).toBe('look at please')
  })

  it('handles a path containing spaces', () => {
    // The whole point of the ordered list: a whitespace-bounded scan would stop
    // at the first space and leave `notes.txt` behind.
    expect(
      stripAttachmentMarkers('[attached_file 1] /a/quarterly report.pdf', ['/a/quarterly report.pdf']),
    ).toBe('')
  })

  it('does not strip a longer path that shares the marker path as a prefix', () => {
    // Without an end boundary, `/d` matched inside `/dossier` and left `ossier`.
    expect(
      stripAttachmentMarkers('see [attached_file 1] /dossier now', ['/d']),
    ).toBe('see [attached_file 1] /dossier now')
  })

  it('preserves indentation and column alignment away from the marker', () => {
    const code = 'def f():\n    x=1\n    [attached_file 1] /d\n    y=2'
    expect(stripAttachmentMarkers(code, ['/d'])).toBe('def f():\n    x=1\n    y=2')
    expect(stripAttachmentMarkers('col1    col2 [attached_file 1] /d', ['/d'])).toBe('col1    col2')
  })

  it('leaves marker indices the list does not cover', () => {
    expect(
      stripAttachmentMarkers('a [attached_file 1] /d b [attached_file 2] /other', ['/d']),
    ).toBe('a b [attached_file 2] /other')
  })

  it('ignores a non-array files value instead of throwing', () => {
    for (const bad of ['oops', 42, { 0: 'x' }, null, undefined]) {
      expect(() => stripAttachmentMarkers('keep me', bad)).not.toThrow()
      expect(stripAttachmentMarkers('keep me', bad)).toBe('keep me')
    }
  })

  it('keeps the index space when a member is not a string', () => {
    expect(metaFileList([null, '/d'])).toEqual(['', '/d'])
    expect(stripAttachmentMarkers('a [attached_file 2] /d', [null, '/d'])).toBe('a')
  })
})

describe('buildFileLabels uniqueness', () => {
  it('disambiguates paths that share a basename', () => {
    const m = buildFileLabels(['/q3/report.docx', '/q4/report.docx'])
    expect(m.get('/q3/report.docx')).toBe('q3/report.docx')
    expect(m.get('/q4/report.docx')).toBe('q4/report.docx')
  })

  it('widens past two segments when the last two also collide', () => {
    // Regression: both collapsed to `x/report.docx`, so two distinct
    // attachments got the same chip label and the same mentionMap key.
    const m = buildFileLabels(['/a/x/report.docx', '/b/x/report.docx'])
    expect(new Set([...m.values()]).size, 'labels must be distinct').toBe(2)
    expect(m.get('/a/x/report.docx')).not.toBe(m.get('/b/x/report.docx'))
  })

  it('leaves an already-unique basename alone', () => {
    const m = buildFileLabels(['/repo/notes.txt', '/repo/other.txt'])
    expect(m.get('/repo/notes.txt')).toBe('notes.txt')
    expect(m.get('/repo/other.txt')).toBe('other.txt')
  })

  it('keeps a colliding mention resolvable to its own path', () => {
    // The label is also the mentionMap key, so a collision made one path
    // unreachable from its own chip.
    const content = '[attached_file 1] /a/x/report.docx\n[attached_file 2] /b/x/report.docx'
    const r = resolveFileSegment(content, ['/a/x/report.docx', '/b/x/report.docx'])
    const targets = new Set([...r.mentionMap.values(), ...r.cardPaths])
    expect(targets.has('/a/x/report.docx')).toBe(true)
    expect(targets.has('/b/x/report.docx')).toBe(true)
  })
})

describe('prepareSendPayload', () => {
  it('includes non-image files without @-mention', () => {
    const result = prepareSendPayload('hello', ['/tmp/data.csv'])
    expect(result.txt).toContain('[attached_file 1]')
    expect(result.txt).toContain('/tmp/data.csv')
    expect(result.filePaths).toEqual(['/tmp/data.csv'])
  })

  it('includes image files as markdown', () => {
    const result = prepareSendPayload('check this', ['/tmp/photo.png'])
    expect(result.txt).toContain('![image](/tmp/photo.png)')
    expect(result.imgPaths).toEqual(['/tmp/photo.png'])
  })

  it('separates the image from the message text with a blank line in displayTxt', () => {
    const result = prepareSendPayload('my caption', ['/tmp/photo.png'])
    // Blank line (Markdown paragraph break) so the image renders in its own
    // block and the text drops to the next line, not inline after the image.
    expect(result.displayTxt).toBe('![image](/tmp/photo.png)\n\nmy caption')
    expect(result.displayTxt).toContain('![image](/tmp/photo.png)\n\nmy caption')
  })

  it('emits image-only displayTxt with no trailing separator when text is empty', () => {
    const result = prepareSendPayload('', ['/tmp/photo.png'])
    expect(result.displayTxt).toBe('![image](/tmp/photo.png)')
  })

  it('separates the image from the message text with a blank line in txt (LLM-facing)', () => {
    const result = prepareSendPayload('my caption', ['/tmp/photo.png'])
    // The blank-line separation is persisted in the LLM-facing `txt`, not just
    // the optimistic displayTxt, so the image renders in its own block on every
    // surface that replays stored content (dashboard re-render after a turn,
    // gateway restart, Slack replay, exports) — the original bug was the
    // single-'\n' persisted content collapsing image + caption onto one line.
    expect(result.txt).toBe('![image](/tmp/photo.png)\n\nmy caption')
  })

  it('emits image-only txt with no trailing separator when text is empty', () => {
    const result = prepareSendPayload('', ['/tmp/photo.png'])
    expect(result.txt).toBe('![image](/tmp/photo.png)')
  })

  it('separates image from caption with a blank line but keeps single newline to appended file tokens', () => {
    const result = prepareSendPayload('my caption', ['/tmp/photo.png', '/tmp/data.csv'])
    // image block -> blank line -> caption -> single newline -> [attached_file].
    expect(result.txt).toBe(
      '![image](/tmp/photo.png)\n\nmy caption\n[attached_file 1] /tmp/data.csv',
    )
  })

  it('includes mixed image and non-image files', () => {
    const result = prepareSendPayload('here', ['/tmp/a.png', '/tmp/b.zip'])
    expect(result.imgPaths).toEqual(['/tmp/a.png'])
    expect(result.filePaths).toEqual(['/tmp/b.zip'])
    expect(result.txt).toContain('![image]')
    expect(result.txt).toContain('[attached_file')
    expect(result.displayTxt).not.toContain('[attached_file')
    expect(result.displayTxt).toContain('![image]')
  })

  it('includes @-referenced files inline and unreferenced as appended tokens', () => {
    const result = prepareSendPayload(
      'see @data.csv for details',
      ['/tmp/data.csv', '/tmp/extra.log'],
    )
    expect(result.filePaths).toContain('/tmp/data.csv')
    expect(result.filePaths).toContain('/tmp/extra.log')
    expect(result.txt).toContain('/tmp/extra.log')
    expect(result.displayTxt).not.toContain('[attached_file')
    expect(result.displayTxt).not.toContain('/tmp/extra.log')
  })

  it('returns empty filePaths when no files pending', () => {
    const result = prepareSendPayload('just text', [])
    expect(result.filePaths).toEqual([])
    expect(result.imgPaths).toEqual([])
  })

  it('replaces @-referenced token inline in txt', () => {
    const result = prepareSendPayload('see @data.csv', ['/tmp/data.csv'])
    expect(result.txt).toContain('[attached_file 1] /tmp/data.csv')
    expect(result.txt).not.toContain('@data.csv')
  })

  it('deduplicates when same file appears twice', () => {
    const result = prepareSendPayload('hello', ['/tmp/a.csv', '/tmp/a.csv'])
    expect(result.filePaths).toEqual(['/tmp/a.csv'])
    expect(result.txt).toContain('[attached_file 1] /tmp/a.csv')
  })

  it('assigns unique token numbers when @-ref is not the first file', () => {
    const result = prepareSendPayload(
      'see @data.csv',
      ['/tmp/extra.log', '/tmp/data.csv'],
    )
    const indices = [...result.txt.matchAll(/\[attached_file (\d+)\]/g)].map(m => m[1])
    expect(indices.length).toBe(2)
    expect(new Set(indices).size).toBe(indices.length)
    expect(result.txt).toContain('[attached_file 1] /tmp/data.csv')
    expect(result.txt).toContain('[attached_file 2] /tmp/extra.log')
    expect(result.filePaths).toEqual(['/tmp/data.csv', '/tmp/extra.log'])
  })
})

describe('parseFilesChecked', () => {
  it('trusts real meta.files', () => {
    const r = parseFilesChecked('see [attached_file 1] /a/q report.pdf', {
      files: ['/a/q report.pdf'],
    })
    expect(r).toEqual({ files: ['/a/q report.pdf'], exact: true })
  })

  it('refuses the content fallback, which cannot recover spaced paths', () => {
    // The `\S+` scan truncates at the first space. Feeding that to
    // stripAttachmentMarkers half-strips the marker (leaving `report.pdf`) AND
    // stages a phantom `/a/quarterly` attachment, so callers must not mutate.
    const r = parseFilesChecked('see [attached_file 1] /a/quarterly report.pdf ok')
    expect(r.exact, 'the content fallback is never trustworthy').toBe(false)
    expect(r.files).toEqual(['/a/quarterly'])
  })

  it('refuses an all-invalid meta array instead of trusting it', () => {
    // `metaFileList` blanks invalid members in place to keep the index space
    // aligned, so `[null]` still has length 1. Trusting that let the cancel path
    // clear unrelated staged files while stripping nothing.
    for (const bad of [[null], [null, null], ['', '']]) {
      const r = parseFilesChecked('see [attached_file 1] /a/b.txt', { files: bad })
      expect(r.exact, `all-invalid meta ${JSON.stringify(bad)} must not be exact`).toBe(false)
    }
    // One valid member is enough to trust the ordered list.
    expect(parseFilesChecked('see [attached_file 1] /a/b.txt', { files: [null, '/a/b.txt'] }).exact).toBe(true)
  })

  it('refuses the fallback even when no path contains a space', () => {
    // A truncated path is indistinguishable from a complete one followed by
    // prose, so there is no safe heuristic -- the fallback is always inexact.
    expect(parseFilesChecked('see [attached_file 1] /a/b.txt').exact).toBe(false)
  })
})

describe('mergeStagedFiles', () => {
  it('keeps the first list order and appends the newer additions', () => {
    // Used when re-filling the composer after an uncommitted send: the payload
    // snapshot comes first (it is the order the user assembled), then anything
    // staged while the send was in flight.
    expect(mergeStagedFiles(['/a.png', '/b.png'], ['/a.png', '/c.png']))
      .toEqual(['/a.png', '/b.png', '/c.png'])
  })

  it('de-duplicates rather than double-staging a path present in both', () => {
    expect(mergeStagedFiles(['/a.png'], ['/a.png'])).toEqual(['/a.png'])
  })

  it('drops falsy members', () => {
    expect(mergeStagedFiles(['', '/a.png'], ['/b.png', ''])).toEqual(['/a.png', '/b.png'])
  })

  it('handles either side being empty', () => {
    expect(mergeStagedFiles([], ['/a.png'])).toEqual(['/a.png'])
    expect(mergeStagedFiles(['/a.png'], [])).toEqual(['/a.png'])
    expect(mergeStagedFiles([], [])).toEqual([])
  })
})

