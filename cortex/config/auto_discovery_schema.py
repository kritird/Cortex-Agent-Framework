"""Pydantic schema for cortex_auto_mcps.yaml — auto-discovered external MCPs.

This file is intentionally separate from schema.py so it does not pollute the
user-facing cortex.yaml configuration models.  The auto-discovery store is
written and read by ExternalMCPRegistry; users never edit it directly.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class AutoDiscoveredMCPRecord(BaseModel):
    """One auto-discovered external MCP server entry."""

    # Identity
    url: str
    name: str
    description: str = ""

    # Capability classification (same vocabulary as ToolServerRegistry)
    capabilities: List[str] = Field(default_factory=list)

    # Security / trust
    trust_tier: str = "external"
    auth_required: bool = False
    auth_required_reason: Optional[str] = None

    # Provenance
    source_registry: str = ""   # which registry source surfaced this server
    discovered_at: str = ""     # ISO-8601 UTC datetime string
    last_verified: Optional[str] = None  # ISO-8601 UTC datetime string

    # State flags
    pending_auth_notification: bool = False  # True until user has been told about auth requirement
    verification_failed: bool = False        # True if last probe attempt failed


class AutoDiscoveryStore(BaseModel):
    """Top-level wrapper persisted to cortex_auto_mcps.yaml."""

    version: str = "1"
    records: List[AutoDiscoveredMCPRecord] = Field(default_factory=list)
    # Servers that require authentication — kept separately so the framework
    # can surface them to the user at session end without re-scanning all records.
    pending_auth: List[AutoDiscoveredMCPRecord] = Field(default_factory=list)
