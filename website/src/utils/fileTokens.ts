/** Shared file-token utilities used by send() and renderUserContent(). */

export const IMG_EXT = /\.(png|jpe?g|gif|webp|bmp|svg)$/i

/** Escape a literal string for safe embedding in a RegExp. */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Boundary-aware regex for @token matching. Prevents `@foo.ts` from matching inside `@foo.tsx`. */
function tokenRegex(token: string, flags = ''): RegExp {
  const escaped = escapeRegExp(token)
  return new RegExp(`@${escaped}(?=\\s|$)`, flags)
}

/**
 * Coerce an untrusted `meta` attachment list to `string[]`.
 *
 * `/api/chat` accepts any dictionary as message metadata, so a persisted
 * `meta.files` / `meta.dirs` can be a string, or an array holding non-strings.
 * A bare `as string[]` cast performed no runtime check, and replay then handed
 * that value to buildFileLabels, where `p.split()` threw and the whole message
 * failed to render.
 *
 * Invalid members are BLANKED IN PLACE (empty string), never dropped: marker
 * number N indexes this list positionally, so filtering a bad entry out would
 * shift every later path down one slot and `[attached_dir 2] /repo/my docs`
 * would resolve against the wrong element — falling through to the
 * whitespace-bounded scan that truncates at the space, the exact bug the
 * ordered lists exist to prevent. A blank placeholder keeps later indices
 * aligned and simply fails the `startsWith` check for its own marker, which
 * degrades to the scan for that one entry alone. A wholly invalid value yields
 * `[]`, which falls through to the content-scan fallback.
 */
export function metaPathList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  const out = value.map(p => (typeof p === 'string' ? p : ''))
  // All-blank is indistinguishable from "no usable metadata" — let the caller's
  // `.length` check fall through to the content scan rather than handing back a
  // list of empty strings that can never match a marker.
  return out.some(p => p.length > 0) ? out : []
}

/** Parse file paths from message meta or [attached_file N] patterns in content. */
export function parseFiles(content: string, meta?: Record<string, unknown>): string[] {
  const metaFiles = metaPathList(meta?.files)
  return metaFiles.length
    ? metaFiles
    : (content.match(/\[attached_file \d+\] (\S+)/g) || []).map(s => s.replace(/\[attached_file \d+\] /, ''))
}

/** Split a path into segments, treating `\` as a separator only for
 *  Windows-style paths. On POSIX a backslash is a legal filename character, so
 *  splitting on it unconditionally mislabeled `/repo/a\b` as `b` in chips,
 *  cards and generated titles. */
export function pathSegments(p: string): string[] {
  const windowsish = /^[a-zA-Z]:[/\\]/.test(p) || (p.includes('\\') && !p.includes('/'))
  return p.split(windowsish ? /[/\\]/ : /\//)
}

/** Build display labels for attachment chips -- basename, extended leftwards
 *  when two paths share a basename (e.g. two `report.docx` in different dirs). */
export function buildFileLabels(paths: string[]): Map<string, string> {
  const seg = pathSegments
  const map = new Map<string, string>()
  // Widen each label leftwards until it is unique among the whole set. Stopping
  // at exactly two segments left genuine collisions: `/a/x/docs` and `/b/x/docs`
  // both labelled `x/docs`, so two distinct attachments rendered identically and
  // the map key for one silently overwrote the other's path — the chip pointed at
  // the wrong directory. Depth grows only as far as the collision requires.
  const partsOf = new Map(paths.map(p => [p, seg(p)]))
  const labelAt = (p: string, depth: number) => {
    const parts = partsOf.get(p) ?? [p]
    return parts.slice(Math.max(0, parts.length - depth)).join('/') || p
  }
  const maxDepth = Math.max(1, ...paths.map(p => (partsOf.get(p) ?? []).length))
  for (const p of paths) {
    let depth = 1
    while (depth < maxDepth) {
      const mine = labelAt(p, depth)
      const clashes = paths.some(q => q !== p && labelAt(q, depth) === mine)
      if (!clashes) break
      depth += 1
    }
    map.set(p, labelAt(p, depth))
  }
  return map
}

export interface ResolvedFileSegment {
  /** Display text with every attachment reference normalized to an `@label` token (embedded) or stripped (standalone). */
  display: string
  /** `@label` (without the leading @) -> full path, for files referenced inline IN THIS content. */
  mentionMap: Map<string, string>
  /** `@label/` (without the leading @) -> full path, for FOLDERS referenced inline IN THIS content. Kept apart from `mentionMap` because a folder chip must not be clickable: there is nothing for the file viewer to open. */
  dirMentionMap: Map<string, string>
  /** Standalone-upload paths whose token appears IN THIS content — render as cards. Does NOT include files that are absent from this content (the caller decides those at message level, to avoid per-segment duplication). */
  cardPaths: string[]
  /** Standalone directory references whose `[attached_dir N]` token appears IN THIS content — render as folder cards. */
  dirCardPaths: string[]
  /** Display label per path (basename, disambiguated). */
  labels: Map<string, string>
}

/**
 * Normalize a user-message text segment for rendering attachments consistently.
 *
 * Single source of truth for how attachment references become display. Both a
 * file the user wove into a sentence (an @-mention) and a bare upload serialize
 * to the SAME `[attached_file N] /path` plumbing in the persisted message, and
 * the server stores that token form in `content` while ALSO keeping
 * `meta.files` — so we cannot branch on `meta.files`, and the token itself does
 * not say which it was. The distinguishing signal is POSITION:
 *
 *   - A token embedded in a line with other text -> inline `@label` chip.
 *   - A token alone on its line -> standalone upload, stripped from the text and
 *     returned in `cardPaths` for the caller to render as a block card.
 * Path resolution is LOSSLESS: the token's number N is the 1-based index into
 * `orderedFiles`, so `orderedFiles[N-1]` recovers a path even when it contains
 * spaces (the serialized `[attached_file N] path` form is not whitespace-
 * delimited) AND even when earlier attachments are images (N indexes the
 * ORIGINAL list, so an image preceding a spaced-filename document still
 * resolves correctly). The whitespace-bounded `\S+` capture is used only as a
 * fallback when N is out of range (e.g. no-meta history replay where
 * `orderedFiles` was itself parsed from the tokens).
 *
 * SEGMENT-SCOPED: `cardPaths` contains ONLY standalone uploads whose token is
 * present in this `content`. Files in `orderedFiles` that are not referenced
 * here at all are NOT emitted — a message split into multiple segments (paste
 * tokens) would otherwise re-emit every unreferenced attachment in every
 * segment. The caller renders truly-unreferenced attachments exactly once at
 * message level via findUnreferencedAttachments.
 *
 * `orderedFiles` is the ORIGINAL ordered attachment list (as persisted / as
 * `meta.files`, IMAGES INCLUDED) so token indices line up. Images are filtered
 * out of `cardPaths` on OUTPUT only (they render as inline `![image]()`
 * markdown, never as file cards); an image referenced by an embedded token is
 * likewise never added to mentionMap.
 */
export function resolveFileSegment(
  content: string,
  orderedFiles: string[],
  orderedDirs: string[] = [],
): ResolvedFileSegment {
  const labels = buildFileLabels(orderedFiles)
  const dirLabels = buildFileLabels(orderedDirs)
  const mentionMap = new Map<string, string>()
  const dirMentionMap = new Map<string, string>()
  const cardPaths: string[] = []
  const dirCardPaths: string[] = []
  const seen = new Set<string>()
  const seenDirs = new Set<string>()

  // Both markers share one scan so their tokens interleave correctly in the
  // output, but each numbers into its OWN ordered list: [attached_file 1] and
  // [attached_dir 1] refer to different things.
  const markerRe = /\[attached_(file|dir) (\d+)\]([^\S\n]+)/g
  let display = ''
  let lastIdx = 0
  let m: RegExpExecArray | null
  while ((m = markerRe.exec(content)) !== null) {
    const isDirMarker = m[1] === 'dir'
    const ordered = isDirMarker ? orderedDirs : orderedFiles
    const n = parseInt(m[2], 10)
    const pathStart = m.index + m[0].length
    const indexed = n >= 1 && n <= ordered.length ? ordered[n - 1] : undefined
    let path: string
    let pathEnd: number
    if (indexed && content.startsWith(indexed, pathStart)) {
      // Lossless: the real path (possibly with spaces) sits verbatim at pathStart.
      path = indexed
      pathEnd = pathStart + indexed.length
    } else {
      // Fallback: whitespace-bounded capture (no-meta replay / index mismatch).
      const rest = content.slice(pathStart)
      const wsIdx = rest.search(/\s/)
      path = wsIdx === -1 ? rest : rest.slice(0, wsIdx)
      pathEnd = pathStart + path.length
    }

    // Embedded when non-whitespace text sits on the SAME line as the token.
    const beforeSlice = content.slice(0, m.index)
    const afterSlice = content.slice(pathEnd)
    const lineBefore = beforeSlice.slice(beforeSlice.lastIndexOf('\n') + 1)
    const nlAfter = afterSlice.indexOf('\n')
    const lineAfter = nlAfter === -1 ? afterSlice : afterSlice.slice(0, nlAfter)
    const embedded = lineBefore.trim().length > 0 || lineAfter.trim().length > 0
    const label = isDirMarker
      ? (dirLabels.get(path) || path.split('/').pop() || path)
      : (labels.get(path) || path.split('/').pop() || path)
    // A directory is never an image, so the image branch only applies to files.
    const isImage = !isDirMarker && IMG_EXT.test(path)

    display += content.slice(lastIdx, m.index)
    if (embedded && !isImage) {
      // Folder chips read with a trailing slash so they are visibly not files,
      // and land in their own map: the caller renders them as non-clickable
      // labels, since a directory has nothing to open in the file viewer.
      if (isDirMarker) {
        dirMentionMap.set(label + '/', path)
        display += `@${label}/`
      } else {
        mentionMap.set(label, path)
        display += `@${label}`
      }
    } else if (!embedded && !isImage) {
      if (isDirMarker) dirCardPaths.push(path)
      else cardPaths.push(path)
      // Drop a trailing newline the standalone token owns so it leaves no blank
      // line; if it had a leading newline instead, drop that from the output.
      if (afterSlice.startsWith('\n')) pathEnd += 1
      else if (content[m.index - 1] === '\n') display = display.slice(0, -1)
    } else {
      // Image token: drop it silently (images render via ![image]() markdown).
      if (afterSlice.startsWith('\n')) pathEnd += 1
      else if (content[m.index - 1] === '\n') display = display.slice(0, -1)
    }
    if (isDirMarker) seenDirs.add(path)
    else seen.add(path)
    lastIdx = pathEnd
    markerRe.lastIndex = pathEnd
  }
  display += content.slice(lastIdx)

  // Recover any `@relative` mentions already present (fresh optimistic bubble),
  // for non-image files not already resolved from a token above. Blank entries
  // are index-alignment placeholders for invalid metadata (see metaPathList) —
  // they are not real paths, so they must never reach a chip or card.
  const notSeen = orderedFiles.filter(p => p && !seen.has(p) && !IMG_EXT.test(p))
  buildRelMap(notSeen, display).forEach((fullPath, suffix) => mentionMap.set(suffix, fullPath))
  // Same for folders: the optimistic bubble still carries `@src/pages/`.
  const dirsNotSeen = orderedDirs.filter(p => p && !seenDirs.has(p))
  buildDirRelMap(dirsNotSeen, display).forEach((fullPath, suffix) => {
    dirMentionMap.set(suffix + '/', fullPath)
  })

  return { display, mentionMap, dirMentionMap, cardPaths, dirCardPaths, labels }
}

/**
 * Message-level companion to resolveFileSegment: given the full (paste-collapsed)
 * message text and the ORIGINAL ordered attachment list (as persisted / as
 * `meta.files`, images included), return the non-image attachments that are not
 * referenced anywhere in the text — neither by an `[attached_file N]` token nor
 * by an `@relative` mention. The caller renders these exactly once as cards, so
 * a message split into multiple segments (paste tokens) can't duplicate them.
 *
 * CRITICAL: token number N indexes `orderedFiles` (the original list) — the same
 * list resolveFileSegment indexes with files[N-1]. It is NOT the image-filtered
 * list, so a mixed image+file upload probes the correct token. Non-image
 * filtering is applied only to the RESULT.
 */
export function findUnreferencedAttachments(text: string, orderedFiles: string[]): string[] {
  const referenced = new Set<string>()
  orderedFiles.forEach((p, i) => {
    const n = i + 1
    if (text.includes(`[attached_file ${n}]`)) { referenced.add(p); return }
    if (buildRelMap([p], text).size) referenced.add(p)
  })
  return orderedFiles.filter(p => p && !IMG_EXT.test(p) && !referenced.has(p))
}

/**
 * Directory counterpart to findUnreferencedAttachments. Token number N indexes
 * `orderedDirs`, an index space entirely separate from the file markers', so a
 * message carrying both `[attached_file 1]` and `[attached_dir 1]` resolves each
 * against the right list.
 */
export function findUnreferencedDirs(text: string, orderedDirs: string[]): string[] {
  const referenced = new Set<string>()
  orderedDirs.forEach((p, i) => {
    const n = i + 1
    if (text.includes(`[attached_dir ${n}]`)) { referenced.add(p); return }
    if (buildDirRelMap([p], text).size) referenced.add(p)
  })
  return orderedDirs.filter(p => p && !referenced.has(p))
}

/** Parse directory paths from message meta or [attached_dir N] patterns in content. */
export function parseDirs(content: string, meta?: Record<string, unknown>): string[] {
  const metaDirs = metaPathList(meta?.dirs)
  return metaDirs.length
    ? metaDirs
    : (content.match(/\[attached_dir \d+\] (\S+)/g) || []).map(s => s.replace(/\[attached_dir \d+\] /, ''))
}

/** Walk path segments to find the shortest @suffix present in text. */
export function buildRelMap(
  paths: string[], text: string,
  /** Token matcher; folder mentions pass `dirTokenRegex`. */
  matcher: (token: string, flags?: string) => RegExp = tokenRegex,
): Map<string, string> {
  const map = new Map<string, string>()
  for (const p of paths) {
    for (const suffix of pathSuffixes(trimDirSeparator(p))) {
      if (matcher(suffix).test(text) && !map.has(suffix)) { map.set(suffix, p); break }
    }
  }
  return map
}

/**
 * Directory variant of tokenRegex. The composer inserts folder mentions with a
 * trailing slash (`@src/pages/ `), so the token must match with OR without it.
 * The trailing separator may be either kind so a Windows path (`src\pages`) that
 * the picker suffixed with `/` still matches.
 */
function dirTokenRegex(token: string, flags = ''): RegExp {
  // Strip only a real trailing separator (see trimDirSeparator): on POSIX a
  // trailing backslash is part of the name, and eating it left the mention
  // unreplaced and duplicated by an appended marker.
  const escaped = escapeRegExp(trimDirSeparator(token))
  return new RegExp(`@${escaped}[/\\\\]?(?=\\s|$)`, flags)
}

/**
 * Path suffixes after each separator, longest first, with the ORIGINAL separator
 * characters preserved.
 *
 * Splitting on `/` alone would collapse a native Windows path
 * (`C:\repo\src\pages`) to a single segment, so no suffix would ever match the
 * `@C:\repo\src\pages/` token the picker inserted, and the mention would be left
 * as raw text while a duplicate `[attached_dir N]` marker was appended. Slicing
 * rather than splitting-and-rejoining keeps the separators native, so the map key
 * matches the text verbatim.
 */
function pathSuffixes(bare: string): string[] {
  // The full path is a candidate too: when the root could not be stripped (e.g.
  // an unrecognized root form), the picker inserts the absolute path, and a
  // suffix-only walk would never match it, leaving the raw mention plus a
  // duplicate marker. Suffixes are tried first so the short form still wins.
  const out: string[] = []
  // Only split on `\` for Windows-style paths — an internal backslash in a legal
  // POSIX name is not a separator, and treating it as one let `@b` resolve to
  // the wrong staged path `/repo/a\b`.
  const windowsish = /^[a-zA-Z]:[/\\]/.test(bare) || (bare.includes('\\') && !bare.includes('/'))
  const sepRe = windowsish ? /[/\\]/g : /\//g
  let m: RegExpExecArray | null
  while ((m = sepRe.exec(bare)) !== null) {
    const suffix = bare.slice(m.index + 1)
    if (suffix) out.push(suffix)
  }
  out.push(bare)
  return out
}

/**
 * Directory variant of buildRelMap. Folder paths carry no trailing separator,
 * but the inserted token does, so matching tolerates either form and either
 * separator style.
 */
export function buildDirRelMap(paths: string[], text: string): Map<string, string> {
  return buildRelMap(paths, text, dirTokenRegex)
}

/**
 * Remove `[attached_file N] path` / `[attached_dir N] path` pairs from text.
 *
 * Mirrors the server's `strip_attachment_markers`. Used when a queued card is
 * cancelled back into the composer: the card stores the SERIALIZED text, so
 * inserting it verbatim showed the raw marker and re-serialized it on resend.
 *
 * Strips by exact `marker + path` using the card's own ordered lists rather than
 * by pattern, so a path containing spaces or brackets is removed exactly.
 * Indices absent from `meta` are left alone — they belong to another family.
 *
 * Whitespace is repaired ONLY at each removal site. A global multi-space collapse
 * plus per-line trim rewrote text far from any marker, so a pasted code snippet
 * lost its indentation and aligned columns collapsed.
 */
export function stripAttachmentMarkers(
  content: string,
  meta?: { files?: string[]; dirs?: string[] },
): string {
  if (!content || !meta) return content
  let out = content
  for (const [prefix, paths] of [
    ['[attached_file ', meta.files],
    ['[attached_dir ', meta.dirs],
  ] as const) {
    // `meta` comes off a server payload, so the value may be any type. Only an
    // array of strings is meaningful — anything else is ignored rather than
    // iterated, which previously threw `paths.forEach is not a function` on the
    // cancel path and took the composer down with it.
    if (!Array.isArray(paths)) continue
    paths.forEach((p, i) => {
      if (!p || typeof p !== 'string') return
      const marker = escapeRegExp(`${prefix}${i + 1}] ${p}`)
      // Require a boundary after the path, or the marker matches a PREFIX of a
      // longer path: with meta owning `/d`, text mentioning `[attached_dir 1]
      // /dossier` was stripped down to `ossier`.
      const bound = '(?=\\s|$)'
      // A marker alone on its line takes the whole line, so no blank line is left.
      out = out.replace(new RegExp(`^[ \\t]*${marker}${bound}[ \\t]*\\n?`, 'gm'), '')
      // Otherwise the marker plus the whitespace hugging it becomes one space.
      out = out.replace(new RegExp(`[ \\t]*${marker}${bound}[ \\t]*`, 'g'), ' ')
      out = out.replace(/[ \t]+$/gm, '')
    })
  }
  return out.trim()
}

/**
 * Drop a directory path's trailing separator for dedup/matching purposes.
 *
 * Deliberately conservative. A blanket `/[/\\]+$/` strip was wrong twice: it
 * collapsed a filesystem root (`/` -> `''`, `C:\` -> `C:`), and on POSIX it ate
 * the last character of a legal directory name ending in a backslash. So a
 * backslash is only treated as a separator when the path actually uses
 * backslashes as separators, and the result is never allowed to go empty.
 */
export function trimDirSeparator(p: string): string {
  const windowsish = /^[a-zA-Z]:[/\\]/.test(p) || (p.includes('\\') && !p.includes('/'))
  const re = windowsish ? /[/\\]+$/ : /\/+$/
  const trimmed = p.replace(re, '')
  // Roots (`/`, `C:\`, `\\server\share`) trim to nothing or to a bare drive —
  // keep the original so they stay valid paths.
  if (!trimmed || /^[a-zA-Z]:$/.test(trimmed)) return p
  return trimmed
}

/** Replace @rel tokens in text using a replacer function. */

export function replaceTokens(
  text: string, paths: string[], relMap: Map<string, string>,
  replacer: (fullPath: string, idx: number) => string,
  /** Token matcher. Folder mentions pass `dirTokenRegex`, which additionally
   *  tolerates the trailing separator the composer inserts. */
  matcher: (token: string, flags?: string) => RegExp = tokenRegex,
): string {
  let result = text
  paths.forEach((p, i) => {
    const rel = [...relMap.entries()].find(([, v]) => v === p)?.[0]
    if (!rel) return
    result = result.replace(matcher(rel, 'g'), () => replacer(p, i))
  })
  return result
}

/** Directory variant of replaceTokens, tolerant of the inserted trailing slash. */
export function replaceDirTokens(
  text: string, paths: string[], relMap: Map<string, string>,
  replacer: (fullPath: string, idx: number) => string,
): string {
  return replaceTokens(text, paths, relMap, replacer, dirTokenRegex)
}

/**
 * Replace every `@mention` in one left-to-right pass.
 *
 * Serialization used to run as sequential `replaceTokens` passes — dirs, then
 * images, then files — over text that ALREADY contained markers emitted by an
 * earlier pass. An emitted marker carries the absolute path verbatim, so when a
 * path itself contains an `@…` sequence that a later pass's token matches, the
 * later pass rewrites text inside a marker it should never have looked at.
 *
 * Observed corruption: input `see @@notes/ and @@notes` with dir `/data/@notes`
 * and file `/repo/@notes` produced
 * `see [attached_dir 1] /data/@notes and [attached_dir 1] /data/@notes` — the
 * file mention was replaced by a duplicate *folder* marker, so the agent was
 * pointed at a directory that was never the attachment. That is a wrong-path
 * substitution, not just a cosmetic glitch.
 *
 * One pass fixes the class rather than the instance: the scanner walks the
 * original text once, and any text it emits is final. Candidates are matched
 * LONGEST-FIRST at each position so `@src/pages/list.tsx` wins over the
 * `@src/pages/` directory that prefixes it.
 */
export function replaceMentionsOnce(
  text: string,
  candidates: Array<{
    /** Relative mention text, without the leading `@` and without a trailing separator. */
    rel: string
    /** Whether the trailing separator the composer inserts is tolerated (dirs). */
    dir: boolean
    /** Absolute path this candidate stands for, when the caller needs to know
     *  which candidates actually won a position. */
    path?: string
    /** Emit the replacement for this candidate. */
    emit: () => string
  }>,
  /** Called for each candidate that actually wins a position and is emitted.
   *  Equal-length candidates compete, so "matched the text" is not the same as
   *  "was emitted" — the caller needs the latter to avoid dropping an
   *  attachment that lost its position to a sibling. */
  onEmit?: (c: { rel: string; dir: boolean; path?: string }) => void,
): string {
  if (!candidates.length) return text
  // Longest rel first: a directory is a prefix of the files beneath it, so a
  // shorter dir candidate must never win against a longer file mention at the
  // same position. On EQUAL length, prefer the file: `@notes` (file) and
  // `@notes/` (dir) both have rel "notes", and dirs are pushed first, so a
  // length-only sort would let the dir swallow the file mention and drop it.
  const ordered = [...candidates].sort(
    (a, b) => b.rel.length - a.rel.length || Number(a.dir) - Number(b.dir),
  )
  let out = ''
  let i = 0
  while (i < text.length) {
    if (text[i] !== '@') { out += text[i]; i += 1; continue }
    let matched = false
    for (const c of ordered) {
      // Compare against the ORIGINAL text only — never against `out`.
      if (!text.startsWith(c.rel, i + 1)) continue
      let end = i + 1 + c.rel.length
      // A folder mention may carry the trailing separator the composer inserts.
      if (c.dir && (text[end] === '/' || text[end] === '\\')) end += 1
      // Same boundary rule as tokenRegex/dirTokenRegex: whitespace or end.
      const next = text[end]
      if (next !== undefined && !/\s/.test(next)) continue
      out += c.emit()
      onEmit?.(c)
      i = end
      matched = true
      break
    }
    if (!matched) { out += text[i]; i += 1 }
  }
  return out
}

/** Build send payload from raw input text and pending files. */
export interface SendPayload {
  txt: string        // LLM-facing content
  displayTxt: string // UI-facing content
  filePaths: string[]
  imgPaths: string[]
  /** Directory references, in the order their `[attached_dir N]` tokens number. */
  dirPaths: string[]
}

/**
 * Serialize a directory reference. Folders get their OWN marker rather than
 * reusing `[attached_file N]`, because the agent must not try to `read` a
 * directory: the marker tells it this is a path to explore with its own
 * glob/grep/read tools. Numbering is independent of the file marker's.
 */
function dirToken(n: number, path: string): string {
  return `[attached_dir ${n}] ${path}`
}

export function prepareSendPayload(
  raw: string,
  pendingFiles: string[],
  pendingDirs: string[] = [],
): SendPayload {
  // All pending files (uploaded via button/drag-drop) are always included.
  // The @-token in text is used for display replacement, not as a gate.
  const files = [...new Set(pendingFiles)]
  const imgPaths = files.filter(p => IMG_EXT.test(p))
  const filePaths = files.filter(p => !IMG_EXT.test(p))
  const imgMd = imgPaths.map(p => `![image](${p})`).join('\n')
  const relMap = buildRelMap(files, raw)

  // Directories are tracked and numbered separately from files so a folder
  // reference never lands in filePaths/imgPaths (nothing downstream should try
  // to read or thumbnail it).
  // Normalize BEFORE dedup so "/repo/src" and "/repo/src/" collapse to one
  // entry rather than producing two tokens for the same folder.
  const dirs = [...new Set(pendingDirs.map(trimDirSeparator))]
  const dirRelMap = buildDirRelMap(dirs, raw)
  const referencedDirs = new Set([...dirRelMap.values()])
  const orderedDirs = [
    ...dirs.filter(p => referencedDirs.has(p)),
    ...dirs.filter(p => !referencedDirs.has(p)),
  ]
  const dirIdxMap = new Map(orderedDirs.map((p, i) => [p, i + 1]))

  // Assign sequential indices to all non-image files, ordered by upload order.
  // Referenced files get lower indices, unreferenced get higher — but indices
  // may not be monotonically increasing in the rendered text if @-mentions
  // appear in a different order than the upload order.
  const referencedPaths = new Set([...relMap.values()])
  // Keep metadata in the same order as token numbers so backend consumers can
  // resolve [attached_file N] directly without scanning every path.
  const indexedFilePaths = [
    ...filePaths.filter(p => referencedPaths.has(p)),
    ...filePaths.filter(p => !referencedPaths.has(p)),
  ]
  const idxMap = new Map(indexedFilePaths.map((p, i) => [p, i + 1]))

  // ONE pass over the original text for all three families. Sequential passes
  // rescanned each other's emitted markers (see replaceMentionsOnce), which let
  // a file token rewrite the inside of an already-emitted folder marker.
  const relFor = (map: Map<string, string>, p: string) =>
    [...map.entries()].find(([, v]) => v === p)?.[0]
  const llmCandidates: Array<{ rel: string; dir: boolean; path?: string; emit: () => string }> = []
  for (const p of orderedDirs) {
    const rel = relFor(dirRelMap, p)
    // `buildRelMap` strips any trailing separator before storing a key, so these
    // are already bare — the normalization below is a cheap invariant guard, not
    // load-bearing. The scanner matches the composer's trailing slash itself via
    // the `dir: true` flag.
    if (rel) llmCandidates.push({ rel: rel.replace(/[/\\]$/, ''), dir: true, path: p, emit: () => dirToken(dirIdxMap.get(p) ?? 0, p) })
  }
  for (const p of imgPaths) {
    const rel = relFor(relMap, p)
    if (rel) llmCandidates.push({ rel, dir: false, emit: () => '' })
  }
  for (const p of filePaths) {
    const rel = relFor(relMap, p)
    if (rel) llmCandidates.push({ rel, dir: false, emit: () => `[attached_file ${idxMap.get(p) ?? 0}] ${p}` })
  }
  // `replaceMentionsOnce` reports which candidates it actually emitted. A
  // directory can be "referenced" per `dirRelMap` (its rel matched the text) and
  // still LOSE that position to an equal-length file candidate — `@notes` with
  // both a file and a folder named `notes`. Trusting `referencedDirs` alone then
  // dropped the folder from both the inline text and the appended tokens, so a
  // staged attachment vanished silently. Append markers for anything staged but
  // not emitted.
  const emittedDirs = new Set<string>()
  const llmRaw = replaceMentionsOnce(raw, llmCandidates, c => {
    if (c.dir && c.path) emittedDirs.add(c.path)
  })
  const unreferenced = filePaths.filter(p => !referencedPaths.has(p))
  const unreferencedTokens = unreferenced.map(p => `[attached_file ${idxMap.get(p) ?? 0}] ${p}`).join('\n')
  const unreferencedDirTokens = orderedDirs
    .filter(p => !emittedDirs.has(p))
    .map(p => dirToken(dirIdxMap.get(p) ?? 0, p))
    .join('\n')
  const displayRaw = replaceTokens(raw, imgPaths, relMap, () => '')

  // Separate the pasted-image markdown from the typed text with a blank line
  // (a Markdown paragraph break) so the image renders in its own block and the
  // text drops to the next line, instead of flowing inline after the image (a
  // single '\n' is only a soft break). Applied to BOTH the LLM-facing `txt`
  // and the UI-facing `displayTxt`, so the *persisted* message keeps the break
  // on every surface that replays stored content — dashboard re-render after a
  // turn, gateway restart, Slack replay, exports — not just the in-memory
  // optimistic bubble. The extra blank line is safe for image attachment: the
  // ACP path (kiro-cli) extracts images in AcpClient._send_prompt by matching
  // the absolute file path and inlines them as a base64 `image` content block.
  // It is newline-agnostic and pulls the image into its own content block, so
  // the surrounding whitespace never changes what the model receives. The
  // caption keeps a single '\n' to its appended [attached_file N] tokens.
  const textBody = [llmRaw, unreferencedTokens, unreferencedDirTokens].filter(Boolean).join('\n')
  return {
    txt: [imgMd, textBody].filter(Boolean).join('\n\n'),
    displayTxt: [imgMd, displayRaw].filter(Boolean).join('\n\n'),
    filePaths: indexedFilePaths,
    imgPaths,
    dirPaths: orderedDirs,
  }
}
