"""Principal identity model for Cortex Agent Framework.

Tracks who initiated a session or task — human user, system agent, or
delegated agent-to-agent call.  Every framework operation carries a
Principal so that storage paths, audit logs, and session isolation work
correctly regardless of how the session was triggered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from cortex.exceptions import CortexInvalidUserError

# Allowed principal_type values
PRINCIPAL_TYPE_USER = "user"
PRINCIPAL_TYPE_SYSTEM = "system"
PRINCIPAL_TYPE_AGENT = "agent"
_VALID_TYPES = {PRINCIPAL_TYPE_USER, PRINCIPAL_TYPE_SYSTEM, PRINCIPAL_TYPE_AGENT}

# System/agent IDs must match  <type>:<name>  — alphanumeric + dash/underscore
_SYSTEM_ID_RE = re.compile(r"^(system|agent):[a-zA-Z0-9_-]+$")


@dataclass(frozen=True)
class Principal:
    """Immutable identity attached to every session and task.

    Attributes:
        principal_id:  Unique identifier.
            - For human users: the application-supplied ``user_id``
              (e.g. ``"user_123"``).
            - For system/autonomous agents: ``"system:<name>"``
              (e.g. ``"system:scheduler"``).
            - For agent-to-agent delegation: ``"agent:<agent_name>"``
              (e.g. ``"agent:sales-bot"``).
        principal_type:  One of ``"user"``, ``"system"``, ``"agent"``.
        display_name:  Optional human-readable label (for audit logs).
        delegation_chain:  Ordered list of principal_ids that led to this
            principal.  The first entry is the *original* human or system
            initiator; the last entry is the immediate caller.  Empty for
            direct (non-delegated) calls.
    """

    principal_id: str
    principal_type: str = PRINCIPAL_TYPE_USER
    display_name: Optional[str] = None
    delegation_chain: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.principal_id or not self.principal_id.strip():
            raise CortexInvalidUserError(
                "principal_id must be a non-empty string. "
                "All framework operations are keyed per principal — a missing or blank "
                "principal_id would corrupt storage paths and session isolation."
            )
        if self.principal_type not in _VALID_TYPES:
            raise CortexInvalidUserError(
                f"principal_type must be one of {_VALID_TYPES}, got '{self.principal_type}'"
            )
        if self.principal_type in (PRINCIPAL_TYPE_SYSTEM, PRINCIPAL_TYPE_AGENT):
            if not _SYSTEM_ID_RE.match(self.principal_id):
                raise CortexInvalidUserError(
                    f"System/agent principal_id must match '<type>:<name>' pattern "
                    f"(alphanumeric, dash, underscore).  Got: '{self.principal_id}'"
                )

    # ── Convenience constructors ──────────────────────────────────────────

    @classmethod
    def from_user_id(cls, user_id: str, display_name: Optional[str] = None) -> Principal:
        """Create a human-user principal from a plain user_id string."""
        return cls(
            principal_id=user_id,
            principal_type=PRINCIPAL_TYPE_USER,
            display_name=display_name,
        )

    @classmethod
    def system(cls, name: str, display_name: Optional[str] = None) -> Principal:
        """Create a system/autonomous agent principal.

        >>> Principal.system("scheduler")
        Principal(principal_id='system:scheduler', principal_type='system', ...)
        """
        return cls(
            principal_id=f"system:{name}",
            principal_type=PRINCIPAL_TYPE_SYSTEM,
            display_name=display_name or f"system:{name}",
        )

    @classmethod
    def agent(
        cls,
        name: str,
        delegated_by: Principal,
        display_name: Optional[str] = None,
    ) -> Principal:
        """Create a delegated agent principal, recording the delegation chain.

        The chain captures the full provenance: who originally initiated the
        request and every agent hop in between.

        >>> user = Principal.from_user_id("user_123")
        >>> bot = Principal.agent("sales-bot", delegated_by=user)
        >>> bot.delegation_chain
        ['user_123']
        >>> inner = Principal.agent("pricing-engine", delegated_by=bot)
        >>> inner.delegation_chain
        ['user_123', 'agent:sales-bot']
        """
        chain = list(delegated_by.delegation_chain) + [delegated_by.principal_id]
        return cls(
            principal_id=f"agent:{name}",
            principal_type=PRINCIPAL_TYPE_AGENT,
            display_name=display_name or f"agent:{name}",
            delegation_chain=chain,
        )

    # ── Derived properties ────────────────────────────────────────────────

    @property
    def origin_id(self) -> str:
        """The original (root) initiator's principal_id.

        For a direct user call this is the user_id itself.
        For delegated calls this is the first entry in the chain.
        """
        if self.delegation_chain:
            return self.delegation_chain[0]
        return self.principal_id

    @property
    def is_human(self) -> bool:
        return self.principal_type == PRINCIPAL_TYPE_USER

    @property
    def is_system(self) -> bool:
        return self.principal_type == PRINCIPAL_TYPE_SYSTEM

    @property
    def is_delegated(self) -> bool:
        return len(self.delegation_chain) > 0

    @property
    def storage_key(self) -> str:
        """Key used for per-principal storage paths.

        For delegated agents this returns the *origin* user/system id so that
        all tasks in a delegation chain share one storage namespace.
        """
        return self.origin_id

    def to_dict(self) -> dict:
        return {
            "principal_id": self.principal_id,
            "principal_type": self.principal_type,
            "display_name": self.display_name,
            "delegation_chain": list(self.delegation_chain),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Principal:
        return cls(
            principal_id=data["principal_id"],
            principal_type=data.get("principal_type", PRINCIPAL_TYPE_USER),
            display_name=data.get("display_name"),
            delegation_chain=data.get("delegation_chain", []),
        )
