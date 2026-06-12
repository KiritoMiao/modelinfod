export function formatTokens(n: unknown): string {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '—'
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 ? 1 : 0)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(n % 1_000 ? 1 : 0)}K`
  return String(n)
}

/** Cost stored per-token -> display per 1M tokens. */
export function formatCostPerMillion(perToken: unknown): string {
  if (typeof perToken !== 'number' || !Number.isFinite(perToken)) return '—'
  const v = perToken * 1_000_000
  return `$${v.toLocaleString(undefined, { maximumFractionDigits: v < 1 ? 4 : 2 })}`
}

export function formatTimestamp(ts: number | null): string {
  if (!ts) return 'never'
  return new Date(ts * 1000).toLocaleString()
}
