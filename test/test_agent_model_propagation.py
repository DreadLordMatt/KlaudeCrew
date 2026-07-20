"""Regression tests for Mesh-2292 model propagation.

On refresh of an existing kirocrew.json, the user-facing authority is
``config.json`` ``agent.model``. An explicit pick (not the "auto" sentinel)
MUST be propagated into the agent file so kiro-cli's ``--agent`` startup load
matches it -- otherwise a stale agent-file model shadows config.json and
session/set_model loses the startup race (the model appears not to change).
The "auto" sentinel must defer to managed/shipped resolution and must never be
written as the literal model.

Reuses the install harness from test_agent.py. _run_install seeds the mc
config file only if it does not already exist, so pre-writing it lets us drive
the agent.model branch.
"""

from __future__ import annotations

import json
from pathlib import Path

from test_agent import _bundled_defaults, _run_install

from kiro_crew import agent_state


def _seed_mc_config(tmp_path: Path, agent_model: str | None) -> None:
    """Pre-create the mc config file _run_install would otherwise write."""
    agent_section: dict = {"kiro_hooks_autoimport": False}
    if agent_model is not None:
        agent_section["model"] = agent_model
    (tmp_path / "empty_mc_config.json").write_text(json.dumps({"agent": agent_section}))


def _write_existing_agent(tmp_path: Path, model: str) -> None:
    kiro_dir = tmp_path / "kiro_agents"
    kiro_dir.mkdir(exist_ok=True)
    (kiro_dir / "kirocrew.json").write_text(
        json.dumps(
            {
                "name": "kirocrew",
                "model": model,
                "tools": ["ReadFile"],
                "allowedTools": ["ReadFile"],
                "mcpServers": {},
                "toolsSettings": {"execute_bash": {"deniedCommands": ["old"]}},
            }
        )
    )


def test_explicit_config_model_propagates_into_agent_file(tmp_path: Path):
    """config.json agent.model (explicit) overrides a stale agent-file model."""
    cfg_dir = _bundled_defaults(tmp_path)
    _write_existing_agent(tmp_path, model="stale-agent-model")
    _seed_mc_config(tmp_path, agent_model="claude-explicit-pick")

    path = _run_install(tmp_path, cfg_dir)
    config = json.loads(path.read_text())

    assert config["model"] == "claude-explicit-pick"


def test_auto_sentinel_never_written_as_literal_model(tmp_path: Path):
    """agent.model == "auto" must defer, never become the literal model."""
    cfg_dir = _bundled_defaults(tmp_path)
    _write_existing_agent(tmp_path, model="stale-agent-model")
    # Unmanaged so refresh does not re-sync to the shipped default; the existing
    # model should be preserved and "auto" must not overwrite it.
    agent_state.set_model_managed("kirocrew", False)
    _seed_mc_config(tmp_path, agent_model="auto")

    path = _run_install(tmp_path, cfg_dir)
    config = json.loads(path.read_text())

    assert config["model"] != "auto"
    assert config["model"] == "stale-agent-model"
