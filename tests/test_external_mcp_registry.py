"""Unit tests for ExternalMCPRegistry — the persistent auto-discovery store."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from cortex.config.auto_discovery_schema import AutoDiscoveredMCPRecord
from cortex.modules.external_mcp_registry import ExternalMCPRegistry


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "cortex_auto_mcps.yaml"


@pytest.fixture
def registry(store_path):
    r = ExternalMCPRegistry(str(store_path))
    r.load()  # no-op since file doesn't exist
    return r


def _make_record(url="https://mcp.example.com", name="Example", caps=None, **kwargs):
    return AutoDiscoveredMCPRecord(
        url=url,
        name=name,
        capabilities=caps or ["web_search"],
        discovered_at=datetime.now(timezone.utc).isoformat(),
        last_verified=datetime.now(timezone.utc).isoformat(),
        **kwargs,
    )


# ── Load / persist ───────────────────────────────────────────────────────────

def test_load_missing_file_is_safe(tmp_path):
    r = ExternalMCPRegistry(str(tmp_path / "nope.yaml"))
    r.load()  # should not raise
    assert r.get_all_verified() == []


def test_register_persists_to_disk(registry, store_path):
    rec = _make_record()
    registry.register(rec)
    assert store_path.exists()
    data = yaml.safe_load(store_path.read_text())
    assert data["records"][0]["url"] == rec.url


def test_register_then_reload_restores_state(registry, store_path):
    registry.register(_make_record(url="https://a.example"))
    registry.register(_make_record(url="https://b.example", name="B"))

    # Fresh instance reads from disk
    fresh = ExternalMCPRegistry(str(store_path))
    fresh.load()
    urls = {r.url for r in fresh.get_all_verified()}
    assert urls == {"https://a.example", "https://b.example"}


# ── Lookup ───────────────────────────────────────────────────────────────────

def test_lookup_by_capability(registry):
    registry.register(_make_record(url="https://a", caps=["web_search"]))
    registry.register(_make_record(url="https://b", caps=["document_generation"]))

    found = registry.lookup_by_capability("web_search")
    assert len(found) == 1
    assert found[0].url == "https://a"


def test_lookup_excludes_auth_required(registry):
    registry.register(_make_record(url="https://a", caps=["web_search"]))
    registry.mark_auth_required("https://a", reason="test")
    assert registry.lookup_by_capability("web_search") == []


def test_lookup_excludes_verification_failed(registry):
    registry.register(_make_record(url="https://a", caps=["web_search"]))
    registry.mark_verification_failed("https://a", reason="down")
    assert registry.lookup_by_capability("web_search") == []


def test_has_url(registry):
    registry.register(_make_record(url="https://x"))
    assert registry.has_url("https://x")
    assert not registry.has_url("https://y")


# ── Staleness ────────────────────────────────────────────────────────────────

def test_needs_reverification_when_never_verified(registry):
    rec = _make_record(url="https://a")
    rec.last_verified = None
    registry.register(rec)
    assert registry.needs_reverification("https://a", max_stale_days=30)


def test_needs_reverification_when_stale(registry):
    old_iso = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    rec = _make_record(url="https://a")
    rec.last_verified = old_iso
    registry.register(rec)
    assert registry.needs_reverification("https://a", max_stale_days=30)


def test_fresh_record_does_not_need_reverification(registry):
    registry.register(_make_record(url="https://a"))
    assert not registry.needs_reverification("https://a", max_stale_days=30)


# ── Auth-pending lifecycle ───────────────────────────────────────────────────

def test_mark_auth_required_queues_and_persists(registry, store_path):
    registry.mark_auth_required("https://needs-auth", name="NA", reason="401")
    pending = registry.get_auth_pending()
    assert len(pending) == 1
    assert pending[0].url == "https://needs-auth"
    assert pending[0].auth_required_reason == "401"

    # Survives reload
    fresh = ExternalMCPRegistry(str(store_path))
    fresh.load()
    assert len(fresh.get_auth_pending()) == 1


def test_clear_auth_pending_removes_notification_flag(registry):
    registry.mark_auth_required("https://x", reason="401")
    assert registry.get_auth_pending()
    registry.clear_auth_pending()
    assert registry.get_auth_pending() == []


def test_mark_auth_required_is_idempotent(registry):
    registry.mark_auth_required("https://x", reason="401")
    registry.mark_auth_required("https://x", reason="401 again")
    assert len(registry.get_auth_pending()) == 1


# ── Mark verified / failed ───────────────────────────────────────────────────

def test_mark_verified_updates_last_verified(registry):
    old_iso = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    rec = _make_record(url="https://a")
    rec.last_verified = old_iso
    rec.verification_failed = True
    registry.register(rec)

    registry.mark_verified("https://a")
    assert not registry.needs_reverification("https://a", max_stale_days=30)
    assert registry.lookup_by_capability("web_search")  # no longer failed
