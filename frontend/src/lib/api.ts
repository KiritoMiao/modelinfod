const BASE = '/modelinfod/api'

export interface Override {
  base_model?: string
  cost_multiplier?: number
  model_info?: Record<string, unknown>
}

export interface ModelEntry {
  model_name: string
  litellm_params: Record<string, unknown>
  model_info: Record<string, unknown>
  _admin?: {
    hidden: boolean
    override: Override | null
    override_inherited_from: string | null // bare model name supplying the override
    is_custom: boolean
    in_upstream: boolean
    manual_match: string | null // "<source>:<key>"
  }
}

export type MetadataSource = 'modelsdev' | 'litellm' | 'openrouter'

export interface MetadataKey {
  source: MetadataSource
  key: string
  info: {
    max_input_tokens?: number
    max_output_tokens?: number
    input_cost_per_token?: number
    output_cost_per_token?: number
    cache_read_input_token_cost?: number
    litellm_provider?: string
    mode?: string
  }
}

export interface StatusInfo {
  upstream_models_url: string
  upstream_synced_at: number | null
  upstream_error: string | null
  upstream_model_count: number
  custom_model_count: number
  override_count: number
  hidden_count: number
  manual_match_count: number
  overrides_dir: string
  override_load_errors: string[]
  litellm_map_entries: number
  prices_refreshed_at: number | null
  prices_source: string
  openrouter_entries: number
  openrouter_refreshed_at: number | null
  modelsdev_entries: number
  modelsdev_refreshed_at: number | null
  sync_interval_seconds: number
}

const TOKEN_KEY = 'modelinfod_admin_token'

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem(TOKEN_KEY)
  return token ? { Authorization: `Bearer ${token}` } : {}
}

async function request<T>(path: string, init?: RequestInit, retried = false): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...authHeaders(), ...init?.headers },
  })
  if (res.status === 401 && !retried) {
    // ADMIN_TOKEN is set on the server; ask once and retry.
    const token = window.prompt('Admin token required:')
    if (token) {
      localStorage.setItem(TOKEN_KEY, token)
      return request<T>(path, init, true)
    }
  }
  if (!res.ok) {
    if (res.status === 401) localStorage.removeItem(TOKEN_KEY)
    let detail = res.statusText
    try {
      const body = await res.json()
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      /* keep statusText */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export const api = {
  listModels: () =>
    request<{ data: ModelEntry[]; known_fields: string[] }>('/models'),
  status: () => request<StatusInfo>('/status'),
  syncNow: () => request<{ synced: number }>('/sync', { method: 'POST' }),
  refreshPrices: () =>
    request<{ entries: number }>('/refresh-prices', { method: 'POST' }),
  refreshOpenrouter: () =>
    request<{ entries: number }>('/refresh-openrouter', { method: 'POST' }),
  refreshModelsdev: () =>
    request<{ entries: number }>('/refresh-modelsdev', { method: 'POST' }),
  reloadOverrides: () =>
    request<{ overrides: number; errors: string[] }>('/reload-overrides', {
      method: 'POST',
    }),
  searchMetadataKeys: (q: string, limit = 30) =>
    request<{ data: MetadataKey[] }>(
      `/metadata-keys?q=${encodeURIComponent(q)}&limit=${limit}`
    ),
  setMatch: (name: string, source: MetadataSource, key: string) =>
    request(`/models/${encodeURIComponent(name)}/match`, {
      method: 'PUT',
      body: JSON.stringify({ source, key }),
    }),
  clearMatch: (name: string) =>
    request(`/models/${encodeURIComponent(name)}/match`, { method: 'DELETE' }),
  setOverride: (name: string, override: Override) =>
    request(`/models/${encodeURIComponent(name)}/override`, {
      method: 'PUT',
      body: JSON.stringify({
        model_info: override.model_info ?? {},
        base_model: override.base_model ?? '',
        cost_multiplier: override.cost_multiplier ?? null,
      }),
    }),
  deleteOverride: (name: string) =>
    request(`/models/${encodeURIComponent(name)}/override`, { method: 'DELETE' }),
  setHidden: (name: string, hidden: boolean) =>
    request(`/models/${encodeURIComponent(name)}/hidden`, {
      method: 'PUT',
      body: JSON.stringify({ hidden }),
    }),
  upsertCustomModel: (body: {
    model_name: string
    litellm_params: Record<string, unknown>
    model_info: Record<string, unknown>
  }) => request('/custom-models', { method: 'POST', body: JSON.stringify(body) }),
  deleteCustomModel: (name: string) =>
    request(`/custom-models/${encodeURIComponent(name)}`, { method: 'DELETE' }),
}
