import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import { renderUserContent } from '../pages/ChatPage'

const noop = () => {}

/** The persisted shape: server stores the LLM-facing token form in content AND
 *  keeps meta alongside it. Tests cover both meta-present and no-meta replay. */
const DIR = '/repo/src/pages'
const FILE = '/repo/data.csv'

describe('renderUserContent — folder references', () => {
  it('renders a standalone folder token as a folder card, not raw text', () => {
    const { container } = render(
      <>{renderUserContent(`[attached_dir 1] ${DIR}`, { dirs: [DIR] }, noop)}</>,
    )
    expect(container).not.toHaveTextContent('attached_dir')
    expect(container).toHaveTextContent('pages/')
  })

  it('renders the folder card with a Folder icon', () => {
    const { container } = render(
      <>{renderUserContent(`[attached_dir 1] ${DIR}`, { dirs: [DIR] }, noop)}</>,
    )
    expect(container.querySelector('[aria-label="Folder"]')).toBeInTheDocument()
  })

  it('exposes the full path as the folder card title', () => {
    const { container } = render(
      <>{renderUserContent(`[attached_dir 1] ${DIR}`, { dirs: [DIR] }, noop)}</>,
    )
    expect(container.querySelector(`[title="${DIR}"]`)).toBeInTheDocument()
  })

  it('does not make the folder card clickable', () => {
    const onFileOpen = vi.fn()
    const { container } = render(
      <>{renderUserContent(`[attached_dir 1] ${DIR}`, { dirs: [DIR] }, onFileOpen)}</>,
    )
    const card = container.querySelector(`[title="${DIR}"]`)!
    expect(card.tagName.toLowerCase()).not.toBe('a')
    expect(card.tagName.toLowerCase()).not.toBe('button')
  })

  it('renders an embedded folder token as an inline chip with a trailing slash', () => {
    const { container } = render(
      <>{renderUserContent(`please review [attached_dir 1] ${DIR} today`, { dirs: [DIR] }, noop)}</>,
    )
    expect(container).not.toHaveTextContent('attached_dir')
    expect(container).toHaveTextContent('please review')
    expect(container).toHaveTextContent('@pages/')
    expect(container).toHaveTextContent('today')
  })

  it('keeps an embedded folder mention on one line with the surrounding text', () => {
    const { container } = render(
      <>{renderUserContent(`check [attached_dir 1] ${DIR} now`, { dirs: [DIR] }, noop)}</>,
    )
    // A block <p> per run would break the line around the chip.
    expect(container.querySelectorAll('p')).toHaveLength(0)
  })

  it('replays a folder token with no meta (history replay path)', () => {
    const { container } = render(
      <>{renderUserContent(`[attached_dir 1] ${DIR}`, undefined, noop)}</>,
    )
    expect(container).not.toHaveTextContent('attached_dir')
    expect(container).toHaveTextContent('pages/')
  })

  it('renders a file card and a folder card together', () => {
    const content = `hello\n[attached_file 1] ${FILE}\n[attached_dir 1] ${DIR}`
    const { container } = render(
      <>{renderUserContent(content, { files: [FILE], dirs: [DIR] }, noop)}</>,
    )
    expect(container).not.toHaveTextContent('attached_file')
    expect(container).not.toHaveTextContent('attached_dir')
    expect(container).toHaveTextContent('data.csv')
    expect(container).toHaveTextContent('pages/')
    expect(container).toHaveTextContent('hello')
  })

  it('resolves same-numbered file and dir tokens against their own lists', () => {
    // Both markers are numbered 1: each must index its OWN ordered list.
    const content = `see [attached_file 1] ${FILE} and [attached_dir 1] ${DIR}`
    const { container } = render(
      <>{renderUserContent(content, { files: [FILE], dirs: [DIR] }, noop)}</>,
    )
    expect(container).toHaveTextContent('@data.csv')
    expect(container).toHaveTextContent('@pages/')
  })

  it('does not make an embedded folder chip clickable', () => {
    // A mentionMap entry renders as an "Open file" chip that calls onFileOpen.
    // A folder must not reach that path: the viewer cannot open a directory.
    const onFileOpen = vi.fn()
    const { container } = render(
      <>{renderUserContent(`please review [attached_dir 1] ${DIR} today`, { dirs: [DIR] }, onFileOpen)}</>,
    )
    const chip = container.querySelector(`[title="${DIR}"]`)!
    expect(chip).toBeInTheDocument()
    expect(chip.tagName.toLowerCase()).not.toBe('a')
    expect(chip.tagName.toLowerCase()).not.toBe('button')
    expect(chip.getAttribute('aria-label') ?? '').not.toMatch(/open file/i)
    expect(chip.className).not.toContain('cursor-pointer')
  })

  it('keeps an embedded file chip clickable alongside a folder chip', () => {
    const onFileOpen = vi.fn()
    const content = `see [attached_file 1] ${FILE} and [attached_dir 1] ${DIR} now`
    const { container } = render(
      <>{renderUserContent(content, { files: [FILE], dirs: [DIR] }, onFileOpen)}</>,
    )
    const fileChip = container.querySelector(`[aria-label="Open file ${FILE}"]`)
    expect(fileChip).toBeInTheDocument()
    const dirChip = container.querySelector(`[title="${DIR}"]`)!
    expect(dirChip.getAttribute('aria-label') ?? '').not.toMatch(/open file/i)
  })

  it('handles a folder path containing spaces', () => {
    const spaced = '/repo/my docs'
    const { container } = render(
      <>{renderUserContent(`[attached_dir 1] ${spaced}`, { dirs: [spaced] }, noop)}</>,
    )
    expect(container).not.toHaveTextContent('attached_dir')
    expect(container.querySelector(`[title="${spaced}"]`)).toBeInTheDocument()
    expect(container).toHaveTextContent('my docs/')
  })

  it('resolves a spaced folder path from the meta the sender actually persists', () => {
    // Regression guard: send() must put dirPaths on meta.dirs. Without it this
    // message replays through the whitespace-splitting content fallback, which
    // truncates the path at the first space.
    const spaced = '/repo/my docs'
    const content = `look at [attached_dir 1] ${spaced} please`
    const withMeta = render(
      <>{renderUserContent(content, { dirs: [spaced] }, noop)}</>,
    )
    expect(withMeta.container).toHaveTextContent('@my docs/')
    expect(withMeta.container).toHaveTextContent('please')
    expect(withMeta.container).not.toHaveTextContent('attached_dir')
    // Same content with no meta is the degraded path: the fallback can only see
    // up to the space, so the tail leaks as text. This asserts the difference is
    // real, which is why meta.dirs must be persisted.
    const noMeta = render(<>{renderUserContent(content, undefined, noop)}</>)
    expect(noMeta.container).toHaveTextContent('docs')
  })

  it('renders the optimistic bubble form (@relative, no token yet)', () => {
    const { container } = render(
      <>{renderUserContent('review @src/pages/ please', { dirs: [DIR] }, noop)}</>,
    )
    expect(container).toHaveTextContent('review')
    expect(container).toHaveTextContent('please')
    expect(container).toHaveTextContent('src/pages/')
  })

  it('renders a folder card exactly once, not per segment', () => {
    const { container } = render(
      <>{renderUserContent(`[attached_dir 1] ${DIR}`, { dirs: [DIR] }, noop)}</>,
    )
    expect(container.querySelectorAll(`[title="${DIR}"]`)).toHaveLength(1)
  })

  it('leaves a message with no attachments untouched', () => {
    const { container } = render(
      <>{renderUserContent('plain **bold** message', undefined, noop)}</>,
    )
    expect(container.querySelector('strong')).toHaveTextContent('bold')
  })

  it('still renders a file-only message exactly as before (no dir regression)', () => {
    const { container } = render(
      <>{renderUserContent(`[attached_file 1] ${FILE}`, { files: [FILE] }, noop)}</>,
    )
    expect(container).not.toHaveTextContent('attached_file')
    expect(container).toHaveTextContent('data.csv')
    expect(container.querySelector('[aria-label="Folder"]')).not.toBeInTheDocument()
  })
})
