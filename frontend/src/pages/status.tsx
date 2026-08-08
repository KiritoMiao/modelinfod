import { useCallback, useEffect, useState } from 'react'
import { api, type StatusInfo } from '../lib/api'
import { formatTimestamp } from '../lib/format'
import { Badge } from '../catalyst/badge'
import { Button } from '../catalyst/button'
import {
  DescriptionDetails,
  DescriptionList,
  DescriptionTerm,
} from '../catalyst/description-list'
import { Divider } from '../catalyst/divider'
import { Heading, Subheading } from '../catalyst/heading'
import { Text } from '../catalyst/text'

export default function StatusPage() {
  const [status, setStatus] = useState<StatusInfo | null>(null)
  const [busy, setBusy] = useState<
    'sync' | 'prices' | 'openrouter' | 'modelsdev' | 'overrides' | null
  >(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setStatus(await api.status())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function run(
    kind: 'sync' | 'prices' | 'openrouter' | 'modelsdev' | 'overrides'
  ) {
    setBusy(kind)
    setNotice(null)
    setError(null)
    try {
      if (kind === 'sync') {
        const r = await api.syncNow()
        setNotice(`Synced ${r.synced} models from upstream.`)
      } else if (kind === 'prices') {
        const r = await api.refreshPrices()
        setNotice(`Refreshed LiteLLM metadata: ${r.entries} entries.`)
      } else if (kind === 'modelsdev') {
        const r = await api.refreshModelsdev()
        setNotice(`Refreshed models.dev catalog: ${r.entries} entries.`)
      } else if (kind === 'openrouter') {
        const r = await api.refreshOpenrouter()
        setNotice(`Refreshed OpenRouter catalog: ${r.entries} entries.`)
      } else {
        const r = await api.reloadOverrides()
        setNotice(
          `Reloaded ${r.overrides} override file(s)` +
            (r.errors.length ? `, ${r.errors.length} ignored.` : '.')
        )
      }
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <Heading>Status</Heading>
        <div className="flex flex-wrap gap-2">
          <Button
            outline
            disabled={busy !== null}
            onClick={() => run('modelsdev')}
          >
            {busy === 'modelsdev' ? 'Refreshing…' : 'Refresh models.dev'}
          </Button>
          <Button
            outline
            disabled={busy !== null}
            onClick={() => run('openrouter')}
          >
            {busy === 'openrouter' ? 'Refreshing…' : 'Refresh OpenRouter'}
          </Button>
          <Button outline disabled={busy !== null} onClick={() => run('prices')}>
            {busy === 'prices' ? 'Refreshing…' : 'Refresh LiteLLM metadata'}
          </Button>
          <Button disabled={busy !== null} onClick={() => run('sync')}>
            {busy === 'sync' ? 'Syncing…' : 'Sync upstream now'}
          </Button>
        </div>
      </div>

      {error && (
        <Text className="mt-4 !text-red-600 dark:!text-red-400">{error}</Text>
      )}
      {notice && <Text className="mt-4">{notice}</Text>}

      {status && (
        <div className="mt-8">
          <Subheading>Upstream</Subheading>
          <Divider className="mt-2" />
          <DescriptionList>
            <DescriptionTerm>Models endpoint</DescriptionTerm>
            <DescriptionDetails className="font-mono text-xs">
              {status.upstream_models_url}
            </DescriptionDetails>
            <DescriptionTerm>Last sync</DescriptionTerm>
            <DescriptionDetails>
              {formatTimestamp(status.upstream_synced_at)}
              {status.upstream_error ? (
                <Badge color="red" className="ml-2">
                  {status.upstream_error}
                </Badge>
              ) : (
                <Badge color="lime" className="ml-2">
                  ok
                </Badge>
              )}
            </DescriptionDetails>
            <DescriptionTerm>Auto-sync interval</DescriptionTerm>
            <DescriptionDetails>
              {status.sync_interval_seconds > 0
                ? `${status.sync_interval_seconds}s`
                : 'disabled'}
            </DescriptionDetails>
            <DescriptionTerm>Models from upstream</DescriptionTerm>
            <DescriptionDetails>{status.upstream_model_count}</DescriptionDetails>
          </DescriptionList>

          <Subheading className="mt-10">Local configuration</Subheading>
          <Divider className="mt-2" />
          <DescriptionList>
            <DescriptionTerm>Custom models</DescriptionTerm>
            <DescriptionDetails>{status.custom_model_count}</DescriptionDetails>
            <DescriptionTerm>Overrides</DescriptionTerm>
            <DescriptionDetails>
              {status.override_count}
              <Button
                outline
                className="ml-3 !py-0.5 !px-2 !text-xs"
                disabled={busy !== null}
                onClick={() => run('overrides')}
              >
                {busy === 'overrides' ? 'Reloading…' : 'Reload files'}
              </Button>
            </DescriptionDetails>
            <DescriptionTerm>Overrides directory</DescriptionTerm>
            <DescriptionDetails className="font-mono text-xs break-all">
              {status.overrides_dir}
              <span className="ml-2 font-sans text-zinc-500">
                (one JSON file per model — shareable between deployments)
              </span>
            </DescriptionDetails>
            {status.override_load_errors.length > 0 && (
              <>
                <DescriptionTerm>Ignored override files</DescriptionTerm>
                <DescriptionDetails>
                  {status.override_load_errors.map((e) => (
                    <Badge key={e} color="red" className="mr-1 mb-1">
                      {e}
                    </Badge>
                  ))}
                </DescriptionDetails>
              </>
            )}
            <DescriptionTerm>Manual matches</DescriptionTerm>
            <DescriptionDetails>{status.manual_match_count}</DescriptionDetails>
            <DescriptionTerm>Hidden models</DescriptionTerm>
            <DescriptionDetails>{status.hidden_count}</DescriptionDetails>
          </DescriptionList>

          <Subheading className="mt-10">
            models.dev catalog
            <span className="ml-2 text-xs font-normal text-zinc-500">
              1st — cache, context-tier and priority pricing
            </span>
          </Subheading>
          <Divider className="mt-2" />
          <DescriptionList>
            <DescriptionTerm>Entries</DescriptionTerm>
            <DescriptionDetails>
              {status.modelsdev_entries}
              {status.modelsdev_entries === 0 && (
                <Badge color="zinc" className="ml-2">
                  not fetched yet
                </Badge>
              )}
            </DescriptionDetails>
            <DescriptionTerm>Last refresh</DescriptionTerm>
            <DescriptionDetails>
              {formatTimestamp(status.modelsdev_refreshed_at)}
            </DescriptionDetails>
          </DescriptionList>

          <Subheading className="mt-10">
            LiteLLM metadata
            <span className="ml-2 text-xs font-normal text-zinc-500">2nd</span>
          </Subheading>
          <Divider className="mt-2" />
          <DescriptionList>
            <DescriptionTerm>Entries</DescriptionTerm>
            <DescriptionDetails>{status.litellm_map_entries}</DescriptionDetails>
            <DescriptionTerm>Source</DescriptionTerm>
            <DescriptionDetails className="font-mono text-xs break-all">
              {status.prices_source}
            </DescriptionDetails>
            <DescriptionTerm>Last refresh</DescriptionTerm>
            <DescriptionDetails>
              {formatTimestamp(status.prices_refreshed_at)}
            </DescriptionDetails>
          </DescriptionList>

          <Subheading className="mt-10">
            OpenRouter catalog
            <span className="ml-2 text-xs font-normal text-zinc-500">3rd</span>
          </Subheading>
          <Divider className="mt-2" />
          <DescriptionList>
            <DescriptionTerm>Entries</DescriptionTerm>
            <DescriptionDetails>
              {status.openrouter_entries}
              {status.openrouter_entries === 0 && (
                <Badge color="zinc" className="ml-2">
                  not fetched yet
                </Badge>
              )}
            </DescriptionDetails>
            <DescriptionTerm>Last refresh</DescriptionTerm>
            <DescriptionDetails>
              {formatTimestamp(status.openrouter_refreshed_at)}
            </DescriptionDetails>
          </DescriptionList>
        </div>
      )}
    </>
  )
}
