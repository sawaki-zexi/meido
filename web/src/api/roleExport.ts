import { request } from './client';

export type CardExportFormat = 'json' | 'png' | 'charx';
export type CardExportSummary = {
  format: CardExportFormat;
  fields: string[];
  includedAssets: string[];
  omittedAssets: string[];
  notes: string[];
};

export async function exportRoleCard(roleId: string, format: CardExportFormat, signal: AbortSignal) {
  const response = await request(`/api/roles/${encodeURIComponent(roleId)}/export?format=${format}`, { signal });
  let summary: CardExportSummary;
  try {
    const header = response.headers.get('X-Meido-Export-Summary');
    if (!header || header.length > 8192) throw new Error();
    summary = JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(header), char => char.charCodeAt(0))));
    if (summary.format !== format || !['fields', 'includedAssets', 'omittedAssets', 'notes'].every(key => {
      const values = summary[key as Exclude<keyof CardExportSummary, 'format'>];
      return Array.isArray(values) && values.length <= 24 && values.every(value => typeof value === 'string' && value.length <= 500);
    })) throw new Error();
  } catch {
    throw new Error('导出摘要无效，请重试。');
  }
  const disposition = response.headers.get('Content-Disposition') ?? '';
  let filename = `role-card.${format}`;
  try {
    const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    if (encoded) filename = decodeURIComponent(encoded).replace(/[\x00-\x1f\x7f/\\:*?"<>|]/g, '_').slice(0, 100);
  } catch { /* Use the safe fallback when a proxy supplies an invalid filename. */ }
  const blob = await response.blob();
  return { blob, filename, summary };
}
