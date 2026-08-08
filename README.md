# modelinfod

A small middleware that sits next to your LLM gateway and serves a
LiteLLM-proxy-style **`/v1/model/info`** endpoint, enriched with community
metadata (context window, pricing, capability flags) from
[models.dev](https://models.dev), LiteLLM's
`model_prices_and_context_window.json` and, as a fallback, the OpenRouter
model catalog. Pricing covers **cache reads/writes**, **above-N-token context
tiers** and the **priority ("fast") service tier**. A built-in admin panel on
**`/modelinfod`** lets you override metadata, pin a model to a specific
catalog entry, hide models, and add custom ones.

```
client ──> nginx / cloudflared ──┬──> your LLM gateway   (/v1/chat/completions, /v1/models, …)
                                 └──> modelinfod         (/v1/model/info, /modelinfod)
```

The fronting proxy handles TLS and routing. `/v1/model/info` **requires the
caller's API key**: modelinfod replays the `Authorization` header against the
upstream `/v1/models`, so the upstream decides both whether the key is valid
and which models it may access — the response is filtered to exactly that
per-key list (metadata still comes from modelinfod's enriched catalog).
Successful lookups are cached for `USER_MODELS_CACHE_TTL_SECONDS` (default
60s, keyed by a hash of the header); rejected keys are never cached.

## Endpoints

| Path | What |
| --- | --- |
| `GET /v1/model/info` (alias `GET /model/info`) | LiteLLM-style model list for the presented API key: `{"data": [{"model_name", "litellm_params", "model_info"}]}`. Requires `Authorization: Bearer <key>` (validated against the upstream). Supports `?litellm_model_id=<name>`. |
| `GET /healthz` | Liveness + model count. |
| `GET /modelinfod` | Admin panel (Catalyst/Tailwind SPA). |
| `/modelinfod/api/*` | Admin REST API (optionally guarded by `ADMIN_TOKEN`). |

## How model data is assembled

For each model id from the upstream `/v1/models` (re-synced every
`SYNC_INTERVAL_SECONDS`, cached on disk so restarts keep the catalog),
**one** metadata source is resolved — first hit wins:

1. **Manual match** — a `modelsdev:<provider>/<id>` / `litellm:<key>` /
   `openrouter:<id>` pin you set in the panel ("Match metadata…"). Always
   wins when set.
2. **models.dev** (primary, auto) — `models.dev/api.json`, refreshed daily,
   converted to litellm-style fields (its per-1M prices become per-token).
   The only source that reliably carries cache, context-tier and priority
   pricing. See below for how a provider is chosen.
3. **LiteLLM map** (secondary, auto) — bundled at build time, refreshed daily
   from GitHub. Name matching strips `:free`/`:tag` and `-YYYYMMDD` suffixes,
   tries provider-qualified spellings (`kimi-k2.5` → `moonshot/kimi-k2.5`),
   and is case/separator-insensitive. Free variants get costs zeroed.
4. **OpenRouter catalog** (last resort, auto) — `openrouter.ai/api/v1/models`,
   refreshed daily, converted to the same litellm-style fields. Same matching
   tiers, plus unique-tail matching for vendor-renamed ids.

The resolved entry's `model_info` then gets **layered on top, later wins**:

5. **Custom model fields** — for models you added that the upstream doesn't list.
6. **Override** — see below. Only the fields you set are stored; everything
   else keeps following the resolved source (so a daily price refresh still
   flows through).

`model_info.key` and `model_info.metadata_source` on each entry tell you
which catalog entry was used.

**Cache pricing is the one field group that crosses sources.** Coverage
differs a lot between the three, so a model whose winning source has no
cache read/write price is topped up from the best source that does, and
`model_info.cache_cost_source` records the donor. Both fields always come
from one donor, and manual matches are never topped up — an explicit pin is
served exactly as pinned.

### Which provider's prices models.dev uses

models.dev is keyed `provider → models`, and about a third of its model ids
are listed by several providers, each with its own price. A bare id resolves
to the most authoritative provider that has it: first the vendors that only
serve their own models (`openai`, `anthropic`, `moonshotai`, `xiaomi`, …),
then first-party clouds that also resell (`alibaba`, `amazon-bedrock`,
`azure`, `openrouter`, …). Pure re-hosts and aggregators never win a bare id
— their pricing is specific to that host — but they are still loaded, so you
can search for one and pin it with a manual match on its fully-qualified
`<provider>/<id>` key.

### Cache, context-tier and priority pricing

Where models.dev has them, entries carry LiteLLM's own flat field names plus
a nested object spelling out each range — so consumers that only read the
flat fields still work:

```jsonc
{
  "input_cost_per_token": 5e-06,
  "cache_read_input_token_cost": 5e-07,
  "cache_creation_input_token_cost": null,

  "input_cost_per_token_above_272k_tokens": 1e-05,   // context tier
  "input_cost_per_token_priority": 1.25e-05,         // priority tier
  "supports_service_tier": true,

  "tiered_pricing": [
    {"range": [0, 272000],      "input_cost_per_token": 5e-06},
    {"range": [272000, null],   "input_cost_per_token": 1e-05}
  ],
  "priority_pricing": {
    "service_tier": "priority",
    "input_cost_per_token": 1.25e-05
  }
}
```

A `cost_multiplier` override scales the nested objects too, so "X at 2×"
stays consistent across every tier.

**Fast/priority model ids resolve automatically.** An upstream id ending in
`-fast`, `-priority`, `:fast` or `:priority` is served as its base model at
the priority tier: the priority prices become the model's own costs and
`model_info.service_tier` records the tier. It only fires when the id does
not name a real model and the base actually publishes priority pricing, so
genuinely-named models (`grok-code-fast-1`, `kimi-k2.6-fast`, Nebius's
`…-fast` hardware variants) are matched normally rather than mispriced.
This replaces hand-maintained `cost_multiplier` files for such variants.

## Overrides: one shareable JSON file per model

Overrides live in `OVERRIDES_DIR` (default `<DATA_DIR>/overrides`), **one
file per model**, so they can be copied / git-synced / volume-mounted
between deployments. Files dropped in or edited externally are picked up
automatically (within ~2s); "Reload files" on the Status page forces it.

```jsonc
// overrides/my-gateway-turbo.json
{
  "model_name": "my-gateway-turbo", // authoritative (filename is just a slug)
  "base_model": "gpt-5.5",          // optional: inherit that model's resolved info
  "cost_multiplier": 1.4,           // optional: scale all *cost* fields
  "model_info": {                   // optional: explicit field patches (win last)
    "max_output_tokens": 65536
  }
}
```

Evaluation order per model: `base_model`'s fully-resolved info (recursive,
cycle-safe) replaces the auto-matched baseline → `cost_multiplier` scales
every numeric `*cost*` field, including inside `tiered_pricing` and
`priority_pricing` → `model_info` fields are applied verbatim. The example
above yields "my-gateway-turbo = gpt-5.5 at 1.4× price", and keeps tracking
gpt-5.5's metadata as catalogs refresh. The same fields are editable in the
panel's override dialog; saving writes the file for you.

You no longer need this pattern for `-fast` / `-priority` ids — those pick
up their real priority prices automatically (see above). An override on such
an id still wins, so a stale hand-written multiplier will keep shadowing the
published price; delete the file to fall back to the catalog.

**Provider prefixes share config.** In ids like `vendor/name` or
`a/b/name`, only the text after the last `/` is the model name proper.
A prefixed id with no override (or manual match) of its own automatically
uses the bare name's — so the `gpt-5.5-fast.json` above also covers
`gpt-pro/gpt-5.5-fast`, `whatever/gpt-5.5-fast`, etc. Such rows show a
`via <name>` badge in the panel; saving an override directly on the
prefixed id forks it away from the shared one.

## Quick start

```sh
cp .env.example .env   # set UPSTREAM_BASE_URL + UPSTREAM_API_KEY
docker compose up -d --build

# use one of your gateway's API keys; the list is filtered to what it can access
curl -H "Authorization: Bearer sk-..." localhost:8080/v1/model/info | jq '.data[0]'
open http://localhost:8080/modelinfod
```

State (overrides, custom models, cached upstream list) lives in the
`modelinfod-data` volume as a single JSON file.

## Reverse-proxy examples

Validated example configs live in [`examples/`](examples/):

- [`examples/nginx/`](examples/nginx/) — full server block routing
  `/v1/model/info`, `/model/info` and `/modelinfod` to modelinfod and
  everything else to your gateway (SSE-safe settings included).
- [`examples/cloudflared/`](examples/cloudflared/) — tunnel ingress rules
  with path regexes, plus [`ACCESS.md`](examples/cloudflared/ACCESS.md)
  showing how to gate `/modelinfod` with a Cloudflare Access policy while
  keeping the public endpoint open.

The short version:

```nginx
location = /v1/model/info { proxy_pass http://127.0.0.1:8080; }
location = /model/info    { proxy_pass http://127.0.0.1:8080; }
location /modelinfod      { proxy_pass http://127.0.0.1:8080; }
# everything else -> your actual LLM gateway
```

## Development

```sh
# backend
cd backend && python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
DATA_DIR=../data UPSTREAM_BASE_URL=... .venv/bin/python -m app.main
.venv/bin/python -m pytest tests/

# frontend (proxies /modelinfod/api to :8080)
cd frontend && npm install && npm run dev
```

The admin SPA is built from `frontend/` (Vite + React + Tailwind CSS v4 +
Catalyst UI Kit) and served by the backend from `app/static` in the Docker
image.

## License

[MIT](LICENSE). Note: `frontend/src/catalyst/` vendors the
[Catalyst UI Kit](https://catalyst.tailwindui.com/), which is licensed
separately under the [Tailwind Plus license](https://tailwindcss.com/plus/license)
and requires a Tailwind Plus account to use in your own projects.
