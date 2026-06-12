import { MagnifyingGlassIcon } from '@heroicons/react/16/solid'
import { useEffect, useRef, useState } from 'react'
import { Badge } from '../catalyst/badge'
import { Button } from '../catalyst/button'
import {
  Dialog,
  DialogActions,
  DialogBody,
  DialogDescription,
  DialogTitle,
} from '../catalyst/dialog'
import { ErrorMessage, Field } from '../catalyst/fieldset'
import { Input, InputGroup } from '../catalyst/input'
import { Text } from '../catalyst/text'
import { api, type MetadataKey, type ModelEntry } from '../lib/api'
import { formatCostPerMillion, formatTokens } from '../lib/format'

interface Props {
  entry: ModelEntry | null // null = closed
  onClose: () => void
  onSaved: () => void
}

/** Pick which litellm/openrouter entry a model's metadata comes from. */
export function MatchDialog({ entry, onClose, onSaved }: Props) {
  const open = entry !== null
  const current = entry?._admin?.manual_match ?? null

  const [query, setQuery] = useState('')
  const [results, setResults] = useState<MetadataKey[]>([])
  const [selected, setSelected] = useState<MetadataKey | null>(null)
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const seq = useRef(0)

  useEffect(() => {
    if (!open || !entry) return
    setError(null)
    setSelected(null)
    // Prefill with the current manual match key, or the model name itself.
    const [, ...keyParts] = (current ?? '').split(':')
    setQuery(current ? keyParts.join(':') : entry.model_name)
  }, [open, entry, current])

  // Debounced search across both metadata sources.
  useEffect(() => {
    if (!open) return
    const q = query.trim()
    if (!q) {
      setResults([])
      return
    }
    const mySeq = ++seq.current
    setSearching(true)
    const t = setTimeout(async () => {
      try {
        const res = await api.searchMetadataKeys(q)
        if (seq.current === mySeq) setResults(res.data)
      } catch (e) {
        if (seq.current === mySeq)
          setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (seq.current === mySeq) setSearching(false)
      }
    }, 250)
    return () => clearTimeout(t)
  }, [open, query])

  async function save() {
    if (!entry || !selected) return
    setSaving(true)
    setError(null)
    try {
      await api.setMatch(entry.model_name, selected.source, selected.key)
      onSaved()
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function clear() {
    if (!entry) return
    setSaving(true)
    setError(null)
    try {
      await api.clearMatch(entry.model_name)
      onSaved()
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onClose={onClose} size="2xl">
      <DialogTitle>Match metadata for {entry?.model_name}</DialogTitle>
      <DialogDescription>
        Pin this model to a LiteLLM or OpenRouter catalog entry. The manual
        match takes precedence over automatic name matching; overrides still
        apply on top.
      </DialogDescription>
      <DialogBody>
        {current && (
          <Text className="mb-3 text-sm">
            Currently matched to <Badge color="purple">{current}</Badge>
          </Text>
        )}
        <InputGroup>
          <MagnifyingGlassIcon />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search litellm keys and OpenRouter ids…"
            autoFocus
          />
        </InputGroup>

        <div className="mt-3 max-h-80 overflow-y-auto rounded-lg border border-zinc-950/10 dark:border-white/10">
          {results.length === 0 && (
            <Text className="p-4 text-sm text-zinc-500">
              {searching
                ? 'Searching…'
                : query.trim()
                  ? 'No catalog entries match.'
                  : 'Type to search.'}
            </Text>
          )}
          {results.map((r) => {
            const isSelected =
              selected?.source === r.source && selected?.key === r.key
            return (
              <button
                key={`${r.source}:${r.key}`}
                type="button"
                onClick={() => setSelected(r)}
                className={`flex w-full items-center justify-between gap-3 border-b border-zinc-950/5 px-4 py-2.5 text-left last:border-b-0 dark:border-white/5 ${
                  isSelected
                    ? 'bg-blue-500/10 dark:bg-blue-400/10'
                    : 'hover:bg-zinc-950/2.5 dark:hover:bg-white/2.5'
                }`}
              >
                <div className="min-w-0">
                  <div className="truncate font-mono text-sm" title={r.key}>
                    {r.key}
                  </div>
                  <div className="mt-0.5 text-xs text-zinc-500">
                    {formatTokens(r.info.max_input_tokens)} ctx ·{' '}
                    {formatCostPerMillion(r.info.input_cost_per_token)} in ·{' '}
                    {formatCostPerMillion(r.info.output_cost_per_token)} out
                  </div>
                </div>
                <Badge color={r.source === 'litellm' ? 'sky' : 'orange'}>
                  {r.source}
                </Badge>
              </button>
            )
          })}
        </div>

        {error && (
          <Field className="mt-3">
            <ErrorMessage>{error}</ErrorMessage>
          </Field>
        )}
      </DialogBody>
      <DialogActions>
        {current && (
          <Button outline disabled={saving} onClick={clear}>
            Clear match
          </Button>
        )}
        <Button plain disabled={saving} onClick={onClose}>
          Cancel
        </Button>
        <Button disabled={saving || !selected} onClick={save}>
          {saving ? 'Saving…' : 'Save match'}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
