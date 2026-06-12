# modelinfod

A small middleware that sits next to your LLM gateway and serves a
LiteLLM-proxy-style **`/v1/model/info`** endpoint, enriched with community
metadata (context window, pricing, capability flags) from LiteLLM's
`model_prices_and_context_window.json` and, as a fallback, the OpenRouter
model catalog. A built-in admin panel on **`/modelinfod`** lets you override
metadata, pin a model to a specific catalog entry, hide models, and add
custom ones.

```
client ──> nginx / cloudflared ──┬──> your LLM gateway   (/v1/chat/completions, /v1/models, …)
                                 └──> modelinfod         (/v1/model/info, /modelinfod)
```

The fronting proxy handles TLS, routing and API-key enforcement.
`/v1/model/info` is intentionally **unauthenticated** here — model metadata is
not sensitive and keys are validated a layer above.

## Endpoints

| Path | What |
| --- | --- |
| `GET /v1/model/info` (alias `GET /model/info`) | LiteLLM-style model list: `{"data": [{"model_name", "litellm_params", "model_info"}]}`. Supports `?litellm_model_id=<name>`. |
| `GET /healthz` | Liveness + model count. |
| `GET /modelinfod` | Admin panel (Catalyst/Tailwind SPA). |
| `/modelinfod/api/*` | Admin REST API (optionally guarded by `ADMIN_TOKEN`). |

## How model data is assembled

For each model id from the upstream `/v1/models` (re-synced every
`SYNC_INTERVAL_SECONDS`, cached on disk so restarts keep the catalog),
**one** metadata source is resolved — first hit wins:

1. **Manual match** — a `litellm:<key>` / `openrouter:<id>` pin you set in
   the panel ("Match metadata…"). Always wins when set.
2. **LiteLLM map** (primary, auto) — bundled at build time, refreshed daily
   from GitHub. Name matching strips `:free`/`:tag` and `-YYYYMMDD` suffixes,
   tries provider-qualified spellings (`kimi-k2.5` → `moonshot/kimi-k2.5`),
   and is case/separator-insensitive. Free variants get costs zeroed.
3. **OpenRouter catalog** (secondary, auto) — `openrouter.ai/api/v1/models`,
   refreshed daily, converted to the same litellm-style fields. Same matching
   tiers, plus unique-tail matching for vendor-renamed ids.

The resolved entry's `model_info` then gets **layered on top, later wins**:

4. **Custom model fields** — for models you added that the upstream doesn't list.
5. **Override** — see below. Only the fields you set are stored; everything
   else keeps following the resolved source (so a daily price refresh still
   flows through).

`model_info.key` and `model_info.metadata_source` on each entry tell you
which catalog entry was used.

## Overrides: one shareable JSON file per model

Overrides live in `OVERRIDES_DIR` (default `<DATA_DIR>/overrides`), **one
file per model**, so they can be copied / git-synced / volume-mounted
between deployments. Files dropped in or edited externally are picked up
automatically (within ~2s); "Reload files" on the Status page forces it.

```jsonc
// overrides/gpt-5.5-fast.json
{
  "model_name": "gpt-5.5-fast",   // authoritative (filename is just a slug)
  "base_model": "gpt-5.5",        // optional: inherit that model's resolved info
  "cost_multiplier": 2.5,         // optional: scale all *cost* fields
  "model_info": {                  // optional: explicit field patches (win last)
    "max_output_tokens": 65536
  }
}
```

Evaluation order per model: `base_model`'s fully-resolved info (recursive,
cycle-safe) replaces the auto-matched baseline → `cost_multiplier` scales
every numeric `*cost*` field → `model_info` fields are applied verbatim.
The example above yields "gpt-5.5-fast = gpt-5.5 at 2.5× price", and keeps
tracking gpt-5.5's metadata as catalogs refresh. The same fields are
editable in the panel's override dialog; saving writes the file for you.

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

curl localhost:8080/v1/model/info | jq '.data[0]'
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
