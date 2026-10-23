import type { FormEvent } from 'react';
import { type Memory, typeLabels, statusLabels } from './types';

export type EditorDraft = { summary: string; memoryType: string; status: string; extraJson: string; sourceRefJson: string; happenedAt: string; emotionalWeight: string };
export const emptyDraft: EditorDraft = { summary: '', memoryType: 'fact', status: 'active', extraJson: '{}', sourceRefJson: '', happenedAt: '', emotionalWeight: '0' };
export function MemoryEditor({ target, draft, onChange, saving, onSubmit, onCancel }: {
  target: Memory | 'new'; draft: EditorDraft; onChange: (value: EditorDraft) => void; saving: boolean;
  onSubmit: (event: FormEvent) => void; onCancel: () => void;
}) {
  const creating = target === 'new';
  return <form className="memory-entry-editor" aria-label={creating ? '记住一件事' : '编辑记忆'} onSubmit={onSubmit}>
    <fieldset disabled={saving}>
      <p className="field-hint">{creating ? '写下希望她记住的事，保存在当前角色的记忆中。' : '内容与类型保留原记录，可调整状态、时间和补充信息。'}</p>
      <label className="field"><span>内容</span><textarea aria-label="内容" autoFocus={creating} required rows={5} disabled={!creating} value={draft.summary} placeholder="例如：主人喜欢红茶，不太喜欢过甜的点心。" onChange={event => onChange({ ...draft, summary: event.target.value })} /></label>
      <label className="field"><span>类型</span><select disabled={!creating} value={draft.memoryType} onChange={event => onChange({ ...draft, memoryType: event.target.value })}>
        {Object.entries(typeLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}
        {!typeLabels[draft.memoryType] && <option value={draft.memoryType}>{draft.memoryType}</option>}
      </select></label>
      {!creating && <label className="field"><span>状态</span><select value={draft.status} onChange={event => onChange({ ...draft, status: event.target.value })}>{Object.entries(statusLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>}
      <label className="field"><span>发生时间</span><input type="datetime-local" value={draft.happenedAt} onChange={event => onChange({ ...draft, happenedAt: event.target.value })} /></label>
      {!creating && <details className="memory-advanced"><summary>高级字段</summary>
        <label className="field"><span>情绪权重</span><input type="number" min="0" max="10" value={draft.emotionalWeight} onChange={event => onChange({ ...draft, emotionalWeight: event.target.value })} /></label>
        <label className="field"><span>扩展字段（JSON）</span><textarea aria-label="扩展字段（JSON）" className="memory-json" rows={5} value={draft.extraJson} onChange={event => onChange({ ...draft, extraJson: event.target.value })} /></label>
        <label className="field"><span>来源（JSON）</span><textarea aria-label="来源（JSON）" className="memory-json" rows={6} value={draft.sourceRefJson} onChange={event => onChange({ ...draft, sourceRefJson: event.target.value })} /></label>
      </details>}
      <div className="form-actions"><button type="button" className="ghost" onClick={onCancel}>取消</button><button type="submit" className="primary">{saving ? '保存中…' : '保存'}</button></div>
    </fieldset>
  </form>;
}
