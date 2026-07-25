# File Search Module

Last Updated: 2026-07-25

## Overview

File search backs the `@`-mention picker in the dashboard chat composer. A user
types `@` followed by 2+ characters, picks a result, and the composer inserts a
token that serializes into the prompt as an attachment marker.

Results cover both **files** and **directories**. A file is an attachment whose
content reaches the agent. A directory is a **path reference only**: the agent
receives the path and explores it with its own glob/grep/read tools. No
directory listing or recursive content is inlined.

## API

### `GET /api/file-search`

| Param | Required | Description |
|---|---|---|
| `q` | yes | Query string. Fewer than 2 characters returns an empty result set. |
| `project` | no | Absolute project path to scope the search to. |
| `workspace` | no | Workspace name to scope the search to, when `project` is absent. |
| `kinds` | no | `all` (default), `files`, or `dirs`. Unrecognized values fall back to `all`. |

Response:

```json
{
  "results": [
    {"path": "/repo/src/pages", "name": "pages", "kind": "dir",  "size": 0,    "mtime": 1750000000},
    {"path": "/repo/src/app.ts", "name": "app.ts", "kind": "file", "size": 2048, "mtime": 1750000000}
  ],
  "root": "/repo"
}
```

- `kind` is `"file"` or `"dir"`. Directory entries always report `size: 0`.
- At most 15 results are returned.
- Ranking is by fuzzy score, then **files before directories** on an equal
  score, then shorter name, then recency. The file bias keeps directory entries
  from crowding out the file a user is most likely searching for.

### Result sourcing

Two paths produce results:

1. **In-memory index fast path.** Used when the request is scoped to a single
   project and that project's `FileIndex` is ready and untruncated.
2. **Per-request walk fallback.** Used otherwise, bounded by a scan budget
   (50k entries scoped, 5k unscoped) and a collection cap. Files and directories
   are collected into separate candidate lists, each with its own cap, and files
   are scanned first at each level. A shared cap let a burst of matching
   directories fill it before the files in the same directory were examined, so
   the file-before-directory tie-break never got the chance to run.

Both paths apply the same exclusions: dot-prefixed names, a skip set
(`node_modules`, `__pycache__`, `dist`, `build`, `venv`, `env`, `out`,
`target`), and `is_sensitive_path`.

## Security

Both file and directory candidates are resolved with `os.path.realpath`
**before** the `is_sensitive_path` check, so a symlink pointing into a sensitive
tree is rejected on its real path rather than its link path. This matches the
existing precedent in `api_browse_dirs` / `api_browse_files`. The two branches
are deliberately symmetrical: a divergence would let a sensitive target be
reachable as a file but not as a directory, or the reverse.

## FileIndex

`FileIndex` keeps an in-memory list of entries per project root, rebuilt every
30 seconds on a background task, capped at 100,000 entries.

Each entry is a 6-tuple: `(path, name, relpath, size, mtime, kind)` where `kind`
is `"file"` or `"dir"` and directory entries carry `size: 0`.

Directories are collected during the walk rather than derived from file paths,
so an **empty** directory is still indexed and searchable. Both files and
directories count toward the entry cap; once the cap is hit the index is marked
truncated and the fast path is disabled for that root, falling back to the
per-request walk.

`FileIndex.search(query, scorer, max_results, kinds)` applies the same
`kinds` filter and file-before-directory tie-break as the endpoint.

## Prompt markers

The composer serializes picked entries into two independent marker families:

| Marker | Meaning |
|---|---|
| `![image](/full/path)` | Image attachment, inlined as markdown |
| `[attached_file N] /full/path` | Non-image file attachment |
| `[attached_dir N] /full/path` | Directory path reference |

Numbering is **per family**: `N` in `[attached_file N]` indexes the ordered file
list, `N` in `[attached_dir N]` indexes the ordered directory list. A single
message can therefore contain both `[attached_file 1]` and `[attached_dir 1]`
referring to different things. Within each family, entries referenced inline in
the user's text are numbered before unreferenced ones.

Path resolution is lossless: because `N` indexes the original ordered list, a
path containing spaces still resolves exactly, and the whitespace-bounded
capture is only a fallback for history replay without metadata.

For that index to survive a round trip, the sender persists the ordered lists on
message metadata: `meta.files` for the file family and `meta.dirs` for the
directory family. Both are required, not optional. The server stores the
LLM-facing token form in `content` while keeping this metadata alongside it, so
replay reads the index from metadata rather than re-parsing the text. Omitting
`meta.dirs` silently degrades folder replay to the whitespace fallback, which
truncates any path containing a space. Mid-turn steering renders its own
optimistic bubble from the same tokenized text, so it carries the same metadata.

Folder tokens are inserted with a trailing slash (`@src/pages/`) so a folder
mention is visually distinct from a file mention in the composer. Matching
tolerates the token with or without that slash.

The agent-facing contract for these markers is documented in `AGENTS.md`
under "File Attachments". Directories must not be passed to a file-read tool.

## Rendering

A resolved marker becomes either an inline chip or a block card depending on
**position**, not on metadata:

- A marker embedded in a line with other text renders as an inline `@label`
  chip. Folder chips carry a trailing slash. File chips are clickable and open
  the file viewer; folder chips are not, so the resolver keeps them in a separate
  `dirMentionMap` rather than the file `mentionMap` that drives the click handler.
- A marker alone on its line renders as a block card. File cards are clickable
  and open the file viewer; folder cards are not clickable, because a directory
  has nothing to open.

Folder token matching tolerates either path separator and an optional trailing
one, so a native Windows path inserted as `@src\pages/` still resolves. Without
that, the mention stayed as raw text and a duplicate marker was appended. Chip
and card labels are derived the same way: the basename split accepts either
separator, so a native Windows path shows its folder name rather than the whole
absolute path, and duplicate names still disambiguate on their parent segment.

The preview strip renders whenever either family is staged, and the composer's
manual-height compensation keys off both. A folders-only strip would otherwise
appear with no wrapper allowance and overlap the textarea.

Attachments never referenced anywhere in the message text are rendered exactly
once at message level, so a message split across multiple paste segments cannot
duplicate them.

Chat titles strip both marker families before prompt construction, so neither a
file path nor a folder path leaks into a generated title.

## Composer state

Staged files and staged folders are tracked in separate state (`pendingFiles`,
`pendingDirs`) so a directory never reaches the upload, thumbnail, or
`attached_file` paths.

Both are persisted **per chat slot**, files via `chatFileDrafts` and folders via
`chatDirDrafts`, each in sessionStorage under its own key. On a slot switch the
outgoing slot's staged entries are written to its draft and the incoming slot's
are restored. Restoring is what keeps the two isolated: without an explicit
reset, a folder picked in one chat would remain staged and ride along on the next
send in a different chat.

When a send fails on a connection error the composer is restored from the
**pre-serialization** state: the raw typed text plus both staged families. The
restored file list is the union of images and non-images, since
`prepareSendPayload` splits those apart for the LLM payload and an image exists
only in `pendingFiles` — restoring the non-image list alone dropped it, so a
retry would have sent the message without the image.

Mid-turn steering carries the same metadata, on both the optimistic bubble and
the server-persisted entry: `steerChat` forwards `meta` in the request body and
the steer branch merges it into the appended history entry. The `steer` marker is
applied server-side last, so a client cannot spoof it. Without server-side
persistence the metadata lived only in the live session, and a reload fell back
to the content scan and truncated any path containing a space.

The `steer_push` WebSocket echo carries that same `meta` and the client consumes
it. Other open tabs render the steered bubble from the echo alone, so an echo
without the ordered lists left them showing a space-truncated path until the
next reload.

The **queue** is the third path a message with attachments can take (mid-turn
without a live steer channel, or held while sub-agents run). `queue_append`
accepts the redacted `meta`, and the drain in `chat_runner` persists it onto the
appended entry — otherwise a queued message kept its markers but lost the lists.
A metadata-bearing entry is never folded into a `[N queued messages merged]`
turn: marker numbers index the message's own list, so concatenating two such
messages would resolve the second message's markers against the first's paths.
It breaks the merge and drains alone.

`meta.files` / `meta.dirs` arrive from an untrusted payload, so `metaPathList`
validates them at the boundary. Invalid members are **blanked in place, never
dropped**: the marker number is a positional index into this list, so filtering
would shift every later path down a slot and a spaced path following an invalid
entry would resolve against the wrong element (or past the end) and fall through
to the truncating scan. A blank is an alignment placeholder only — it is filtered
out of the chip/card and unreferenced-attachment outputs so it can never render
as an empty attachment.

Both write paths roll back on rejection. A failed steer removes its optimistic
bubble (`removeOptimisticMessage`, scoped to `meta.optimistic` so a
server-confirmed message with the same timestamp is never deleted) and re-stages
the composer; a rejected `createSlot` during send restores the captured text,
files (images included) and folders instead of letting `.unwrap()` throw past the
restore path.

## Key Files

| File | Role |
|---|---|
| `src/kiro_crew/dashboard/handlers/files.py` | `api_file_search` endpoint, fuzzy scorer, walk fallback |
| `src/kiro_crew/dashboard/file_index.py` | `FileIndex`, `FileIndexRegistry` |
| `src/kiro_crew/dashboard/chat_title.py` | Marker stripping for title generation |
| `website/src/components/FilePickerMenu.tsx` | Picker UI, `kind` propagation, trailing-slash insertion |
| `website/src/components/ChatInput.tsx` | Composer wiring, pending file/folder preview strip |
| `website/src/utils/fileTokens.ts` | Marker serialization and resolution |
| `website/src/utils/chatDirDrafts.ts` | Per-slot staged folder-reference persistence |
| `website/src/pages/ChatPage.tsx` | Pending state, send payload, message rendering |

## Tests

| File | Coverage |
|---|---|
| `test/test_file_search.py` | Endpoint behaviour, scoring, exclusions |
| `test/test_file_index.py` | Index build, refresh, registry refcounting |
| `test/test_file_search_dirs.py` | Directory results, `kinds` filter, symlink security |
| `test/test_chat_title_dirs.py` | Marker stripping for both families |
| `website/src/test/FilePickerMenu.dirs.test.tsx` | Folder rows, selection payloads |
| `website/src/test/fileTokens.dirs.test.ts` | `[attached_dir N]` serialization and numbering |
| `website/src/test/renderUserContent.dirs.test.tsx` | Folder chips and cards |
| `website/src/test/chatDirDrafts.test.ts` | Per-slot folder-draft store |
| `website/src/test/ChatPageDrafts.test.tsx` | Draft isolation, `meta.dirs` persistence guards, send-failure attachment restore, createSlot/steer rejection restore |
| `website/src/test/chatSlice.removeOptimistic.test.ts` | Optimistic-bubble rollback stays scoped to unconfirmed messages |
| `website/src/test/ChatInput.dirStripHeight.test.tsx` | Preview-strip height compensation for a folders-only strip |
| `test/test_chat_steer.py` | Steered messages persist `meta.files` / `meta.dirs`; `steer` marker is not spoofable; `steer_push` echoes meta; queued sends carry meta to the drain and never merge |
