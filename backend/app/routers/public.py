"""Public, unauthenticated endpoints. API-key checks happen at the layer in
front of this service; model info is not sensitive."""

from fastapi import APIRouter, Request

from ..service import get_catalog

router = APIRouter()


@router.get("/v1/model/info")
@router.get("/model/info")
async def model_info(request: Request, litellm_model_id: str | None = None):
    entries = get_catalog(request.app.state).build(name=litellm_model_id)
    return {"data": entries}


@router.get("/healthz")
async def healthz(request: Request):
    state = request.app.state.store.state
    return {
        "status": "ok",
        "models": len(state["upstream_models"]),
        "upstream_synced_at": state["upstream_synced_at"],
    }
