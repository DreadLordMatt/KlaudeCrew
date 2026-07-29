import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// The util value-imports `api` from the client barrel (for the default
// resolver). Stub it so this unit test doesn't drag in the whole client module
// and its transport side effects — every case below injects its own fakes.
vi.mock('../api/client', () => ({ api: { fileResolve: vi.fn() } }))

import {
  resolveFileRead,
  rawFileRead,
  fileResolveQueryKey,
  FILE_NOT_FOUND_MD,
  FILE_MISSING_MD,
  FILE_UNREADABLE_MD,
  type FileResolveResult,
} from '../utils/fileResolve'

const gone = (path: string): FileResolveResult => ({ path, exists: false, resolved_path: null, method: null, confidence: null })
const renamed = (path: string, to: string): FileResolveResult => ({ path, exists: false, resolved_path: to, method: 'git-rename', confidence: 0.9 })
const present = (path: string): FileResolveResult => ({ path, exists: true, resolved_path: path, method: 'exact', confidence: 1 })

describe('fileResolveQueryKey', () => {
  it('is stable and path-scoped so rows and the opener dedupe', () => {
    expect(fileResolveQueryKey('/p/a.ts')).toEqual(['file-resolve', '/p/a.ts'])
  })
})

describe('resolveFileRead', () => {
  it('returns the read as-is when the file exists (no resolver call)', async () => {
    const readFile = vi.fn().mockResolvedValue({ text: 'body', ok: true, status: 200 })
    const resolveFile = vi.fn()
    const r = await resolveFileRead('/p/a.ts', readFile, resolveFile)
    expect(r).toEqual({ text: 'body', ok: true, status: 200, path: '/p/a.ts', renamedFrom: null })
    expect(resolveFile).not.toHaveBeenCalled()
  })

  it('does not resolve on a non-404 read failure', async () => {
    const readFile = vi.fn().mockResolvedValue({ text: FILE_UNREADABLE_MD, ok: false, status: 500 })
    const resolveFile = vi.fn()
    const r = await resolveFileRead('/p/a.ts', readFile, resolveFile)
    expect(r.ok).toBe(false)
    expect(r.path).toBe('/p/a.ts')
    expect(resolveFile).not.toHaveBeenCalled()
  })

  it('follows a rename on 404 and returns the successor content under the new path', async () => {
    const readFile = vi.fn()
      .mockResolvedValueOnce({ text: FILE_NOT_FOUND_MD, ok: false, status: 404 }) // old path
      .mockResolvedValueOnce({ text: 'new body', ok: true, status: 200 })          // successor
    const resolveFile = vi.fn().mockResolvedValue(renamed('/p/old.ts', '/p/new.ts'))
    const r = await resolveFileRead('/p/old.ts', readFile, resolveFile)
    expect(r).toEqual({ text: 'new body', ok: true, status: 200, path: '/p/new.ts', renamedFrom: '/p/old.ts' })
    expect(readFile).toHaveBeenNthCalledWith(2, '/p/new.ts')
  })

  it('reports a plain "missing" placeholder when the resolver finds no successor', async () => {
    const readFile = vi.fn().mockResolvedValue({ text: FILE_NOT_FOUND_MD, ok: false, status: 404 })
    const resolveFile = vi.fn().mockResolvedValue(gone('/p/old.ts'))
    const r = await resolveFileRead('/p/old.ts', readFile, resolveFile)
    expect(r).toEqual({ text: FILE_MISSING_MD, ok: false, status: 404, path: '/p/old.ts', renamedFrom: null })
  })

  it('keeps the original not-found text when the resolver is unavailable', async () => {
    const readFile = vi.fn().mockResolvedValue({ text: FILE_NOT_FOUND_MD, ok: false, status: 404 })
    const resolveFile = vi.fn().mockRejectedValue(new Error('resolver down'))
    const r = await resolveFileRead('/p/old.ts', readFile, resolveFile)
    expect(r).toEqual({ text: FILE_NOT_FOUND_MD, ok: false, status: 404, path: '/p/old.ts', renamedFrom: null })
  })

  it('treats a resolver-that-still-cannot-be-read successor as missing (renamedFrom retained)', async () => {
    const readFile = vi.fn()
      .mockResolvedValueOnce({ text: FILE_NOT_FOUND_MD, ok: false, status: 404 }) // old path
      .mockResolvedValueOnce({ text: FILE_NOT_FOUND_MD, ok: false, status: 404 }) // successor unreadable
    const resolveFile = vi.fn().mockResolvedValue(renamed('/p/old.ts', '/p/new.ts'))
    const r = await resolveFileRead('/p/old.ts', readFile, resolveFile)
    expect(r).toEqual({ text: FILE_MISSING_MD, ok: false, status: 404, path: '/p/new.ts', renamedFrom: '/p/old.ts' })
  })

  it('ignores a resolver echoing the same path (exists) — no phantom rename', async () => {
    const readFile = vi.fn().mockResolvedValue({ text: FILE_NOT_FOUND_MD, ok: false, status: 404 })
    const resolveFile = vi.fn().mockResolvedValue(present('/p/a.ts'))
    const r = await resolveFileRead('/p/a.ts', readFile, resolveFile)
    expect(r.renamedFrom).toBeNull()
    expect(r.path).toBe('/p/a.ts')
  })
})

describe('rawFileRead', () => {
  const prevFetch = global.fetch
  beforeEach(() => { vi.restoreAllMocks() })
  afterEach(() => { global.fetch = prevFetch })

  it('returns ok + body on 200', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, text: async () => 'hi' }) as unknown as typeof fetch
    expect(await rawFileRead('/p/a.ts')).toEqual({ text: 'hi', ok: true, status: 200 })
  })

  it('maps 404 to the not-found placeholder', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 404, text: async () => '' }) as unknown as typeof fetch
    expect(await rawFileRead('/p/a.ts')).toEqual({ text: FILE_NOT_FOUND_MD, ok: false, status: 404 })
  })

  it('never throws on a network rejection — returns ok:false status:0', async () => {
    global.fetch = vi.fn().mockRejectedValue(new Error('offline')) as unknown as typeof fetch
    expect(await rawFileRead('/p/a.ts')).toEqual({ text: FILE_UNREADABLE_MD, ok: false, status: 0 })
  })
})
