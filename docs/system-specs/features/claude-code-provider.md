# Claude Code ACP backend — live in this fork (KlaudeCrew)

> **Fork note:** this doc originally recorded a DIFFERENT, older removal — a
> standalone (non-ACP) Claude provider and a Bedrock provider, both deleted
> during upstream's de-Amazoning along with their agent-renderer/mirror
> modules and the dashboard's provider selector. Those stay gone; they were a
> separate code path (a full alternate `LLMProvider` implementation) from
> what this section now describes. What upstream ALSO ships — and what this
> fork changes — is a second, still-ACP seam: `acp/client.py`'s
> `ACP_BACKEND_CLAUDE` / `_is_claude` branch, which upstream keeps dormant
> (`agent.provider` fixed to `acp`, kiro-cli the only backend selected) so
> that "an internal companion package can re-register" it. **This fork is
> that companion.** See `AGENTS.md` § "This fork: Claude Code is the default
> ACP backend" for the overview.

## How the fork re-enables it

`agent.acp_backend` (config, enum `"claude"`/`"kiro"`, default `"claude"`)
drives `KiroCrewConfig.create_provider_factory()`'s `_acp()` closure, which
passes `acp_backend=ACP_BACKEND_CLAUDE` into `AcpProvider`/`AcpClient` unless
the operator opts out. The ~30 `_is_claude` branches already in
`acp/client.py` (protocol version, `session/set_config_option` vs
`session/set_model`, permission-option field names, etc.) needed no changes
— they were written for exactly this. The registration glue two of those
branches look up via `getattr` — MCP server injection, the
`settings.local.json` permission seed, and session-transcript lifecycle — is
supplied by the new `src/kiro_crew/klaude/` package, attached through the
`ProviderRegistry` extension point (`klaude/registry.py`, swapped in at
`platform/bootstrap.py`) rather than by editing `acp/client.py` itself.

The seam's binary-resolution details (`_resolve_claude_acp_bin`, the per-session
`settings.local.json` permission routing, `CLAUDE_CONFIG_DIR` isolation) are
documented in [`acp-client.md`](../modules/acp-client.md) §Backend Selection.

## Model registry

`model_registry.json` (loaded by `model_registry.py`, mirrored to
`website/src/model_registry.json` and guarded by `test/test_model_registry_parity.py`)
remains the single source of truth for model names and context windows. Its
per-entry `providers` map keys models under the canonical `claude_code` namespace
— that is just the canonical key form (the public ACP model-id shape, e.g.
`global.anthropic.claude-opus-4-8[1m]`); it is not a selectable provider.

Canonical keys include `fable-5-1m` (leads the JSON but is **not** default —
Opus 4.8 `opus-4.8-1m` remains the sole `"default": true` entry), and
`available_models()` (`model_registry.py`) is default-first-ordered, so a
non-default entry sitting ahead of Opus in the file cannot change the
`auto`-path pick. Each entry's `aliases` also include the bare, prefix-stripped
provider-id spelling (e.g. `claude-fable-5`) so `from_provider_id` folds bare
ids onto the canonical key.
