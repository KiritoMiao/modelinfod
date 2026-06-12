import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def client(tmp_path, monkeypatch, httpx_mock_models):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("UPSTREAM_BASE_URL", "http://upstream.test")
    monkeypatch.setenv("SYNC_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("PRICES_REFRESH_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("OPENROUTER_REFRESH_INTERVAL_SECONDS", "0")

    import app.config
    import app.main

    app.config.get_settings.cache_clear()
    importlib.reload(app.main)
    with TestClient(app.main.app) as c:
        yield c
    app.config.get_settings.cache_clear()


@pytest.fixture()
def httpx_mock_models(monkeypatch):
    import app.upstream

    async def fake_fetch(settings):
        return ["gpt-4o", "claude-3-5-sonnet-20240620", "my-private-model"]

    monkeypatch.setattr(app.upstream, "fetch_upstream_models", fake_fetch)
    # main.py imported the symbol directly too
    import app.main

    monkeypatch.setattr(app.main, "fetch_upstream_models", fake_fetch, raising=False)


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

    import app.config
    import app.main

    app.config.get_settings.cache_clear()
    importlib.reload(app.main)
    with TestClient(app.main.app) as c:
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


def test_provider_prefix_lookup(client):
    # ids like "openai/gpt-4o" should still resolve metadata
    client.post(
        "/modelinfod/api/custom-models",
        json={"model_name": "openai/gpt-4o-mini", "litellm_params": {}, "model_info": {}},
    )
    by_name = {e["model_name"]: e for e in client.get("/v1/model/info").json()["data"]}
    assert by_name["openai/gpt-4o-mini"]["model_info"].get("litellm_provider") == "openai"
