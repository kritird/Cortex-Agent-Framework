"""Unit tests for MCPOutputGuard — the three-layer external MCP output checker."""
import pytest

from cortex.security.mcp_output_guard import MCPOutputGuard, MCPOutputSecurityError


@pytest.fixture
def guard():
    return MCPOutputGuard()


# ── Layer 1: MIME allowlist ──────────────────────────────────────────────────

def test_allows_plain_text(guard):
    assert guard.check("hello world", content_type="text/plain") == "hello world"


def test_allows_json(guard):
    payload = '{"ok": true}'
    assert guard.check(payload, content_type="application/json") == payload


def test_rejects_pdf_mime(guard):
    with pytest.raises(MCPOutputSecurityError, match="blocked MIME"):
        guard.check(b"%PDF-1.4 stuff", content_type="application/pdf")


def test_rejects_application_javascript_mime(guard):
    with pytest.raises(MCPOutputSecurityError, match="blocked MIME"):
        guard.check("alert(1)", content_type="application/javascript")


def test_rejects_octet_stream_mime(guard):
    with pytest.raises(MCPOutputSecurityError, match="blocked MIME"):
        guard.check(b"\x00\x01", content_type="application/octet-stream")


def test_rejects_unknown_mime_not_in_allowlist(guard):
    with pytest.raises(MCPOutputSecurityError, match="not in the safe allowlist"):
        guard.check("foo", content_type="application/x-weird")


def test_mime_with_charset_param_is_parsed(guard):
    assert guard.check("hi", content_type="text/plain; charset=utf-8") == "hi"


# ── Layer 2: Extension blocklist ─────────────────────────────────────────────

@pytest.mark.parametrize("fname", [
    "hack.sh", "malware.exe", "payload.py", "doc.pdf", "script.js",
    "bundle.wasm", "archive.zip", "lib.so", "run.bat",
])
def test_rejects_dangerous_extensions(guard, fname):
    with pytest.raises(MCPOutputSecurityError, match="file extension"):
        guard.check("safe content", content_type="text/plain", filename=fname)


def test_allows_txt_extension(guard):
    assert guard.check("report", content_type="text/plain", filename="notes.txt") == "report"


# ── Layer 3a: Magic bytes ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,label", [
    (b"\x7fELF\x02\x01\x01\x00", "ELF"),
    (b"MZ\x90\x00", "PE/Windows"),
    (b"PK\x03\x04\x14\x00", "ZIP/JAR"),
    (b"\x1f\x8b\x08\x00", "GZIP"),
    (b"%PDF-1.7", "PDF"),
    (b"\x00asm\x01\x00", "WebAssembly"),
])
def test_magic_byte_rejection(guard, raw, label):
    with pytest.raises(MCPOutputSecurityError, match="binary/archive"):
        guard.check(raw, content_type="text/plain")


# ── Layer 3b: Shebang ────────────────────────────────────────────────────────

def test_rejects_shebang(guard):
    with pytest.raises(MCPOutputSecurityError, match="shebang"):
        guard.check(b"#!/usr/bin/env python\nprint(1)", content_type="text/plain")


def test_double_hash_comment_is_not_a_shebang(guard):
    assert guard.check("## heading", content_type="text/markdown") == "## heading"


# ── Layer 3c: Script patterns in text ────────────────────────────────────────

@pytest.mark.parametrize("body,label", [
    ("<script>alert(1)</script>", "script tag"),
    ('<a href="javascript:alert(1)">x</a>', "javascript URI"),
    ('<div onclick="x()">hi</div>', "event handler"),
    ("import os\nos.system('rm -rf /')", "os.system"),
    ("import subprocess", "import subprocess"),
    ("__import__('os')", "__import__"),
    ("<iframe src='x'></iframe>", "iframe"),
])
def test_script_patterns_in_text_rejected(guard, body, label):
    with pytest.raises(MCPOutputSecurityError, match="unsafe pattern"):
        guard.check(body, content_type="text/plain")


# ── SVG sanitisation ─────────────────────────────────────────────────────────

def test_svg_script_block_is_stripped(guard):
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<script>alert(1)</script>'
        '<circle cx="50" cy="50" r="40"/>'
        '</svg>'
    )
    cleaned = guard.check(svg, content_type="image/svg+xml")
    assert "<script" not in cleaned
    assert "<circle" in cleaned


def test_svg_event_handler_is_stripped(guard):
    svg = '<svg><circle onclick="evil()" cx="5" cy="5" r="3"/></svg>'
    cleaned = guard.check(svg, content_type="image/svg+xml")
    assert "onclick" not in cleaned
    assert "<circle" in cleaned


def test_svg_javascript_href_is_stripped(guard):
    svg = '<svg><a xlink:href="javascript:alert(1)"><text>x</text></a></svg>'
    cleaned = guard.check(svg, content_type="image/svg+xml")
    assert "javascript:" not in cleaned


# ── HTML sanitisation ────────────────────────────────────────────────────────

def test_html_script_is_stripped(guard):
    html = "<html><body><p>Hi</p><script>evil()</script></body></html>"
    cleaned = guard.check(html, content_type="text/html")
    assert "<script" not in cleaned
    assert "<p>Hi</p>" in cleaned


def test_html_event_handler_is_stripped(guard):
    html = '<html><body><button onclick="pwn()">go</button></body></html>'
    cleaned = guard.check(html, content_type="text/html")
    assert "onclick" not in cleaned


# ── Bytes input ──────────────────────────────────────────────────────────────

def test_accepts_bytes_input(guard):
    result = guard.check(b"plain bytes", content_type="text/plain")
    assert result == "plain bytes"


# ── Unknown content_type + filename → text scan ──────────────────────────────

def test_unknown_type_with_safe_content(guard):
    assert guard.check("nothing scary here") == "nothing scary here"


def test_unknown_type_with_scripty_content_rejected(guard):
    with pytest.raises(MCPOutputSecurityError):
        guard.check("<script>x</script>")
