import { useEffect, useMemo, useState } from 'react'
import { Button } from '../catalyst/button'
import {
  Dialog,
  DialogActions,
  DialogBody,
  DialogDescription,
  DialogTitle,
} from '../catalyst/dialog'
import { ErrorMessage, Field, FieldGroup, Label } from '../catalyst/fieldset'
import { Input } from '../catalyst/input'
import { Text } from '../catalyst/text'
import { Textarea } from '../catalyst/textarea'
import { api, type ModelEntry } from '../lib/api'

/** Fields shown as dedicated inputs in the editor. */
const QUICK_FIELDS: { key: string; label: string; kind: 'number' | 'text' }[] = [
  { key: 'max_input_tokens', label: 'Max input tokens', kind: 'number' },
  { key: 'max_output_tokens', label: 'Max output tokens', kind: 'number' },
  { key: 'input_cost_per_token', label: 'Input cost / token (USD)', kind: 'number' },
  { key: 'output_cost_per_token', label: 'Output cost / token (USD)', kind: 'number' },
  { key: 'litellm_provider', label: 'Provider', kind: 'text' },
  { key: 'mode', label: 'Mode (chat, embedding, …)', kind: 'text' },
]

interface Props {
  entry: ModelEntry | null // null = closed; create-mode uses `creating`
  creating?: boolean
  onClose: () => void
  onSaved: () => void
}

export function ModelDialog({ entry, creating = false, onClose, onSaved }: Props) {
  const open = creating || entry !== null
  const override = entry?._admin?.override ?? null

  const [modelName, setModelName] = useState('')
  const [baseModel, setBaseModel] = useState('')
  const [costMultiplier, setCostMultiplier] = useState('')
  const [quick, setQuick] = useState<Record<string, string>>({})
  const [extraJson, setExtraJson] = useState('')
  const [paramsJson, setParamsJson] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  // Initialize form state whenever the dialog target changes.
  useEffect(() => {
    if (!open) return
    setError(null)
    if (creating) {
      setModelName('')
      setBaseModel('')
      setCostMultiplier('')
      setQuick({})
      setExtraJson('{}')
      setParamsJson('{}')
      return
    }
    if (!entry) return
    setModelName(entry.model_name)
    setBaseModel(override?.base_model ?? '')
    setCostMultiplier(
      override?.cost_multiplier != null ? String(override.cost_multiplier) : ''
    )
    const overrideInfo = override?.model_info ?? {}
    const source: Record<string, unknown> = entry._admin?.is_custom
      ? { ...(entry.model_info ?? {}), ...overrideInfo }
      : { ...overrideInfo }
    const q: Record<string, string> = {}
    const extra: Record<string, unknown> = {}
    const derived = ['id', 'db_model', 'key', 'metadata_source', 'base_model', 'cost_multiplier']
    for (const [k, v] of Object.entries(source)) {
      if (derived.includes(k)) continue
      const quickField = QUICK_FIELDS.find((f) => f.key === k)
      if (quickField) q[k] = String(v)
      else extra[k] = v
    }
    setQuick(q)
    setExtraJson(Object.keys(extra).length ? JSON.stringify(extra, null, 2) : '{}')
    setParamsJson(
      entry._admin?.is_custom
        ? JSON.stringify(entry.litellm_params ?? {}, null, 2)
        : '{}'
    )
  }, [open, creating, entry, override])

  const isCustom = creating || Boolean(entry?._admin?.is_custom)

  const baseline = useMemo(() => {
    // What the model resolves to without an override — shown as placeholders.
    if (!entry || creating) return {}
    return entry.model_info ?? {}
  }, [entry, creating])

  function buildInfo(): Record<string, unknown> {
    let extra: Record<string, unknown> = {}
    if (extraJson.trim()) {
      const parsed: unknown = JSON.parse(extraJson)
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('Extra fields must be a JSON object')
      }
      extra = parsed as Record<string, unknown>
    }
    const info: Record<string, unknown> = { ...extra }
    for (const f of QUICK_FIELDS) {
      const raw = quick[f.key]?.trim()
      if (!raw) continue
      if (f.kind === 'number') {
        const num = Number(raw)
        if (!Number.isFinite(num)) throw new Error(`${f.label} must be a number`)
        info[f.key] = num
      } else {
        info[f.key] = raw
      }
    }
    return info
  }

  async function save() {
    setSaving(true)
    setError(null)
    try {
      if (isCustom) {
        const name = creating ? modelName.trim() : entry!.model_name
        if (!name) throw new Error('Model name is required')
        let params: unknown = {}
        if (paramsJson.trim()) params = JSON.parse(paramsJson)
        if (params === null || typeof params !== 'object' || Array.isArray(params)) {
          throw new Error('litellm_params must be a JSON object')
        }
        await api.upsertCustomModel({
          model_name: name,
          litellm_params: params as Record<string, unknown>,
          model_info: buildInfo(),
        })
      } else {
        let mult: number | undefined
        const rawMult = costMultiplier.trim()
        if (rawMult) {
          mult = Number(rawMult)
          if (!Number.isFinite(mult) || mult <= 0) {
            throw new Error('Cost multiplier must be a positive number')
          }
        }
        await api.setOverride(entry!.model_name, {
          base_model: baseModel.trim() || undefined,
          cost_multiplier: mult,
          model_info: buildInfo(),
        })
      }
      onSaved()
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  async function clearOverride() {
    if (!entry) return
    setSaving(true)
    setError(null)
    try {
      await api.deleteOverride(entry.model_name)
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
      <DialogTitle>
        {creating ? 'Add custom model' : entry?.model_name}
      </DialogTitle>
      <DialogDescription>
        {creating
          ? 'Expose a model that the upstream /v1/models does not list.'
          : isCustom
            ? 'Edit this custom model.'
            : 'Override metadata for this upstream model. Empty fields fall back to LiteLLM defaults (shown as placeholders).'}
      </DialogDescription>
      <DialogBody>
        <FieldGroup>
          {creating && (
            <Field>
              <Label>Model name</Label>
              <Input
                value={modelName}
                onChange={(e) => setModelName(e.target.value)}
                placeholder="my-org/my-model"
                autoFocus
              />
            </Field>
          )}

          {!isCustom && (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Field>
                <Label>Derive from base model</Label>
                <Input
                  value={baseModel}
                  onChange={(e) => setBaseModel(e.target.value)}
                  placeholder="e.g. gpt-5.5"
                />
                <Text className="mt-1 text-xs">
                  Inherit all metadata from another model in this catalog.
                </Text>
              </Field>
              <Field>
                <Label>Cost multiplier</Label>
                <Input
                  inputMode="decimal"
                  value={costMultiplier}
                  onChange={(e) => setCostMultiplier(e.target.value)}
                  placeholder="e.g. 2.5"
                />
                <Text className="mt-1 text-xs">
                  Scales all cost fields of the inherited/matched data.
                </Text>
              </Field>
            </div>
          )}

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {QUICK_FIELDS.map((f) => (
              <Field key={f.key}>
                <Label>{f.label}</Label>
                <Input
                  type={f.kind === 'number' ? 'text' : 'text'}
                  inputMode={f.kind === 'number' ? 'decimal' : undefined}
                  value={quick[f.key] ?? ''}
                  placeholder={
                    baseline[f.key] !== undefined ? String(baseline[f.key]) : ''
                  }
                  onChange={(e) =>
                    setQuick((prev) => ({ ...prev, [f.key]: e.target.value }))
                  }
                />
              </Field>
            ))}
          </div>

          <Field>
            <Label>Extra model_info fields (JSON)</Label>
            <Textarea
              rows={5}
              value={extraJson}
              onChange={(e) => setExtraJson(e.target.value)}
              className="font-mono"
            />
          </Field>

          {isCustom && (
            <Field>
              <Label>litellm_params (JSON)</Label>
              <Textarea
                rows={4}
                value={paramsJson}
                onChange={(e) => setParamsJson(e.target.value)}
                className="font-mono"
              />
              <Text className="mt-1 text-xs">
                e.g. {'{"model": "ollama/llama3", "api_base": "http://gpu:11434"}'}
              </Text>
            </Field>
          )}

          {error && (
            <Field>
              <ErrorMessage>{error}</ErrorMessage>
            </Field>
          )}
        </FieldGroup>
      </DialogBody>
      <DialogActions>
        {!creating && !isCustom && override && (
          <Button outline disabled={saving} onClick={clearOverride}>
            Remove override
          </Button>
        )}
        <Button plain disabled={saving} onClick={onClose}>
          Cancel
        </Button>
        <Button disabled={saving} onClick={save}>
          {saving ? 'Saving…' : 'Save'}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
