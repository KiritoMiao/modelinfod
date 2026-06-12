"""Merge upstream models, metadata sources, and admin overrides into
LiteLLM-proxy-style /v1/model/info entries:

    {
      "model_name": "...",
      "litellm_params": {"model": "..."},
      "model_info": {"id": "...", "db_model": false, ...metadata}
    }

Metadata resolution order for each model (first hit wins, one source per
model): admin manual match -> LiteLLM map (auto) -> OpenRouter catalog
(auto). On top of that baseline, in order: the override's `base_model`
swap, custom-model fields, the override's `cost_multiplier`, and finally
the override's explicit `model_info` fields.
"""

import hashlib
from typing import Any

from .litellm_map import LiteLLMMap
from .matching import scale_costs
from .openrouter import OpenRouterCatalog
from .overrides import OverridesStore
from .store import Store

_MAX_BASE_DEPTH = 5  # base_model chains deeper than this are ignored


def _entry_id(model_name: str) -> str:
    return hashlib.sha256(model_name.encode("utf-8")).hexdigest()


def resolve_manual_match(
    ref: str, litellm_map: LiteLLMMap, openrouter: OpenRouterCatalog
) -> tuple[dict[str, Any] | None, str, str]:
    """Resolve a stored "source:key" ref. Keys may contain ":" themselves
    (openrouter ids like "deepseek/deepseek-chat:free"), so split once."""
    source, _, key = ref.partition(":")
    if source == "litellm":
        return litellm_map.get(key), source, key
    if source == "openrouter":
        return openrouter.get(key), source, key
    return None, source, key


class Catalog:
    """Resolves model entries against all data sources."""

    def __init__(
        self,
        store: Store,
        litellm_map: LiteLLMMap,
        openrouter: OpenRouterCatalog,
        overrides: OverridesStore,
    ) -> None:
        self.store = store
        self.litellm_map = litellm_map
        self.openrouter = openrouter
        self.overrides = overrides

    def _auto_baseline(self, model_name: str) -> dict[str, Any]:
        """Manual match -> litellm -> openrouter metadata for one name."""
        manual_ref = self.store.state["manual_matches"].get(model_name)
        if manual_ref:
            entry, src, key = resolve_manual_match(
                manual_ref, self.litellm_map, self.openrouter
            )
            if entry is not None:
                return {**entry, "key": key, "metadata_source": src}
            # Dangling ref (key dropped from a refreshed source): fall through
            # to auto matching; the admin UI still shows the stored match.
        mapped, key = self.litellm_map.lookup(model_name)
        if mapped is not None:
            return {**mapped, "key": key, "metadata_source": "litellm"}
        mapped, key = self.openrouter.lookup(model_name)
        if mapped is not None:
            return {**mapped, "key": key, "metadata_source": "openrouter"}
        return {}

    def _resolve_info(
        self, model_name: str, _seen: frozenset[str]
    ) -> tuple[dict[str, Any], bool]:
        """Full model_info for one name: baseline + custom + override.

        Returns (info, poisoned). `poisoned` is True when a base_model cycle
        (or an over-deep chain) was detected anywhere below: every model on a
        cyclic chain then ignores its base reference and falls back to its
        own auto baseline, so a config mistake degrades predictably instead
        of inheriting another cycle member's partial data.
        """
        override = self.overrides.get(model_name)

        info: dict[str, Any] = {}
        poisoned = False
        base_name = override.get("base_model") if override else None
        if base_name and base_name != model_name:
            if base_name in _seen or len(_seen) >= _MAX_BASE_DEPTH:
                poisoned = True
            else:
                base_info, poisoned = self._resolve_info(
                    base_name, _seen | {model_name}
                )
                if not poisoned and base_info:
                    info = dict(base_info)
                    info["base_model"] = base_name
        if not info:
            info = dict(self._auto_baseline(model_name))

        custom = self.store.state["custom_models"].get(model_name)
        if custom:
            custom_info = custom.get("model_info")
            if isinstance(custom_info, dict):
                info.update(custom_info)

        if override:
            mult = override.get("cost_multiplier")
            if isinstance(mult, (int, float)) and mult > 0:
                info = scale_costs(info, float(mult))
                info["cost_multiplier"] = float(mult)
            explicit = override.get("model_info")
            if isinstance(explicit, dict):
                info.update(explicit)

        return info, poisoned

    def build_model_entry(self, model_name: str) -> dict[str, Any]:
        resolved, _ = self._resolve_info(model_name, frozenset())
        model_info: dict[str, Any] = {
            "id": _entry_id(model_name),
            "db_model": False,
            **resolved,
        }
        # The hash id / db_model of *this* model always win (a base_model
        # baseline carries the base's values otherwise).
        model_info["id"] = _entry_id(model_name)
        model_info["db_model"] = False

        litellm_params: dict[str, Any] = {"model": model_name}
        custom = self.store.state["custom_models"].get(model_name)
        if custom:
            params = custom.get("litellm_params")
            if isinstance(params, dict):
                litellm_params.update(params)
        # Never leak credentials even if someone stuffs them into params.
        litellm_params.pop("api_key", None)

        return {
            "model_name": model_name,
            "litellm_params": litellm_params,
            "model_info": model_info,
        }

    def build(
        self, *, include_hidden: bool = False, name: str | None = None
    ) -> list[dict[str, Any]]:
        state = self.store.state
        custom: dict[str, Any] = state["custom_models"]
        hidden: set[str] = set(state["hidden"])

        names: list[str] = list(state["upstream_models"])
        seen = set(names)
        names.extend(m for m in custom if m not in seen)

        if name is not None:
            names = [n for n in names if n == name]

        return [
            self.build_model_entry(n)
            for n in names
            if include_hidden or n not in hidden
        ]

    def annotate_admin_fields(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add management metadata the admin UI needs (not exposed publicly)."""
        state = self.store.state
        hidden = set(state["hidden"])
        custom = state["custom_models"]
        manual = state["manual_matches"]
        upstream = set(state["upstream_models"])
        for e in entries:
            n = e["model_name"]
            e["_admin"] = {
                "hidden": n in hidden,
                "override": self.overrides.get(n),
                "is_custom": n in custom,
                "in_upstream": n in upstream,
                "manual_match": manual.get(n),
            }
        return entries


def get_catalog(app_state: Any) -> Catalog:
    return Catalog(
        app_state.store, app_state.litellm_map, app_state.openrouter, app_state.overrides
    )
