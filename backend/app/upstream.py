"""Fetch model lists from the upstream OpenAI-compatible endpoint."""

import hashlib
import logging
import time
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


class UpstreamAuthError(Exception):
    """The upstream rejected the presented API key (401/403)."""

    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"upstream returned {status_code}")


def parse_model_ids(payload: Any) -> list[str]:
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
    return [i for i in ids if not (i in seen or seen.add(i))]


async def fetch_upstream_models(settings: Settings) -> list[str]:
    """Full model list, fetched with the service's own key (admin/sync path)."""
    headers: dict[str, str] = {}
    if settings.upstream_api_key:
        headers["Authorization"] = f"Bearer {settings.upstream_api_key}"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(settings.upstream_models_url, headers=headers)
        resp.raise_for_status()
        payload: Any = resp.json()

    unique = parse_model_ids(payload)
    logger.info("upstream sync: %d models from %s", len(unique), settings.upstream_models_url)
    return unique


async def fetch_models_for_key(
    settings: Settings, authorization: str, client: httpx.AsyncClient
) -> list[str]:
    """Models the caller's key may access, per the upstream /v1/models.

    `authorization` is the client's Authorization header value, forwarded
    verbatim so the upstream applies its own key-based model permissions.
    Raises UpstreamAuthError when the upstream rejects the key.
    """
    resp = await client.get(
        settings.upstream_models_url, headers={"Authorization": authorization}
    )
    if resp.status_code in (401, 403):
        raise UpstreamAuthError(resp.status_code)
    resp.raise_for_status()
    return parse_model_ids(resp.json())


class PerKeyModelsCache:
    """TTL cache of per-key model lists, keyed by a hash of the auth header
    (raw keys are never stored). Only successful lookups are cached, so a
    rejected key is always re-checked against the upstream."""

    def __init__(self, ttl_seconds: float, max_entries: int = 1024):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[str, tuple[float, list[str]]] = {}

    @staticmethod
    def _hash(authorization: str) -> str:
        return hashlib.sha256(authorization.encode("utf-8")).hexdigest()

    def get(self, authorization: str) -> list[str] | None:
        if self.ttl <= 0:
            return None
        cached = self._entries.get(self._hash(authorization))
        if cached is None:
            return None
        expires_at, models = cached
        if time.monotonic() >= expires_at:
            return None
        return models

    def set(self, authorization: str, models: list[str]) -> None:
        if self.ttl <= 0:
            return
        now = time.monotonic()
        if len(self._entries) >= self.max_entries:
            self._entries = {
                h: e for h, e in self._entries.items() if e[0] > now
            }
            while len(self._entries) >= self.max_entries:
                self._entries.pop(next(iter(self._entries)))
        self._entries[self._hash(authorization)] = (now + self.ttl, models)
