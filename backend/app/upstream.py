"""Fetch the model list from the upstream OpenAI-compatible endpoint."""

import logging
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


async def fetch_upstream_models(settings: Settings) -> list[str]:
    headers: dict[str, str] = {}
    if settings.upstream_api_key:
        headers["Authorization"] = f"Bearer {settings.upstream_api_key}"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(settings.upstream_models_url, headers=headers)
        resp.raise_for_status()
        payload: Any = resp.json()

    # OpenAI shape: {"object": "list", "data": [{"id": ...}, ...]}
    # Be lenient: accept a bare list, or a list of strings.
    items = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError(f"unexpected /v1/models payload type: {type(payload).__name__}")

    ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict) and isinstance(item.get("id"), str):
            ids.append(item["id"])

    seen: set[str] = set()
    unique = [i for i in ids if not (i in seen or seen.add(i))]
    logger.info("upstream sync: %d models from %s", len(unique), settings.upstream_models_url)
    return unique
