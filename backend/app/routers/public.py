"""Public endpoints. /v1/model/info authenticates each caller by replaying
their API key against the upstream /v1/models: the upstream decides which
models the key may access, and only those entries are returned."""

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

from ..config import get_settings
from ..service import get_catalog
from ..upstream import UpstreamAuthError, fetch_models_for_key

logger = logging.getLogger(__name__)

router = APIRouter()


async def allowed_models_for(request: Request) -> list[str]:
    """The caller's model list per the upstream, from cache or live."""
    authorization = request.headers.get("authorization", "").strip()
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    state = request.app.state
    cached = state.user_models_cache.get(authorization)
    if cached is not None:
        return cached

    try:
        models = await fetch_models_for_key(
            get_settings(), authorization, state.upstream_client
        )
    except UpstreamAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail="invalid API key")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("per-key upstream check failed: %s", exc)
        raise HTTPException(status_code=502, detail="upstream model check failed")

    state.user_models_cache.set(authorization, models)
    return models


@router.get("/v1/model/info")
@router.get("/model/info")
async def model_info(request: Request, litellm_model_id: str | None = None):
    allowed = await allowed_models_for(request)
    entries = get_catalog(request.app.state).build(
        name=litellm_model_id, allowed=allowed
    )
    return {"data": entries}


@router.get("/healthz")
async def healthz(request: Request):
    state = request.app.state.store.state
    return {
        "status": "ok",
        "models": len(state["upstream_models"]),
        "upstream_synced_at": state["upstream_synced_at"],
    }
