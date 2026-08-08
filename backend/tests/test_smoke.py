import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Keys the fake upstream recognizes. FULL_KEY sees the whole catalog (tests
# append to its list as they add custom models), LIMITED_KEY only one model,
# BOOM_KEY simulates an unreachable upstream. Anything else is rejected.
FULL_KEY = "Bearer sk-full"
LIMITED_KEY = "Bearer sk-limited"
BOOM_KEY = "Bearer sk-boom"


@pytest.fixture()
def client(tmp_path, monkeypatch, httpx_mock_models):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("UPSTREAM_BASE_URL", "http://upstream.test")
    monkeypatch.setenv("SYNC_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("PRICES_REFRESH_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("OPENROUTER_REFRESH_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("MODELSDEV_REFRESH_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("USER_MODELS_CACHE_TTL_SECONDS", "0")

    import app.config
    import app.main

    app.config.get_settings.cache_clear()
    importlib.reload(app.main)
    with TestClient(app.main.app, headers={"Authorization": FULL_KEY}) as c:
        yield c
    app.config.get_settings.cache_clear()


@pytest.fixture()
def httpx_mock_models(monkeypatch):
    import httpx

    import app.routers.public
    import app.upstream
    from app.upstream import UpstreamAuthError

    async def fake_fetch(settings):
        return ["gpt-4o", "claude-3-5-sonnet-20240620", "my-private-model"]

    key_models = {
        FULL_KEY: ["gpt-4o", "claude-3-5-sonnet-20240620", "my-private-model"],
        LIMITED_KEY: ["gpt-4o"],
    }
    calls: list[str] = []

    async def fake_for_key(settings, authorization, client):
        calls.append(authorization)
        if authorization == BOOM_KEY:
            raise httpx.ConnectError("upstream down")
        if authorization not in key_models:
            raise UpstreamAuthError(401)
        return list(key_models[authorization])

    monkeypatch.setattr(app.upstream, "fetch_upstream_models", fake_fetch)
    # main.py / public.py imported the symbols directly too
    import app.main

    monkeypatch.setattr(app.main, "fetch_upstream_models", fake_fetch, raising=False)
    monkeypatch.setattr(app.routers.public, "fetch_models_for_key", fake_for_key)
    return {"key_models": key_models, "calls": calls}


def test_model_info_merges_litellm_metadata(client):
    resp = client.get("/v1/model/info")
    assert resp.status_code == 200
    data = resp.json()["data"]
    by_name = {e["model_name"]: e for e in data}
    assert set(by_name) == {"gpt-4o", "claude-3-5-sonnet-20240620", "my-private-model"}

    gpt = by_name["gpt-4o"]
    assert gpt["litellm_params"]["model"] == "gpt-4o"
    assert gpt["model_info"]["litellm_provider"] == "openai"
    assert gpt["model_info"]["max_input_tokens"] == 128000
    assert gpt["model_info"]["db_model"] is False

    unknown = by_name["my-private-model"]
    assert "litellm_provider" not in unknown["model_info"]

    # alias route
    assert client.get("/model/info").status_code == 200
    # filter param
    only = client.get("/v1/model/info", params={"litellm_model_id": "gpt-4o"}).json()["data"]
    assert [e["model_name"] for e in only] == ["gpt-4o"]


def test_override_hide_and_custom_model(client, tmp_path):
    r = client.put(
        "/modelinfod/api/models/my-private-model/override",
        json={"model_info": {"max_input_tokens": 32768, "mode": "chat"}},
    )
    assert r.status_code == 200
    info = {
        e["model_name"]: e for e in client.get("/v1/model/info").json()["data"]
    }["my-private-model"]["model_info"]
    assert info["max_input_tokens"] == 32768
    assert info["mode"] == "chat"

    # the override landed as its own shareable JSON file
    import json as _json

    files = list((tmp_path / "overrides").glob("*.json"))
    assert len(files) == 1
    payload = _json.loads(files[0].read_text())
    assert payload["model_name"] == "my-private-model"
    assert payload["model_info"] == {"max_input_tokens": 32768, "mode": "chat"}

    # hide it
    assert (
        client.put(
            "/modelinfod/api/models/my-private-model/hidden", json={"hidden": True}
        ).status_code
        == 200
    )
    names = [e["model_name"] for e in client.get("/v1/model/info").json()["data"]]
    assert "my-private-model" not in names
    # still present in admin listing, flagged hidden
    admin_entries = client.get("/modelinfod/api/models").json()["data"]
    mine = [e for e in admin_entries if e["model_name"] == "my-private-model"][0]
    assert mine["_admin"]["hidden"] is True

    # custom model not present upstream
    r = client.post(
        "/modelinfod/api/custom-models",
        json={
            "model_name": "local/llama-fast",
            "litellm_params": {"model": "ollama/llama3", "api_base": "http://gpu:11434"},
            "model_info": {"max_input_tokens": 8192, "litellm_provider": "ollama"},
        },
    )
    assert r.status_code == 200
    by_name = {e["model_name"]: e for e in client.get("/v1/model/info").json()["data"]}
    entry = by_name["local/llama-fast"]
    assert entry["litellm_params"]["model"] == "ollama/llama3"
    assert entry["model_info"]["max_input_tokens"] == 8192

    # status endpoint reflects counts
    s = client.get("/modelinfod/api/status").json()
    assert s["custom_model_count"] == 1
    assert s["hidden_count"] == 1
    assert s["litellm_map_entries"] > 1000


def test_litellm_map_matching_tiers():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.litellm_map import LiteLLMMap

    lm = LiteLLMMap()
    lm.load_bundled()

    # exact
    entry, key = lm.lookup("gpt-4o")
    assert key == "gpt-4o" and entry["litellm_provider"] == "openai"

    # provider-qualified candidate for a bare id
    entry, key = lm.lookup("kimi-k2.5")
    assert key == "moonshot/kimi-k2.5"

    # openrouter ":free" tag stripped, costs zeroed
    entry, key = lm.lookup("openai/gpt-oss-120b:free")
    assert key == "openrouter/openai/gpt-oss-120b"
    assert entry["input_cost_per_token"] == 0.0
    assert entry["output_cost_per_token"] == 0.0
    # the underlying map entry must NOT have been mutated
    raw, _ = lm.lookup("openrouter/openai/gpt-oss-120b")
    assert raw["input_cost_per_token"] > 0

    # date suffix stripped when the dated key is absent
    entry, key = lm.lookup("claude-3-5-haiku-20241022")
    assert entry is not None and "claude-3-5-haiku" in key

    # "-free" suffix variant, costs zeroed
    entry, key = lm.lookup("minimax-m2.5-free")
    assert entry is not None and entry["input_cost_per_token"] == 0.0

    # separator-insensitive tier ("z-ai" vs litellm's "zai")
    entry, key = lm.lookup("z-ai/glm-4.5-air:free")
    assert key == "zai/glm-4.5-air" and entry["input_cost_per_token"] == 0.0

    # no match -> (None, None)
    assert lm.lookup("definitely-not-a-real-model-xyz") == (None, None)


OPENROUTER_SAMPLE = {
    "id": "vendorx/super-model",
    "name": "VendorX: Super Model",
    "context_length": 100000,
    "architecture": {
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
    },
    "pricing": {"prompt": "0.000001", "completion": "0.000002", "input_cache_read": "0.0000005"},
    "top_provider": {"max_completion_tokens": 8000},
    "supported_parameters": ["tools", "tool_choice", "reasoning", "response_format"],
}


def test_openrouter_convert_and_lookup():
    from app.openrouter import OpenRouterCatalog, convert_entry

    info = convert_entry(OPENROUTER_SAMPLE)
    assert info["max_input_tokens"] == 100000
    assert info["max_output_tokens"] == 8000
    assert info["input_cost_per_token"] == 1e-6
    assert info["output_cost_per_token"] == 2e-6
    assert info["cache_read_input_token_cost"] == 5e-7
    assert info["supports_function_calling"] is True
    assert info["supports_tool_choice"] is True
    assert info["supports_reasoning"] is True
    assert info["supports_response_schema"] is True
    assert info["supports_vision"] is True
    assert info["supports_prompt_caching"] is True
    assert info["litellm_provider"] == "openrouter"

    cat = OpenRouterCatalog()
    cat._set_models([OPENROUTER_SAMPLE])
    # exact, vendor-stripped tail, ":free" variant (costs zeroed)
    assert cat.lookup("vendorx/super-model")[1] == "vendorx/super-model"
    assert cat.lookup("super-model")[1] == "vendorx/super-model"
    entry, key = cat.lookup("vendorx/super-model:free")
    assert key == "vendorx/super-model" and entry["input_cost_per_token"] == 0.0
    assert cat.lookup("nope-model") == (None, None)
    # search
    assert cat.search("super") == ["vendorx/super-model"]


MODELSDEV_SAMPLE = {
    # A preferred provider: wins bare ids.
    "openai": {
        "id": "openai",
        "name": "OpenAI",
        "models": {
            "super-model": {
                "id": "super-model",
                "name": "Super Model",
                "attachment": True,
                "reasoning": True,
                "tool_call": True,
                "structured_output": True,
                "modalities": {"input": ["text", "image", "pdf"], "output": ["text"]},
                "limit": {"context": 400000, "input": 380000, "output": 64000},
                "cost": {
                    "input": 3,
                    "output": 15,
                    "cache_read": 0.3,
                    "cache_write": 3.75,
                    "tiers": [
                        {
                            "input": 6,
                            "output": 22.5,
                            "cache_read": 0.6,
                            "tier": {"type": "context", "size": 272000},
                        }
                    ],
                },
                "experimental": {
                    "modes": {
                        # Priced -> becomes the priority tier.
                        "fast": {
                            "cost": {"input": 7.5, "output": 37.5, "cache_read": 0.75},
                            "provider": {"body": {"service_tier": "priority"}},
                        },
                        # Unpriced -> ignored.
                        "pro": {"provider": {"body": {"reasoning": {"mode": "pro"}}}},
                    }
                },
            },
            # No cache pricing of its own; the LiteLLM map does price gpt-4o's
            # cache reads, so the merge layer must top this up from there.
            "gpt-4o": {
                "id": "gpt-4o",
                "name": "GPT-4o via models.dev",
                "tool_call": True,
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 128000, "output": 16384},
                "cost": {"input": 2.5, "output": 10},
            },
            # Genuinely named "-fast": a model, not a service tier.
            "quick-model-fast": {
                "id": "quick-model-fast",
                "name": "Quick Model Fast",
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 8000, "output": 4000},
                "cost": {"input": 1, "output": 2},
            },
        },
    },
    # Not a preferred provider: loaded and pinnable, but never wins a bare id.
    "poe": {
        "id": "poe",
        "models": {
            "super-model": {
                "id": "super-model",
                "name": "Super Model, resold",
                "modalities": {"input": ["text"], "output": ["text"]},
                "limit": {"context": 400000, "output": 64000},
                "cost": {"input": 99, "output": 99},
            }
        },
    },
}


def test_modelsdev_convert_costs_tiers_and_priority():
    from app.modelsdev import ModelsDevCatalog

    cat = ModelsDevCatalog()
    cat._set_providers(MODELSDEV_SAMPLE)

    info = cat.get("openai/super-model")
    # models.dev quotes per 1M tokens; litellm wants per token.
    assert info["input_cost_per_token"] == 3e-6
    assert info["output_cost_per_token"] == 15e-6
    assert info["cache_read_input_token_cost"] == 0.3e-6
    assert info["cache_creation_input_token_cost"] == 3.75e-6
    assert info["supports_prompt_caching"] is True
    # limit.input is the real input cap when it is tighter than the context
    assert info["max_input_tokens"] == 380000
    assert info["max_tokens"] == 400000
    assert info["max_output_tokens"] == 64000

    # Context tier: flat litellm-style fields...
    assert info["input_cost_per_token_above_272k_tokens"] == 6e-6
    assert info["output_cost_per_token_above_272k_tokens"] == 22.5e-6
    assert info["cache_read_input_token_cost_above_272k_tokens"] == 0.6e-6
    # ...and the nested array spelling out every range.
    assert info["tiered_pricing"] == [
        {
            "range": [0, 272000],
            "input_cost_per_token": 3e-6,
            "output_cost_per_token": 15e-6,
            "cache_read_input_token_cost": 0.3e-6,
            "cache_creation_input_token_cost": 3.75e-6,
        },
        {
            "range": [272000, None],
            "input_cost_per_token": 6e-6,
            "output_cost_per_token": 22.5e-6,
            "cache_read_input_token_cost": 0.6e-6,
        },
    ]

    # Priority tier: flat fields plus the nested object naming the tier.
    assert info["input_cost_per_token_priority"] == 7.5e-6
    assert info["output_cost_per_token_priority"] == 37.5e-6
    assert info["cache_read_input_token_cost_priority"] == 0.75e-6
    assert info["supports_service_tier"] is True
    assert info["priority_pricing"] == {
        "service_tier": "priority",
        "input_cost_per_token": 7.5e-6,
        "output_cost_per_token": 37.5e-6,
        "cache_read_input_token_cost": 0.75e-6,
    }

    assert info["supports_vision"] is True
    assert info["supports_pdf_input"] is True
    assert info["supports_function_calling"] is True
    assert info["supports_response_schema"] is True
    assert info["litellm_provider"] == "openai"


def test_modelsdev_provider_preference_and_fast_variants():
    from app.modelsdev import ModelsDevCatalog

    cat = ModelsDevCatalog()
    cat._set_providers(MODELSDEV_SAMPLE)

    # A bare id resolves to the preferred provider, never the reseller.
    entry, key = cat.lookup("super-model")
    assert key == "openai/super-model"
    assert entry["input_cost_per_token"] == 3e-6
    # The reseller is still loaded, searchable and pinnable by full key.
    assert cat.get("poe/super-model")["input_cost_per_token"] == 99e-6
    assert cat.lookup("poe/super-model")[1] == "poe/super-model"
    assert "poe/super-model" in cat.search("super-model")

    # "-fast" promotes the priority prices to the model's own.
    entry, key = cat.lookup("super-model-fast")
    assert key == "openai/super-model"
    assert entry["input_cost_per_token"] == 7.5e-6
    assert entry["output_cost_per_token"] == 37.5e-6
    assert entry["cache_read_input_token_cost"] == 0.75e-6
    assert entry["service_tier"] == "priority"
    # Nothing left restating the priority price, and the standard-tier
    # context pricing is dropped rather than under-pricing a priority model.
    assert not [k for k in entry if k.endswith("_priority")]
    assert "priority_pricing" not in entry
    assert "tiered_pricing" not in entry
    assert not [k for k in entry if "_above_" in k]
    # ":priority" spells the same thing.
    assert cat.lookup("super-model:priority")[0]["input_cost_per_token"] == 7.5e-6

    # A model genuinely named "-fast" matches itself, untouched.
    entry, key = cat.lookup("quick-model-fast")
    assert key == "openai/quick-model-fast"
    assert entry["input_cost_per_token"] == 1e-6
    assert "service_tier" not in entry

    # A base with no priority tier is never promoted...
    assert cat.lookup("gpt-4o-fast") == (None, None)
    # ...and neither is an id with no base at all.
    assert cat.lookup("nothing-here-fast") == (None, None)

    # The source entry is never mutated by promotion or by ":free" zeroing.
    assert cat.lookup("super-model:free")[0]["input_cost_per_token"] == 0.0
    assert cat.get("openai/super-model")["input_cost_per_token"] == 3e-6


def test_free_and_multiplier_reach_nested_pricing():
    """`tiered_pricing` / `priority_pricing` are nested, so the cost helpers
    have to recurse or a multiplier would silently miss them."""
    from app.matching import scale_costs, zero_costs
    from app.modelsdev import ModelsDevCatalog

    cat = ModelsDevCatalog()
    cat._set_providers(MODELSDEV_SAMPLE)
    entry = cat.get("openai/super-model")

    scaled = scale_costs(entry, 2.0)
    assert scaled["input_cost_per_token"] == 6e-6
    assert scaled["tiered_pricing"][1]["input_cost_per_token"] == 12e-6
    assert scaled["priority_pricing"]["input_cost_per_token"] == 15e-6
    # Non-cost members of the nested objects survive untouched.
    assert scaled["tiered_pricing"][1]["range"] == [272000, None]
    assert scaled["priority_pricing"]["service_tier"] == "priority"

    zeroed = zero_costs(entry)
    assert zeroed["input_cost_per_token"] == 0.0
    assert zeroed["tiered_pricing"][0]["input_cost_per_token"] == 0.0
    assert zeroed["priority_pricing"]["output_cost_per_token"] == 0.0
    assert zeroed["priority_pricing"]["service_tier"] == "priority"
    assert entry["priority_pricing"]["input_cost_per_token"] == 7.5e-6  # unmutated


def test_modelsdev_is_primary_source_and_tops_up_cache_costs(client):
    import app.main

    app.main.app.state.modelsdev._set_providers(MODELSDEV_SAMPLE)

    def fetch(name):
        return {
            e["model_name"]: e for e in client.get("/v1/model/info").json()["data"]
        }[name]["model_info"]

    # models.dev outranks the LiteLLM map for a model both describe.
    gpt = fetch("gpt-4o")
    assert gpt["metadata_source"] == "modelsdev"
    assert gpt["key"] == "openai/gpt-4o"
    assert gpt["input_cost_per_token"] == 2.5e-6

    # models.dev has no cache price for it, but litellm does: the gap is
    # filled from there rather than left empty, and the donor is recorded.
    assert gpt["cache_read_input_token_cost"] == 1.25e-6
    assert gpt["cache_cost_source"] == "litellm"
    assert gpt["supports_prompt_caching"] is True

    # A source that has its own cache pricing is never topped up.
    client.post(
        "/modelinfod/api/custom-models",
        json={"model_name": "super-model", "litellm_params": {}, "model_info": {}},
    )
    sup = fetch("super-model")
    assert sup["metadata_source"] == "modelsdev"
    assert sup["cache_read_input_token_cost"] == 0.3e-6
    assert "cache_cost_source" not in sup

    # A "-fast" upstream id gets the priority prices end to end.
    client.post(
        "/modelinfod/api/custom-models",
        json={"model_name": "super-model-fast", "litellm_params": {}, "model_info": {}},
    )
    fast = fetch("super-model-fast")
    assert fast["input_cost_per_token"] == 7.5e-6
    assert fast["service_tier"] == "priority"
    assert fast["id"] != sup["id"]  # identity stays the model's own

    # models.dev is pinnable, including at a reseller's key.
    assert (
        client.put(
            "/modelinfod/api/models/my-private-model/match",
            json={"source": "modelsdev", "key": "poe/super-model"},
        ).status_code
        == 200
    )
    mine = fetch("my-private-model")
    assert mine["metadata_source"] == "modelsdev"
    assert mine["input_cost_per_token"] == 99e-6

    # Status and the search picker both cover the new source.
    status = client.get("/modelinfod/api/status").json()
    assert status["modelsdev_entries"] == 4
    res = client.get(
        "/modelinfod/api/metadata-keys", params={"q": "super-model"}
    ).json()["data"]
    assert {r["key"] for r in res if r["source"] == "modelsdev"} == {
        "openai/super-model",
        "poe/super-model",
    }


def test_cost_multiplier_scales_a_models_dev_baseline(client):
    """The override layer and the nested pricing objects have to compose:
    "X at 2x" must scale the tier and priority prices too."""
    import app.main

    app.main.app.state.modelsdev._set_providers(MODELSDEV_SAMPLE)
    client.post(
        "/modelinfod/api/custom-models",
        json={"model_name": "resold-super", "litellm_params": {}, "model_info": {}},
    )
    client.put(
        "/modelinfod/api/models/resold-super/override",
        json={"base_model": "super-model", "cost_multiplier": 2.0},
    )
    info = {
        e["model_name"]: e for e in client.get("/v1/model/info").json()["data"]
    }["resold-super"]["model_info"]

    assert info["input_cost_per_token"] == 6e-6
    assert info["cache_read_input_token_cost"] == 0.6e-6
    assert info["cache_creation_input_token_cost"] == 7.5e-6
    assert info["input_cost_per_token_above_272k_tokens"] == 12e-6
    assert info["input_cost_per_token_priority"] == 15e-6
    assert info["tiered_pricing"][1]["input_cost_per_token"] == 12e-6
    assert info["priority_pricing"]["input_cost_per_token"] == 15e-6
    assert info["priority_pricing"]["service_tier"] == "priority"


def test_openrouter_fallback_and_manual_match(client):
    import app.main

    # Seed the app's (empty) OpenRouter catalog in-process.
    app.main.app.state.openrouter._set_models([OPENROUTER_SAMPLE])

    # my-private-model has no litellm or openrouter auto match.
    by_name = {e["model_name"]: e for e in client.get("/v1/model/info").json()["data"]}
    assert "metadata_source" not in by_name["my-private-model"]["model_info"]
    # gpt-4o resolves from litellm (primary), not openrouter
    assert by_name["gpt-4o"]["model_info"]["metadata_source"] == "litellm"

    # Manual match my-private-model to the openrouter entry.
    r = client.put(
        "/modelinfod/api/models/my-private-model/match",
        json={"source": "openrouter", "key": "vendorx/super-model"},
    )
    assert r.status_code == 200
    info = {
        e["model_name"]: e for e in client.get("/v1/model/info").json()["data"]
    }["my-private-model"]["model_info"]
    assert info["metadata_source"] == "openrouter"
    assert info["key"] == "vendorx/super-model"
    assert info["max_input_tokens"] == 100000

    # Manual match wins over auto: point gpt-4o at a different litellm key.
    r = client.put(
        "/modelinfod/api/models/gpt-4o/match",
        json={"source": "litellm", "key": "gpt-4o-mini"},
    )
    assert r.status_code == 200
    info = {
        e["model_name"]: e for e in client.get("/v1/model/info").json()["data"]
    }["gpt-4o"]["model_info"]
    assert info["key"] == "gpt-4o-mini"

    # Bad key is rejected.
    assert (
        client.put(
            "/modelinfod/api/models/gpt-4o/match",
            json={"source": "litellm", "key": "not-a-real-key"},
        ).status_code
        == 404
    )

    # Admin annotation + status count.
    admin_entries = client.get("/modelinfod/api/models").json()["data"]
    mine = [e for e in admin_entries if e["model_name"] == "my-private-model"][0]
    assert mine["_admin"]["manual_match"] == "openrouter:vendorx/super-model"
    assert client.get("/modelinfod/api/status").json()["manual_match_count"] == 2

    # Search endpoint covers both sources.
    res = client.get("/modelinfod/api/metadata-keys", params={"q": "super-model"}).json()
    assert {(r["source"], r["key"]) for r in res["data"]} == {
        ("openrouter", "vendorx/super-model")
    }
    res = client.get("/modelinfod/api/metadata-keys", params={"q": "gpt-4o", "limit": 5}).json()
    assert any(r["source"] == "litellm" and r["key"] == "gpt-4o" for r in res["data"])

    # Clearing the match restores auto resolution.
    assert client.delete("/modelinfod/api/models/gpt-4o/match").status_code == 200
    info = {
        e["model_name"]: e for e in client.get("/v1/model/info").json()["data"]
    }["gpt-4o"]["model_info"]
    assert info["key"] == "gpt-4o"
    assert client.delete("/modelinfod/api/models/gpt-4o/match").status_code == 404


def test_file_overrides_base_model_and_multiplier(client, tmp_path):
    import json as _json

    # gpt-5.5-fast is not upstream; create it as a custom model with an
    # override deriving from gpt-4o at 2.5x cost.
    client.post(
        "/modelinfod/api/custom-models",
        json={"model_name": "gpt-5.5-fast", "litellm_params": {}, "model_info": {}},
    )
    r = client.put(
        "/modelinfod/api/models/gpt-5.5-fast/override",
        json={"base_model": "gpt-4o", "cost_multiplier": 2.5},
    )
    assert r.status_code == 200

    def fetch(name):
        return {
            e["model_name"]: e for e in client.get("/v1/model/info").json()["data"]
        }[name]["model_info"]

    base = fetch("gpt-4o")
    derived = fetch("gpt-5.5-fast")
    assert derived["base_model"] == "gpt-4o"
    assert derived["cost_multiplier"] == 2.5
    assert derived["input_cost_per_token"] == base["input_cost_per_token"] * 2.5
    assert derived["output_cost_per_token"] == base["output_cost_per_token"] * 2.5
    assert derived["max_input_tokens"] == base["max_input_tokens"]
    # identity fields are the model's own, not the base's
    assert derived["id"] != base["id"]

    # explicit model_info fields win over the derived values
    client.put(
        "/modelinfod/api/models/gpt-5.5-fast/override",
        json={
            "base_model": "gpt-4o",
            "cost_multiplier": 2.5,
            "model_info": {"max_output_tokens": 4096},
        },
    )
    assert fetch("gpt-5.5-fast")["max_output_tokens"] == 4096

    # hand-dropping a file into the overrides dir is picked up (after the
    # rescan TTL; force it via the reload endpoint like an admin would)
    odir = tmp_path / "overrides"
    (odir / "dropped.json").write_text(
        _json.dumps(
            {
                "model_name": "claude-3-5-sonnet-20240620",
                "cost_multiplier": 0.5,
            }
        )
    )
    r = client.post("/modelinfod/api/reload-overrides")
    assert r.status_code == 200 and r.json()["overrides"] == 2
    sonnet = fetch("claude-3-5-sonnet-20240620")
    assert sonnet["cost_multiplier"] == 0.5

    # a base_model cycle degrades gracefully instead of recursing forever
    (odir / "cycle-a.json").write_text(
        _json.dumps({"model_name": "gpt-4o", "base_model": "gpt-5.5-fast"})
    )
    client.post("/modelinfod/api/reload-overrides")
    cyc = fetch("gpt-4o")
    assert cyc["litellm_provider"] == "openai"  # still resolves
    (odir / "cycle-a.json").unlink()
    client.post("/modelinfod/api/reload-overrides")

    # status reports the dir and count
    s = client.get("/modelinfod/api/status").json()
    assert s["override_count"] == 2
    assert s["overrides_dir"].endswith("overrides")

    # delete via API removes the file
    client.delete("/modelinfod/api/models/gpt-5.5-fast/override")
    names = {p.stem for p in odir.glob("*.json")}
    assert "gpt-5.5-fast" not in names


def test_prefixed_ids_inherit_bare_name_config(client):
    import app.main

    app.main.app.state.openrouter._set_models([OPENROUTER_SAMPLE])

    # Three upstream spellings of the same model, via custom models.
    for n in ("the-model", "prov/the-model", "a/b/the-model"):
        client.post(
            "/modelinfod/api/custom-models",
            json={"model_name": n, "litellm_params": {}, "model_info": {}},
        )

    # One override on the bare name covers every prefixed variant.
    client.put(
        "/modelinfod/api/models/the-model/override",
        json={"base_model": "gpt-4o", "cost_multiplier": 2.0},
    )

    def fetch(name):
        return {
            e["model_name"]: e for e in client.get("/v1/model/info").json()["data"]
        }[name]["model_info"]

    base_in = fetch("gpt-4o")["input_cost_per_token"]
    for n in ("the-model", "prov/the-model", "a/b/the-model"):
        info = fetch(n)
        assert info["base_model"] == "gpt-4o", n
        assert info["input_cost_per_token"] == base_in * 2.0, n

    # A direct override on the prefixed id wins over the inherited one.
    client.put(
        "/modelinfod/api/models/prov/the-model/override",
        json={"base_model": "gpt-4o", "cost_multiplier": 5.0},
    )
    assert fetch("prov/the-model")["input_cost_per_token"] == base_in * 5.0
    assert fetch("a/b/the-model")["input_cost_per_token"] == base_in * 2.0  # unchanged

    # Admin annotation distinguishes own vs inherited override.
    admin = {
        e["model_name"]: e["_admin"]
        for e in client.get("/modelinfod/api/models").json()["data"]
    }
    assert admin["prov/the-model"]["override"] is not None
    assert admin["prov/the-model"]["override_inherited_from"] is None
    assert admin["a/b/the-model"]["override"] is None
    assert admin["a/b/the-model"]["override_inherited_from"] == "the-model"

    # Manual matches inherit the same way: bare name's match applies to
    # prefixed ids that have none of their own.
    client.put(
        "/modelinfod/api/models/the-model/match",
        json={"source": "openrouter", "key": "vendorx/super-model"},
    )
    client.delete("/modelinfod/api/models/the-model/override")
    client.delete("/modelinfod/api/models/prov/the-model/override")
    assert fetch("a/b/the-model")["key"] == "vendorx/super-model"
    assert fetch("a/b/the-model")["metadata_source"] == "openrouter"


def test_legacy_override_migration(tmp_path, monkeypatch, httpx_mock_models):
    import json as _json

    state = {
        "overrides": {"gpt-4o": {"max_input_tokens": 9999}},
        "custom_models": {},
        "hidden": [],
        "upstream_models": [],
    }
    (tmp_path / "state.json").write_text(_json.dumps(state))

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("UPSTREAM_BASE_URL", "http://upstream.test")
    monkeypatch.setenv("SYNC_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("PRICES_REFRESH_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("OPENROUTER_REFRESH_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("MODELSDEV_REFRESH_INTERVAL_SECONDS", "0")

    import app.config
    import app.main

    app.config.get_settings.cache_clear()
    importlib.reload(app.main)
    with TestClient(app.main.app, headers={"Authorization": FULL_KEY}) as c:
        info = {
            e["model_name"]: e for e in c.get("/v1/model/info").json()["data"]
        }["gpt-4o"]["model_info"]
        assert info["max_input_tokens"] == 9999
    app.config.get_settings.cache_clear()

    # moved out of state.json into a file
    on_disk = _json.loads((tmp_path / "state.json").read_text())
    assert "overrides" not in on_disk
    files = list((tmp_path / "overrides").glob("*.json"))
    assert len(files) == 1
    assert _json.loads(files[0].read_text())["model_name"] == "gpt-4o"


def test_model_info_requires_valid_key(client, httpx_mock_models):
    # No Authorization header at all -> 401.
    resp = client.get("/v1/model/info", headers={"Authorization": ""})
    assert resp.status_code == 401
    # A key the upstream rejects -> 401.
    resp = client.get("/v1/model/info", headers={"Authorization": "Bearer sk-wrong"})
    assert resp.status_code == 401
    # Upstream unreachable -> 502, not an empty (or full) list.
    resp = client.get("/v1/model/info", headers={"Authorization": BOOM_KEY})
    assert resp.status_code == 502
    # healthz stays open.
    assert client.get("/healthz", headers={"Authorization": ""}).status_code == 200


def test_model_info_filters_to_key_permissions(client, httpx_mock_models):
    full = client.get("/v1/model/info").json()["data"]
    assert {e["model_name"] for e in full} == {
        "gpt-4o",
        "claude-3-5-sonnet-20240620",
        "my-private-model",
    }

    limited = client.get(
        "/v1/model/info", headers={"Authorization": LIMITED_KEY}
    ).json()["data"]
    assert [e["model_name"] for e in limited] == ["gpt-4o"]
    # metadata still resolves for the filtered view
    assert limited[0]["model_info"]["litellm_provider"] == "openai"

    # the filter param composes with the per-key list
    only = client.get(
        "/v1/model/info",
        params={"litellm_model_id": "my-private-model"},
        headers={"Authorization": LIMITED_KEY},
    ).json()["data"]
    assert only == []

    # hidden models stay hidden even when the key is allowed them
    client.put("/modelinfod/api/models/gpt-4o/hidden", json={"hidden": True})
    limited = client.get(
        "/v1/model/info", headers={"Authorization": LIMITED_KEY}
    ).json()["data"]
    assert limited == []


def test_per_key_models_cache(tmp_path, monkeypatch, httpx_mock_models):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("UPSTREAM_BASE_URL", "http://upstream.test")
    monkeypatch.setenv("SYNC_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("PRICES_REFRESH_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("OPENROUTER_REFRESH_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("MODELSDEV_REFRESH_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("USER_MODELS_CACHE_TTL_SECONDS", "60")

    import app.config
    import app.main

    app.config.get_settings.cache_clear()
    importlib.reload(app.main)
    calls = httpx_mock_models["calls"]
    with TestClient(app.main.app, headers={"Authorization": FULL_KEY}) as c:
        assert c.get("/v1/model/info").status_code == 200
        assert c.get("/v1/model/info").status_code == 200
        assert calls.count(FULL_KEY) == 1  # second hit came from the cache

        # rejected keys are never cached: every attempt goes upstream
        for _ in range(2):
            assert (
                c.get(
                    "/v1/model/info", headers={"Authorization": "Bearer sk-wrong"}
                ).status_code
                == 401
            )
        assert calls.count("Bearer sk-wrong") == 2
    app.config.get_settings.cache_clear()


def test_per_key_cache_unit():
    from app.upstream import PerKeyModelsCache

    cache = PerKeyModelsCache(ttl_seconds=60, max_entries=2)
    cache.set("Bearer a", ["m1"])
    assert cache.get("Bearer a") == ["m1"]
    assert cache.get("Bearer b") is None
    # raw keys never stored, only hashes
    assert "Bearer a" not in repr(cache._entries)

    # bounded: inserting past max_entries evicts rather than growing
    cache.set("Bearer b", ["m2"])
    cache.set("Bearer c", ["m3"])
    assert len(cache._entries) <= 2
    assert cache.get("Bearer c") == ["m3"]

    # ttl 0 disables caching entirely
    off = PerKeyModelsCache(ttl_seconds=0)
    off.set("Bearer a", ["m1"])
    assert off.get("Bearer a") is None


def test_provider_prefix_lookup(client):
    # ids like "openai/gpt-4o" should still resolve metadata
    client.post(
        "/modelinfod/api/custom-models",
        json={"model_name": "openai/gpt-4o-mini", "litellm_params": {}, "model_info": {}},
    )
    by_name = {e["model_name"]: e for e in client.get("/v1/model/info").json()["data"]}
    assert by_name["openai/gpt-4o-mini"]["model_info"].get("litellm_provider") == "openai"
