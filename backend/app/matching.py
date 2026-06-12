"""Shared model-id matching helpers used by both metadata sources
(the LiteLLM map and the OpenRouter catalog)."""

import re
from typing import Any

TAG_RE = re.compile(r":([A-Za-z][\w.-]*)$")  # openrouter ":free" / ":nitro" tags
DATE_RE = re.compile(r"-20\d{6}$")           # "-20241022" date suffixes


def norm_dashes(s: str) -> str:
    return s.lower().replace("_", "-")


def norm_tight(s: str) -> str:
    return re.sub(r"[-_.]", "", s.lower())


def zero_costs(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with all cost fields zeroed (for free model variants)."""
    out: dict[str, Any] = {}
    for k, v in entry.items():
        if "cost" in k:
            if isinstance(v, (int, float)):
                out[k] = 0.0
            # non-numeric cost structures are dropped for free variants
        else:
            out[k] = v
    return out


def scale_costs(entry: dict[str, Any], factor: float) -> dict[str, Any]:
    """Return a copy with all numeric cost fields multiplied by `factor`."""
    out: dict[str, Any] = {}
    for k, v in entry.items():
        if "cost" in k and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v * factor
        else:
            out[k] = v
    return out


def iter_forms(model_id: str) -> list[tuple[str, bool]]:
    """Base forms to try for an upstream id, most faithful first.

    Returns (base, is_free_variant) pairs: the id as-is; with a ":tag"
    suffix removed (free when the tag is "free"); with a "-YYYYMMDD" date
    suffix removed; with a "-free" suffix removed (free).
    """
    forms: list[tuple[str, bool]] = [(model_id, False)]
    tag_match = TAG_RE.search(model_id)
    if tag_match:
        forms.append((model_id[: tag_match.start()], tag_match.group(1) == "free"))
    date_stripped = DATE_RE.sub("", model_id)
    if date_stripped != model_id:
        forms.append((date_stripped, False))
    if model_id.endswith("-free"):
        forms.append((model_id[: -len("-free")], True))
    return forms
