"""Credential scrubbing from logs, streams, and LLM responses."""
import re
from typing import List, Optional


DEFAULT_SCRUB_PATTERNS = [
    r'Bearer\s+[A-Za-z0-9\-._~+/]+=*',
    r'api[_\-]?key[_\-]?[=:]\s*\S+',
    r'password[_\-]?[=:]\s*\S+',
    r'token[_\-]?[=:]\s*\S+',
    r'secret[_\-]?[=:]\s*\S+',
    r'Authorization:\s*\S+',
    r'sk-[A-Za-z0-9]{20,}',
    r'xoxb-[A-Za-z0-9\-]{20,}',
    r'ghp_[A-Za-z0-9]{36}',
    r'AKIA[A-Z0-9]{16}',
]

REPLACEMENT = '[REDACTED]'


class CredentialScrubber:
    """Scrubs credentials and secrets from strings before logging or returning to users."""

    def __init__(self, extra_patterns: Optional[List[str]] = None):
        all_patterns = DEFAULT_SCRUB_PATTERNS + (extra_patterns or [])
        self._compiled = [re.compile(p, re.IGNORECASE) for p in all_patterns]

    def scrub(self, text: str) -> str:
        if not isinstance(text, str):
            return text
        for pattern in self._compiled:
            text = pattern.sub(REPLACEMENT, text)
        return text

    def scrub_dict(self, data: dict) -> dict:
        result = {}
        for k, v in data.items():
            if isinstance(v, str):
                result[k] = self.scrub(v)
            elif isinstance(v, dict):
                result[k] = self.scrub_dict(v)
            elif isinstance(v, list):
                result[k] = self.scrub_list(v)
            else:
                result[k] = v
        return result

    def scrub_list(self, data: list) -> list:
        result = []
        for item in data:
            if isinstance(item, str):
                result.append(self.scrub(item))
            elif isinstance(item, dict):
                result.append(self.scrub_dict(item))
            elif isinstance(item, list):
                result.append(self.scrub_list(item))
            else:
                result.append(item)
        return result

    def is_clean(self, text: str) -> bool:
        for pattern in self._compiled:
            if pattern.search(text):
                return False
        return True
