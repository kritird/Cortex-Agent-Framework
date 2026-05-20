"""Model-power registry — recommends an initial concurrency ceiling per LLM.

The right value of `max_parallel_llm_calls` depends on the backend:
- a single local Ollama serializes inference, so 1 is correct;
- Anthropic Haiku / GPT-4o-mini happily serve 8+ parallel HTTP calls;
- Opus / GPT-4 are slow and expensive enough that 2–4 is usually right.

This module picks a sensible starting value from the configured provider+model
identity. AdaptiveLLMGate (cortex/llm/adaptive_gate.py) then self-tunes from
there at runtime — the registry only needs to be a reasonable starting point,
not a precise capacity estimate.
"""
from __future__ import annotations

import logging
from fnmatch import fnmatchcase
from typing import List, Tuple

from cortex.config.schema import LLMAccessConfig

logger = logging.getLogger(__name__)


# Ordered (pattern, ceiling) rules matched against `provider:model_lower`.
# First match wins. Keep the table small and obvious — AdaptiveLLMGate will
# correct anything that turns out wrong in practice.
MODEL_POWER_TABLE: List[Tuple[str, int]] = [
    ("local:*",                 1),   # Ollama / vLLM single-stream
    ("anthropic_compatible:*",  2),   # custom OpenAI-compatible — be conservative
    ("anthropic:*haiku*",       8),
    ("anthropic:*sonnet*",      6),
    ("anthropic:*opus*",        4),
    ("openai:*nano*",           8),
    ("openai:*mini*",           8),
    ("openai:*gpt-4o*",         6),
    ("openai:*gpt-4*",          4),
    ("openai:*gpt-3.5*",        8),
    ("gemini:*flash*",          8),
    ("gemini:*pro*",            6),
    ("grok:*",                  4),
    ("mistral:*",               6),
    ("deepseek:*",              6),
    ("bedrock:*haiku*",         8),
    ("bedrock:*sonnet*",        6),
    ("bedrock:*opus*",          4),
    ("azure_ai:*",              6),
    ("custom:*",                2),
]

DEFAULT_CEILING = 2


def recommend_max_parallel_llm_calls(config: LLMAccessConfig) -> int:
    """Return a recommended ceiling based on the default provider+model.

    Looks up `provider:model_lower` against MODEL_POWER_TABLE. Falls back to
    DEFAULT_CEILING if nothing matches.
    """
    provider = (config.default.provider or "").strip().lower()
    model = (config.default.model or "").strip().lower()
    key = f"{provider}:{model}"

    for pattern, ceiling in MODEL_POWER_TABLE:
        if fnmatchcase(key, pattern):
            logger.info(
                "max_parallel_llm_calls auto-derived: %d (%s)", ceiling, key,
            )
            return ceiling

    logger.info(
        "max_parallel_llm_calls auto-derived: %d (%s — no registry match, using default)",
        DEFAULT_CEILING, key,
    )
    return DEFAULT_CEILING
