# Cloudflare Access policy for the admin panel

Cloudflare Access sits at the edge, in front of the tunnel, and is the right
place to gate `/modelinfod` when you publish through cloudflared. The
`/v1/model/info` route needs no Access policy: modelinfod validates each
caller's API key against the gateway's `/v1/models` and only returns the
models that key can access.

## Self-hosted application (Zero Trust dashboard)

Zero Trust → Access → Applications → **Add an application** → *Self-hosted*:

| Field              | Value                                  |
| ------------------ | -------------------------------------- |
| Application name   | `modelinfod admin`                     |
| Session duration   | e.g. `24h`                             |
| Public hostname    | `llm.example.com`                      |
| Path               | `modelinfod`                           |

A path of `modelinfod` protects `/modelinfod` and everything beneath it
(the SPA, `/modelinfod/assets/*`, and the REST API `/modelinfod/api/*`).
Do **not** add an application for the bare hostname, or you will lock out
API clients calling the gateway and `/v1/model/info`.

Then add a policy, for example:

- **Action**: Allow
- **Include**: Emails — `you@example.com` (or an email domain / IdP group)

Everything outside `/modelinfod` bypasses Access automatically because no
application matches it. If your Access plan/UI exposes regex path matching
instead of prefix paths, use:

```
^/modelinfod(/.*)?$
```

## Service tokens (automation / CI)

To script the admin API through Access, create a service token (Zero Trust →
Access → Service auth), add an Access policy with **Action: Service Auth**
including that token, and send its credentials as headers:

```sh
curl https://llm.example.com/modelinfod/api/status \
  -H "CF-Access-Client-Id: <token-id>.access" \
  -H "CF-Access-Client-Secret: <token-secret>"
```

## Defense in depth

Access authenticates *people at the edge*; `ADMIN_TOKEN` in modelinfod's
`.env` authenticates *requests at the service*. With both enabled, a user
passes Cloudflare Access first, then the SPA prompts once for the admin
token. Keep `ADMIN_TOKEN` set even behind Access so a tunnel/router
misconfiguration (or someone on the same LAN as the service) cannot reach
the admin API unauthenticated.
