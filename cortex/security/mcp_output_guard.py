"""MCPOutputGuard — hardened three-layer safety check for external MCP outputs.

Layer 1: Content-type allowlist  — only well-known safe MIME types pass.
Layer 2: Extension blocklist     — reject known executable/script extensions.
Layer 3: Content inspection      — magic bytes, shebang, script pattern scan.

SVG is handled specially: script elements and event-handler attributes are
stripped and the sanitised content is returned rather than rejected outright,
unless unsafe patterns survive after stripping.

HTML is likewise stripped of scripts/handlers before a residual pattern scan.

PDF is rejected entirely (can embed arbitrary JavaScript via /JS and /Action).

Raises MCPOutputSecurityError on any failure with a human-readable reason.
"""
from __future__ import annotations

import logging
import os
import re
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Layer 1: MIME type allowlist ──────────────────────────────────────────────
# Only these base MIME types are accepted. Anything not in this set is rejected
# unless it is also absent from the blocked set (unknown → still rejected, because
# the allowlist is the authority, not the blocklist).
_SAFE_MIME_TYPES: frozenset = frozenset({
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/xml",
    "application/json",
    "application/xml",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/svg+xml",    # sanitised by Layer 3 SVG handler
    "text/html",        # sanitised by Layer 3 HTML handler
})

# MIME types that are immediately rejected — no further inspection attempted.
_BLOCKED_MIME_TYPES: frozenset = frozenset({
    "application/pdf",              # can embed JavaScript via /JS, /Action, /URI
    "application/octet-stream",     # generic binary — unknown content
    "application/x-executable",
    "application/x-sharedlib",
    "application/x-msdos-program",
    "application/javascript",
    "text/javascript",
    "application/wasm",
    "application/x-httpd-php",
    "application/x-sh",
    "application/x-bat",
    "application/x-msdownload",
    "application/x-python-code",
    "application/zip",
    "application/x-tar",
    "application/gzip",
    "application/x-bzip2",
    "application/x-7z-compressed",
    "application/x-rar-compressed",
})


# ── Layer 2: Extension blocklist ──────────────────────────────────────────────
_BLOCKED_EXTENSIONS: frozenset = frozenset({
    # Shell / scripting
    ".sh", ".bash", ".zsh", ".fish", ".ksh", ".csh",
    # Python / bytecode
    ".py", ".pyc", ".pyo", ".pyd",
    # JavaScript / TypeScript
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    # Windows executables / scripts
    ".exe", ".dll", ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe",
    # Unix native
    ".elf", ".so", ".dylib",
    # Java
    ".jar", ".class", ".war", ".ear",
    # WebAssembly
    ".wasm",
    # Other scripting
    ".pl", ".rb", ".php", ".lua", ".r", ".tcl",
    # PDF (executable scripting risk)
    ".pdf",
    # Package installers
    ".deb", ".rpm", ".pkg", ".msi", ".dmg", ".appimage",
    # Archives — could contain any of the above
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
})


# ── Layer 3a: Magic byte signatures ───────────────────────────────────────────
# Ordered from longest to shortest prefix to avoid false positives.
_MAGIC_SIGNATURES: List[Tuple[bytes, str]] = [
    (b"\xfd7zXZ\x00",    "XZ archive"),
    (b"\x7fELF",         "ELF binary"),
    (b"\xca\xfe\xba\xbe","Mach-O fat binary"),
    (b"\xcf\xfa\xed\xfe","Mach-O 64-bit binary"),
    (b"\xce\xfa\xed\xfe","Mach-O 32-bit binary"),
    (b"\x00asm",         "WebAssembly binary"),
    (b"PK\x03\x04",      "ZIP/JAR archive"),
    (b"Rar!\x1a\x07",    "RAR archive"),
    (b"\x1f\x8b",        "GZIP archive"),
    (b"BZh",             "BZIP2 archive"),
    (b"MZ",              "PE/Windows executable"),
    (b"%PDF",            "PDF document"),
]

# Shebang — dangerous only when it names a script interpreter.
_SHEBANG_RE = re.compile(rb"^#!\s*/(?:usr/(?:local/)?)?(?:bin/)?\w")


# ── Layer 3b: Script content patterns ─────────────────────────────────────────
# Applied to all text content (plain, JSON, XML, CSV) after strip operations.
_SCRIPT_CONTENT_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"<script[\s>]",                re.IGNORECASE), "embedded <script> tag"),
    (re.compile(r"javascript\s*:",              re.IGNORECASE), "javascript: URI"),
    (re.compile(r"\bon\w{1,20}\s*=\s*[\"']",   re.IGNORECASE), "inline event handler attribute"),
    (re.compile(r"\beval\s*\(",                 re.IGNORECASE), "eval() call"),
    (re.compile(r"\bexec\s*\(",                 re.IGNORECASE), "exec() call"),
    (re.compile(r"__import__\s*\(",             re.IGNORECASE), "__import__() call"),
    (re.compile(r"\bos\.system\s*\(",           re.IGNORECASE), "os.system() call"),
    (re.compile(r"\bsubprocess\b",              re.IGNORECASE), "subprocess usage"),
    (re.compile(r"\bimport\s+os\b",             re.IGNORECASE), "import os statement"),
    (re.compile(r"\bimport\s+subprocess\b",     re.IGNORECASE), "import subprocess statement"),
    (re.compile(r"\bimport\s+sys\b",            re.IGNORECASE), "import sys statement"),
    (re.compile(r"document\s*\.\s*write\s*\(",  re.IGNORECASE), "document.write() call"),
    (re.compile(r"window\s*\.\s*location",      re.IGNORECASE), "window.location redirect"),
    (re.compile(r"<\s*iframe\b",                re.IGNORECASE), "embedded iframe"),
    (re.compile(r"<\s*object\b",                re.IGNORECASE), "embedded object element"),
    (re.compile(r"<\s*embed\b",                 re.IGNORECASE), "embedded embed element"),
]

# ── Layer 3c: SVG sanitisation ────────────────────────────────────────────────
_SVG_SCRIPT_BLOCK_RE = re.compile(r"<script[\s\S]*?</script\s*>", re.IGNORECASE)
_SVG_SCRIPT_SELF_RE  = re.compile(r"<script\b[^>]*/>",             re.IGNORECASE)
_SVG_EVENT_ATTR_RE   = re.compile(r'\bon\w{1,20}\s*=\s*(?:"[^"]*"|\'[^\']*\'|\S+)', re.IGNORECASE)
_SVG_JS_HREF_RE      = re.compile(r'(?:xlink:)?href\s*=\s*"javascript:[^"]*"', re.IGNORECASE)

# ── Layer 3d: HTML sanitisation ───────────────────────────────────────────────
_HTML_SCRIPT_BLOCK_RE = re.compile(r"<script[\s\S]*?</script\s*>", re.IGNORECASE)
_HTML_SCRIPT_SELF_RE  = re.compile(r"<script\b[^>]*/>",             re.IGNORECASE)
_HTML_EVENT_ATTR_RE   = re.compile(r'\bon\w{1,20}\s*=\s*(?:"[^"]*"|\'[^\']*\'|\S+)', re.IGNORECASE)

# Text extensions considered scannable (script-pattern check applied)
_TEXT_EXTENSIONS: frozenset = frozenset({
    ".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".toml",
})


class MCPOutputSecurityError(Exception):
    """Raised when an MCP output fails the safety check."""

    def __init__(self, reason: str):
        super().__init__(f"MCP output rejected: {reason}")
        self.reason = reason


class MCPOutputGuard:
    """
    Three-layer safety check for external MCP tool outputs.

    Instantiate once and call :meth:`check` for each response.

    Example::

        guard = MCPOutputGuard()
        safe_text = guard.check(
            content=raw_bytes,
            content_type="text/plain; charset=utf-8",
            filename=None,
        )
        # raises MCPOutputSecurityError if unsafe
        # returns (possibly sanitised) string on success
    """

    def check(
        self,
        content: "bytes | str",
        content_type: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> str:
        """
        Run all three safety layers.  Returns safe (possibly sanitised) string.
        Raises :exc:`MCPOutputSecurityError` with a reason on any failure.

        Parameters
        ----------
        content:
            Raw bytes or decoded string from the MCP response body.
        content_type:
            Value of the ``Content-Type`` header (charset params are ignored).
        filename:
            Filename hint from the MCP payload, if any.  Used for extension
            checks.  Not used as a trust signal on its own.
        """
        raw_bytes: bytes = (
            content if isinstance(content, bytes)
            else content.encode("utf-8", errors="replace")
        )
        raw_str: str = (
            content if isinstance(content, str)
            else content.decode("utf-8", errors="replace")
        )

        base_mime = self._parse_base_mime(content_type)

        # ── Layer 1: MIME type allowlist ──────────────────────────────────────
        if base_mime is not None:
            if base_mime in _BLOCKED_MIME_TYPES:
                raise MCPOutputSecurityError(f"blocked MIME type '{base_mime}'")
            if base_mime not in _SAFE_MIME_TYPES:
                raise MCPOutputSecurityError(
                    f"MIME type '{base_mime}' is not in the safe allowlist"
                )

        # ── Layer 2: Extension blocklist ──────────────────────────────────────
        if filename:
            ext = self._get_ext(filename)
            if ext in _BLOCKED_EXTENSIONS:
                raise MCPOutputSecurityError(
                    f"file extension '{ext}' is not permitted for external MCP outputs"
                )

        # ── Layer 3: Content inspection ───────────────────────────────────────

        # 3a — magic bytes
        self._check_magic(raw_bytes)

        # 3b — shebang line
        if raw_bytes[:2] == b"#!" and _SHEBANG_RE.match(raw_bytes):
            raise MCPOutputSecurityError("content begins with a script shebang line")

        # 3c — SVG: sanitise then scan residual
        if base_mime == "image/svg+xml" or (
            filename and filename.lower().endswith(".svg")
        ):
            return self._sanitise_svg(raw_str)

        # 3d — HTML: sanitise then scan residual
        if base_mime == "text/html" or (
            filename and filename.lower().endswith(".html")
        ):
            return self._sanitise_html(raw_str)

        # 3e — text / JSON / XML / CSV: scan for script patterns
        if self._is_text(base_mime, filename):
            self._check_script_patterns(raw_str)

        return raw_str

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_base_mime(content_type: Optional[str]) -> Optional[str]:
        if not content_type:
            return None
        return content_type.split(";")[0].strip().lower()

    @staticmethod
    def _get_ext(filename: str) -> str:
        _, ext = os.path.splitext(filename.lower())
        return ext

    @staticmethod
    def _check_magic(raw: bytes) -> None:
        for sig, label in _MAGIC_SIGNATURES:
            if raw[: len(sig)] == sig:
                raise MCPOutputSecurityError(f"binary/archive content detected ({label})")

    @staticmethod
    def _is_text(base_mime: Optional[str], filename: Optional[str]) -> bool:
        if base_mime and (
            base_mime.startswith("text/")
            or base_mime in ("application/json", "application/xml", "text/xml")
        ):
            return True
        if filename:
            _, ext = os.path.splitext(filename.lower())
            return ext in _TEXT_EXTENSIONS
        # Unknown type — treat as text and scan it to be safe
        return True

    @staticmethod
    def _check_script_patterns(text: str) -> None:
        for pattern, label in _SCRIPT_CONTENT_PATTERNS:
            if pattern.search(text):
                raise MCPOutputSecurityError(f"unsafe pattern detected in content: {label}")

    @staticmethod
    def _sanitise_svg(svg: str) -> str:
        """Strip all script elements and event-handler attributes from SVG."""
        cleaned = _SVG_SCRIPT_BLOCK_RE.sub("", svg)
        cleaned = _SVG_SCRIPT_SELF_RE.sub("", cleaned)
        cleaned = _SVG_EVENT_ATTR_RE.sub("", cleaned)
        cleaned = _SVG_JS_HREF_RE.sub('href=""', cleaned)
        # Residual scan — catch anything that survived stripping
        for pattern, label in _SCRIPT_CONTENT_PATTERNS:
            if pattern.search(cleaned):
                raise MCPOutputSecurityError(
                    f"SVG contains unsafe pattern after sanitisation: {label}"
                )
        logger.debug("MCPOutputGuard: SVG sanitised — scripts/event-handlers removed")
        return cleaned

    @staticmethod
    def _sanitise_html(html: str) -> str:
        """Strip all script elements and event-handler attributes from HTML."""
        cleaned = _HTML_SCRIPT_BLOCK_RE.sub("", html)
        cleaned = _HTML_SCRIPT_SELF_RE.sub("", cleaned)
        cleaned = _HTML_EVENT_ATTR_RE.sub("", cleaned)
        for pattern, label in _SCRIPT_CONTENT_PATTERNS:
            if pattern.search(cleaned):
                raise MCPOutputSecurityError(
                    f"HTML contains unsafe pattern after sanitisation: {label}"
                )
        logger.debug("MCPOutputGuard: HTML sanitised — scripts/event-handlers removed")
        return cleaned
