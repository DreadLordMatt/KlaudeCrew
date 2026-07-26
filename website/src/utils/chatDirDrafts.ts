/**
 * Per-slot pending-folder-reference persistence (directory paths staged in the
 * compose box before send). Thin instance of `createSlotDraftStore`, mirroring
 * `chatFileDrafts`.
 *
 * A folder reference is a path handed to the agent, not an upload, so nothing
 * server-side can garbage-collect it. It is still kept in sessionStorage rather
 * than localStorage to match the file-attachment lifetime: a staged reference
 * belongs to the composing session, and a path that is valid today may not
 * exist after a tab-close-and-return.
 */
import { createSlotDraftStore } from './slotDraftStore'

export const DIR_DRAFTS_KEY = 'mc-chat-dir-drafts'

export type DirDrafts = Record<string, string[]>

/** Coerce to a non-empty string[] (dropping non-string members), or null. The
 *  returned copy isolates the store from caller mutations and vice versa. */
const sanitizePaths = (v: unknown): string[] | null => {
  if (!Array.isArray(v)) return null
  const arr = v.filter((x): x is string => typeof x === 'string')
  return arr.length ? arr.slice() : null
}

const store = createSlotDraftStore<string[]>({
  key: DIR_DRAFTS_KEY,
  storage: 'session',
  sanitize: sanitizePaths,
})

export const loadDirDrafts = store.load
export const saveDirDrafts = store.save
export const setDirDraft = store.set
