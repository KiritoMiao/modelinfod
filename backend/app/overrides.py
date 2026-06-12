"""File-based per-model overrides.

Each override lives in its own JSON file inside `overrides_dir` so it can be
shared between deployments (drop the file in, it is picked up live). Format:

    {
      "model_name": "gpt-5.5-fast",       // optional; defaults to file stem
      "base_model": "gpt-5.5",            // optional: inherit Y's resolved info
      "cost_multiplier": 2.5,             // optional: scale all *cost* fields
      "model_info": {"mode": "chat"}      // optional: explicit field patches
    }

Filenames are slugs (model names may contain "/" which filenames cannot);
the `model_name` field inside the file is authoritative. Files written by
the admin API always include it.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RESCAN_TTL = 2.0  # seconds between change checks on hot paths

OVERRIDE_KEYS = ("base_model", "cost_multiplier", "model_info")


def _slug(model_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "__", model_name).strip("._") or "model"


def normalize_override(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Validate/clean one override dict. Returns None if it is empty."""
    out: dict[str, Any] = {}
    base = raw.get("base_model")
    if isinstance(base, str) and base.strip():
        out["base_model"] = base.strip()
    mult = raw.get("cost_multiplier")
    if isinstance(mult, (int, float)) and not isinstance(mult, bool) and mult > 0:
        out["cost_multiplier"] = float(mult)
    info = raw.get("model_info")
    if isinstance(info, dict) and info:
        out["model_info"] = info
    return out or None


class OverridesStore:
    """In-memory view of the overrides directory, reloaded when files change."""

    def __init__(self, overrides_dir: str) -> None:
        self.dir = Path(overrides_dir)
        self._lock = asyncio.Lock()
        self._overrides: dict[str, dict[str, Any]] = {}  # model_name -> override
        self._files: dict[str, Path] = {}                # model_name -> file path
        self._signature: tuple = ()
        self._checked_at = 0.0
        self.load_errors: list[str] = []
        self.reload()

    # -- reading -----------------------------------------------------------

    def _scan_signature(self) -> tuple:
        try:
            return tuple(
                sorted(
                    (p.name, p.stat().st_mtime_ns, p.stat().st_size)
                    for p in self.dir.glob("*.json")
                )
            )
        except OSError:
            return ()

    def reload(self) -> int:
        """Rescan the directory. Returns the number of overrides loaded."""
        overrides: dict[str, dict[str, Any]] = {}
        files: dict[str, Path] = {}
        errors: list[str] = []
        if self.dir.is_dir():
            for path in sorted(self.dir.glob("*.json")):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(raw, dict):
                        raise ValueError("not a JSON object")
                except (json.JSONDecodeError, ValueError, OSError) as exc:
                    errors.append(f"{path.name}: {exc}")
                    continue
                name = raw.get("model_name")
                if not isinstance(name, str) or not name.strip():
                    name = path.stem  # hand-authored file without model_name
                name = name.strip()
                cleaned = normalize_override(raw)
                if cleaned is None:
                    errors.append(f"{path.name}: no override fields")
                    continue
                if name in overrides:
                    errors.append(f"{path.name}: duplicate override for {name!r}")
                    continue
                overrides[name] = cleaned
                files[name] = path
        self._overrides = overrides
        self._files = files
        self.load_errors = errors
        self._signature = self._scan_signature()
        self._checked_at = time.monotonic()
        for e in errors:
            logger.warning("override file ignored: %s", e)
        return len(overrides)

    def _maybe_rescan(self) -> None:
        now = time.monotonic()
        if now - self._checked_at < _RESCAN_TTL:
            return
        self._checked_at = now
        sig = self._scan_signature()
        if sig != self._signature:
            self.reload()
            logger.info("overrides dir changed, reloaded %d overrides", len(self._overrides))

    @property
    def overrides(self) -> dict[str, dict[str, Any]]:
        self._maybe_rescan()
        return self._overrides

    def get(self, model_name: str) -> dict[str, Any] | None:
        return self.overrides.get(model_name)

    # -- writing -----------------------------------------------------------

    def _path_for(self, model_name: str) -> Path:
        existing = self._files.get(model_name)
        if existing is not None:
            return existing
        path = self.dir / f"{_slug(model_name)}.json"
        # Avoid clobbering a different model whose name slugs identically.
        taken = {p for n, p in self._files.items() if n != model_name}
        if path in taken:
            import hashlib

            digest = hashlib.sha256(model_name.encode("utf-8")).hexdigest()[:8]
            path = self.dir / f"{_slug(model_name)}-{digest}.json"
        return path

    async def set(self, model_name: str, override: dict[str, Any]) -> None:
        """Write one override file atomically (admin API path)."""
        async with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self._path_for(model_name)
            payload = {"model_name": model_name, **override}
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".override-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                    fh.write("\n")
                os.replace(tmp, path)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            self.reload()

    async def delete(self, model_name: str) -> bool:
        async with self._lock:
            path = self._files.get(model_name)
            if path is None:
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            self.reload()
            return True
