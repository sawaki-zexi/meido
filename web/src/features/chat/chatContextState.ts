// Adapted from Shiori c14152ed chatContextState.ts (MIT; third_party/shiori-agent).
export type ContextUsage = { inputTokens: number; capacity: number | null; source: 'actual' | 'anchor_delta' | 'local'; configurationId: string | null; model?: string | null };

export function readContextUsage(value: unknown): ContextUsage | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as ContextUsage;
  return Number.isFinite(item.inputTokens) && item.inputTokens >= 0 && (item.source === 'actual' || item.source === 'anchor_delta' || item.source === 'local')
    ? { ...item, capacity: typeof item.capacity === 'number' && Number.isFinite(item.capacity) && item.capacity > 0 ? item.capacity : null } : null;
}

/** Unknown capacity never becomes zero or a fictional percentage. */
export function contextUsageLabel(usage: ContextUsage | null) {
  if (!usage || !usage.capacity) return { label: '上下文用量未知', ratio: null };
  const ratio = usage.inputTokens / usage.capacity;
  const source = { actual: '实际', anchor_delta: '锚点估算', local: '本地估算' }[usage.source];
  return { label: `上下文 ${usage.inputTokens.toLocaleString()} / ${usage.capacity.toLocaleString()}，${Math.round(ratio * 100)}%（${source}）`, ratio };
}
