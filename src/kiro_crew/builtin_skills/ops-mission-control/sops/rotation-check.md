---
cron: ops-mission-control/rotation-check
schedule: "*/5 * * * *"
tier: always
silent: true
---

# SOP: Rotation check

Arms and disarms the `on_shift` automation tier so nobody has to remember to turn
anything on when their rotation starts.


## Authenticate first

```bash
URL=$(kirocrew token 2>/dev/null | grep -oE 'http://[^ ]+' | head -1)
BASE="${URL%%\?*}"; TOKEN="${URL#*token=}"
```

Reuse `$BASE`/`$TOKEN` for every call below and pass `?token=$TOKEN`. Never hardcode a
port and never hunt for a token elsewhere — see SKILL.md § Calling the API for why.

## Steps

0. **Stop immediately if the app is not set up.** `GET
   /api/apps/ops-mission-control/state`; if no entry in `providers` has
   `configured: true`, produce NO output and stop. There is nothing to arm, and this
   job runs every 5 minutes — on a fresh install it must cost nothing at all.

   This is the ONE cron that ships enabled, because it is the only thing that resumes
   the others: `dispatch` is armed by the `on_shift` tier, and the tier is armed here.
   Ship it paused and nothing ever fires no matter what the operator configures.

1. `GET /api/apps/ops-mission-control/rotation` — returns `on_shift`, `who`,
   `until`, `unknown`, the `tiers` map, `tier_crons` (cron names **per tier**), and
   `armed_crons` (the flat union of what is armed right now).

2. Compare `tiers.on_shift` against the live cron state (`cron_list`), acting **only
   on the names in `tier_crons.on_shift`**:
   - Armed but those crons paused → `cron_resume` each one.
   - Not armed but those crons active → `cron_pause` each one.
   - Already matching → **exit silently.**

   **Do NOT pause `armed_crons`.** That list is the union across every armed tier,
   and off shift it still contains `ops-mission-control/rotation-check` — this job. Pausing it
   strands the instance with no way to re-arm itself, which silently ends the team's
   incident response until a human notices. `tier_crons.on_shift` is the only list
   you may act on.

3. Notify only on a genuine transition (shift started or ended), once. A
   five-minute poll that announced its own findings would post 288 times a day.

## Rules

- `unknown: true` means the rotation source could not be reached. The tier map
  already reports `on_shift` as **armed** in that case — fail-open is deliberate.
  Do not "correct" this by pausing: failing to reach a rotation API must never
  silently switch off a team's incident response. Log it and move on.
- Only ever pause or resume the names in `tier_crons.on_shift`. The `always` tier
  includes this job, and the `primary` tier owns nightly ledger maintenance —
  touching either from here is out of scope and, in the first case, unrecoverable.
- With no rotation source configured the default is always-on, so a solo operator
  gets continuous coverage rather than a tier that never fires.
