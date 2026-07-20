"""Amazon-overlay checks for the wave-2 CPP consumption-site wiring.

Wave 1 (sandbox / security / slack_gate / identity / mcp_tooling) is covered by
``test_cpp_wiring_amazon``.  This suite covers the remaining points wired in
wave 2 — for each, it composes an ``amazon`` PlatformContext with an inline
overlay adapter and asserts the wired core consumption site now reflects the
EXTENDED amazon value:

* ``apps_loader``  — registered builtins include the companion's feature apps
  discovered from an extra ``manifest_sources`` directory, and the declared
  ``bundled_app_names`` are honored by orphan detection.
* ``registry``     — the clone-sandbox-mode trusted-host check includes an extra
  internal git host (an SSH clone to it is allowed ~/.ssh; standalone keeps an
  unknown host strict).
* ``embeddings``   — the embed client sources its model + remote endpoint from
  the amazon source, and SigV4-style signed headers come from the source's
  ``sign_request`` (no ``embedding_auth`` config needed).
* ``credentials``  — the context-routed ``redact`` still redacts (delegating to
  the amazon credential policy), and standalone-equivalent text is unchanged.
* ``telemetry``    — ``frontend_rum_config()`` returns the amazon RUM blob and
  ``record_event`` is invoked (captured by the overlay).
* ``providers``    — ``register_acp_backends`` is invoked exactly once at boot.

The core never imports a companion package — these inline overlays live in the
test (outside the core), exactly as ``test_cpp_wiring_amazon`` does.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import List, Optional

import pytest

from kiro_crew import embeddings
from kiro_crew.apps import registry as app_registry
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.platform import (
    PROFILE_AMAZON,
    build_default_context,
    current_context,
    set_context,
)

# ── Sentinel amazon values the test asserts on ──
# A *second* internal git host NOT in KiroCrew's Default trusted set, so the
# overlay must extend the set for an SSH clone to it to become "standard".
_AMZN_GIT_HOST = "git.internal.amazon.dev"
_AMZN_EMBED_MODEL = "amazon-internal-embed:1.0"
_AMZN_EMBED_ENDPOINT = "https://embed.internal.amazon.dev"
_AMZN_RUM_CONFIG = {
    "identityPoolId": "us-east-1:fake-pool",
    "applicationId": "kirocrew-amazon",
    "region": "us-east-1",
}
_AMZN_FEATURE_APPS = [
    "mimir",
    "code-reviewer",
    "team-manager",
    "secretary",
    "taskkeeper",
    "quip",
]


# ── Inline amazon overlay adapters ──


class _AmazonAppsLoader:
    def __init__(self, manifest_dir: Path) -> None:
        self._dir = manifest_dir

    def bundled_app_names(self) -> List[str]:
        return list(_AMZN_FEATURE_APPS)

    def manifest_sources(self) -> List[Path]:
        return [self._dir]


class _AmazonRegistryPolicy:
    def public_git_hosts(self):
        # The KiroCrew Default trusted set PLUS an extra internal git host.
        return app_registry._PUBLIC_GIT_HOSTS | frozenset({_AMZN_GIT_HOST})

    def clone_sandbox_mode(self, git_url, trusted_hosts):
        # Reuse the core decision verbatim with the extended trusted set.
        return app_registry._clone_sandbox_mode(git_url, trusted_hosts)


class _AmazonEmbeddingSource:
    def registry_model(self) -> str:
        return _AMZN_EMBED_MODEL

    def endpoint_url(self) -> Optional[str]:
        return _AMZN_EMBED_ENDPOINT

    def sign_request(self, method, url, headers, body) -> Optional[dict]:
        signed = dict(headers)
        signed["Authorization"] = "AWS4-HMAC-SHA256 Credential=FAKE/amazon"
        return signed


class _AmazonCredentialPolicy:
    def redact(self, text: str) -> str:
        # Delegate to the core redaction (so baseline credential redaction is
        # preserved) and add one internal-token redaction on top.
        from kiro_crew import security

        return security.redact(text).replace("MIDWAY-COOKIE", "[REDACTED-MIDWAY]")


class _AmazonTelemetryProvider:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record_event(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, dict(data)))

    def frontend_rum_config(self) -> Optional[dict]:
        return dict(_AMZN_RUM_CONFIG)


class _CountingProviderRegistry:
    """Default provider factory + a counter on register_acp_backends."""

    def __init__(self) -> None:
        self.register_calls = 0

    def create_factory(self, cfg):
        return cfg.create_provider_factory()

    def register_acp_backends(self) -> None:
        self.register_calls += 1


def _write_feature_manifest(root: Path, name: str) -> None:
    app_dir = root / name
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "app.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "displayName": name.replace("-", " ").title(),
                "description": f"Amazon feature app {name}",
                "author": "amazon",
                "defaultEnabled": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def manifest_dir(tmp_path: Path) -> Path:
    src = tmp_path / "amazon_feature_apps"
    src.mkdir()
    for name in _AMZN_FEATURE_APPS:
        _write_feature_manifest(src, name)
    return src


@pytest.fixture
def amazon_ctx(manifest_dir: Path):
    """Install an inline amazon PlatformContext with the wave-2 overlays."""
    cfg = KiroCrewConfig()
    base = build_default_context(cfg, profile=PROFILE_AMAZON)
    ctx = dataclasses.replace(
        base,
        apps_loader=_AmazonAppsLoader(manifest_dir),
        registry=_AmazonRegistryPolicy(),
        embeddings=_AmazonEmbeddingSource(),
        credentials=_AmazonCredentialPolicy(),
        telemetry=_AmazonTelemetryProvider(),
        providers=_CountingProviderRegistry(),
    )
    set_context(ctx)
    return ctx


# ── apps_loader (point 1) ──


def test_edition_builtin_apps_discovered_from_manifest_sources(amazon_ctx) -> None:
    from kiro_crew.apps.manager import _edition_builtin_apps

    apps = _edition_builtin_apps()
    names = {a["name"] for a in apps}
    # All six companion feature apps are discovered from the manifest_sources dir.
    assert set(_AMZN_FEATURE_APPS) <= names
    # And each carries the manifest fields (proving real discovery, not just names).
    sample = next(a for a in apps if a["name"] == "mimir")
    assert sample["version"] == "1.0.0"
    assert sample["displayName"] == "Mimir"


def test_edition_bundled_app_names_reflect_amazon(amazon_ctx) -> None:
    from kiro_crew.apps.manager import _edition_bundled_app_names

    assert set(_edition_bundled_app_names()) == set(_AMZN_FEATURE_APPS)


def test_missing_manifest_source_is_skipped_gracefully(tmp_path: Path) -> None:
    """A non-existent manifest_sources dir must not raise — discovery skips it."""
    cfg = KiroCrewConfig()
    base = build_default_context(cfg, profile=PROFILE_AMAZON)
    missing = tmp_path / "does_not_exist"
    ctx = dataclasses.replace(base, apps_loader=_AmazonAppsLoader(missing))
    set_context(ctx)
    from kiro_crew.apps.manager import _edition_builtin_apps

    assert _edition_builtin_apps() == []


def test_register_builtin_apps_includes_feature_apps(amazon_ctx, tmp_path, monkeypatch) -> None:
    """register_builtin_apps installs the companion feature apps as builtins."""
    from kiro_crew.apps import manager

    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    monkeypatch.setattr(manager, "app_dir", lambda name: apps_root / name)
    monkeypatch.setattr(manager, "apps_dir", lambda: apps_root)

    manager.register_builtin_apps()

    for name in _AMZN_FEATURE_APPS:
        installed = apps_root / name / "installed.json"
        assert installed.is_file(), f"{name} was not registered"
    # And none of the freshly-registered feature apps are flagged as orphans.
    orphans = manager.detect_orphaned_builtins(force_refresh=True)
    assert not (set(_AMZN_FEATURE_APPS) & orphans)


# ── registry (point 3) ──


def test_internal_git_host_allows_ssh_clone(amazon_ctx) -> None:
    """An SSH clone to the extra internal host gets the 'standard' (ssh-exposed) mode."""
    url = f"git@{_AMZN_GIT_HOST}:team/app.git"
    assert app_registry._context_clone_sandbox_mode(url) == "standard"


def test_public_https_clone_stays_strict_under_amazon(amazon_ctx) -> None:
    """https never needs ~/.ssh — stays strict even under the amazon policy."""
    assert app_registry._context_clone_sandbox_mode("https://github.com/x/y.git") == "strict"


def test_untrusted_ssh_host_stays_strict_under_amazon(amazon_ctx) -> None:
    """An SSH clone to a NON-allowlisted host still fails closed (strict)."""
    assert app_registry._context_clone_sandbox_mode("git@evil.example.com:x/y.git") == "strict"


# ── embeddings (point 2) ──
#
# Since the in-process embeddings landed (vendored llama.cpp, always-on) the
# core no longer routes embed requests over HTTP: the async EmbeddingClient +
# SigV4 signing were deleted, and the EmbeddingSource endpoint_url/sign_request
# slots are a dormant seam. The active companion swap path is now
# embeddings.register_embedding_backend — asserted below. The amazon overlay's
# EmbeddingSource stays composable (registry_model still resolves).


def test_embedding_source_registry_model_resolves(amazon_ctx) -> None:
    """The context's EmbeddingSource still composes and resolves the model id."""
    assert current_context().embeddings.registry_model() == _AMZN_EMBED_MODEL


def test_embedding_backend_swap_seam(amazon_ctx) -> None:
    """A companion swaps runtimes via register_embedding_backend: the shared
    embedder is constructed through the registered factory and its model_id
    names the (incomparable) vector space."""

    class _AmazonBackend(embeddings.EmbeddingBackend):
        @property
        def model_id(self) -> str:
            return _AMZN_EMBED_MODEL

        @property
        def dim(self) -> int:
            return 4

        def is_ready(self) -> bool:
            return True

        def embed(self, text: str):
            return [0.0, 1.0, 2.0, 3.0]

        def embed_batch(self, texts):
            return [[0.0, 1.0, 2.0, 3.0] for _ in texts]

        def close(self) -> None:
            pass

    embeddings.register_embedding_backend(_AmazonBackend)
    embeddings.reset_shared_embedder()
    try:
        emb = embeddings.get_shared_embedder()
        assert isinstance(emb, _AmazonBackend)
        assert emb.model_id == _AMZN_EMBED_MODEL
        assert emb.embed("x") == [0.0, 1.0, 2.0, 3.0]
    finally:
        embeddings.register_embedding_backend(None)
        embeddings.reset_shared_embedder()


# ── credentials (point 6) ──


def test_context_redact_runs_amazon_policy(amazon_ctx) -> None:
    """The mcp_core + agent context-routed redact uses the amazon policy."""
    from kiro_crew import agent, mcp_core

    # Internal token redaction (amazon overlay) applies through both callers.
    assert "[REDACTED-MIDWAY]" in mcp_core.redact("token=MIDWAY-COOKIE")
    assert "[REDACTED-MIDWAY]" in agent.redact("token=MIDWAY-COOKIE")
    # Baseline credential redaction is preserved (delegated to security.redact).
    out = mcp_core.redact("AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in out


# ── telemetry (point 5) ──


def test_frontend_rum_config_present_when_configured(amazon_ctx) -> None:
    assert current_context().telemetry.frontend_rum_config() == _AMZN_RUM_CONFIG


def test_record_event_captured_by_amazon_telemetry(amazon_ctx) -> None:
    current_context().telemetry.record_event("gateway_start", {"k": "v"})
    events = current_context().telemetry.events  # type: ignore[attr-defined]
    assert ("gateway_start", {"k": "v"}) in events


# ── providers (point 7) ──


def test_register_acp_backends_called_once_at_boot(monkeypatch, manifest_dir) -> None:
    """bootstrap_context invokes register_acp_backends exactly once after set_context."""
    import kiro_crew.platform.bootstrap as bootstrap

    counting = _CountingProviderRegistry()
    cfg = KiroCrewConfig()

    def _fake_discover(profile, cfg_):
        base = build_default_context(cfg_, profile=PROFILE_AMAZON)
        return dataclasses.replace(base, providers=counting)

    monkeypatch.setenv("KIROCREW_PROFILE", "amazon")
    monkeypatch.setattr(bootstrap, "plugin_entry_points", lambda: ["amazon"])
    monkeypatch.setattr(bootstrap, "discover_companion_context", _fake_discover)

    bootstrap.bootstrap_context(cfg)
    assert counting.register_calls == 1
