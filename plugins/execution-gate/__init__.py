"""
Execution Gate Plugin

Gated execution system for orchestrator profiles.
Tier 1: Contract-gated delegation
Tier 2: Kanban boards (integrates with kanban plugin)
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

# Plugin metadata
PLUGIN_NAME = "execution-gate"
PLUGIN_VERSION = "1.0.0"


def register(ctx) -> None:
    """Register execution-gate tools with the Hermes plugin registry."""
    from .tools import TOOLS

    for item in TOOLS:
        schema = item["schema"]
        ctx.register_tool(
            name=schema["name"],
            toolset="execution_gate",
            schema=schema,
            handler=item["handler"],
            emoji="🧪",
        )


# Backward-compatible exports for older/local plugin experiments.
def get_tools() -> List[Dict[str, Any]]:
    """Return tool definitions for legacy callers."""
    from .tools import TOOLS
    return TOOLS


def get_toolset() -> Dict[str, Any]:
    """Return legacy toolset definition."""
    return {
        "execution_gate": {
            "description": (
                "Gated execution for orchestrator profiles. Route requests into "
                "Tier 1 finite-deliverable contracts or Tier 2 recurring/durable boards, "
                "declare contracts with success criteria before delegating work, and "
                "validate worker artifacts against criteria. Supports hidden audit criteria, "
                "parallel decomposition, aggregation, and retry with feedback."
            ),
            "tools": [
                "gate_intake",
                "gate_intake_verify",
                "gate_conversion_ledger",
                "gate_route",
                "gate_contract",
                "gate_delegate",
                "gate_parallel",
                "gate_validate",
                "gate_aggregate",
                "gate_status",
                "gate_abandon",
            ],
            "includes": [],
        }
    }


def check_enabled() -> bool:
    """Legacy enable check."""
    return os.environ.get("EXECUTION_GATE_ENABLED", "0") == "1"


def on_load(config: Optional[Dict[str, Any]] = None) -> None:
    """Called when plugin is loaded by legacy loaders."""
    if config:
        from . import gate as gate_module
        from .gate import ExecutionGate
        gate_module._gate = ExecutionGate(config=config)


def on_unload() -> None:
    """Called when plugin is unloaded."""
    pass


__all__ = [
    "PLUGIN_NAME",
    "PLUGIN_VERSION",
    "register",
    "get_tools",
    "get_toolset",
    "check_enabled",
    "on_load",
    "on_unload",
]
