# Powers Module

Last Updated: 2026-07-26 (initial: `POWER.md` bundle parser, `PowersStore` over `powers_dir()`, `installed.json` provenance schema, inert install with atomic stage/rollback/commit, `powers_providers` registry (official monorepo + marketplace mirror), `fetch.py` bundle-download security controls, five `/api/powers*` routes. **Activation — trust grant, enable/disable, MCP registration, generated docs skill — is deliberately NOT in this module yet; see "Deferred: activation".**)

## Overview

A **Power** is an installable capability bundle from the upstream Kiro ecosystem. Upstream Kiro IDE 0.7 (Dec 2025) ships them via a marketplace at `https://kiro.dev/powers`; `kiro-cli` has **no** Powers runtime (verified on 2.14.0: no subcommand, no `~/.kiro/powers`, upstream lists CLI support as "coming soon"), so KiroCrew implements Powers itself.

The implementation deliberately adds **no new runtime**. A Power is materialized onto primitives KiroCrew already has:

| Power mechanic | Existing subsystem it uses | Status |
|---|---|---|
| Registry search / detail / install | the shape of `mcp_providers` + `handlers/mcp_discover.py` | **implemented** |
| MCP server registration | the single write path `dashboard/handlers/mcp._set_kirocrew_entry` | deferred |
| On-demand guidance | a generated skill under `skills_dir()/powers/<name>/` consumed by `SkillsLoader` (memory-skills-hooks.md) | deferred |
| Just-in-time tool loading | `AgentConfig.tool_search` deferred MCP tools (providers.md) | deferred |

Modules: `powers.py` (parser + store + installer), `powers_providers/` (`base`, `official`, `marketplace`, `fetch`), `dashboard/handlers/powers.py` (HTTP).

**UI surface:** `/powers`, registered in the left rail's **Apps** group (`website/src/surfaces/builtins.tsx`), rendered by `PowersPage` → `PowersTab`. Powers sit under Apps rather than in the Agent Capabilities tab strip because a Power is an installable unit with its own browsable catalog, closer to the app grid than to the per-agent configuration surfaces (Skills, Hooks, MCP) that Capabilities hosts. The rail row is deliberately **not** `appOnly` — `App.tsx` builds its Apps list from `getBuiltinSurfaces()`, which filters `appOnly` and `hiddenFromNav` out, so either flag would remove the row entirely. The icon (`components/icons/PowerIcon.tsx`) is Kiro's upstream Powers bolt mark, filled with `currentColor` so it themes like the lucide glyphs beside it.

## Bundle format (upstream contract)

```
<power>/POWER.md       REQUIRED — YAML frontmatter + markdown body
<power>/mcp.json       optional — {"mcpServers": {...}}; present ⇒ "Guided MCP Power",
                       absent ⇒ "Knowledge Base Power"
<power>/steering/*.md  optional — workflow docs
```

`POWER.md` frontmatter recognises **exactly five** fields: `name` (required, `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`), `displayName` (required), `description` (required), `keywords` (optional `list[str]`), `author` (optional). Upstream states `version`, `tags`, `repository`, `license` do **not** exist. Unknown keys are tolerated and ignored — never invented. `parse_power_md` rejects input over 256 KiB.

**Only these files are ever copied.** `_copy_power_files` copies `POWER.md`, `mcp.json`, and `steering/*.md` and nothing else. This is a security boundary, not tidiness: the `folder` install kind takes a caller-selected path, and vetting only the selected root is insufficient — an allowed ancestor (a home directory) can contain a valid `POWER.md` *and* sensitive descendants, so a recursive copy would relocate credentials or governance files into the agent-readable powers directory. An allowlist copy makes that unrepresentable.

## Scope: install is inert (contract)

This module downloads a bundle, puts it on disk, lists it, and removes it. It does **not** activate anything, and that is a security property rather than an omission — it is what makes install safe to offer for arbitrary third-party GitHub repositories.

An installed Power is unreachable by construction, on both halves:

1. **No MCP server is registered.** The bundle's `mcp.json` is never parsed for specs. `_declares_mcp_servers()` is a presence test used only to label the Power `mcp` vs `knowledge` in the UI, so a declared `command` has no path to `_set_kirocrew_entry` or any other execution site. A malformed `mcp.json` therefore cannot break an install; it just reads as `knowledge`.
2. **No skill is materialized.** `POWER.md` and `steering/*.md` stay inside `powers_dir()`, and nothing else reads that directory — `skills.py`, `context.py` and `agent.py` contain no reference to `powers_dir()`. Third-party markdown cannot enter agent context, so the prompt-injection surface is closed rather than gated.

Consequently `installed.json` carries **no** `trusted` or `enabled` field. A trust flag that gated nothing would assert a security control the code does not implement; the record is provenance only. `test/test_powers.py::TestInertInstall` pins all three claims (no MCP config written, no `SKILL.md` anywhere, no activation keys in the record).

The UI states this plainly instead of leaving the absence unexplained: an installed Power shows an **Inactive** badge and a note that no MCP server is registered and no guidance is loaded. There is deliberately no disabled-looking toggle, since that would imply an activation path that does not exist. `PowersTab.test.tsx` asserts the *absence* of any switch or trust control.

## Deferred: activation (pull model)

Activation is a separate change. Its shape is taken from the IDE's own
implementation rather than invented here — see *Upstream Powers implementation*
below for the evidence. The short version: **the agent pulls a Power's
capabilities through KiroCrew-owned tools; nothing about a Power is pushed into
agent configuration or agent context.**

### Tool surface

Four tools on the **existing** `kirocrew-core` MCP server. No new server is
registered, and `~/.kiro/agents/kirocrew.json` is not written at all.

| Tool | Purpose |
|---|---|
| `power_list(filter?)` | Installed Powers with name, description, `kind`, and declared MCP server names |
| `power_learn(power)` | Tool inventory for that Power's servers — requires starting them, so this is the first consent point |
| `power_use(power, server, tool, arguments)` | Dispatch one call to one tool of one server |
| `power_steering(power, file)` | Return one `steering/*.md` file's contents |

`power_list` needs the server *names*, which means activation is the point where
`mcp.json` stops being merely presence-tested and starts being parsed. That is
the exact boundary at which this PR's inert property ends, and it should be
called out in the activation PR's description rather than left implicit.

### What the pull model removes

Each item below was a required deliverable of the previous push design. None of
them exists in this one — they are not solved, they are absent:

- **No merge into the rendered agent config.** The constraint that dominated the
  old plan — `includeMcpJson` is `false`, so sessions read the rendered
  `~/.kiro/agents/kirocrew.json` and every writer must call
  `agent.rebuild_agent_config()` while uninstall additionally strips entries by
  hand — simply does not apply. Nothing is written, so nothing must be rebuilt or
  stripped, and enable can no longer be a silent no-op.
- **No `power-<power>-<server>` namespace.** With no registered entries there is
  no name to collide: the injectivity problem (both segments admit hyphens, so
  `power-git-` also prefixes `power-git-helper-srv`) and the ownership record that
  a non-prefix purge required both disappear. The Power name and server name stay
  arguments to a call.
- **No skill materialization.** Guidance is returned as a tool *result* for the
  one file the agent named, so third-party markdown never enters a session
  unasked, never competes for the steering budget, and cannot be triggered by
  `SkillsLoader` keyword matching.
- **No `trusted` / `enabled` fields.** This is the load-bearing consequence for
  the current PR: `installed.json` as shipped here needs **no migration**, and the
  "readers must treat a missing key as false" requirement becomes moot because no
  such key is ever added.
- **No enable/disable transaction.** The install/remove transaction — one blocking
  callable with a pinned root descriptor — does not need to grow two more state
  transitions over the same lock.

### Consent

The trust flag is replaced by the approval mechanism the rest of the product
already uses. The first `power_learn` or `power_use` for a Power in a session
raises an approval naming the Power, the server, and the **resolved command** that
would run; per-session Trust auto-approves that Power for the rest of the session.

This is deliberately the same decision PR #518 reached: its round-4 change deleted
bespoke trust grants (`slot._trust`, `_enforce_trust_ttl`, `_TRUST_TTL_SECS`) and
routed workers through core approvals instead. A second bespoke trust store for
Powers would repeat the mistake that change corrected.

A durable "always allow this Power" is **out of scope** for the first activation
change. Session-scoped consent is strictly safer, and a persistent grant is a
separate decision with its own revocation surface.

### Execution

`power_use` spawns the Power's server on demand using the same hardening the
existing `McpToolClient` (`cron_script.py`) applies to config-declared servers:
`wrap_argv(..., mode="standard")`, `cgroup_scope_argv()` for the DoS ceiling,
`resource_limit_preexec()`, and stderr captured to a file so a handshake failure
is legible instead of surfacing as a generic disconnect. The one difference is
where argv comes from: an explicit spec parsed from the Power's `mcp.json`, not
`_resolve_mcp_server()` over agent config.

### Where the risk now concentrates

The push design spread risk across config merging, purge correctness and context
injection. The pull design collapses it into a single chokepoint — the process
`power_use` starts — which is easier to reason about but must carry all of:

1. **Spec validation** before spawn: shape of `command` / `args`, no shell
   interpolation, no argv assembled from the tool's own arguments.
2. **Environment discipline**: no ambient inheritance of the gateway's
   environment; an explicit allowlist, since the bundle author chose the command.
3. **Per-call timeout and output bound**, with results passing through the
   existing mandatory redactor (`powers_providers/redact.py`) — a Power's tool
   output is third-party text on its way into a transcript.
4. **SEL audit per invocation**, recording `(power, server, tool)` and the consent
   decision. The old plan's `power_trust_grant` / `power_enable` events are
   replaced by an event per call, which is a better record: it says what actually
   ran, not what was once permitted.
5. **A bash-layer write guard for the bundle's `mcp.json`.** Today
   `powers/installed.json` and `powers/.marketplace-cache.json` are in
   `_WRITE_PROTECTED_BASH_LEAVES` (`security.py`) while the bundle tree itself is
   only covered on the file-tool path — proportionate while nothing in the bundle
   is ever executed. Activation changes that: `mcp.json` becomes the source of an
   argv KiroCrew spawns, so a prompt-injected shell write to
   `powers/<name>/mcp.json` becomes command injection with a consent prompt
   attached to the *Power's* name. This requirement carried over from the push
   design, where the trigger was a forgeable `trusted` flag; under the pull model
   the trigger is different but the guard is the same and it is more clearly
   load-bearing, because the file is read on every call rather than once at enable.

### One deliberate divergence from upstream

The IDE has no consent prompt — install is one click and the Power's tools are
then callable. KiroCrew adds one anyway, because the IDE always has a human at the
keyboard while KiroCrew runs turns from cron, Slack and Discord where nobody is
watching. This divergence is intentional and should survive review rather than
being "fixed" toward upstream parity.

## Upstream Powers implementation (observed, not inferred)

Read from `kiro-team/kiro-extension` (`src/extension/powers/**`,
`packages/kiro-shared-types/src/powers.ts`). Recorded because the previous version
of this spec guessed at upstream behaviour and guessed wrong.

**Tool surface.** `listPowers`, `usePower(powerName, serverName, toolName,
arguments)`, `readPowerSteering(powerName, fileName)`, and `configurePowers`
(ships behind `ENABLE_WEBUI = false`). The extension's `usePower` is itself a stub
that defers to a native implementation, but the *contract* is the namespaced
dispatch shown above. `listPowers` returns `mcpServers` names and `keywords`
without starting anything.

**Steering access rules** — independently identical to ours: `.md` only, no path
separators, no leading dot, and a validated join against the steering directory.

**On-disk layout**, at `~/.kiro/powers` (`KIRO_POWERS_HOME` overrides):

```
installed.json            {version: "1.0.0", installedPowers: [{name, registryId, autoInstalled?}],
                           dismissedAutoInstalls: [...]}
installed/<name>/         the bundle
registries/<id>.json      one file per registry, plus user-added.json
registry-repos/<id>/      cloned registries
repos/<power>/            cloned power repositories
```

**Divergences from this module, stated so they are choices rather than accidents:**

| | Upstream | Here |
|---|---|---|
| Home | `~/.kiro/powers` | `~/.kiro/crew/powers` (our data home) |
| Record | versioned envelope, array of entries, provenance is `registryId` | name-keyed map, provenance is `{kind, ref}` |
| Bundle path | `installed/<name>/` | `<name>/` |
| Field names | `tags`, `repositoryUrl` | `keywords`, `githubUrl` (`keywords` matches upstream's own `listPowers` output) |

Because the two homes are different trees there is no collision — and no interop:
a Power installed through KiroCrew is invisible to the IDE and vice versa. That is
acceptable while nothing is activated, and it is the first thing to revisit if
`kiro-cli` ships a native Powers runtime. The `Power` type upstream carries
`iconUrl`, which is the field this PR's registry cards use.

## `installed.json` schema

`powers_dir()/installed.json`, written via `atomic_write` under the Powers lock. Keyed by power name:

```jsonc
{
  "<power-name>": {
    "source": { "kind": "registry|github|folder", "ref": "<resolved-url|path>" },  // contract
    "installedAt": "<ISO-8601 UTC>"   // incidental (display only)
  }
}
```

Activation keys (`trusted`, `enabled`, `mcpServers`) are **absent by design** — see "Scope: install is inert" — and under the pull model (see *Deferred: activation*) they are never added, so this schema is expected to survive activation unchanged. Consent is per-session and lives in the approval mechanism, not on disk; the server names activation needs are read from the bundle's own `mcp.json` rather than copied into the record. An earlier draft of this spec promised the opposite ("when activation lands it adds them"), which would have obliged every later reader to treat a missing key as `false`; that obligation is withdrawn.

`source.ref` records the **resolved** provenance: for `registry` / `github` installs it is the
canonical GitHub tree URL the bundle actually came from, not the provider-scoped slug the
caller sent (a marketplace id is meaningless outside its provider, and storing it makes the
installed record disagree with the card's repository). For `folder` it is the vetted path.

Fields marked incidental may change shape; contract fields may not without a migration.

Not part of the schema: the bundle tree itself (`powers_dir()/<name>/`), the marketplace cache (`powers_dir()/.marketplace-cache.json`), and transient `.staging-*` / `.backup-*` scratch dirs (dot-prefixed so they can never collide with a valid power name, and invisible to `list_powers()` which enumerates `installed.json`, not the directory).

## Install and remove atomicity

A Power's identity is the `name` in its `POWER.md`, and nothing reserves that name globally: a monorepo directory and an independent author's repository can both declare `kb`. Installing the second used to replace the first bundle **and its provenance record**, leaving the store describing a Power the user never chose. An install is therefore **refused with 409** when a record for that name already exists and its recorded source differs (`PowerSourceConflict`); the caller uninstalls first if the replacement is intended. The check runs inside the transaction, after crash recovery, so it cannot race a concurrent install or compare against a record whose bundle is about to be restored.

The rule is narrower than "the source differs". `folder` -> `folder` is exempt, because that is the development loop — pointing the installer at a rebuilt or relocated directory — where the path is incidental rather than provenance and there is no third party to impersonate. Every other combination involves at least one remote source, including `folder` -> `registry` and `registry` -> `folder`, since losing a recorded upstream to a local directory of the same name is the same loss in the other direction. Same-source reinstalls are always allowed, which is what keeps the upgrade path open.

`install_from_dir` stages into `powers_dir()/.staging-<name>-<pid>`, copies **only** the contract files (`POWER.md`, `mcp.json`, `steering/*.md`) rather than the source tree, re-parses the *staged* `POWER.md` and requires its `name` to still match, moves any live bundle aside to `.backup-<name>`, then renames staging into place. The backup is **retained until the `installed.json` write commits** — which happens inside the same transaction — so there is no window in which the tree is live and unrecorded, and a failure after the swap restores the previous bundle rather than leaving a half-migrated install. The backup name is deterministic (no pid): a pid-suffixed name is unrecoverable by any later process, so a kill between the two renames stranded the bundle under a name nothing would look for.

Two source-mutability points drive that shape: the install source is caller-owned, so an allowlist copy makes "an allowed ancestor also contains `~/.ssh`" unrepresentable, and a rewrite between validation and copy would otherwise commit a different Power under an already-validated name.

### Crash recovery never destroys bytes

Recovery is deliberately conservative because a filesystem state cannot always say what the interrupted transaction intended:

- `backup` present, `dest` absent — unambiguous: the previous transaction died between its two renames, so the backup is restored.
- **Both present** — ambiguous, and not resolvable from disk: a kill after the swap leaves new bytes at `dest` and the previous bundle at `backup`, with no record of whether the metadata write committed. The backup is preserved under a timestamped `.orphaned-backup-<name>-<ts>` with a logged warning rather than deleted, because deleting it discards the only copy of the bundle the live record may still describe.
- `staging` is always safe to discard: it is pre-commit scratch and never the only copy of anything.

Every transaction begins by reconciling the store in both directions (`_reconcile_store`). `_prune_absent_records` drops records whose bundle is gone — from an interruption, or from a bundle deleted by hand. `_sweep_orphaned_removals` deletes `.removing-<name>` trees that no record names any more, which is the residual of the ordering below. Neither repair needs anyone to retry the specific Power that was interrupted: the next install or uninstall of any Power converges the store. Record pruning deliberately skips names with a pending `.removing-*` or `.backup-*` tree, which are mid-recovery rather than absent; running it before the backup-recovery step stranded exactly that case.

A leftover `.removing-<name>` whose name is **still recorded** is an uninstall interrupted before its record was committed, which means the scratch tree is the only copy of those bytes. `remove_power` **restores it to its own name and re-runs the durable path** rather than deleting it directly: deleting first and dropping the record afterwards is the ordering this section removed, and doing it in the retry branch would destroy the bundle permanently whenever the record replacement then failed. A leftover whose name is **no longer recorded** is the ordinary residual of a committed removal whose cleanup failed; it is reclaimed and reported as already removed, because raising there reports a failure for a removal that succeeded.

`remove_power` renames the bundle to `.removing-<name>`, **commits the record, and only then destroys those bytes**. The rename is what makes committing first safe, and it is the whole reason the ordering can be this way round:

- The rename proves the tree can be moved before anything irreversible happens, and parks it at a name **derived from the record's own name**, directly under the root. So a delete that fails afterwards leaves an *enumerable* leftover, not an invisible one — `_sweep_orphaned_removals` finds it by prefix without needing a record to point at it.
- Committing the record is therefore the single step that decides the outcome. Before it, nothing is authoritative and a failure restores the bundle to its own name with nothing lost. After it, the Power is removed as far as every reader is concerned, and a delete that fails is reclaimable garbage rather than a failed removal — so it is logged and swept later, not raised.

This ordering was reversed for most of the PR's life, on the reasoning that destroying the bytes first could never orphan them. That protected against a crash but made a **failed** uninstall destructive: with the bundle already deleted, a record replacement that fails — a held-open `installed.json` on Windows is the concrete case — returned an error for a Power that was in fact gone. The bytes were unrecoverable at that point, so no amount of reconciliation could undo it. Trading that for a leftover directory is the better side of the exchange, and the rename-aside is what makes the leftover cheap.

Deletion failures are never suppressed: swallowing them let the caller drop the record and report success while the bundle was still there.

The record write is still **staged before** anything is destroyed, which a full filesystem makes load-bearing: staging allocates the space and fsyncs the bytes up front (`_stage_write_at`), and the commit is a rename within one directory (`_commit_staged_at`) that needs no space. An uncommitted staged file is discarded on every failure path, since it describes a removal that did not happen. A bundle with no record at all is deleted strictly — nothing is authoritative in that case, so a failed delete is reported as a failed removal.

Staging runs on **every** platform. An earlier revision applied it only where `dir_fd` is available and recorded the rest as a known residual; that was a real data-loss window kept for convenience, since staging needs only a temp file in the same directory and an atomic replace — both of which `os.replace` provides on Windows. Confinement and durability are therefore independent: the pinned descriptor is still POSIX-only and still gates confinement, while the record is durable before the delete everywhere.

### One blocking transaction per operation

`install_from_dir` and `remove_power` are each **one** blocking callable that takes the lock, performs every mutation, rolls itself back on failure, and releases the lock — all inside a single executor submission, awaited under `asyncio.shield`.

That shape is not a style choice; the alternative is unfixable in principle. Earlier revisions split the work across several executor calls and compensated from the coroutine afterwards, which produced a pair of findings that could not both be satisfied:

- A cancelled task re-raises from **every** subsequent `await` (proven on Python 3.10 in CI), so awaited compensation silently never runs and the bundle is stranded.
- Waiting synchronously to avoid that stalls the gateway event loop for the duration.

Each fix for one reintroduced the other, across five review rounds. With a single worker there is nothing to compensate from outside and nothing to settle: cancelling the await cannot interleave with the work, because the worker runs to completion in its own thread and owns its rollback. The lock is taken and dropped inside the same callable, so it can neither leak nor be released while mutations are in flight.

### Confinement comes from a pinned descriptor, not a check

`mcp.json` is capped the same way (`MAX_MCP_JSON_BYTES`, 256 KiB). `_declares_mcp_servers` runs on `GET /api/powers` for every installed Power, and the file is a server map rather than a document, so the 8 MiB whole-bundle budget was a far looser bound than it ever needs. Over-cap is treated as "declares nothing" rather than raising: this function answers a yes/no question used to label a card, and install-time validation is what refuses malformed bundles.

`POWER.md` is read through a capped reader (`_read_power_md`) everywhere: the pre-install parse of the caller's source, the post-install `load_power`, and the staged re-parse. `MAX_POWER_MD_BYTES` was already enforced by `parse_power_md`, but only *after* `read_text()` had pulled the whole file into memory — and the size is not fixed at install time, because the installed bundle stays editable and the install source is caller-owned. So the refusal existed while the memory exhaustion it was meant to prevent did not. Over-cap is a refusal rather than a truncation, since a truncated `POWER.md` either fails frontmatter parsing confusingly or parses as a different Power than the file describes.

The source `steering/` directory is refused when it is a symlink **or a Windows directory junction** (`_assert_not_reparse_point`) on the no-descriptor branch. `Path.is_symlink()` is False for a junction, so the old test fell through to `is_dir()` — which a junction satisfies — and globbed the target, copying external Markdown into the agent-readable Powers tree. The POSIX branch does not need this because it pins the directory with `O_NOFOLLOW` and enumerates through that handle; this is the same reparse-point class already guarded at the store root, applied on the branch that actually depends on a path check. A symlinked `steering/` is now refused rather than silently skipped, since skipping is what let the junction past.

Contract files are refused when `st_nlink > 1`. The sensitive-path refusal is path-based, so a hardlink defeats it: `steering/guide.md` can share an inode with a protected file, and the copy would place those bytes inside the Powers tree where an agent can read them back through `power_steering`. The symlink check does not catch this — a hardlink *is* the file, so `lstat` and `fstat` agree on identity and there is no link to detect. This is the same rule `hooks.safe_read_file_bytes_nolink` applies on the read side.

`_root_lock` opens the powers root once with `O_NOFOLLOW|O_DIRECTORY` and **every** mutation in the transaction goes through that descriptor: the renames, the staging `mkdir`, the recursive delete, the contract-file copy, and the `installed.json` write. Anchoring only the renames was not enough and the gap was not theoretical — a lexical `shutil.rmtree` walk reopens each component by name, so a root swapped mid-walk deleted an external tree, and a lexical state write put the record describing the store outside it. The recursive delete is therefore hand-written against `dir_fd` (`_rmtree_fd`) rather than delegated to `shutil.rmtree`, whose `dir_fd` parameter is newer than the Python versions CI runs, and the copy receives a descriptor for the staging directory it was given so a decoy of the same pid-derived name cannot receive the bundle.

The **source** side is pinned too, not just the store. The install source is caller-owned and mutable — the premise behind the staged-`POWER.md` re-parse — so `steering/` is opened once with `O_NOFOLLOW|O_DIRECTORY` and enumerated through that handle, and every contract file is opened relative to a pinned source descriptor. A `is_dir()`/`is_symlink()` check followed by `glob()` re-resolves the directory name, and per-file `O_NOFOLLOW` does not object to what it then finds: after the swap those are ordinary regular files that simply live somewhere the caller never offered.

The transaction's **reads** are anchored for the same reason. An anchored write over a lexical read is worse than neither: the record lands in the right place with content taken from a decoy, so an install rebuilds `installed.json` from foreign state and erases every other Power's provenance — a file that is intact, correctly located, and wrong. `_load_installed` therefore takes the handle inside a transaction. `list_powers` and `load_power` deliberately do not: they are read-only paths with no handle in scope, where a decoy yields a wrong listing but cannot destroy state. A validate-then-mutate sequence cannot promise this however tightly the two are placed: the root can be swapped for a symlink in the gap, and the mutation then acts *through* the link on directories outside the store. Pinning the identity makes the swap unobservable to the transaction.

`_SUPPORTS_DIR_FD` gates the descriptor path. Windows has no `dir_fd` support and keeps the path-based root check, where the residual exposure is much narrower because creating a symlink requires elevation or developer mode. That platform split is deliberate and must keep working.

The lock itself is a cross-process advisory file lock on `powers_dir()/.lock`, deliberately **not** the MCP file lock: this module writes no MCP config, and borrowing that lock would serialize installs against every unrelated dashboard MCP write.

All filesystem traversal, copying, and recursive deletion runs in `maintenance_executor()` — never on the gateway event loop, where slow storage would stall chat, dashboard requests, and heartbeat processing.

## HTTP API

All routes are dashboard-authenticated (see `dashboard/server.py` registration block).

| Route | Behaviour |
|---|---|
| `GET /api/powers` | `{installed: Power[]}` |
| `GET /api/powers/registry?q=&category=&scope=&limit=` | `{items, providers, stale?}` — fan-out search |
| `GET /api/powers/registry/detail?id=&provider=` | `{power: RegistryPowerDetail}` |
| `POST /api/powers/install` | `{source: {kind, ref, provider?}}` → `{power}`; the bundle is stored inert |
| `DELETE /api/powers/{name}` | remove tree + record |

There is no toggle or trust route: nothing is activatable yet. `POST /{name}/toggle` and `POST /{name}/trust` arrive with activation.

## Registry providers

`powers_providers` mirrors `mcp_providers`: a `PowersProvider` Protocol plus `PowersProviderRegistry` with fan-out search and per-provider failure isolation.

- **official** — GitHub contents API over `kirodotdev/powers`; one directory per power, filtered on the presence of `POWER.md`. Honours `GITHUB_TOKEN` when set; distinguishes 403 rate-limit from 404.
- **marketplace** — parses the **server-rendered** `https://kiro.dev/powers` listing. There is no public JSON API (`/api/powers` and `/powers.json` both fail; the sitemap does not enumerate powers). Each card yields a launcher `/launch/powers/add?name=<slug>` and a canonical GitHub tree URL; the pair is the essential data.

**Marketplace fragility is a known, bounded coupling.** Parsing is bounded by neighbouring launchers rather than a fixed character window — a fixed window silently pairs each card with its *predecessor's* repository. Display name comes from the launcher's own `aria-label`, author from the adjacent span, and scope from the "official Kiro power" badge plus AWS authorship. **Per-card category is not present in the listing** (categories appear only as aggregate filter counts), so `category` is always empty from this provider and the UI hides the facet rather than showing a dead control. On a parse failure the provider logs one warning, returns empty, and marks itself unavailable so the official provider keeps serving; results are disk-cached with a TTL (read and written off the event loop). Unavailability is **not latched**: the registry filters on `is_available()` *before* calling `search()`, so a permanent False would mean the provider could never retry — it therefore reports available again after a cooldown so the next search re-probes.

The official provider verifies each candidate directory actually contains `POWER.md`, using ONE recursive git-tree request rather than a per-directory contents call; a name-only denylist would advertise any future infrastructure directory as installable and the install would then fail on the missing file. A truncated or unavailable tree falls back to the name heuristic rather than showing an empty catalog.

The cache lives under `powers_dir()`, **not** the shared temp dir, because `fetch._resolve_ref` resolves a marketplace id to its source repository through it — it decides *which repository gets installed*, so a predictable world-writable path would let any local process point a familiar marketplace name at a repository of its choosing.

Marketplace ids are resolved to their canonical tree URL **server-side**, not by trusting a client-supplied URL, so the id the user clicked unambiguously determines what is installed.

### Bounded response reads

Every provider body is read through `base.read_capped(stream, cap + 1)`, never a single `StreamReader.read(cap + 1)`. `read(n)` returns whatever is buffered when it wakes and does **not** loop to fill `n`, so one call silently truncates any body larger than one wire chunk: measured against the live upstreams, it returned 57 KiB of a 649 KiB marketplace page and 9.8 KiB of a 25.6 KiB GitHub JSON document. The consequences were not read errors but *provider* errors — the marketplace scrape found zero cards and the official provider failed `json.loads`, both reported as "provider unavailable", and the browse view was empty against the real registry. The per-file byte cap was equally unenforceable, because a short read never reached it. The caller keeps its own `len(body) > cap` check, so overflow policy (raise vs. truncate) stays per call site.

This is only reachable through a real socket: the unit suite stubs `_http_get_json` / `_http_get_bytes` / `_fetch_html`, so `TestBoundedStreamRead` runs a loopback HTTP server instead.

### Overlapping providers

The official monorepo and the marketplace overlap substantially (26 of 82 entries at time of writing). Dedup is by canonical repository URL and **provider order decides ownership** — official first — but the losing duplicate still donates any facet the winner left blank (`description`, `author`, `category`, `keywords`). The official provider lists a GitHub directory, so those fields are structurally empty for it; dropping the duplicate outright rendered every overlapping Power authorless.

`scope` is deliberately **not** merged. The two providers genuinely disagree rather than one being blank: the official provider reports `official` because the Power lives in the official monorepo, while the marketplace reports the *authorship* facet (`aws` / `community`). The consequence is recorded rather than papered over: an AWS-authored Power that is also mirrored into the official monorepo answers to the **Official** scope chip, not the AWS one, while its card still shows `AWS` as the author.

One Power (`spark-troubleshooting-agent`) legitimately appears twice, because the official monorepo copy and the `aws-samples` original are different repositories and dedup is by URL.

## `fetch.py` security controls

- **HTTPS-only host allowlist**: `github.com`, `api.github.com`, `raw.githubusercontent.com`. Rejects other hosts, non-https schemes, userinfo-in-authority, and IP literals. Every download URL is re-validated.
- **No `git clone`** — files are pulled via the contents API / raw host only.
- **Traversal and symlink rejection** — `_safe_join` refuses `..`, absolute and drive components and verifies containment; any entry typed `symlink` (or not `file`/`dir`) is refused.
- **Bounds** — 8 MiB total, 4 MiB per file, 200 files, max tree depth 8, enforced incrementally.
- **Bounded overall timeout**, no unbounded retries.
- `POWER.md` is required; the temp tree is removed on every failure path (including cancellation), off the event loop.

## Upstream convergence (recorded intent)

Powers are IDE-only upstream today and `kiro-cli` support is on the roadmap. The
intended posture remains **detect-and-defer**: if `kiro-cli` gains a Powers
runtime with its own on-disk home, KiroCrew should detect it and stop managing
what the native runtime owns rather than racing it.

The pull model makes that handover cheaper than the push model would have. With
nothing merged into agent configuration there are no shared `mcp.json` keys to
race for and no rendered entries to strip — the handover is confined to the bundle
directory and the provenance record. The concrete divergences to reconcile are
tabulated under *Upstream Powers implementation* above; the one to preserve rather
than reconcile is KiroCrew's consent prompt, which exists because KiroCrew runs
unattended turns and the IDE does not.

## Related specs

- governance.md — SEL audit events for install / remove (and, with activation, trust grants)
- memory-skills-hooks.md — `SkillsLoader`, which will consume the generated docs skill once activation lands
- providers.md — `tool_search` deferred MCP tools, the mechanism intended to keep an enabled Power's tools out of upfront context
- security.md — sensitive-path guard (`is_sensitive_path`) used by `resolve_install_source`
