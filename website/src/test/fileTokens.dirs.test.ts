import { describe, it, expect } from 'vitest'
import { prepareSendPayload, buildDirRelMap, replaceDirTokens, resolveFileSegment, parseDirs, parseFiles, buildFileLabels, findUnreferencedDirs, stripAttachmentMarkers, trimDirSeparator } from '../utils/fileTokens'
describe('buildDirRelMap', () => {
  it('matches a token written with the inserted trailing slash', () => {
    const map = buildDirRelMap(['/repo/src/pages'], 'look at @src/pages/ please')
    expect(map.get('src/pages')).toBe('/repo/src/pages')
  })

  it('matches a token written without a trailing slash', () => {
    const map = buildDirRelMap(['/repo/src/pages'], 'look at @src/pages please')
    expect(map.get('src/pages')).toBe('/repo/src/pages')
  })

  it('matches a token at end of input', () => {
    const map = buildDirRelMap(['/repo/src/pages'], 'look at @src/pages/')
    expect(map.get('src/pages')).toBe('/repo/src/pages')
  })

  it('does not match a longer sibling path', () => {
    const map = buildDirRelMap(['/repo/src/page'], 'look at @src/pages/ please')
    expect(map.size).toBe(0)
  })
})

describe('replaceDirTokens', () => {
  it('replaces the token including its trailing slash', () => {
    const dirs = ['/repo/src/pages']
    const map = buildDirRelMap(dirs, 'in @src/pages/ now')
    const out = replaceDirTokens(map.size ? 'in @src/pages/ now' : '', dirs, map, p => `<${p}>`)
    expect(out).toBe('in </repo/src/pages> now')
  })
})

describe('trimDirSeparator', () => {
  it('drops a trailing POSIX separator', () => {
    expect(trimDirSeparator('/repo/src/')).toBe('/repo/src')
    expect(trimDirSeparator('/repo/src///')).toBe('/repo/src')
  })

  it('preserves a filesystem root instead of collapsing it to empty', () => {
    // A blanket /[/\\]+$/ strip turned '/' into '' and 'C:\' into 'C:', neither
    // of which is a usable path.
    expect(trimDirSeparator('/')).toBe('/')
    expect(trimDirSeparator('C:\\')).toBe('C:\\')
  })

  it('does not eat a POSIX directory name legally ending in a backslash', () => {
    // On POSIX a backslash is an ordinary filename character, not a separator.
    expect(trimDirSeparator('/repo/weird\\')).toBe('/repo/weird\\')
  })

  it('still treats a backslash as a separator on windows-style paths', () => {
    expect(trimDirSeparator('C:\\repo\\src\\')).toBe('C:\\repo\\src')
    expect(trimDirSeparator('repo\\src\\')).toBe('repo\\src')
  })
})

describe('prepareSendPayload with directories', () => {
  it('emits an attached_dir token for an inline folder mention', () => {
    const r = prepareSendPayload('review @src/pages/ for me', [], ['/repo/src/pages'])
    expect(r.txt).toBe('review [attached_dir 1] /repo/src/pages for me')
    expect(r.dirPaths).toEqual(['/repo/src/pages'])
  })

  it('appends an attached_dir token for an unreferenced folder', () => {
    const r = prepareSendPayload('take a look', [], ['/repo/src/pages'])
    expect(r.txt).toBe('take a look\n[attached_dir 1] /repo/src/pages')
  })

  it('never uses the attached_file marker for a directory', () => {
    const r = prepareSendPayload('review @src/pages/', [], ['/repo/src/pages'])
    expect(r.txt).not.toContain('attached_file')
  })

  it('prefers the file when a file and folder mention have equal rel length', () => {
    // `@notes` (file /repo/notes) and `@notes/` (dir /repo/notes) share rel
    // "notes". Directories are pushed into the candidate list first, so a
    // length-only sort let the dir win and the file was dropped entirely.
    const r = prepareSendPayload('open @notes', ['/repo/notes'], ['/repo/notes'])
    expect(r.txt).toContain('[attached_file 1] /repo/notes')
    expect(r.txt).not.toContain('attached_dir 1] /repo/notes for')
    expect(r.filePaths).toEqual(['/repo/notes'])
  })

  it('keeps the losing folder as an appended marker on an equal-length collision', () => {
    // `@notes` awards the position to the FILE, but the folder is still staged.
    // Filtering appended tokens on dirRelMap ("matched the text") instead of what
    // was actually emitted dropped the folder from both the inline text and the
    // appended block, so a staged attachment vanished with no warning.
    const r = prepareSendPayload('open @notes', ['/repo/notes'], ['/repo/notes'])
    expect(r.txt).toContain('[attached_file 1] /repo/notes')
    expect(r.txt, 'the losing folder must still be attached').toContain('[attached_dir 1] /repo/notes')
    expect(r.dirPaths).toEqual(['/repo/notes'])
  })

  it('does not double-emit a folder that did win its position', () => {
    const r = prepareSendPayload('open @notes/', [], ['/repo/notes'])
    expect(r.txt.match(/attached_dir 1\]/g)?.length).toBe(1)
  })

  it('still lets the folder win when its mention carries the separator', () => {
    const r = prepareSendPayload('open @notes/', ['/repo/notes'], ['/repo/notes'])
    expect(r.txt).toContain('[attached_dir 1] /repo/notes')
  })

  it('round-trips a cancelled card back to de-tokenized text', () => {
    // handleCancelQueued puts the card's serialized content back in the
    // composer. Without stripping, the raw marker showed on screen and was
    // re-serialized on resend, doubling it and truncating the spaced path.
    const serialized = 'review [attached_dir 1] /repo/my docs now'
    expect(stripAttachmentMarkers(serialized, { dirs: ['/repo/my docs'] })).toBe('review now')
  })

  it('leaves marker indices the metadata does not cover', () => {
    expect(
      stripAttachmentMarkers('a [attached_dir 1] /d b [attached_dir 2] /other', { dirs: ['/d'] }),
    ).toBe('a b [attached_dir 2] /other')
  })

  it('strips both families from one string', () => {
    expect(
      stripAttachmentMarkers('x [attached_file 1] /f.txt y [attached_dir 1] /d z', {
        files: ['/f.txt'],
        dirs: ['/d'],
      }),
    ).toBe('x y z')
  })

  it('widens labels until unique instead of stopping at two segments', () => {
    // `/a/x/docs` and `/b/x/docs` both reduced to `x/docs`, so two distinct
    // attachments rendered identically and one map key overwrote the other.
    const m = buildFileLabels(['/a/x/docs', '/b/x/docs'])
    expect(m.get('/a/x/docs')).not.toBe(m.get('/b/x/docs'))
    expect(new Set([...m.values()]).size).toBe(2)
  })

  it('does not widen labels that are already unique', () => {
    const m = buildFileLabels(['/repo/notes.txt', '/repo/other.txt'])
    expect(m.get('/repo/notes.txt')).toBe('notes.txt')
  })

  it('labels a POSIX path containing a backslash by its real basename', () => {
    // A backslash is a legal POSIX filename character, so `a\b` is ONE segment.
    const m = buildFileLabels(['/repo/a\\b'])
    expect(m.get('/repo/a\\b')).toBe('a\\b')
  })

  it('preserves indentation and column alignment outside the marker', () => {
    const code = 'def f():\n    x=1\n    [attached_dir 1] /d\n    y=2'
    expect(stripAttachmentMarkers(code, { dirs: ['/d'] })).toBe('def f():\n    x=1\n    y=2')
    expect(stripAttachmentMarkers('col1    col2 [attached_dir 1] /d', { dirs: ['/d'] })).toBe('col1    col2')
  })

  it('does not strip a longer path that shares the marker path as a prefix', () => {
    // meta owns `/d`; the text mentions `/dossier`. Without a boundary the `/d`
    // was stripped and left `ossier`.
    expect(
      stripAttachmentMarkers('see [attached_dir 1] /dossier now', { dirs: ['/d'] }),
    ).toBe('see [attached_dir 1] /dossier now')
  })

  it('resolves a mention against a POSIX path containing a backslash', () => {
    // `\` is a legal POSIX filename char, so `/repo/a\b` has ONE segment after
    // `/repo`. Treating the backslash as a separator let `@b` resolve to it
    // instead of `/other/b`. The referenced dir takes index 1.
    const r = prepareSendPayload('see @b', [], ['/repo/a\\b', '/other/b'])
    expect(r.txt).toContain('[attached_dir 1] /other/b')
    expect(r.txt, 'the backslash path must not win the @b mention').not.toMatch(
      /see \[attached_dir \d+\] \/repo\/a\\b/,
    )
  })

  it('ignores non-array meta lists instead of throwing', () => {
    // `meta` comes off a server payload, so the value may be any type. The cancel
    // path calls this synchronously in a click handler — a TypeError here took the
    // composer down instead of just failing to strip.
    for (const bad of ['oops', 42, { 0: 'x' }, null, undefined]) {
      expect(() =>
        stripAttachmentMarkers('keep me', { dirs: bad as never }),
      ).not.toThrow()
      expect(stripAttachmentMarkers('keep me', { dirs: bad as never })).toBe('keep me')
    }
  })

  it('skips non-string members without shifting the index space', () => {
    // Index 2 must still resolve to '/d' even though member 1 is unusable.
    expect(stripAttachmentMarkers('a [attached_dir 2] /d', { dirs: [null as never, '/d'] })).toBe('a')
  })

  it('keeps directories out of filePaths and imgPaths', () => {
    const r = prepareSendPayload('see @src/pages/', [], ['/repo/src/pages'])
    expect(r.filePaths).toEqual([])
    expect(r.imgPaths).toEqual([])
    expect(r.dirPaths).toEqual(['/repo/src/pages'])
  })

  it('normalizes a trailing slash on the incoming pending dir path', () => {
    const r = prepareSendPayload('see @src/pages/', [], ['/repo/src/pages/'])
    expect(r.dirPaths).toEqual(['/repo/src/pages'])
    expect(r.txt).toBe('see [attached_dir 1] /repo/src/pages')
  })

  it('deduplicates repeated pending dirs', () => {
    const r = prepareSendPayload('hi', [], ['/repo/src', '/repo/src/', '/repo/src'])
    expect(r.dirPaths).toEqual(['/repo/src'])
    expect(r.txt.match(/attached_dir/g)).toHaveLength(1)
  })

  it('numbers directories independently of files', () => {
    const r = prepareSendPayload(
      'compare @src/pages/ with @data.csv',
      ['/repo/data.csv'],
      ['/repo/src/pages'],
    )
    // Both start at 1: the two marker namespaces are separate.
    expect(r.txt).toContain('[attached_dir 1] /repo/src/pages')
    expect(r.txt).toContain('[attached_file 1] /repo/data.csv')
    expect(r.filePaths).toEqual(['/repo/data.csv'])
    expect(r.dirPaths).toEqual(['/repo/src/pages'])
  })

  it('numbers referenced dirs before unreferenced ones', () => {
    const r = prepareSendPayload(
      'only @src/pages/ is mentioned',
      [],
      ['/repo/docs', '/repo/src/pages'],
    )
    expect(r.dirPaths).toEqual(['/repo/src/pages', '/repo/docs'])
    expect(r.txt).toContain('[attached_dir 1] /repo/src/pages')
    expect(r.txt).toContain('[attached_dir 2] /repo/docs')
  })

  it('does not consume a file path that sits beneath a mentioned dir', () => {
    // The dir pass runs first, but "@src/pages/list.tsx" must resolve to the
    // FILE, not to the dir prefix plus stray text.
    const r = prepareSendPayload(
      'open @src/pages/list.tsx',
      ['/repo/src/pages/list.tsx'],
      ['/repo/src/pages'],
    )
    expect(r.txt).toContain('[attached_file 1] /repo/src/pages/list.tsx')
    // The dir was not mentioned on its own, so its token is appended.
    expect(r.txt).toContain('[attached_dir 1] /repo/src/pages')
    expect(r.txt).not.toContain('[attached_dir 1] /repo/src/pages/list.tsx')
  })

  it('handles a folder name containing spaces', () => {    const r = prepareSendPayload('check @my docs/', [], ['/repo/my docs'])
    expect(r.dirPaths).toEqual(['/repo/my docs'])
    expect(r.txt).toContain('[attached_dir 1] /repo/my docs')
  })

  it('places dir tokens after file tokens when both are unreferenced', () => {
    const r = prepareSendPayload('hello', ['/repo/data.csv'], ['/repo/src'])
    expect(r.txt).toBe('hello\n[attached_file 1] /repo/data.csv\n[attached_dir 1] /repo/src')
  })

  it('leaves displayTxt free of dir markers', () => {
    const r = prepareSendPayload('review @src/pages/ please', [], ['/repo/src/pages'])
    expect(r.displayTxt).toBe('review @src/pages/ please')
    expect(r.displayTxt).not.toContain('attached_dir')
  })

  it('is a no-op when no dirs are pending (back-compat with the 2-arg call)', () => {
    const withArg = prepareSendPayload('hello', ['/repo/data.csv'], [])
    const withoutArg = prepareSendPayload('hello', ['/repo/data.csv'])
    expect(withoutArg.txt).toBe(withArg.txt)
    expect(withoutArg.dirPaths).toEqual([])
    expect(withoutArg.txt).not.toContain('attached_dir')
  })

  it('coexists with an image attachment', () => {
    const r = prepareSendPayload('see @src/pages/', ['/repo/shot.png'], ['/repo/src/pages'])
    expect(r.txt).toContain('![image](/repo/shot.png)')
    expect(r.txt).toContain('[attached_dir 1] /repo/src/pages')
    expect(r.imgPaths).toEqual(['/repo/shot.png'])
    expect(r.dirPaths).toEqual(['/repo/src/pages'])
  })
})

describe('buildDirRelMap — Windows separators', () => {
  it('resolves an absolute-path mention (unstripped root)', () => {
    // If the root could not be stripped, the picker inserts the absolute path.
    // A suffix-only walk never matched it, so the raw mention survived and a
    // duplicate marker was appended.
    const map = buildDirRelMap(['C:\\repo\\src'], 'look at @C:\\repo\\src/ please')
    expect(map.get('C:\\repo\\src')).toBe('C:\\repo\\src')
  })

  it('prefers the short suffix over the full path when both could match', () => {
    const map = buildDirRelMap(['/repo/src/pages'], 'look at @src/pages/ please')
    expect(map.get('src/pages')).toBe('/repo/src/pages')
    expect(map.has('/repo/src/pages')).toBe(false)
  })

  it('serializes an absolute-path mention to exactly one marker', () => {
    const r = prepareSendPayload('review @C:\\repo\\src/ please', [], ['C:\\repo\\src'])
    expect(r.txt).toBe('review [attached_dir 1] C:\\repo\\src please')
    expect(r.txt.match(/attached_dir/g)).toHaveLength(1)
  })

  it('matches a backslash path mention', () => {
    // On native Windows the picker inserts the backslash path with a trailing
    // slash. Splitting on '/' alone yielded one segment, so no suffix matched
    // and the raw @-text survived alongside a duplicate marker.
    const map = buildDirRelMap(['C:\\repo\\src\\pages'], 'look at @src\\pages/ please')
    expect(map.get('src\\pages')).toBe('C:\\repo\\src\\pages')
  })

  it('matches a backslash mention with a trailing backslash', () => {
    const map = buildDirRelMap(['C:\\repo\\src\\pages'], 'look at @src\\pages\\ please')
    expect(map.get('src\\pages')).toBe('C:\\repo\\src\\pages')
  })

  it('serializes a backslash folder mention to exactly one marker', () => {
    const r = prepareSendPayload('review @src\\pages/ please', [], ['C:\\repo\\src\\pages'])
    expect(r.txt).toBe('review [attached_dir 1] C:\\repo\\src\\pages please')
    expect(r.txt.match(/attached_dir/g)).toHaveLength(1)
    expect(r.dirPaths).toEqual(['C:\\repo\\src\\pages'])
  })

  it('normalizes a trailing backslash before dedup', () => {
    const r = prepareSendPayload('', [], ['C:\\repo\\src', 'C:\\repo\\src\\'])
    expect(r.dirPaths).toEqual(['C:\\repo\\src'])
    expect(r.txt.match(/attached_dir/g)).toHaveLength(1)
  })
})

describe('resolveFileSegment — folder mentions are kept out of mentionMap', () => {
  it('routes an embedded folder token to dirMentionMap only', () => {
    // mentionMap entries render as clickable "Open file" chips; a directory has
    // nothing for the file viewer to open, so it must not land there.
    const r = resolveFileSegment('review [attached_dir 1] /repo/src/pages today', [], ['/repo/src/pages'])
    expect(r.mentionMap.size).toBe(0)
    expect(r.dirMentionMap.get('pages/')).toBe('/repo/src/pages')
  })

  it('keeps the two maps separate for a mixed message', () => {
    const r = resolveFileSegment(
      'see [attached_file 1] /repo/data.csv and [attached_dir 1] /repo/src/pages now',
      ['/repo/data.csv'],
      ['/repo/src/pages'],
    )
    expect(r.mentionMap.get('data.csv')).toBe('/repo/data.csv')
    expect(r.mentionMap.has('pages/')).toBe(false)
    expect(r.dirMentionMap.get('pages/')).toBe('/repo/src/pages')
  })

  it('routes an optimistic-form folder mention to dirMentionMap', () => {
    const r = resolveFileSegment('review @src/pages/ please', [], ['/repo/src/pages'])
    expect(r.mentionMap.size).toBe(0)
    expect(r.dirMentionMap.get('src/pages/')).toBe('/repo/src/pages')
  })
})

describe('parseDirs / parseFiles — untrusted meta', () => {
  it('falls back to the content scan when meta.dirs is a string', () => {
    // /api/chat accepts any dict as meta, so this shape is reachable. An
    // unvalidated cast reached buildFileLabels, where p.split() threw and the
    // whole message failed to render.
    const content = '[attached_dir 1] /repo/src'
    expect(parseDirs(content, { dirs: 'nope' } as Record<string, unknown>)).toEqual(['/repo/src'])
  })

  it('blanks non-string members of meta.dirs in place, preserving marker indices', () => {
    // Blanked, NOT dropped: marker N indexes this list positionally, so
    // filtering would shift later paths down a slot (see the spaced-path test
    // below for the user-visible consequence).
    const dirs = ['/repo/src', 42, null, '', '/repo/docs'] as unknown
    expect(parseDirs('', { dirs } as Record<string, unknown>)).toEqual(['/repo/src', '', '', '', '/repo/docs'])
  })

  it('resolves a spaced path whose marker follows an INVALID meta entry', () => {
    // Regression: with `.filter()`, the bad entry at index 0 shifted
    // '/repo/my docs' from slot 2 to slot 1, so `[attached_dir 2]` indexed past
    // the end, fell through to the whitespace-bounded scan, and truncated the
    // path at the space — rendering '/repo/my' and losing '/docs'.
    const content = 'check [attached_dir 2] /repo/my docs please'
    const dirs = parseDirs(content, { dirs: [42, '/repo/my docs'] } as unknown as Record<string, unknown>)
    expect(dirs).toEqual(['', '/repo/my docs'])
    const { display, dirMentionMap } = resolveFileSegment(content, [], dirs)
    // The full spaced path survives as a chip, not truncated at the space.
    expect([...dirMentionMap.values()]).toContain('/repo/my docs')
    expect(display).not.toContain('/repo/my docs')
    expect(display).not.toMatch(/\[attached_dir 2\]/)
  })

  it('does not surface a blank placeholder as an attachment card', () => {
    // A blank is an index-alignment placeholder, never a real path — it must not
    // reach findUnreferencedDirs and render an empty card.
    expect(findUnreferencedDirs('no markers here', ['', '/repo/docs'])).toEqual(['/repo/docs'])
  })

  it('falls back to the content scan when every meta.dirs member is invalid', () => {
    // All-blank carries no usable path, so `.length` must stay falsy for the
    // caller's fallback rather than yielding ['', ''].
    expect(parseDirs('[attached_dir 1] /repo/src', { dirs: [1, 2] } as unknown as Record<string, unknown>))
      .toEqual(['/repo/src'])
  })

  it('falls back when meta.dirs is an object', () => {
    expect(parseDirs('[attached_dir 1] /repo/src', { dirs: { a: 1 } } as Record<string, unknown>))
      .toEqual(['/repo/src'])
  })

  it('applies the same validation to meta.files', () => {
    expect(parseFiles('[attached_file 1] /repo/a.csv', { files: 'nope' } as Record<string, unknown>))
      .toEqual(['/repo/a.csv'])
    expect(parseFiles('', { files: ['/repo/a.csv', 7] } as unknown as Record<string, unknown>))
      .toEqual(['/repo/a.csv', ''])
  })

  it('resolveFileSegment survives malformed meta-derived lists', () => {
    const dirs = parseDirs('review [attached_dir 1] /repo/src/pages now', { dirs: 99 } as Record<string, unknown>)
    expect(() => resolveFileSegment('review [attached_dir 1] /repo/src/pages now', [], dirs)).not.toThrow()
  })
})

describe('buildFileLabels with Windows separators', () => {
  it('takes the basename of a backslash path', () => {
    // Splitting on `/` alone left the whole absolute path as the "basename", so
    // every chip and card displayed the full native Windows path.
    const map = buildFileLabels(['C:\\repo\\website\\docs'])
    expect(map.get('C:\\repo\\website\\docs')).toBe('docs')
  })

  it('disambiguates duplicate names across backslash paths', () => {
    const paths = ['C:\\repo\\docs', 'C:\\repo\\website\\docs']
    const map = buildFileLabels(paths)
    expect(map.get(paths[0])).toBe('repo/docs')
    expect(map.get(paths[1])).toBe('website/docs')
  })

  it('still handles POSIX paths unchanged', () => {
    const paths = ['/repo/docs', '/repo/website/docs', '/repo/a.txt']
    const map = buildFileLabels(paths)
    expect(map.get('/repo/docs')).toBe('repo/docs')
    expect(map.get('/repo/website/docs')).toBe('website/docs')
    expect(map.get('/repo/a.txt')).toBe('a.txt')
  })
})

describe('marker emission does not rescan its own output', () => {
  // Serialization ran as sequential passes over text that ALREADY contained
  // markers emitted by an earlier pass. A generated marker carries the absolute
  // path verbatim, so if that path happens to contain an `@mention` matching a
  // later pass's token, the later pass rewrites text inside the marker it should
  // never have looked at.
  it('leaves a folder marker intact when its path contains a later token', () => {
    // The dir pass emits `[attached_dir 1] /repo/@data.csv`. The file pass then
    // scans that output; `@data.csv` sits at end-of-string inside the emitted
    // path, which is exactly what tokenRegex matches.
    const dirs = ['/repo/@data.csv']
    const { txt, dirPaths } = prepareSendPayload('check @@data.csv/', [], dirs)
    expect(dirPaths).toEqual(['/repo/@data.csv'])
    expect(txt, 'the folder path must survive verbatim').toContain('[attached_dir 1] /repo/@data.csv')
    // A second marker for the same path means the emitted one got rewritten.
    expect(txt.match(/\[attached_dir 1\]/g) || []).toHaveLength(1)
    expect(txt, 'no file marker should appear — no file was attached').not.toContain('[attached_file')
  })

  it('does not let a file token rewrite the inside of a folder marker', () => {
    // The true rescan case. The dir pass emits
    //   `[attached_dir 1] /repo/@data.csv`
    // and that emitted text contains `@data.csv` — which is exactly the mention
    // for an UNRELATED attached file `/other/data.csv`. The later file pass
    // scanned the dir pass's output and substituted a file marker INSIDE the
    // folder marker. Note the two paths share no suffix, so this is not the
    // ambiguous-mention case: it is purely an artifact of re-scanning.
    const dirs = ['/repo/@data.csv']
    const files = ['/other/data.csv']
    const { txt } = prepareSendPayload('folder @@data.csv/ file @data.csv', files, dirs)
    expect(txt, 'the folder path must survive verbatim').toContain('[attached_dir 1] /repo/@data.csv')
    expect(txt, 'the file gets its own marker').toContain('[attached_file 1] /other/data.csv')
    // Exactly one of each — a nested/duplicated marker means the emitted output
    // was rescanned.
    expect(txt.match(/\[attached_dir /g) || []).toHaveLength(1)
    expect(txt.match(/\[attached_file /g) || []).toHaveLength(1)
  })

  it('resolves the longest mention first when a dir prefixes a file', () => {
    // `@src/pages/` (dir) is a prefix of `@src/pages/list.tsx` (file). The
    // scanner must prefer the longer candidate at a given position, or the file
    // mention would be half-consumed as a folder.
    const dirs = ['/repo/src/pages']
    const files = ['/repo/src/pages/list.tsx']
    const { txt } = prepareSendPayload('see @src/pages/ and @src/pages/list.tsx', files, dirs)
    expect(txt).toContain('[attached_dir 1] /repo/src/pages')
    expect(txt).toContain('[attached_file 1] /repo/src/pages/list.tsx')
    expect(txt.match(/\[attached_dir /g) || []).toHaveLength(1)
  })

  it('keeps an image path out of an already-emitted folder marker', () => {
    // Images are replaced with '' by their pass, so under the old multi-pass
    // order (dirs, then images rescanning the dir pass's OUTPUT) an image mention
    // occurring inside an emitted folder marker had its text deleted from inside
    // that marker — corrupting the path handed to the agent.
    //
    // Reproducing this needs the image's MENTION TEXT to appear in the dir path:
    // buildRelMap resolves the shortest suffix present in the text, so the image
    // /assets/hero.png resolves to `hero.png`, and the folder /shots/@hero.png
    // emits a marker containing `@hero.png` for the image pass to eat. An image
    // whose suffix does NOT occur in the dir path leaves both orderings
    // identical, which is why a mismatched pair proves nothing.
    const dirs = ['/shots/@hero.png']
    const files = ['/assets/hero.png']
    const { txt } = prepareSendPayload('look at @@hero.png/ and @hero.png', files, dirs)
    // The folder marker survives intact — nothing was excised from inside it.
    expect(txt).toContain('[attached_dir 1] /shots/@hero.png')
  })
})
