/** Shared file-token utilities used by send() and renderUserContent(). */

export const IMG_EXT = /\.(png|jpe?g|gif|webp|bmp|svg)$/i

/** Boundary-aware regex for @token matching. Prevents `@foo.ts` from matching inside `@foo.tsx`. */
function tokenRegex(token: string, flags = ''): RegExp {
  const escaped = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
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
function metaPathList(value: unknown): string[] {
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

/** Per-path display label: basename, disambiguated to the last-2 path segments
 *  when two paths share a basename (e.g. two `report.docx` in different dirs). */
export function buildFileLabels(paths: string[]): Map<string, string> {
  // Split on either separator: a native Windows path uses `\`, and splitting on
  // `/` alone would leave the whole absolute path as the "basename", so every
  // chip and card would display the full path and duplicate folder names would
  // never disambiguate.
  const seg = (p: string) => p.split(/[/\\]/)
  const basenames = paths.map(p => seg(p).pop() || p)
  const dupes = new Set(basenames.filter((n, i) => basenames.indexOf(n) !== i))
  const map = new Map<string, string>()
  for (const p of paths) {
    const parts = seg(p)
    const name = parts.pop() || p
    map.set(p, dupes.has(name) ? [parts.pop() ?? '', name].filter(Boolean).join('/') : name)
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
    for (const suffix of pathSuffixes(p.replace(/[/\\]+$/, ''))) {
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
  const escaped = token.replace(/[/\\]$/, '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
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
  const sepRe = /[/\\]/g
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
  const dirs = [...new Set(pendingDirs.map(p => p.replace(/[/\\]+$/, '')))]
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

  // Replace inline folder mentions first: the dir regex tolerates the trailing
  // slash the composer inserts, and dir paths are prefixes of the file paths
  // beneath them, so running this before the file pass avoids a partial match.
  const withDirs = replaceDirTokens(raw, orderedDirs, dirRelMap, p => dirToken(dirIdxMap.get(p) ?? 0, p))

  const llmRaw = replaceTokens(
    replaceTokens(withDirs, imgPaths, relMap, () => ''),
    filePaths, relMap, (p) => `[attached_file ${idxMap.get(p) ?? 0}] ${p}`,
  )
  const unreferenced = filePaths.filter(p => !referencedPaths.has(p))
  const unreferencedTokens = unreferenced.map(p => `[attached_file ${idxMap.get(p) ?? 0}] ${p}`).join('\n')
  const unreferencedDirTokens = orderedDirs
    .filter(p => !referencedDirs.has(p))
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
