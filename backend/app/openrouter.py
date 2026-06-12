"""OpenRouter model catalog (https://openrouter.ai/api/v1/models) as a
secondary metadata source for models the LiteLLM map doesn't know.

Entries are converted to litellm-style model_info fields at load time so the
merge layer can treat both sources uniformly. The fetched catalog is cached
in the data dir; there is no bundled copy, so until the first successful
fetch this source is simply empty.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .matching import iter_forms, norm_dashes, norm_tight, zero_costs

logger = logging.getLogger(__name__)

CACHE_FILE = "openrouter_models.json"

# OpenRouter pricing is per token, as decimal strings -> litellm cost fields.
_PRICING_FIELDS = {
    "prompt": "input_cost_per_token",
    "completion": "output_cost_per_token",
    "input_cache_read": "cache_read_input_token_cost",
    "input_cache_write": "cache_creation_input_token_cost",
}


def _to_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def convert_entry(m: dict[str, Any]) -> dict[str, Any]:
    """Convert one OpenRouter /models entry to litellm-style model_info."""
    info: dict[str, Any] = {"litellm_provider": "openrouter", "mode": "chat"}

    ctx = m.get("context_length")
    if isinstance(ctx, int) and ctx > 0:
        info["max_input_tokens"] = ctx
        info["max_tokens"] = ctx
    top = m.get("top_provider") or {}
    max_out = top.get("max_completion_tokens")
    if isinstance(max_out, int) and max_out > 0:
        info["max_output_tokens"] = max_out

    for src, dst in _PRICING_FIELDS.items():
        cost = _to_float((m.get("pricing") or {}).get(src))
        if cost is not None:
            info[dst] = cost

    params = set(m.get("supported_parameters") or [])
    if "tools" in params:
        info["supports_function_calling"] = True
        info["supports_tool_choice"] = "tool_choice" in params
    if "parallel_tool_calls" in params:
        info["supports_parallel_function_calling"] = True
    if "reasoning" in params or "include_reasoning" in params:
        info["supports_reasoning"] = True
    if "response_format" in params or "structured_outputs" in params:
        info["supports_response_schema"] = True
    if "web_search_options" in params:
        info["supports_web_search"] = True

    arch = m.get("architecture") or {}
    inputs = arch.get("input_modalities") or []
    if inputs:
        info["supported_modalities"] = list(inputs)
        info["supports_vision"] = "image" in inputs
        info["supports_audio_input"] = "audio" in inputs
        info["supports_pdf_input"] = "file" in inputs
    outputs = arch.get("output_modalities") or []
    if outputs:
        info["supported_output_modalities"] = list(outputs)
        info["supports_audio_output"] = "audio" in outputs
    if "cache_read_input_token_cost" in info:
        info["supports_prompt_caching"] = True

    name = m.get("name")
    if isinstance(name, str) and name:
        info["openrouter_name"] = name
    return info


class OpenRouterCatalog:
    """In-memory OpenRouter catalog keyed by model id, with the same
    candidate/normalization matching tiers as the LiteLLM map."""

    def __init__(self, data_dir: str | None = None) -> None:
        self._map: dict[str, dict[str, Any]] = {}
        self._lower: dict[str, str] = {}
        self._dashes: dict[str, str] = {}
        self._tight: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._cache_path = Path(data_dir) / CACHE_FILE if data_dir else None
        self.refreshed_at: float | None = None

    @property
    def size(self) -> int:
        return len(self._map)

    def get(self, key: str) -> dict[str, Any] | None:
        return self._map.get(key)

    def search(self, query: str, limit: int = 50) -> list[str]:
        q = norm_dashes(query.strip())
        if not q:
            return []
        hits = [k for k in self._map if q in norm_dashes(k)]
        hits.sort(key=lambda k: (len(k), k))
        return hits[:limit]

    def load_cached(self) -> None:
        """Load the last fetched catalog from disk, if any."""
        if self._cache_path is None or not self._cache_path.is_file():
            return
        try:
            payload = json.loads(self._cache_path.read_text(encoding="utf-8"))
            self._set_models(payload["models"])
            self.refreshed_at = payload.get("refreshed_at")
            logger.info("loaded cached openrouter catalog: %d entries", len(self._map))
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            logger.warning("ignoring bad openrouter cache: %s", exc)

    def _set_models(self, models: list[dict[str, Any]]) -> None:
        converted: dict[str, dict[str, Any]] = {}
        for m in models:
            mid = m.get("id")
            if isinstance(mid, str) and mid:
                converted[mid] = convert_entry(m)
        self._map = converted
        lower: dict[str, str] = {}
        dashes: dict[str, str] = {}
        tight: dict[str, str] = {}
        for k in self._map:
            lower.setdefault(k.lower(), k)
            dashes.setdefault(norm_dashes(k), k)
            tight.setdefault(norm_tight(k), k)
        self._lower, self._dashes, self._tight = lower, dashes, tight

    async def refresh(self, url: str) -> int:
        import time

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        models = data.get("data") if isinstance(data, dict) else None
        if not isinstance(models, list) or len(models) < 10:
            raise ValueError("unexpected payload for openrouter models")
        async with self._lock:
            self._set_models(models)
            self.refreshed_at = time.time()
            if self._cache_path is not None:
                try:
                    self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                    self._cache_path.write_text(
                        json.dumps({"refreshed_at": self.refreshed_at, "models": models}),
                        encoding="utf-8",
                    )
                except OSError as exc:
                    logger.warning("could not cache openrouter catalog: %s", exc)
        logger.info("refreshed openrouter catalog from %s: %d entries", url, len(self._map))
        return len(self._map)

    def _candidates(self, base: str) -> list[str]:
        """OpenRouter ids are "vendor/model"; gateways often drop the vendor."""
        out: list[str] = []

        def add(c: str) -> None:
            if c not in out:
                out.append(c)

        add(base)
        parts = base.split("/")
        for i in range(1, len(parts)):
            add("/".join(parts[i:]))
        return out

    def _find(self, base: str) -> str | None:
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
        # Ids whose vendor segment differs from OR's ("kimi-k2.5",
        # "opencode-go/mimo-v2.5" vs OR's "xiaomi/mimo-v2.5"): match on the
        # bare tail, but only when exactly one catalog entry has that tail.
        bare = norm_dashes(base.split("/")[-1])
        tails = [k for k in self._map if norm_dashes(k.split("/")[-1]) == bare]
        if len(tails) == 1:
            return tails[0]
        return None

    def lookup(self, model_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """Resolve an upstream model id to an OpenRouter catalog entry.

        Same base forms as the LiteLLM map (as-is, ":tag" stripped, date
        stripped, "-free" stripped) with costs zeroed for free variants.
        Returns (model_info, matched_key) or (None, None).
        """
        for base, is_free in iter_forms(model_id):
            key = self._find(base)
            if key is not None:
                entry = self._map[key]
                return (zero_costs(entry) if is_free else entry), key
        return None, None
