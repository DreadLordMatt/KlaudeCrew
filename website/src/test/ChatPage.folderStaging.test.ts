/**
 * Guards the file/folder split at the composer boundary.
 *
 * A folder mention is a PATH REFERENCE: the agent receives the path and
 * explores it with its own tools. A file mention is an ATTACHMENT whose content
 * is read. Routing a picked folder into `pendingFiles` would make the send path
 * treat a directory as an uploadable file, so the two staged families must stay
 * separate from the moment the picker hands a selection over.
 *
 * These are static source guards rather than a full ChatPage render: the wiring
 * lives in a 3500-line component whose mount pulls in the entire dashboard
 * store, and the assertion here is about which state setter a `kind` routes to.
 * Brittle by design — if these lines are renamed, UPDATE the substrings rather
 * than deleting the guard.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const chatPage = readFileSync(resolve(here, '../pages/ChatPage.tsx'), 'utf8')
const chatInput = readFileSync(resolve(here, '../components/ChatInput.tsx'), 'utf8')

describe('picked folders never enter the file-attachment list', () => {
  it('routes a dir selection to setPendingDirs, not setPendingFiles', () => {
    expect(chatPage, 'onFileSelect must branch on kind').toContain(
      'onFileSelect={(path, kind) =>',
    )
    expect(chatPage, 'a dir must be staged into pendingDirs').toMatch(
      /kind === 'dir'\)?\s*(\?|setPendingDirs\()/,
    )
    expect(chatPage, 'the non-dir branch must stage into pendingFiles').toMatch(
      /(else |: )setPendingFiles\(/,
    )
  })

  it('keeps the two staged families in separate state', () => {
    expect(chatPage).toContain('const [pendingDirs, setPendingDirs] = useState<string[]>([])')
    expect(chatPage, 'pendingDirs must not be derived from pendingFiles').not.toMatch(
      /setPendingDirs\(\s*pendingFiles/,
    )
  })

  it('clears staged folders on send so they do not leak into the next message', () => {
    // Both the normal send and the mid-turn path clear the composer; a staged
    // folder left behind would silently ride along on the following message.
    const clears = chatPage.match(/setPendingFiles\(\[\]\); setPendingDirs\(\[\]\)/g) || []
    expect(clears.length, 'every composer clear must clear folders too').toBeGreaterThanOrEqual(2)
  })

  it('passes the picker kind through ChatInput rather than dropping it', () => {
    expect(chatInput, 'the picker callback must forward kind').toContain('onFileSelect(path, kind)')
    expect(chatInput, 'onFileSelect must accept a kind argument').toMatch(
      /onFileSelect\?:\s*\(path: string, kind\?: FileKind\)/,
    )
  })
})
