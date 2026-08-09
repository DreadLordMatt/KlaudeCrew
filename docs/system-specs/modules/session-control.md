# Session Control Module

## Overview

Session control lets one of the user's chat sessions act on another: send it a
message, stop its in-flight turn, and read its transcript. It exists because
context does not travel between sessions. A session that has spent an hour on a
PR knows things a fresh session would have to be told, and today the only way to
move that knowledge is for the human to retype it. Session control makes the
session that already holds the context hand the work over directly.

Three MCP tools on `kirocrew-core`, three routes, one config switch:

| Tool | Route | What it does |
|------|-------|--------------|
| `session_message_send` | `POST /api/session-control/send` | Deliver a message to another session as a user turn |
| `session_stop` | `POST /api/session-control/stop` | Stop another session's in-flight turn |
| `session_read_message` | `GET /api/session-control/read` | Read another session's transcript tail + liveness |

Discovery is not new: `list_sessions` already enumerates the caller's sessions,
and its keys are what `target` accepts.

## Two properties that shape everything else

### Delivery is a user turn, not an injection

A delivered message arrives on the target's **normal input path** — the same one
a person typing in that tab uses. There is no side channel that writes into the
transcript behind the turn lifecycle's back. Concretely, `send` picks one of
three landings and reports which:

| Target state | `mode` | Landing | `delivered` |
|--------------|--------|---------|-------------|
| Running a turn | `steer` (default) | Interrupts the running turn via the live ACP client | `steered` |
| Running a turn | `steer`, but the client refuses or cannot steer | Falls through to the queue | `queued` |
| Running a turn | `queue` | Queue, without touching the running turn | `queued` |
| Idle | either | Appends the user message and starts a turn | `started` |

This matters beyond tidiness. Because delivery goes through the ordinary path,
everything built on that path keeps working on a controlled session: Stop still
cancels it, the queue still drains it, the context meter still counts it, the
transcript still shows it, and a page reload still finds it. An injection that
bypassed the turn would have to re-implement each of those, and would diverge
from all of them on the next change.

The steer and queue bookkeeping is **shared with `POST /api/chat`**, not
duplicated: both call `steer_into_running_turn` / `queue_for_next_turn` in
`dashboard/chat_delivery.py`. That sharing is load-bearing, because the ordering
rules there are subtle and a second copy would drift:

- The pending steer is registered **before** the steer RPC is awaited. `steer()`
  suspends on `stdin.drain()`, and if the turn's teardown runs during that
  suspension it must already see the steer in order to requeue it. Appending
  after the await would land on an idle slot and orphan the message.
- A refused steer **unwinds** that registration before the caller queues. If the
  entry is already gone, the turn's teardown requeued it (or a hard kill
  discarded it) during the await — the message is accounted for, and queueing
  again would deliver it twice. `steer_into_running_turn` distinguishes those two
  cases apart. Three outcomes, not two: `STEER_UNAVAILABLE` (queue it),
  `STEER_REQUEUED` (the teardown moved it into the queue — do not queue it
  again, it will run), and `STEER_DISCARDED` (a force-stop threw it away with
  the turn — do not queue it, and do not claim it is pending). The queue is
  what tells the last two apart: teardown moves unconsumed steers into it, a
  hard stop clears it. Collapsing them reports a thrown-away message as
  queued, which is worse than an error the caller can retry — so `send`
  answers 409 `delivery_discarded`, and `POST /api/chat` refuses so the client
  hands the composer back instead of rendering a card for text that is gone.
- Both lists are compared by **count**, never by membership. Identical text can
  already be pending (another caller's live steer) or already be queued (an
  unrelated earlier entry), and `message in list` cannot tell whose entry it is.
  A pending count that falls back means ours left; a queue count that rises means
  ours landed there. Reading mere presence in the queue as "our requeue" drops the
  transcript row for a message the turn genuinely consumed.
- `send` verifies the target is **still the same live slot** after the await
  (`state.get_slot(key) is slot`) before reporting success. A tab closed mid-steer
  leaves the delivery on a detached object where neither the row nor the reply can
  surface, so it answers 409 `delivery_discarded`. The check is identity, not
  `is None`: a same-key slot recreated during the await is a different
  conversation, and delivering into it would be worse than losing the message.
- **At most one pending steer per distinct text**, enforced before the RPC. Every
  consumer of `_pending_steers` matches by CONTENT (the teardown requeues by
  content, the queue comparison matches by content), so with two identical entries
  in flight nothing downstream can say whose survived: if another caller's copy is
  consumed while ours is refused, the count falls back exactly as it would had ours
  gone, and a refused message gets persisted as delivered and then requeued. So a
  second identical steer is refused up front with `STEER_UNAVAILABLE` and its caller
  queues instead — nothing is lost. Because a concurrent caller hits the same guard,
  once an entry is appended no further identical entry can appear, and that is what
  makes every post-await check unambiguously about the caller's own entry.
- Each in-flight steer carries an opaque **delivery id** (`_ChatSlot._steer_delivery_ids`,
  keyed by text because the one-per-text rule makes that key unique). The requeue moves
  the id onto the queue entry, and the drain unions entry meta onto the row it appends —
  so a row carrying the id proves the delivery is **already persisted**, even when the
  drain merged several queued messages into one row where no content comparison would
  match. That check runs first, because it is the only signal that survives every
  transition: the full requeue-then-drain sequence can complete while the RPC is
  suspended, leaving the entry in neither list and otherwise indistinguishable from the
  running turn having consumed it. Bare text cannot make that distinction, which is why
  the id exists rather than another content comparison.
- The send cooldown is a **claim, not an observation**: `_last_send` is recorded
  before the first await, past every refusal that can still reject the send. Delivery
  suspends, so two sends that both read the map before either suspended would both
  steer and the target would take a burst inside the window the cooldown exists to
  prevent. The delivery failure paths release the claim again, so a discarded or
  detached send does not lock the target out for the rest of the window.
- `STEER_DISCARDED` is the **only** outcome that tells a caller to resend, so it
  fires only when the text is provably gone: absent from both lists **with a stop
  in flight** (the hard-kill clear). In particular a raised `steer()` does not
  imply loss — `stdin.drain()` can fail after the bytes reached the child, and if
  the registration is gone with nothing queued and no stop, the running turn
  consumed it. Trusting the exception there would make the caller resend a message
  the target already has.
- Cursor positions are exact only while nothing has been trimmed. `_disk_older_count`
  counts every trimmed row including transient ones, while indexes here are built
  over durable rows only, so once a transient row ages into the frozen prefix the
  two disagree and a `since` read would repeat a durable message. Rather than
  duplicate silently, a `since` read on a trimmed session answers 409
  `cursor_unavailable` and the response omits `next_since` in favour of
  `cursor_exact: false`; tail reads (no `since`) keep working. The window is 10,000
  rows, so only a long-lived session reaches this. The real fix needs a
  durable-only prefix counter, which belongs to persistence — tracked in #2474.
- On a successful steer the in-flight text segment is cut at the steer boundary
  **before** the user message is persisted, so the transcript reads
  `[assistant(pre-steer), user(steer), …]` — the order the client rendered live.

### Provenance is never hidden

The delivered text is prefixed with one line naming the sending session:

```
[from session: Watchdog work]
stop rebasing, I took that commit
```

This is in the **content**, not display-only metadata, because the receiving
model reads content and it is the party that most needs to know the turn came
from a peer rather than the human. A session that could type as the user would
be an impersonation primitive; the label is what makes it a handoff instead. The
persisted message additionally carries `meta.session_control.from_slot` /
`from_title` for display attribution.

## Authorization

Deny-by-default, and checked in **one** place — `authorize_target` — for all
three verbs, so a guard cannot be present on `send` and missing on `read`. Every
refusal is recorded in the SEL as `session_control.<op>` with `outcome=denied`,
so an attempt to reach a session that is out of bounds is visible after the fact
even though nothing happened.

| Refusal | Status | Why |
|---------|--------|-----|
| Config switch off (`agent.session_control`) | 403 | Operator opted out |
| Caller session cannot be identified | 403 | An unidentifiable caller makes the self-send guard blind |
| Caller is an unattended session (`cron-*`, `workflow-*`) | 403 | A scheduled job typing into live conversations is not a handoff |
| Caller is itself incognito, temporary, or app-scoped | 403 | Caller-side isolation — the direction the target-side checks cannot see |
| Caller is channel-linked (`linked_session_key` set) | 403 | The exfiltration direction: a linked caller's conversation IS a channel thread, so a read would hand a private dashboard transcript to that channel's readers. `CHANNEL_AGENT_BLOCKED_TOOLS` keys on the agent identity; a linked slot is a second route to the same surface |
| Caller's own session is no longer open | 403 | Nothing to attribute the message to |
| Target is the caller | 403 | Self-send is a loop with no exit |
| Target is unattended (`cron-*`, `workflow-*`) | 403 | A `workflow-<run_id>` slot is display-only and a cron's turns are driven by a schedule; a message there starts a turn nobody reads |
| Target is incognito or temporary | 403 | Never addressable, matching `list_sessions` |
| Target is app-scoped | 403 | App sessions are the app's, not a peer's |
| Target is channel-linked (`linked_session_key` set) | 403 | Its conversation is mirrored to Slack/Telegram, so reaching it crosses a surface boundary both ways — and its stop cannot be honoured, because the stop path addresses `dashboard:<slot>` while a linked slot's turns run under its linked key |
| Target is in another workspace | 403 | Workspaces are the memory boundary |
| Target names no open session | 404 | A mistake, not an authorization failure |
| Title matches more than one session | 409 | Guessing means delivering to the wrong conversation |

Two notes on scope:

- **Only sessions the dashboard currently holds are addressable.** A closed tab
  is out of reach on purpose — waking one would resurrect a conversation the
  user put away. This is narrower than `list_sessions`, which also lists history.
- **All three tools are on
  `CHANNEL_AGENT_BLOCKED_TOOLS`, including the read.** A channel agent is
  contained to channel posts, and session control crosses that boundary in both
  directions: send / stop reach the user through one of their dashboard
  transcripts, and `session_read_message` pulls a private dashboard conversation
  into a channel other humans can see. Containment is about what crosses the
  boundary, not about who writes, so the read is blocked alongside the writes.

All three tools additionally require a **signed** caller identity
(`_resolve_session_key_strict`), not the lenient `/proc` ancestor walk. A
subagent spawned by `spawn_run` lives under its parent slot's process tree, so
the walk resolves it to the parent — and since authorization here is entirely
"what may this session reach", that would let a subagent read, message, or stop
the parent's sibling sessions. A caller the gateway issued no key to is refused
with an explanation rather than silently borrowing one.

The routes are **strict-internal** (`_STRICT_INTERNAL_API_PATHS`): loopback plus
`X-Internal-Secret`, with no cookie fall-through. No browser calls them, and they
are the entry point to typing into, stopping, and reading another live
conversation — a cookie path there would be a new authorization surface rather
than a convenience. The MCP process holds the secret; an agent's own sandbox does
not (`KIROCREW_INTERNAL_SECRET` is stripped from agent env), which is why these
are tools rather than something an agent can curl.

Each handler **re-asserts** `request["internal_auth"] is True` rather than
trusting the path classification. Strict is not self-enforcing at the handler:
with the header absent the middleware falls through to cookie auth, and a
`local_only=False` deployment reclassifies strict paths as mixed. Because these
routes authorize on the `X-Session-Key` the caller supplies, a same-origin page
holding only a dashboard cookie could otherwise act **as** any of the user's
sessions. `internal_auth` is set only after a constant-time secret match, so one
check closes the cookie path, the app-token path, and the non-loopback
reclassification together. The same reasoning is why
`/api/computer-use/frame` re-asserts it.

The config read fails **closed**: `KiroCrewConfig.load()` raising resolves to
disabled rather than to the field's (enabled) default, so malformed config in an
unrelated section cannot silently undo an explicit `session_control: false`.

## Flood control

Two bounds, both on the **target**, because the resource being protected is the
target's attention and context window:

- **Cooldown** — `SEND_COOLDOWN_SECS` (3s) minimum gap between sends to one
  session; a tighter caller gets 429. The bookkeeping map prunes expired rows on
  every check, so it stays bounded on a long-lived gateway rather than retaining
  a row per target forever.
- **Queue depth** — at `MAX_QUEUE_DEPTH` (5) waiting messages, further sends are
  429. A target that is not draining does not need more text.

Message size is capped at `MAX_MESSAGE_CHARS` (8000). Reads cap at 100 messages
and truncate any single message at `MAX_READ_CONTENT_CHARS` (4000), flagging the
row with `truncated: true`.

Redaction order is load-bearing in both directions:

- **Outbound**, the handoff body is scrubbed *before* the attributed text is
  built, because that text reaches the target's **provider** (`client.steer`,
  `_run_chat`) and not just its transcript. Scrubbing at the persist site alone
  would hand the model a raw credential and keep the clean copy on disk.
- **Inbound**, reads go through `redact_and_truncate`, which scans the complete
  message before slicing. Truncating first cuts a boundary-straddling secret
  into a prefix the scanner no longer matches, and that fragment ships.

A relay also carries **synthetic origin** on both delivery paths — the queue entry
is tagged `SESSION_CONTROL_PAYLOAD`, and a direct start passes
`_synthetic_payload=True`. That is what keeps it off the target's linked
Slack/Telegram thread: the text is a peer agent's, and mirroring it as the target
user's speech would put words in their mouth on a surface other people read.
Riding the user-input path is a property of *delivery*; it is not a claim about
authorship, and the mirror is the one consumer that must not confuse the two.

## The send → wait → read loop

`send` returns as soon as the message lands. It does **not** wait for the
target's reply, because the target's turn can run for minutes and holding a tool
call open for it would burn the caller's own turn budget. Polling is the
supported shape:

1. `session_message_send(target, message)` — note nothing; the send is fire-and-forget.
2. `session_read_message(target)` — record `total`.
3. `wait(seconds=…)`.
4. `session_read_message(target, since=<previous next_since>)` — returns only what
   arrived since, so a loop does not re-read the same messages.

`total` is an **absolute position** in the session, not the length of the live
window. A slot retains only its most recent messages in memory and credits each
trimmed row to a frozen-prefix counter, so a length-derived cursor would freeze
at the retention cap — and a poller on a long session would silently stop seeing
replies, on exactly the sessions that need it most. A `since` older than the
window returns `trimmed: N`: those rows aged out, and a poller that lost them is
told rather than fast-forwarded onto newer rows as if they were the ones asked
for.

`running` is what makes the loop terminable: `running: false` with an empty
window means the target finished and went idle, which is different from "nothing
new yet". `queue_depth` reports how much the target still owes.

The cursor deliberately stops **before the streaming tail**. `chat_runner`
appends a `chunk` row per token burst and `_flush_segment` then deletes that
trailing run, replacing it with one durable assistant message — so chunk rows are
always a suffix, never interleaved. Counting them would inflate `total`, the
flush would shrink the list back under it, and the next `since=next_since` read would
skip the finished reply permanently. A read taken mid-reply therefore reports
`streaming: true`, so an empty window while the target is composing is
distinguishable from an empty window because nothing is happening.

A stale cursor is not an error. A compacted transcript shrinks, so `since` past
the end returns an empty window rather than failing a poller that cannot act on
the failure.

## Configuration

`agent.session_control` (bool, default **true**). Off makes all three tools
refuse with a message naming the switch. Default-on is deliberate: every target
is one of the user's own sessions on their own machine, reached over loopback
with an audited internal secret, and a feature that ships invisible is a feature
nobody finds.

The value is read with `_safe_bool(..., False)`, so a **malformed** value
disables the feature rather than falling back to the field's own (enabled)
default: `bool("false")` is `True`, and a user who wrote the opt-out in an editor
that quotes values would otherwise keep cross-session control on while believing
it off. Absent still means enabled, because the lookup supplies a real `True`.

## What is deliberately not here

- **No cross-workspace or cross-machine reach.** The boundary is one gateway's
  live sessions in one workspace.
- **No waking closed sessions.** See above.
- **No reply-waiting send.** Polling keeps the caller's turn budget intact and
  keeps the two sides independent.
- **No provenance graph.** The chain is bounded by a single `hops` counter
  (`MAX_HOPS`), not by tracking who relayed what to whom. The counter rides in the
  delivered message's `session_control` meta and is read off the caller's newest
  user turn, so a relay chain terminates while a human typing resets it — enough
  to stop an unattended A ↔ B loop without modelling the topology.
