"""ExternalMCPRegistry — persistent store of auto-discovered external MCP servers.

Backed by ``cortex_auto_mcps.yaml`` (path supplied by the caller; defaults to
``{storage.base_path}/cortex_auto_mcps.yaml`` when wired from CortexFramework).

The registry is the single source of truth for what external MCPs are known to
the framework.  CapabilityScout consults it before triggering an internet search,
and writes to it after a successful validation-and-registration cycle.

Thread-safety: single-process async — all mutations are synchronous file-I/O
(fast) so no asyncio locking is required.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import yaml

from cortex.config.auto_discovery_schema import AutoDiscoveredMCPRecord, AutoDiscoveryStore

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExternalMCPRegistry:
    """
    Load / query / persist auto-discovered external MCP records.

    Lifecycle::

        registry = ExternalMCPRegistry(store_path="/data/cortex_auto_mcps.yaml")
        registry.load()                 # called once during framework.initialize()
        ...
        records = registry.lookup_by_capability("web_search")
        registry.register(new_record)  # persists immediately
        ...
        pending = registry.get_auth_pending()   # surfaced at session end
        registry.clear_auth_pending()           # after user has been notified
    """

    def __init__(self, store_path: str) -> None:
        self._store_path = Path(store_path)
        # url → record (primary index)
        self._records: dict[str, AutoDiscoveredMCPRecord] = {}
        # auth-required servers waiting to be shown to the user
        self._pending_auth: list[AutoDiscoveredMCPRecord] = []

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load records from disk.  Safe to call when the file does not exist."""
        if not self._store_path.exists():
            logger.debug(
                "ExternalMCPRegistry: store file not found at %s — starting empty",
                self._store_path,
            )
            return
        try:
            raw = self._store_path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw) or {}
            store = AutoDiscoveryStore(**data)
            for rec in store.records:
                self._records[rec.url] = rec
            self._pending_auth = list(store.pending_auth)
            logger.info(
                "ExternalMCPRegistry: loaded %d record(s), %d pending-auth from %s",
                len(self._records),
                len(self._pending_auth),
                self._store_path,
            )
        except Exception as exc:
            logger.warning(
                "ExternalMCPRegistry: failed to load %s (%s) — starting empty",
                self._store_path,
                exc,
            )

    def persist(self) -> None:
        """Write current state to disk atomically (write to .tmp then rename)."""
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            store = AutoDiscoveryStore(
                records=list(self._records.values()),
                pending_auth=self._pending_auth,
            )
            tmp = self._store_path.with_suffix(".tmp")
            tmp.write_text(
                yaml.dump(
                    store.model_dump(),
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._store_path)
            logger.debug(
                "ExternalMCPRegistry: persisted %d record(s) to %s",
                len(self._records),
                self._store_path,
            )
        except Exception as exc:
            logger.warning(
                "ExternalMCPRegistry: failed to persist to %s: %s",
                self._store_path,
                exc,
            )

    # ── Read ───────────────────────────────────────────────────────────────────

    def lookup_by_capability(self, capability: str) -> List[AutoDiscoveredMCPRecord]:
        """Return usable records that declare this capability.

        Excludes records that require auth or have a failed verification state.
        """
        return [
            r for r in self._records.values()
            if capability in r.capabilities
            and not r.auth_required
            and not r.verification_failed
        ]

    def get_all_verified(self) -> List[AutoDiscoveredMCPRecord]:
        """Return all records that are usable (no auth needed, not failed)."""
        return [
            r for r in self._records.values()
            if not r.auth_required and not r.verification_failed
        ]

    def get_auth_pending(self) -> List[AutoDiscoveredMCPRecord]:
        """Records that need auth and whose notification flag is still set.

        Called by CortexFramework at the end of each session to decide whether
        to emit an ``external_mcp_auth_required`` SSE event.
        """
        return [r for r in self._pending_auth if r.pending_auth_notification]

    def has_url(self, url: str) -> bool:
        return url in self._records

    def needs_reverification(self, url: str, max_stale_days: int) -> bool:
        """True if the record exists but was last verified more than *max_stale_days* ago."""
        rec = self._records.get(url)
        if not rec or not rec.last_verified:
            return True
        try:
            last = datetime.fromisoformat(rec.last_verified)
            age_days = (datetime.now(timezone.utc) - last).days
            return age_days >= max_stale_days
        except Exception:
            return True

    # ── Write ──────────────────────────────────────────────────────────────────

    def register(self, record: AutoDiscoveredMCPRecord) -> None:
        """Add or update a record.  Persists immediately."""
        self._records[record.url] = record
        logger.info(
            "ExternalMCPRegistry: registered '%s' (%s) capabilities=%s",
            record.name,
            record.url,
            record.capabilities,
        )
        self.persist()

    def mark_auth_required(self, url: str, name: str = "", reason: str = "") -> None:
        """Mark an MCP as requiring authentication and queue it for user notification.

        If the record was never registered (the probe revealed auth before we
        could collect full metadata), a minimal stub is created for tracking.
        Persists immediately so the notification survives across runs.
        """
        rec = self._records.get(url)
        if rec is None:
            rec = AutoDiscoveredMCPRecord(
                url=url,
                name=name or url,
                discovered_at=_now_iso(),
            )
        rec.auth_required = True
        rec.auth_required_reason = reason or "authentication required"
        rec.pending_auth_notification = True
        self._records[url] = rec

        if not any(p.url == url for p in self._pending_auth):
            self._pending_auth.append(rec)

        logger.info(
            "ExternalMCPRegistry: '%s' requires auth — queued for user notification (%s)",
            url,
            reason,
        )
        self.persist()

    def mark_verification_failed(self, url: str, reason: str = "") -> None:
        """Mark a record as having failed its last verification probe."""
        rec = self._records.get(url)
        if rec:
            rec.verification_failed = True
            logger.debug(
                "ExternalMCPRegistry: marked '%s' as verification-failed: %s",
                url,
                reason,
            )
            self.persist()

    def mark_verified(self, url: str) -> None:
        """Stamp *last_verified* on a record after a successful probe."""
        rec = self._records.get(url)
        if rec:
            rec.last_verified = _now_iso()
            rec.verification_failed = False
            self.persist()

    def clear_auth_pending(self) -> None:
        """Clear the notification flag after the user has been informed.

        Records remain in ``_records`` so we don't re-discover them; only the
        pending-auth list and the notification flag are cleared.
        """
        for rec in self._pending_auth:
            rec.pending_auth_notification = False
        self._pending_auth.clear()
        self.persist()
