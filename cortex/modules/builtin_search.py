"""Built-in DuckDuckGo web search — no API key required.

Uses DuckDuckGo Lite (lite.duckduckgo.com) which returns clean HTML without
JavaScript, making it reliable for programmatic access. Automatically used
when no external search tool server (Brave, SerpAPI, etc.) is configured.
"""
import asyncio
import logging
import re
import ssl
from typing import Dict, List, Tuple

import aiohttp

logger = logging.getLogger(__name__)

_DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
_DDG_INSTANT_URL = "https://api.duckduckgo.com/"
_DEFAULT_MAX_RESULTS = 8
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://lite.duckduckgo.com",
    "Referer": "https://lite.duckduckgo.com/",
}

# Reuse a permissive SSL context — avoids failures in corporate proxy environments
def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class DuckDuckGoSearch:
    """Built-in web search via DuckDuckGo Lite. No API key required."""

    async def search(self, query: str, max_results: int = _DEFAULT_MAX_RESULTS) -> str:
        query = _extract_query(query)
        logger.info("Built-in DDG search: %r", query)
        ssl_ctx = _ssl_ctx()
        async with aiohttp.ClientSession(headers=_HEADERS, timeout=_TIMEOUT) as session:
            instant, results = await asyncio.gather(
                self._instant_answer(session, query, ssl_ctx),
                self._lite_search(session, query, max_results, ssl_ctx),
                return_exceptions=True,
            )
        if isinstance(instant, Exception):
            logger.debug("DDG instant failed: %s", instant)
            instant = {}
        if isinstance(results, Exception):
            logger.debug("DDG lite search failed: %s", results)
            results = []
        return _format_results(query, instant, results)

    async def _instant_answer(
        self, session: aiohttp.ClientSession, query: str, ssl_ctx: ssl.SSLContext
    ) -> Dict:
        async with session.get(
            _DDG_INSTANT_URL,
            params={"q": query, "format": "json", "no_redirect": "1", "no_html": "1"},
            ssl=ssl_ctx,
        ) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
        return {}

    async def _lite_search(
        self,
        session: aiohttp.ClientSession,
        query: str,
        max_results: int,
        ssl_ctx: ssl.SSLContext,
    ) -> List[Dict]:
        async with session.post(
            _DDG_LITE_URL,
            data={"q": query},
            ssl=ssl_ctx,
        ) as resp:
            if resp.status == 200:
                html = await resp.text()
                return _parse_lite_results(html, max_results)
        return []


# ── parsing ───────────────────────────────────────────────────────────────────

# DDG Lite structure (href comes before class in the anchor tag):
#   <a rel="nofollow" href="URL" class='result-link'>Title</a>
#   <td class='result-snippet'>Snippet text</td>
_LINK_RE = re.compile(
    r"<a\b[^>]*href=\"(https?://[^\"]+)\"[^>]*class='result-link'[^>]*>(.*?)</a>",
    re.DOTALL,
)
_SNIPPET_RE = re.compile(
    r"class='result-snippet'[^>]*>(.*?)</td>",
    re.DOTALL,
)


def _parse_lite_results(html: str, max_results: int) -> List[Dict]:
    links: List[Tuple[str, str]] = [
        (m.group(1), _clean(m.group(2)))
        for m in _LINK_RE.finditer(html)
        if "duckduckgo.com/y.js" not in m.group(1)  # skip ad redirect URLs
    ]
    snippets: List[str] = [
        _clean(m.group(1)) for m in _SNIPPET_RE.finditer(html)
    ]
    results = []
    for i, (url, title) in enumerate(links[:max_results]):
        if title:
            results.append({
                "title": title,
                "url": url,
                "snippet": snippets[i] if i < len(snippets) else "",
            })
    return results


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _clean(text: str) -> str:
    from html import unescape
    return unescape(_strip_tags(text))


# ── query extraction ──────────────────────────────────────────────────────────

_PREAMBLE_RE = re.compile(
    r"^(search (?:the web |internet |online )?for|look up|find|get|what is|"
    r"what are|search|fetch|retrieve|tell me(?: about)?)\s+",
    re.IGNORECASE,
)


def _extract_query(instruction: str) -> str:
    q = _PREAMBLE_RE.sub("", instruction.strip())
    return q.strip(" .?!\"'")[:300]


# ── formatting ────────────────────────────────────────────────────────────────

def _format_results(query: str, instant: Dict, results: List[Dict]) -> str:
    lines = [f"Web search results for: **{query}**\n"]

    answer = (instant.get("Answer") or "").strip()
    answer_type = (instant.get("AnswerType") or "").strip()
    abstract = (instant.get("AbstractText") or "").strip()
    abstract_url = (instant.get("AbstractURL") or "").strip()

    if answer:
        label = answer_type.replace("_", " ").title() if answer_type else "Direct answer"
        lines.append(f"**{label}:** {answer}\n")

    if abstract:
        lines.append(f"**Summary:** {abstract}")
        if abstract_url:
            lines.append(f"Source: {abstract_url}")
        lines.append("")

    if results:
        lines.append("**Search Results:**")
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. [{r['title']}]({r['url']})")
            if r.get("snippet"):
                lines.append(f"   {r['snippet']}")
    elif not abstract and not answer:
        lines.append("No results found. Try rephrasing your query.")

    lines.append("\n_Search powered by DuckDuckGo_")
    return "\n".join(lines)
