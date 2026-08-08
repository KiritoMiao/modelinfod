"""models.dev catalog (https://models.dev/api.json) as the primary metadata
source: it is the most actively curated of the three and, unlike the LiteLLM
map and the OpenRouter catalog, carries cache pricing, context-tier pricing
and priority ("fast") service-tier pricing for most frontier models.

Entries are converted to litellm-style model_info fields at load time so the
merge layer can treat all sources uniformly. models.dev quotes costs per
million tokens; litellm quotes them per token, so every cost is divided by
1e6 on the way in.

Payload shape is `{provider_id: {..., "models": {model_id: {...}}}}`, so the
same model id appears under many providers (~1/3 of all ids do) with each
host's own pricing. Entries are keyed "<provider>/<model_id>" and a bare id
resolves to the most authoritative provider that lists it — see
`PREFERRED_PROVIDERS`. The fetched catalog is cached in the data dir; there
is no bundled copy, so until the first successful fetch this source is
simply empty.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .matching import iter_forms, norm_dashes, norm_tight, zero_costs

logger = logging.getLogger(__name__)

CACHE_FILE = "modelsdev_models.json"

_PER_MILLION = 1_000_000.0

# models.dev cost keys -> litellm model_info field names. Tier and priority
# variants reuse these with a suffix (see _convert_costs).
_COST_FIELDS = {
    "input": "input_cost_per_token",
    "output": "output_cost_per_token",
    "cache_read": "cache_read_input_token_cost",
    "cache_write": "cache_creation_input_token_cost",
    "reasoning": "output_cost_per_reasoning_token",
    "input_audio": "input_cost_per_audio_token",
    "output_audio": "output_cost_per_audio_token",
}

# The two cache fields this source exists to supply; `service.py` tops these
# up on entries resolved from a source that lacks them.
CACHE_COST_FIELDS = (
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
)

# Providers that serve only models they built themselves, so their prices
# are the model's official ones.
_VENDOR_PROVIDERS = (
    "openai", "anthropic", "google", "google-vertex", "google-vertex-anthropic",
    "xai", "meta", "llama", "mistral", "deepseek", "moonshotai", "moonshotai-cn",
    "zai", "zhipuai", "minimax", "minimax-cn", "xiaomi", "stepfun", "stepfun-ai",
    "cohere", "perplexity", "upstage", "inception", "sarvam", "morph",
)

# First-party clouds and vendor gateways: authoritative for their own models
# but they also resell other vendors', so they rank below every vendor above
# — otherwise "minimax-m2.5" would price from Alibaba's Model Studio listing
# rather than from MiniMax.
_PLATFORM_PROVIDERS = (
    "alibaba", "alibaba-cn", "amazon-bedrock", "azure", "google-vertex",
    "databricks", "groq", "cerebras", "opencode", "opencode-go", "openrouter",
)

# The only providers a bare model id resolves to, most authoritative first.
# Re-hosts and aggregators (deepinfra, poe, abacus, …) are deliberately
# excluded: their pricing is specific to that host and would be misleading
# for a different gateway, and a model they carry is usually described more
# accurately by the LiteLLM map anyway. Their entries are still loaded and
# stay reachable by fully-qualified "<provider>/<id>" key — from the search
# box and from a manual match.
PREFERRED_PROVIDERS = _VENDOR_PROVIDERS + _PLATFORM_PROVIDERS


def _to_float(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _per_token(v: Any) -> float | None:
    """models.dev quotes USD per 1M tokens; litellm wants USD per token."""
    cost = _to_float(v)
    return None if cost is None else cost / _PER_MILLION


def _threshold_label(size: int) -> str:
    """litellm spells these "above_200k_tokens" / "above_272k_tokens"."""
    if size % 1000 == 0:
        return f"above_{size // 1000}k_tokens"
    return f"above_{size}_tokens"


def _convert_costs(cost: dict[str, Any], suffix: str = "") -> dict[str, float]:
    """One models.dev cost object -> litellm cost fields, optionally suffixed
    ("_priority", "_above_272k_tokens")."""
    out: dict[str, float] = {}
    for src, dst in _COST_FIELDS.items():
        value = _per_token(cost.get(src))
        if value is not None:
            out[f"{dst}{suffix}"] = value
    return out


def _context_tiers(cost: dict[str, Any]) -> list[dict[str, Any]]:
    """`cost.tiers` entries that price tokens above a context threshold,
    smallest threshold first. Other tier types are ignored."""
    tiers: list[dict[str, Any]] = []
    for tier in cost.get("tiers") or []:
        if not isinstance(tier, dict):
            continue
        spec = tier.get("tier")
        if not isinstance(spec, dict) or spec.get("type") != "context":
            continue
        size = spec.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            continue
        tiers.append({"size": size, "cost": tier})
    tiers.sort(key=lambda t: t["size"])
    return tiers


def _tiered_pricing(cost: dict[str, Any], tiers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """litellm's nested `tiered_pricing`: contiguous [lo, hi) token ranges,
    the first carrying the base price and each tier taking over above its
    threshold. The final range is open-ended (hi = None)."""
    bounds = [t["size"] for t in tiers]
    rows = [{"range": [0, bounds[0]], **_convert_costs(cost)}]
    for i, tier in enumerate(tiers):
        hi = bounds[i + 1] if i + 1 < len(bounds) else None
        rows.append({"range": [tier["size"], hi], **_convert_costs(tier["cost"])})
    return rows


PRIORITY_MODE = "fast"


def _priority_mode(model: dict[str, Any]) -> dict[str, Any] | None:
    """`experimental.modes.fast` — the priority/fast service tier. Only modes
    that actually carry pricing are of interest ("pro" only sets a reasoning
    flag)."""
    experimental = model.get("experimental")
    if not isinstance(experimental, dict):
        return None
    mode = (experimental.get("modes") or {}).get(PRIORITY_MODE)
    if not isinstance(mode, dict) or not isinstance(mode.get("cost"), dict):
        return None
    return mode


def _service_tier(mode: dict[str, Any]) -> str:
    """What the provider calls this tier. OpenAI sends `service_tier:
    "priority"` in the request body; Anthropic instead sets `speed: "fast"`
    plus a beta header, so fall back to the mode's own name rather than
    claiming a tier name the provider never uses."""
    body = (mode.get("provider") or {}).get("body") or {}
    tier = body.get("service_tier")
    return tier if isinstance(tier, str) and tier else PRIORITY_MODE


def convert_entry(provider_id: str, m: dict[str, Any]) -> dict[str, Any]:
    """Convert one models.dev model entry to litellm-style model_info."""
    info: dict[str, Any] = {"litellm_provider": provider_id, "mode": "chat"}

    limit = m.get("limit") or {}
    context = limit.get("context")
    if isinstance(context, int) and context > 0:
        info["max_input_tokens"] = context
        info["max_tokens"] = context
    # `limit.input` is the (smaller) real input cap when the model reserves
    # part of its context for output.
    max_in = limit.get("input")
    if isinstance(max_in, int) and max_in > 0:
        info["max_input_tokens"] = max_in
    max_out = limit.get("output")
    if isinstance(max_out, int) and max_out > 0:
        info["max_output_tokens"] = max_out

    cost = m.get("cost")
    if isinstance(cost, dict):
        info.update(_convert_costs(cost))

        # Context-tier pricing: flat litellm-style "_above_<N>_tokens" fields
        # for consumers that read those, plus the nested `tiered_pricing`
        # array that spells out every range.
        tiers = _context_tiers(cost)
        for tier in tiers:
            info.update(_convert_costs(tier["cost"], f"_{_threshold_label(tier['size'])}"))
        if tiers:
            info["tiered_pricing"] = _tiered_pricing(cost, tiers)

    # Priority ("fast") service tier: same treatment — flat "_priority"
    # fields plus a nested object naming the tier.
    mode = _priority_mode(m)
    if mode is not None:
        priority_costs = _convert_costs(mode["cost"])
        if priority_costs:
            info.update(_convert_costs(mode["cost"], "_priority"))
            info["priority_pricing"] = {
                "service_tier": _service_tier(mode),
                **priority_costs,
            }
            info["supports_service_tier"] = True

    if m.get("tool_call") is True:
        info["supports_function_calling"] = True
        info["supports_tool_choice"] = True
    if m.get("reasoning") is True:
        info["supports_reasoning"] = True
    if m.get("structured_output") is True:
        info["supports_response_schema"] = True

    modalities = m.get("modalities") or {}
    inputs = modalities.get("input") or []
    if inputs:
        info["supported_modalities"] = list(inputs)
        info["supports_vision"] = "image" in inputs
        info["supports_audio_input"] = "audio" in inputs
        info["supports_pdf_input"] = "pdf" in inputs
    outputs = modalities.get("output") or []
    if outputs:
        info["supported_output_modalities"] = list(outputs)
        info["supports_audio_output"] = "audio" in outputs
    if "cache_read_input_token_cost" in info:
        info["supports_prompt_caching"] = True

    name = m.get("name")
    if isinstance(name, str) and name:
        info["modelsdev_name"] = name
    if m.get("status") == "deprecated":
        info["deprecated"] = True
    return info


def promote_priority(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with the priority tier's prices as the model's own.

    Used for upstream ids like "gpt-5.5-fast", which are the same model
    served at the priority service tier: the base costs become the priority
    costs, and `service_tier` records why. The `*_priority` fields and the
    `priority_pricing` object are dropped — they would now just restate the
    base price.
    """
    priority = entry.get("priority_pricing")
    if not isinstance(priority, dict):
        return entry
    out = {
        k: v
        for k, v in entry.items()
        if not k.endswith("_priority") and k != "priority_pricing"
    }
    out.update({k: v for k, v in priority.items() if k != "service_tier"})
    out["service_tier"] = priority.get("service_tier", "priority")
    # Context-tier pricing is quoted at the standard tier only; leaving it
    # would under-price large requests on a priority model.
    out.pop("tiered_pricing", None)
    for key in list(out):
        if "_above_" in key and "cost" in key:
            del out[key]
    return out


# Upstream spellings for "this model at the priority service tier".
_FAST_SUFFIXES = ("-fast", "-priority", ":fast", ":priority")


def strip_fast_suffix(model_id: str) -> str | None:
    """The base id behind a fast/priority variant, or None."""
    for suffix in _FAST_SUFFIXES:
        if model_id.endswith(suffix) and len(model_id) > len(suffix):
            return model_id[: -len(suffix)]
    return None


class ModelsDevCatalog:
    """In-memory models.dev catalog keyed "<provider>/<model_id>", with the
    same candidate/normalization matching tiers as the other sources plus
    provider-preference resolution for bare ids."""

    def __init__(self, data_dir: str | None = None) -> None:
        self._map: dict[str, dict[str, Any]] = {}
        self._bare: dict[str, str] = {}    # normalized bare id -> best key
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
            self._set_providers(payload["providers"])
            self.refreshed_at = payload.get("refreshed_at")
            logger.info("loaded cached models.dev catalog: %d entries", len(self._map))
        except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
            logger.warning("ignoring bad models.dev cache: %s", exc)

    def _set_providers(self, providers: dict[str, Any]) -> None:
        converted: dict[str, dict[str, Any]] = {}
        # bare id -> (provider rank, key); lower rank wins.
        best: dict[str, tuple[int, str]] = {}
        rank_of = {p: i for i, p in enumerate(PREFERRED_PROVIDERS)}

        for provider_id, provider in sorted(providers.items()):
            if not isinstance(provider, dict):
                continue
            models = provider.get("models")
            if not isinstance(models, dict):
                continue
            rank = rank_of.get(provider_id)
            for model_id, model in models.items():
                if not isinstance(model_id, str) or not model_id or not isinstance(model, dict):
                    continue
                key = f"{provider_id}/{model_id}"
                converted[key] = convert_entry(provider_id, model)
                if rank is None:
                    continue  # loaded, but never wins a bare id
                # Both the id as listed and its bare tail resolve here:
                # "openai/gpt-5.5" under provider "zenmux" should be findable
                # as "openai/gpt-5.5" and as "gpt-5.5".
                for form in {model_id, model_id.rsplit("/", 1)[-1]}:
                    norm = norm_dashes(form)
                    current = best.get(norm)
                    if current is None or rank < current[0]:
                        best[norm] = (rank, key)

        self._map = converted
        self._bare = {norm: key for norm, (_, key) in best.items()}
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

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, dict) or len(data) < 5:
            raise ValueError("unexpected payload for models.dev catalog")
        async with self._lock:
            self._set_providers(data)
            self.refreshed_at = time.time()
            if self._cache_path is not None:
                try:
                    self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                    self._cache_path.write_text(
                        json.dumps({"refreshed_at": self.refreshed_at, "providers": data}),
                        encoding="utf-8",
                    )
                except OSError as exc:
                    logger.warning("could not cache models.dev catalog: %s", exc)
        logger.info("refreshed models.dev catalog from %s: %d entries", url, len(self._map))
        return len(self._map)

    def _find(self, base: str) -> str | None:
        """Match one base form against the catalog, exact tiers before fuzzy.

        A fully-qualified "<provider>/<id>" wins; otherwise the id resolves
        through the provider-preference index, which already collapses the
        many hosts listing the same model.
        """
        if base in self._map:
            return base
        key = self._bare.get(norm_dashes(base))
        if key is not None:
            return key
        # Gateways re-expose ids with an extra vendor prefix ("gpt-pro/gpt-5.5")
        # or drop one ("gpt-5.5" for "openai/gpt-5.5"); try shorter tails.
        parts = base.split("/")
        for i in range(1, len(parts)):
            tail = "/".join(parts[i:])
            if tail in self._map:
                return tail
            key = self._bare.get(norm_dashes(tail))
            if key is not None:
                return key
        for index, norm in (
            (self._lower, str.lower),
            (self._dashes, norm_dashes),
            (self._tight, norm_tight),
        ):
            key = index.get(norm(base))
            if key is not None:
                return key
        # Separator-insensitive bare match ("glm4.6" vs "glm-4.6").
        tight = norm_tight(base.split("/")[-1])
        matches = [k for norm, k in self._bare.items() if norm_tight(norm) == tight]
        if len(set(matches)) == 1:
            return matches[0]
        return None

    def lookup(self, model_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """Resolve an upstream model id to a models.dev catalog entry.

        Same base forms as the other sources (as-is, ":tag" stripped, date
        stripped, "-free" stripped) with costs zeroed for free variants.

        Each form is matched directly first and only then read as a
        fast/priority variant, so a model genuinely named that way
        ("grok-code-fast-1", "kimi-k2.6-fast") matches itself rather than
        being mistaken for a service tier of some neighbour. As a variant it
        resolves to its base with the priority prices promoted to the model's
        own — and only when that base actually publishes them, so an
        unrelated "-fast" id resolves to nothing rather than to a wrongly
        priced model.

        Returns (model_info, matched_key) or (None, None).
        """
        for base, is_free in iter_forms(model_id):
            key = self._find(base)
            if key is not None:
                entry = self._map[key]
                return (zero_costs(entry) if is_free else entry), key
            # Not a model in its own right — is it the priority tier of one?
            stripped = strip_fast_suffix(base)
            if stripped is None:
                continue
            key = self._find(stripped)
            if key is not None and "priority_pricing" in self._map[key]:
                entry = promote_priority(self._map[key])
                return (zero_costs(entry) if is_free else entry), key
        return None, None
