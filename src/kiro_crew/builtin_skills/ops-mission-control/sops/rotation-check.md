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

   This job ships enabled because it is the only thing that resumes the `on_shift`
   tier: `dispatch` and `reconcile` both ship paused, and the tier is armed here. Ship
   this one paused and nothing ever fires no matter what the operator configures.
   (`ledger-hygiene` also ships enabled — it sits on the `primary` tier, which nothing
   arms from here, so it self-gates on `is_primary()` at runtime instead.)

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

- `unknown: true` means the rotation source could not answer. **Do not infer arming
  from it — read `tiers.on_shift` and do exactly what it says.** The two sources
  answer an indeterminate case differently, on purpose: a rotation *API* reports
  `on_shift: true, unknown: true` (a network fault must never silently switch off a
  team's incident response), while a committed `rotation.yaml` reports
  `on_shift: false, unknown: true` (if the file cannot say this operator owns the
  shift, assuming they do is how every teammate ends up claiming the same alarm).
  `unknown` is an explanation for the operator, never an arming input.
- Only ever pause or resume the names in `tier_crons.on_shift`. The `always` tier
  includes this job, and the `primary` tier owns nightly ledger maintenance —
  touching either from here is out of scope and, in the first case, unrecoverable.
- With no rotation source configured the default is always-on, so a solo operator
  gets continuous coverage rather than a tier that never fires.
