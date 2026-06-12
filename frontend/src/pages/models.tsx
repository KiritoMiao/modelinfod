import {
  EllipsisHorizontalIcon,
  MagnifyingGlassIcon,
  PlusIcon,
} from '@heroicons/react/16/solid'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Badge } from '../catalyst/badge'
import { Button } from '../catalyst/button'
import {
  Dropdown,
  DropdownButton,
  DropdownItem,
  DropdownMenu,
} from '../catalyst/dropdown'
import { Heading } from '../catalyst/heading'
import { Input, InputGroup } from '../catalyst/input'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '../catalyst/table'
import { Text } from '../catalyst/text'
import { MatchDialog } from '../components/match-dialog'
import { ModelDialog } from '../components/model-dialog'
import { api, type ModelEntry } from '../lib/api'
import { formatCostPerMillion, formatTokens } from '../lib/format'

export default function ModelsPage() {
  const [entries, setEntries] = useState<ModelEntry[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState<ModelEntry | null>(null)
  const [matching, setMatching] = useState<ModelEntry | null>(null)
  const [creating, setCreating] = useState(false)

  const load = useCallback(async () => {
    try {
      const res = await api.listModels()
      setEntries(res.data)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    const list = q
      ? entries.filter((e) => {
          const provider = String(e.model_info.litellm_provider ?? '')
          return (
            e.model_name.toLowerCase().includes(q) ||
            provider.toLowerCase().includes(q)
          )
        })
      : entries
    return [...list].sort((a, b) => a.model_name.localeCompare(b.model_name))
  }, [entries, query])

  async function toggleHidden(entry: ModelEntry) {
    try {
      await api.setHidden(entry.model_name, !entry._admin?.hidden)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  async function removeCustom(entry: ModelEntry) {
    if (!window.confirm(`Delete custom model "${entry.model_name}"?`)) return
    try {
      await api.deleteCustomModel(entry.model_name)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="max-sm:w-full sm:flex-1">
          <Heading>Models</Heading>
          <div className="mt-4 max-w-xl">
            <InputGroup>
              <MagnifyingGlassIcon />
              <Input
                name="search"
                placeholder="Filter by name or provider…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </InputGroup>
          </div>
        </div>
        <Button onClick={() => setCreating(true)}>
          <PlusIcon />
          Add custom model
        </Button>
      </div>

      {error && (
        <Text className="mt-4 !text-red-600 dark:!text-red-400">{error}</Text>
      )}

      <Table className="mt-8 [--gutter:--spacing(6)] lg:[--gutter:--spacing(10)]">
        <TableHead>
          <TableRow>
            <TableHeader>Model</TableHeader>
            <TableHeader>Provider</TableHeader>
            <TableHeader className="text-right">Context</TableHeader>
            <TableHeader className="text-right">Max out</TableHeader>
            <TableHeader className="text-right">Input $/1M</TableHeader>
            <TableHeader className="text-right">Output $/1M</TableHeader>
            <TableHeader>Flags</TableHeader>
            <TableHeader className="relative w-0">
              <span className="sr-only">Actions</span>
            </TableHeader>
          </TableRow>
        </TableHead>
        <TableBody>
          {loading && (
            <TableRow>
              <TableCell colSpan={8} className="text-center text-zinc-500">
                Loading…
              </TableCell>
            </TableRow>
          )}
          {!loading && filtered.length === 0 && (
            <TableRow>
              <TableCell colSpan={8} className="text-center text-zinc-500">
                {entries.length === 0
                  ? 'No models. Check upstream sync on the Status page.'
                  : 'No models match the filter.'}
              </TableCell>
            </TableRow>
          )}
          {filtered.map((entry) => {
            const info = entry.model_info
            const admin = entry._admin
            return (
              <TableRow key={entry.model_name}>
                <TableCell>
                  <div className="flex items-center gap-2">
                    <span
                      className="max-w-72 truncate font-medium"
                      title={entry.model_name}
                    >
                      {entry.model_name}
                    </span>
                    {admin?.is_custom && <Badge color="blue">custom</Badge>}
                    {admin?.override && Object.keys(admin.override).length > 0 && (
                      <Badge color="purple">override</Badge>
                    )}
                    {admin?.override_inherited_from && (
                      <Badge
                        color="violet"
                        title={`Uses ${admin.override_inherited_from}'s override; edit to give this id its own`}
                      >
                        via {admin.override_inherited_from}
                      </Badge>
                    )}
                    {admin?.manual_match && (
                      <Badge color="sky" title={admin.manual_match}>
                        matched
                      </Badge>
                    )}
                    {admin?.hidden && <Badge color="zinc">hidden</Badge>}
                    {!admin?.in_upstream && !admin?.is_custom && (
                      <Badge color="amber">stale</Badge>
                    )}
                  </div>
                </TableCell>
                <TableCell className="text-zinc-500">
                  {String(info.litellm_provider ?? '—')}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {formatTokens(info.max_input_tokens ?? info.max_tokens)}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {formatTokens(info.max_output_tokens)}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {formatCostPerMillion(info.input_cost_per_token)}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {formatCostPerMillion(info.output_cost_per_token)}
                </TableCell>
                <TableCell>
                  <div className="flex gap-1 whitespace-nowrap">
                    {Boolean(info.supports_vision) && <Badge>vision</Badge>}
                    {Boolean(info.supports_function_calling) && <Badge>tools</Badge>}
                    {Boolean(info.supports_reasoning) && <Badge>reasoning</Badge>}
                    {Boolean(info.supports_prompt_caching) && <Badge>caching</Badge>}
                  </div>
                </TableCell>
                <TableCell>
                  <div className="-mx-3 -my-1.5 sm:-mx-2.5">
                    <Dropdown>
                      <DropdownButton plain aria-label="More options">
                        <EllipsisHorizontalIcon />
                      </DropdownButton>
                      <DropdownMenu anchor="bottom end">
                        <DropdownItem onClick={() => setEditing(entry)}>
                          {admin?.is_custom ? 'Edit' : 'Edit override'}
                        </DropdownItem>
                        <DropdownItem onClick={() => setMatching(entry)}>
                          Match metadata…
                        </DropdownItem>
                        <DropdownItem onClick={() => toggleHidden(entry)}>
                          {admin?.hidden ? 'Unhide' : 'Hide from /v1/model/info'}
                        </DropdownItem>
                        {admin?.is_custom && (
                          <DropdownItem onClick={() => removeCustom(entry)}>
                            Delete custom model
                          </DropdownItem>
                        )}
                      </DropdownMenu>
                    </Dropdown>
                  </div>
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>

      <ModelDialog
        entry={editing}
        creating={creating}
        onClose={() => {
          setEditing(null)
          setCreating(false)
        }}
        onSaved={load}
      />
      <MatchDialog
        entry={matching}
        onClose={() => setMatching(null)}
        onSaved={load}
      />
    </>
  )
}
