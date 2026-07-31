---
cron: ops-mission-control/reconcile
schedule: "*/15 * * * *"
tier: always
silent: true
---

# SOP: Reconcile board against provider truth

Keeps the board honest. Without this it drifts into fiction: incidents sit
`investigating` for signals that cleared an hour ago, so the board stops being a
statement about the world and people learn to ignore it.

**You do not manage Slack.** The pin board maintains itself — recording a status
change updates that incident's Slack message in place, automatically. There is
nothing here to pin, unpin, or react to; do not try, and do not hand-post state
changes. Your whole job in this SOP is to make the *incident status* true, and the
channel follows.


## Authenticate first

```bash
URL=$(kirocrew token 2>/dev/null | grep -oE 'http://[^ ]+' | head -1)
BASE="${URL%%\?*}"; TOKEN="${URL#*token=}"
```

Reuse `$BASE`/`$TOKEN` for every call below and pass `?token=$TOKEN`. Never hardcode a
port and never hunt for a token elsewhere — see SKILL.md § Calling the API for why.

## Pass 1 — signals that cleared

1. `GET /api/apps/ops-mission-control/incidents?status=investigating`, then again
   for `needs_human` and `dispatched`.
2. `GET /api/apps/ops-mission-control/signals` to see what is still firing. Compare
   on the signal **`id`** (e.g. `github-issues:cli/cli#14001`) — comparing titles
   will mismatch the moment a provider edits one.
3. For each open incident whose signal is **no longer** in the firing set:

   ```bash
   curl -sS -X POST "$GATEWAY/api/apps/ops-mission-control/incident/transition" \
     -H 'Content-Type: application/json' \
     -d '{"id": "INV-7", "status": "resolved",
          "resolution": "signal cleared at the provider — no longer firing"}'
   ```

   A `409` means that transition is not legal from the incident's current state.
   Read the error; do not retry blindly.

## Pass 2 — a cleared signal is not always a fixed problem

Before resolving, check whether the incident already carries a diagnosis. A signal
that stopped firing because someone fixed it and one that stopped because it flapped
look identical from here. If there is no diagnosis and the signal cleared on its own,
say so in the `resolution` ("cleared without diagnosis — may recur") rather than
implying it was solved. A ledger entry written from a guess is worse than none,
because the next responder inherits it with confidence attached.

## Pass 3 — stale release

Incidents idle beyond the stale window are already released by the dispatch
heartbeat, so there is normally nothing to do. Verify none are stuck in `stale` while
their signal is still firing — that combination means work is being dropped, and it
is worth one line to the channel.

## Rules

- Cap at 10 incidents per run to stay inside provider rate limits.
- **If nothing changed, exit silently.** No "board is clean" message.
