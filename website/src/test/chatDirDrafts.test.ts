import { describe, it, expect, beforeEach } from 'vitest'
import { DIR_DRAFTS_KEY, loadDirDrafts, saveDirDrafts, setDirDraft } from '../utils/chatDirDrafts'
import { FILE_DRAFTS_KEY } from '../utils/chatFileDrafts'

describe('chatDirDrafts', () => {
  beforeEach(() => { sessionStorage.clear() })

  it('roundtrips dir drafts through sessionStorage', () => {
    const drafts = {
      'chat-1-100': ['/repo/src/pages', '/repo/docs'],
      'chat-2-200': ['/repo/my docs'],
    }
    saveDirDrafts(drafts)
    expect(loadDirDrafts()).toEqual(drafts)
    expect(sessionStorage.getItem(DIR_DRAFTS_KEY)).toBe(JSON.stringify(drafts))
  })

  it('uses a storage key distinct from the file drafts key', () => {
    // A shared key would smear folder references into the file-attachment list,
    // putting a directory back on the upload/thumbnail path.
    expect(DIR_DRAFTS_KEY).not.toBe(FILE_DRAFTS_KEY)
  })

  it('returns {} on missing, corrupt, or non-object storage', () => {
    expect(loadDirDrafts()).toEqual({})
    sessionStorage.setItem(DIR_DRAFTS_KEY, 'not json')
    expect(loadDirDrafts()).toEqual({})
    sessionStorage.setItem(DIR_DRAFTS_KEY, '[]')
    expect(loadDirDrafts()).toEqual({})
    sessionStorage.setItem(DIR_DRAFTS_KEY, 'null')
    expect(loadDirDrafts()).toEqual({})
  })

  it('filters out non-array and non-string-element values (corruption guard)', () => {
    sessionStorage.setItem(DIR_DRAFTS_KEY, JSON.stringify({
      'good': ['/repo/src'],
      'string-value': 'not-an-array',
      'number-value': 42,
      'null-value': null,
      'mixed-array': ['/repo/a', 42, null, '/repo/b'],
      'empty-array': [],
    }))
    expect(loadDirDrafts()).toEqual({
      'good': ['/repo/src'],
      'mixed-array': ['/repo/a', '/repo/b'],
    })
  })

  it('setDirDraft stores non-empty and deletes empty', () => {
    const d: Record<string, string[]> = { 'chat-1-100': ['/repo/old'] }
    setDirDraft(d, 'chat-1-100', ['/repo/new1', '/repo/new2'])
    expect(d).toEqual({ 'chat-1-100': ['/repo/new1', '/repo/new2'] })
    setDirDraft(d, 'chat-1-100', [])
    expect(d).toEqual({})
  })

  it('setDirDraft stores a defensive copy so caller mutations do not leak', () => {
    const d: Record<string, string[]> = {}
    const live: string[] = ['/repo/a']
    setDirDraft(d, 'chat-1', live)
    live.push('/repo/b')
    expect(d['chat-1']).toEqual(['/repo/a'])
  })

  it('keeps per-slot entries isolated', () => {
    const d: Record<string, string[]> = {}
    setDirDraft(d, 'chat-a', ['/repo/src/pages'])
    setDirDraft(d, 'chat-b', ['/repo/docs'])
    expect(d['chat-a']).toEqual(['/repo/src/pages'])
    expect(d['chat-b']).toEqual(['/repo/docs'])
  })

  it('preserves a path containing spaces', () => {
    const d: Record<string, string[]> = {}
    setDirDraft(d, 'chat-1', ['/repo/my docs'])
    saveDirDrafts(d)
    expect(loadDirDrafts()['chat-1']).toEqual(['/repo/my docs'])
  })
})
