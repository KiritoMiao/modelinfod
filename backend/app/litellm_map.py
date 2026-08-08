"""LiteLLM community model metadata (model_prices_and_context_window.json).

A copy is bundled at build time so the service works offline; it is refreshed
in the background from `prices_refresh_url` when reachable.
"""

import asyncio
import json
import logging
from importlib import resources
from typing import Any

import httpx

from .matching import iter_forms, norm_dashes, norm_tight, zero_costs

logger = logging.getLogger(__name__)

# Fields we surface as model_info, mirroring what the LiteLLM proxy returns
# from /v1/model/info. Anything else in the map entry is passed through too;
# this list only drives the admin UI's "known fields" ordering.
PRIMARY_FIELDS = [
    "max_tokens",
    "max_input_tokens",
    "max_output_tokens",
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
    "input_cost_per_token_priority",
    "output_cost_per_token_priority",
    "cache_read_input_token_cost_priority",
    "cache_creation_input_token_cost_priority",
    "priority_pricing",
    "tiered_pricing",
    "service_tier",
    "supports_service_tier",
    "litellm_provider",
    "mode",
    "supports_function_calling",
    "supports_parallel_function_calling",
    "supports_tool_choice",
    "supports_vision",
    "supports_reasoning",
    "supports_prompt_caching",
    "supports_response_schema",
    "supports_system_messages",
    "supports_web_search",
    "supports_pdf_input",
    "supports_audio_input",
    "supports_audio_output",
    "supported_modalities",
    "supported_output_modalities",
    "supported_endpoints",
]

# Providers whose litellm pricing matches their official API — used to resolve
# bare ids like "kimi-k2.5" to "moonshot/kimi-k2.5". Per-host aggregators
# (deepinfra, novita, …) are deliberately excluded: their pricing is specific
# to that host and would be misleading for a different gateway.
_PROVIDERS = (
    "openai", "anthropic", "gemini", "vertex_ai", "azure", "mistral", "groq",
    "deepseek", "xai", "openrouter", "bedrock", "cohere", "fireworks_ai",
    "together_ai", "moonshot", "zai", "minimax", "dashscope", "meta_llama",
    "nvidia", "perplexity",
)


class LiteLLMMap:
    def __init__(self) -> None:
        self._map: dict[str, dict[str, Any]] = {}
        self._lower: dict[str, str] = {}
        self._dashes: dict[str, str] = {}
        self._tight: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def load_bundled(self) -> None:
        raw = resources.files("app.data").joinpath("model_prices_seed.json").read_text("utf-8")
        self._set_map(json.loads(raw))
        logger.info("loaded bundled litellm map: %d entries", len(self._map))

    def _set_map(self, data: dict[str, Any]) -> None:
        data.pop("sample_spec", None)
        self._map = {k: v for k, v in data.items() if isinstance(v, dict)}
        # Normalized indexes; first key wins on collision (dict order = file
        # order, which puts canonical entries before regional dupes).
        lower: dict[str, str] = {}
        dashes: dict[str, str] = {}
        tight: dict[str, str] = {}
        for k in self._map:
            lower.setdefault(k.lower(), k)
            dashes.setdefault(norm_dashes(k), k)
            tight.setdefault(norm_tight(k), k)
        self._lower, self._dashes, self._tight = lower, dashes, tight

    async def refresh_from_url(self, url: str) -> int:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, dict) or len(data) < 100:
            raise ValueError("unexpected payload for litellm price map")
        async with self._lock:
            self._set_map(data)
        logger.info("refreshed litellm map from %s: %d entries", url, len(self._map))
        return len(self._map)

    @property
    def size(self) -> int:
        return len(self._map)

    def get(self, key: str) -> dict[str, Any] | None:
        """Exact-key access, used by manual matches."""
        return self._map.get(key)

    def search(self, query: str, limit: int = 50) -> list[str]:
        """Substring search over map keys (for the manual-match picker)."""
        q = norm_dashes(query.strip())
        if not q:
            return []
        hits = [k for k in self._map if q in norm_dashes(k)]
        # Shorter keys first: "gpt-4o" should rank above regional variants.
        hits.sort(key=lambda k: (len(k), k))
        return hits[:limit]

    def _candidates(self, base: str) -> list[str]:
        """Spellings to try for one base id, most specific first."""
        out: list[str] = []

        def add(c: str) -> None:
            if c not in out:
                out.append(c)

        add(base)
        parts = base.split("/")
        if len(parts) > 1:
            # Gateways often re-expose openrouter ids without the prefix;
            # litellm keys them under an "openrouter/" section.
            add(f"openrouter/{base}")
            # Progressively shorter tails: "openrouter/openai/gpt-4o" -> "openai/gpt-4o" -> "gpt-4o"
            for i in range(1, len(parts)):
                add("/".join(parts[i:]))
        bare = parts[-1]
        for p in _PROVIDERS:
            add(f"{p}/{bare}")
        return out

    def _find(self, base: str) -> str | None:
        """Match one base form against the map, exact tiers before fuzzy."""
        cands = self._candidates(base)
        for c in cands:
            if c in self._map:
                return c
        for index, norm in (
            (self._lower, str.lower),
            (self._dashes, norm_dashes),
            (self._tight, norm_tight),
        ):
            for c in cands:
                key = index.get(norm(c))
                if key is not None:
                    return key
        return None

    def lookup(self, model_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """Resolve an upstream model id to a litellm map entry.

        Base forms tried in order: the id as-is; with a ":tag" suffix removed
        (costs zeroed for ":free"); with a "-YYYYMMDD" date suffix removed;
        with a "-free" suffix removed (costs zeroed). Each form is expanded to
        provider-qualified/unqualified candidate spellings and matched
        exactly, then case-insensitively, then separator-insensitively.
        Returns (entry, matched_key); entry is a copy when costs were zeroed.
        """
        for base, is_free in iter_forms(model_id):
            key = self._find(base)
            if key is not None:
                entry = self._map[key]
                return (zero_costs(entry) if is_free else entry), key
        return None, None
