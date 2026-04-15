"""Tests for security modules."""
import asyncio
import pytest
from cortex.security.sanitiser import InputSanitiser
from cortex.security.scrubber import CredentialScrubber
from cortex.security.bash_sandbox import BashSandbox
from cortex.exceptions import CortexSecurityError
import tempfile


def test_sanitise_text_strips_nulls():
    s = InputSanitiser()
    result = s.sanitise_text_input("hello\x00world")
    assert "\x00" not in result


def test_sanitise_filename():
    s = InputSanitiser()
    assert s.sanitise_filename("../../../etc/passwd") == "passwd"
    assert s.sanitise_filename("normal.txt") == "normal.txt"


def test_scrubber_bearer_token():
    sc = CredentialScrubber()
    result = sc.scrub("Authorization: Bearer eyJhbGc.sometoken")
    assert "Bearer" not in result or "[REDACTED]" in result


def test_scrubber_api_key():
    sc = CredentialScrubber()
    result = sc.scrub("api_key=super_secret_12345")
    assert "super_secret_12345" not in result


def test_scrubber_is_clean():
    sc = CredentialScrubber()
    assert sc.is_clean("This is a normal string with no secrets")


@pytest.mark.asyncio
async def test_bash_sandbox_blocks_rm():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = BashSandbox(tmpdir)
        with pytest.raises(CortexSecurityError):
            await sb.execute("rm -rf /tmp/something")


@pytest.mark.asyncio
async def test_bash_sandbox_allows_safe_commands():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = BashSandbox(tmpdir)
        result = await sb.execute("echo hello")
        assert "hello" in result


@pytest.mark.asyncio
async def test_bash_sandbox_blocks_path_traversal():
    with tempfile.TemporaryDirectory() as tmpdir:
        sb = BashSandbox(tmpdir)
        with pytest.raises(CortexSecurityError):
            await sb.execute("cat ../../etc/passwd")
