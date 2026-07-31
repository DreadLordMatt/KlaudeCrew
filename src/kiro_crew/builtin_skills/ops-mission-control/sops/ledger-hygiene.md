---
cron: ops-mission-control/ledger-hygiene
schedule: "0 3 * * *"
tier: primary
silent: true
---

# SOP: Ledger hygiene

Keeps the knowledge ledger from rotting. A ledger that only accumulates becomes
noise, and noise is exactly what the tribal-knowledge approach this replaces
already suffered from.

Runs on the `primary` tier only — a team with five instances sharing one ledger
must not run this five times.


## Authenticate first

```bash
URL=$(kirocrew token 2>/dev/null | grep -oE 'http://[^ ]+' | head -1)
BASE="${URL%%\?*}"; TOKEN="${URL#*token=}"
```

Reuse `$BASE`/`$TOKEN` for every call below and pass `?token=$TOKEN`. Never hardcode a
port and never hunt for a token elsewhere — see SKILL.md § Calling the API for why.

## Steps

1. Run the hygiene pass. It is an HTTP endpoint, not a Python call — there is no
   interpreter for you to call `ledger.hygiene()` from:

   ```bash
   curl -sS -X POST "$GATEWAY/api/apps/ops-mission-control/ledger/hygiene"
   ```

   It returns `{"summary": {"deduped": N, "decayed": N, "pruned": N}, ...}` and
   performs three things deterministically, so no judgement is needed here:
   - **Dedupe** by content-addressed id, merging fingerprints and keeping the
     highest use count. Duplicates arrive when two people learn the same lesson.
   - **Decay** confidence one step for entries unused past the decay window. An
     entry nobody has needed in three months should not still claim `high`.
   - **Prune** to the entry cap, dropping least-used / weakest / oldest first.
     The cap exists because matched entries are read into an investigation's
     context — an unbounded ledger is an unbounded token cost.

2. Review what remains for entries that contradict each other. Two entries with
   overlapping fingerprints and different fixes mean the failure has more than one
   cause — split the pattern descriptions so each is distinguishable, rather than
   deleting one.

3. Promote `observed` → `verified` for any entry whose fix has now been applied
   successfully more than once. That promotion is what unlocks the fast path, so it
   is worth doing deliberately.

   There is no separate "update" route: re-post the entry with its **exact same
   `pattern` and `fix`**. Ids are content-addressed over those two fields, so this
   merges into the existing entry — fingerprints union, `use_count` carries forward,
   and trust upgrades to `verified`. It can never weaken what is already known.

   ```bash
   curl -sS -X POST "$GATEWAY/api/apps/ops-mission-control/ledger" \
     -H 'Content-Type: application/json' \
     -d '{"pattern": "<byte-identical to the stored pattern>",
          "fix": "<byte-identical to the stored fix>",
          "confidence": "high", "trust": "verified"}'
   ```

   Change a single character of `pattern` or `fix` and you get a NEW entry rather
   than a promotion, leaving a near-duplicate for the next hygiene run to dedupe.
   Read the entry first (`GET /ledger`) and copy the fields verbatim.

4. Report only if something changed. A no-op night produces no output.

## Rules

- Never delete an entry that has been used. Decay it instead — use count is
  evidence that it described something real.
- Do not invent entries here. This job curates what investigations recorded; it
  does not author knowledge.
