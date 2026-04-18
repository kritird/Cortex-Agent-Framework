"""Ant Colony — self-spawning specialist agent subsystem.

Each 'ant' is an independent Cortex agent running as an MCP server.
The AntColony hatches ants on-demand to fill capability gaps, supervises
their lifecycle, and registers them with the ToolServerRegistry.

Trust tier: "ant" — higher than external (no output guard), lower than
internal (spawned at runtime, not developer-configured in cortex.yaml).
"""
from cortex.ants.ant_colony import AntColony, AntInfo

__all__ = ["AntColony", "AntInfo"]
