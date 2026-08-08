"""Management API mounted under /modelinfod/api (the admin SPA's backend)."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..config import get_settings
from ..litellm_map import PRIMARY_FIELDS
from ..overrides import normalize_override
from ..service import get_catalog
from ..upstream import fetch_upstream_models

logger = logging.getLogger(__name__)


async def require_admin(request: Request) -> None:
    token = get_settings().admin_token
    if not token:
        return
    auth = request.headers.get("authorization", "")
    if auth != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="invalid admin token")


router = APIRouter(dependencies=[Depends(require_admin)])


class OverrideBody(BaseModel):
    model_info: dict[str, Any] = Field(default_factory=dict)
    base_model: str = ""
    cost_multiplier: float | None = Field(default=None, gt=0)


class CustomModelBody(BaseModel):
    model_name: str = Field(min_length=1, max_length=512)
    litellm_params: dict[str, Any] = Field(default_factory=dict)
    model_info: dict[str, Any] = Field(default_factory=dict)


class HiddenBody(BaseModel):
    hidden: bool


class MatchBody(BaseModel):
    source: str = Field(pattern="^(litellm|openrouter|modelsdev)$")
    key: str = Field(min_length=1, max_length=512)


@router.get("/models")
async def list_models(request: Request):
    catalog = get_catalog(request.app.state)
    entries = catalog.build(include_hidden=True)
    catalog.annotate_admin_fields(entries)
    return {"data": entries, "known_fields": PRIMARY_FIELDS}


@router.get("/status")
async def status(request: Request):
    settings = get_settings()
    s = request.app.state.store.state
    overrides = request.app.state.overrides
    return {
        "upstream_models_url": settings.upstream_models_url,
        "upstream_synced_at": s["upstream_synced_at"],
        "upstream_error": s["upstream_error"],
        "upstream_model_count": len(s["upstream_models"]),
        "custom_model_count": len(s["custom_models"]),
        "override_count": len(overrides.overrides),
        "overrides_dir": str(overrides.dir),
        "override_load_errors": overrides.load_errors,
        "hidden_count": len(s["hidden"]),
        "manual_match_count": len(s["manual_matches"]),
        "litellm_map_entries": request.app.state.litellm_map.size,
        "prices_refreshed_at": s["prices_refreshed_at"],
        "prices_source": s["prices_source"],
        "openrouter_entries": request.app.state.openrouter.size,
        "openrouter_refreshed_at": request.app.state.openrouter.refreshed_at,
        "modelsdev_entries": request.app.state.modelsdev.size,
        "modelsdev_refreshed_at": request.app.state.modelsdev.refreshed_at,
        "sync_interval_seconds": settings.sync_interval_seconds,
    }


@router.post("/sync")
async def sync_now(request: Request):
    settings = get_settings()
    store = request.app.state.store
    try:
        ids = await fetch_upstream_models(settings)
    except Exception as exc:  # surface the failure to the UI, keep old cache
        message = f"{type(exc).__name__}: {exc}"
        await store.set_upstream_error(message)
        raise HTTPException(status_code=502, detail=message)
    await store.set_upstream_models(ids)
    return {"synced": len(ids)}


@router.post("/refresh-prices")
async def refresh_prices(request: Request):
    settings = get_settings()
    try:
        n = await request.app.state.litellm_map.refresh_from_url(settings.prices_refresh_url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")
    await request.app.state.store.set_prices_refreshed(settings.prices_refresh_url)
    return {"entries": n}


@router.post("/refresh-openrouter")
async def refresh_openrouter(request: Request):
    settings = get_settings()
    try:
        n = await request.app.state.openrouter.refresh(settings.openrouter_models_url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")
    return {"entries": n}


@router.post("/refresh-modelsdev")
async def refresh_modelsdev(request: Request):
    settings = get_settings()
    try:
        n = await request.app.state.modelsdev.refresh(settings.modelsdev_url)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")
    return {"entries": n}


_SUMMARY_FIELDS = (
    "max_input_tokens",
    "max_output_tokens",
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "litellm_provider",
    "mode",
)


def _summarize(source: str, key: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": source,
        "key": key,
        "info": {f: entry[f] for f in _SUMMARY_FIELDS if f in entry},
    }


@router.get("/metadata-keys")
async def search_metadata_keys(request: Request, q: str = "", limit: int = 30):
    """Search every metadata source for the manual-match picker."""
    limit = max(1, min(limit, 100))
    state = request.app.state
    sources = (
        ("modelsdev", state.modelsdev),
        ("litellm", state.litellm_map),
        ("openrouter", state.openrouter),
    )
    results = [
        _summarize(name, k, source.get(k))
        for name, source in sources
        for k in source.search(q, limit)
    ]
    return {"data": results}


@router.put("/models/{model_name:path}/match")
async def set_match(model_name: str, body: MatchBody, request: Request):
    source = {
        "litellm": request.app.state.litellm_map,
        "openrouter": request.app.state.openrouter,
        "modelsdev": request.app.state.modelsdev,
    }[body.source]
    entry = source.get(body.key)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"no {body.source} entry named {body.key!r}"
        )
    await request.app.state.store.set_manual_match(model_name, f"{body.source}:{body.key}")
    return {"ok": True}


@router.delete("/models/{model_name:path}/match")
async def delete_match(model_name: str, request: Request):
    if not await request.app.state.store.delete_manual_match(model_name):
        raise HTTPException(status_code=404, detail="no manual match for this model")
    return {"ok": True}


@router.put("/models/{model_name:path}/override")
async def set_override(model_name: str, body: OverrideBody, request: Request):
    cleaned = normalize_override(
        {
            "base_model": body.base_model,
            "cost_multiplier": body.cost_multiplier,
            "model_info": body.model_info,
        }
    )
    if cleaned is None:
        # Saving an empty override removes the file (same as DELETE).
        await request.app.state.overrides.delete(model_name)
        return {"ok": True}
    await request.app.state.overrides.set(model_name, cleaned)
    return {"ok": True}


@router.delete("/models/{model_name:path}/override")
async def delete_override(model_name: str, request: Request):
    if not await request.app.state.overrides.delete(model_name):
        raise HTTPException(status_code=404, detail="no override for this model")
    return {"ok": True}


@router.post("/reload-overrides")
async def reload_overrides(request: Request):
    n = request.app.state.overrides.reload()
    return {"overrides": n, "errors": request.app.state.overrides.load_errors}


@router.put("/models/{model_name:path}/hidden")
async def set_hidden(model_name: str, body: HiddenBody, request: Request):
    await request.app.state.store.set_hidden(model_name, body.hidden)
    return {"ok": True}


@router.post("/custom-models")
async def upsert_custom_model(body: CustomModelBody, request: Request):
    entry = {"litellm_params": body.litellm_params, "model_info": body.model_info}
    await request.app.state.store.upsert_custom_model(body.model_name, entry)
    return {"ok": True}


@router.delete("/custom-models/{model_name:path}")
async def delete_custom_model(model_name: str, request: Request):
    if not await request.app.state.store.delete_custom_model(model_name):
        raise HTTPException(status_code=404, detail="not a custom model")
    return {"ok": True}
