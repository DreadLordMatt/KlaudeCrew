# Team mesh: claim arbitration across instances

**Status:** design, not built. This settles the contract the backlog said to settle
"before building the transport", so the transport choice stops being blocked on it.

## The problem, measured

Two instances pointed at the same provider both claim the same alarm:

```
instance A claimed: INV-1
instance B claimed: INV-1
BOTH claimed the same signal: True
```

Reproduced with two data homes and one signal (`cloudwatch:alarm/Shared`). They do not
merely double-claim — they mint the **same incident id**, because `_next_incident_id`
counts a local index. So the failure is worse than duplicated work:

- two agents investigate one alarm, burning two sessions on the same diagnosis
- two Slack threads for one incident, so the board stops being a board
- two ledger entries whose `use_count` each under-counts the real recurrence rate
- `INV-1` is ambiguous across the mesh, so any cross-instance reference is undefined

`store.claim` is atomic *within* an instance (exclusive file lock + compare-and-set), and
that is all it ever claimed to be. Nothing arbitrates *between* instances.

## Why the ledger's answer does not transfer

`ledger_sync` already moves shared state over git and merges correctly. It is tempting to
add `incidents/index.json` to `TRACKED_FILES` and be done. That is wrong, and the code
already says why: *"the index because it is not merge-safe"*.

The ledger merges because it is **append-only** with **content-addressed** ids
(`sha256(pattern + fix)`), so two instances that learn the same lesson produce the same
line, and a conflict reconciles by union. The index is neither: it is a mutable map keyed
by a locally-assigned counter, so a git merge is last-writer-wins and would silently drop
one instance's incident — or worse, hand two instances the same row and let each believe
it owns the work.

**The transferable part is the *shape*, not the file.** Make claims append-only and
content-addressed and they merge for the same reason lessons do.

## The contract

### 1. A claim is an append-only, content-addressed record

`claims.jsonl` beside `ledger.jsonl`, tracked by the existing `ledger_sync` transport:

```json
{"claim_id": "<sha256(signal_id + instance_id + claimed_at)>",
 "signal_id": "cloudwatch:alarm/Shared",
 "instance_id": "<stable per-install id>",
 "claimed_at": "2026-07-31T20:14:03Z",
 "released_at": ""}
```

`signal_id` is `source:native_id` (`models.Signal.create`), which is **identical on every
instance** for the same provider object — that is what makes it a usable arbitration key
without a coordinator.

Append-only means a merge is **reconcilable** to a union, and content-addressing means a
re-published claim is the same line rather than a duplicate.

**Verified, and the result corrects a tempting overstatement.** Two instances claiming the
same signal blind to each other, merged with a real `git merge
--allow-unrelated-histories`:

```
merge exit: 1 | conflict markers: True
claims visible to B after merge: 2
    inst-b 2026-07-31T20:00:05Z
    inst-a 2026-07-31T20:00:00Z
deterministic winner: inst-a (earliest claimed_at)
```

Git **does** conflict — both instances appended to the same region, exactly as the ledger
does. What append-only buys is not a clean merge; it is that **no claim is lost** and the
conflict is mechanically reconcilable. So `claims.jsonl` needs the same marker-tolerant
read the ledger already has (`read_entries` skips malformed lines; `resolve_conflict`
rewrites the union). Reusing that reconciler is a requirement of this design, not an
optional nicety — without it a conflicted `claims.jsonl` would read as *zero* claims and
every instance would believe it won.

### 2. Arbitration is lowest-`claimed_at`, ties broken by `instance_id`

After a pull, an instance holds every claim for a signal. The winner is the earliest
`claimed_at`; ties (identical timestamps, which a coarse clock makes likely) break on
lexicographically-lowest `instance_id`. Deterministic, so every instance computes the same
winner from the same file with no messages exchanged.

A loser **releases**: append a record with `released_at` set, transition its local
incident to a terminal state, and stop investigating. It does not delete the row — the
duplicate claim is real history and the audit trail should show that two instances raced.

**This is deliberately not a lock.** No leases, no heartbeats, no consensus. Losing a race
costs one wasted poll, and the cost of getting distributed locking wrong (a stuck lease
holding an alarm unworked) is far worse than the cost of an occasional duplicate turn.

### 3. Clock skew is bounded, not trusted

Lowest-`claimed_at` uses wall clocks on independent machines. Skew is real, so:

- the tie-break makes the outcome deterministic even with *identical* timestamps
- a claim whose `claimed_at` is more than `MAX_CLAIM_SKEW_SECS` (suggest 300) in the
  future is **ignored** for arbitration — a wildly-wrong clock cannot let one instance win
  every race by claiming to be first
- skew never causes *both* to proceed: worst case is that the wrong instance wins, which
  costs nothing beyond which agent does the work

### 4. Incident ids must stop colliding

`INV-1` on two instances is undefined across a mesh. Prefix the instance:
`<instance_id>/INV-1`. Local display can keep the short form; anything crossing the mesh
(Slack thread keys, chat slot keys, ledger references) uses the qualified form.

This is a schema change to a persisted id, so it needs a migration path for existing
single-instance installs — and it is the one part of this design that touches shipped
data. Worth doing before a mesh exists, not after.

### 5. What the transport must provide — and what it must not

`ledger_sync` already suffices: `claims.jsonl` joins `TRACKED_FILES` and rides the same
pull → hygiene → index → push cadence. The mesh therefore needs **no new transport**,
which is the useful conclusion here: SSH keys, a message bus, and a group-chat protocol
are all orthogonal to arbitration, and none of them is required to stop double-claiming.

The dispatch index still must **never** sync, for the reason already recorded. Claims are
the shared fact; the index stays local derived state.

## Consequences accepted

- **Arbitration is eventually consistent.** Between a claim and the next pull, two
  instances can both be investigating. The window is the sync interval; the resolution is
  that the loser releases. A mesh that cannot tolerate one duplicate turn needs a real
  coordinator, which is a much larger change and is not what "a mesh of Ops agents on an
  internal network" asks for.
- **A malicious member can claim everything.** Every member can write the repo, so a
  hostile instance could publish claims with early timestamps and starve the others. This
  design assumes the same trust boundary the shared ledger already assumes — a team repo
  whose members can already write each other's lessons. Worth stating explicitly rather
  than implying the design is Byzantine-tolerant.
- **Group chat is unspecified.** It is a separate feature; it does not gate arbitration.

## Open question for the owner

Should a losing instance's incident be `resolved` or a new `superseded` status? `resolved`
overloads a status that asserts "the condition cleared", which is not what happened.
`superseded` is more honest but adds a status to the grammar and every UI that renders it.

Recommendation: **`superseded`** — the grammar is `LEGAL_TRANSITIONS`-driven and adding a
terminal status is a data change, and mislabelling a race as a resolution would corrupt
the one signal (`resolved` counts) an operator reads to judge whether the app is working.
