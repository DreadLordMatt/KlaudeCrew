import { api } from '../api/client'
import { fileReadUrl } from './fileReadUrl'

/** Response shape of GET /api/file-resolve — mirrors the backend contract.
 *  `resolved_path` is the successor after a rename; it equals `path` when the
 *  file still exists, and is null when the file is gone and unresolvable. */
export interface FileResolveResult {
  path: string
  exists: boolean
  resolved_path: string | null
  method: 'exact' | 'git-rename' | 'content-match' | null
  confidence: number | null
}

/** Shared React Query key so FileRow's per-row probe and handleFileOpen's
 *  on-404 lookup hit the SAME cache entry — one resolve per path, deduped. */
export const fileResolveQueryKey = (path: string) => ['file-resolve', path] as const

/** React Query options for a resolve probe. Mirrors ToolCallLine's existence
 *  probe (React Query + staleTime, never a hand-rolled useState/useEffect
 *  fetch) so rows, tabs, and the file opener all share one cached lookup. */
export function fileResolveQueryOptions(path: string) {
  return {
    queryKey: fileResolveQueryKey(path),
    queryFn: () => api.fileResolve(path),
    staleTime: 30_000,
  }
}

// Markdown placeholders shown in the file viewer when a read fails. Shared so
// all three 404 sites (handleFileOpen, cold-tab hydration, inline FilePreview)
// speak with one voice.
export const FILE_NOT_FOUND_MD = '_File not found on disk. It may have been moved or deleted._'
export const FILE_MISSING_MD = '_This file no longer exists on disk._'
export const FILE_UNREADABLE_MD = '_Unable to read file._'

export interface FileReadResult { text: string; ok: boolean; status: number }

/** Read a file's content over /api/file-read. Never throws: a network-level
 *  rejection returns ok:false (status 0) so callers always get a defined
 *  result and never mount an editor over an empty buffer. */
export async function rawFileRead(path: string): Promise<FileReadResult> {
  try {
    const res = await fetch(fileReadUrl(path))
    const text = res.ok
      ? await res.text()
      : res.status === 404 ? FILE_NOT_FOUND_MD : FILE_UNREADABLE_MD
    return { text, ok: res.ok, status: res.status }
  } catch {
    return { text: FILE_UNREADABLE_MD, ok: false, status: 0 }
  }
}

export interface ResolvedFileRead extends FileReadResult {
  /** The path actually read — the successor when a rename was followed. */
  path: string
  /** The stale path we started from; set only when a rename was followed. */
  renamedFrom: string | null
}

/** Read a file, and on a 404 ask the resolver whether it was renamed. When a
 *  successor is found and readable, its content is returned under the NEW path
 *  with `renamedFrom` set. When the file is gone with no successor, a plain
 *  "no longer exists" placeholder is returned; when the resolver is
 *  unavailable, the original not-found text is kept (we can't be sure).
 *
 *  `readFile` / `resolveFile` are injectable so callers can route through their
 *  own React Query cache (handleFileOpen) or use the plain fetch defaults
 *  (declarative useQuery queryFns, which must not re-enter fetchQuery on their
 *  own key). */
export async function resolveFileRead(
  path: string,
  readFile: (p: string) => Promise<FileReadResult> = rawFileRead,
  resolveFile: (p: string) => Promise<FileResolveResult> = (p) => api.fileResolve(p),
): Promise<ResolvedFileRead> {
  const first = await readFile(path)
  if (first.ok || first.status !== 404) return { ...first, path, renamedFrom: null }
  let resolved: FileResolveResult | null = null
  try {
    resolved = await resolveFile(path)
  } catch {
    resolved = null // resolver unavailable — fall through, keep not-found text
  }
  if (resolved && !resolved.exists && resolved.resolved_path && resolved.resolved_path !== path) {
    const successor = await readFile(resolved.resolved_path)
    if (successor.ok) return { ...successor, path: resolved.resolved_path, renamedFrom: path }
    // Resolver pointed somewhere we still can't read — treat as missing.
    return { text: FILE_MISSING_MD, ok: false, status: successor.status, path: resolved.resolved_path, renamedFrom: path }
  }
  // Definitively gone (resolver says no successor) → plain "missing" message.
  // Resolver unavailable/errored → keep the original not-found text.
  const gone = !!resolved && !resolved.exists && !resolved.resolved_path
  return { text: gone ? FILE_MISSING_MD : first.text, ok: false, status: first.status, path, renamedFrom: null }
}
