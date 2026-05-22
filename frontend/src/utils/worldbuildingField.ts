export type StructuredWorldbuildingField = {
  summary?: string
  quick_ref?: {
    label?: string
    keywords?: unknown[]
    ladder?: unknown[]
    rules?: unknown[]
    costs?: unknown[]
  }
}

export function parseStructuredWorldbuildingField(value: unknown): StructuredWorldbuildingField | null {
  const text = String(value || '').trim()
  if (!text.startsWith('{')) return null
  try {
    const parsed = JSON.parse(text) as StructuredWorldbuildingField
    if (!parsed || typeof parsed !== 'object') return null
    if (!('summary' in parsed) && !('quick_ref' in parsed)) return null
    return parsed
  } catch {
    return null
  }
}

export function listWorldbuildingItems(items: unknown[] | undefined, limit = 6): string[] {
  if (!Array.isArray(items)) return []
  return items.map(v => String(v || '').trim()).filter(Boolean).slice(0, limit)
}

export function formatWorldbuildingFieldPreview(value: unknown): string {
  const parsed = parseStructuredWorldbuildingField(value)
  if (!parsed) return String(value || '').trim()
  const quick = parsed.quick_ref || {}
  const lines: string[] = []
  const keywords = listWorldbuildingItems(quick.keywords)
  const ladder = listWorldbuildingItems(quick.ladder)
  const rules = listWorldbuildingItems(quick.rules)
  const costs = listWorldbuildingItems(quick.costs)
  if (keywords.length) lines.push(`关键词：${keywords.join('、')}`)
  if (ladder.length) lines.push(`结构：${ladder.join(' / ')}`)
  if (rules.length) lines.push(`规则：${rules.join('；')}`)
  if (costs.length) lines.push(`代价：${costs.join('；')}`)
  if (parsed.summary) lines.push(`说明：${String(parsed.summary).trim()}`)
  return lines.join('\n')
}
