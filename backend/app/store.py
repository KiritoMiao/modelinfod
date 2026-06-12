"""Persistent JSON state: admin overrides, custom models, hidden models and
the last-seen upstream model list (so restarts don't blank the catalog)."""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "state.json"
        self._lock = asyncio.Lock()
        self._state: dict[str, Any] = {
            # NOTE: overrides moved to per-model files (see overrides.py);
            # a legacy "overrides" key in state.json is migrated at startup.
            "custom_models": {},    # model_name -> full entry (litellm_params + model_info)
            "manual_matches": {},   # model_name -> "litellm:<key>" | "openrouter:<id>"
            "hidden": [],           # model_names excluded from public /v1/model/info
            "upstream_models": [],  # cached ids from the upstream /v1/models
            "upstream_synced_at": None,
            "upstream_error": None,
            "prices_refreshed_at": None,
            "prices_source": "bundled",
        }
        self._load()

    def _load(self) -> None:
        try:
            on_disk = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(on_disk, dict):
                self._state.update(on_disk)
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError):
            # Corrupt state file: keep defaults, keep the broken file aside.
            try:
                self.path.rename(self.path.with_suffix(".corrupt"))
            except OSError:
                pass

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- snapshots ---------------------------------------------------------

    @property
    def state(self) -> dict[str, Any]:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._state))

    def pop_legacy_overrides(self) -> dict[str, Any]:
        """Remove and return the pre-file-based "overrides" key, if present."""
        legacy = self._state.pop("overrides", None)
        if legacy:
            self._persist()
            return legacy if isinstance(legacy, dict) else {}
        return {}

    # -- mutations ---------------------------------------------------------

    async def upsert_custom_model(self, model_name: str, entry: dict[str, Any]) -> None:
        async with self._lock:
            self._state["custom_models"][model_name] = entry
            self._persist()

    async def delete_custom_model(self, model_name: str) -> bool:
        async with self._lock:
            existed = self._state["custom_models"].pop(model_name, None) is not None
            if existed:
                self._persist()
            return existed

    async def set_manual_match(self, model_name: str, ref: str) -> None:
        async with self._lock:
            self._state["manual_matches"][model_name] = ref
            self._persist()

    async def delete_manual_match(self, model_name: str) -> bool:
        async with self._lock:
            existed = self._state["manual_matches"].pop(model_name, None) is not None
            if existed:
                self._persist()
            return existed

    async def set_hidden(self, model_name: str, hidden: bool) -> None:
        async with self._lock:
            names = set(self._state["hidden"])
            if hidden:
                names.add(model_name)
            else:
                names.discard(model_name)
            self._state["hidden"] = sorted(names)
            self._persist()

    async def set_upstream_models(self, ids: list[str]) -> None:
        async with self._lock:
            self._state["upstream_models"] = ids
            self._state["upstream_synced_at"] = time.time()
            self._state["upstream_error"] = None
            self._persist()

    async def set_upstream_error(self, message: str) -> None:
        async with self._lock:
            self._state["upstream_error"] = message
            self._persist()

    async def set_prices_refreshed(self, source: str) -> None:
        async with self._lock:
            self._state["prices_refreshed_at"] = time.time()
            self._state["prices_source"] = source
            self._persist()
