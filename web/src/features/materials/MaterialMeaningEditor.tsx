import { useState } from 'react';
import type { MaterialChanges, MeaningScope, RoleMaterial } from '../../api/materials';

const listFields = [['aliases', '别名'], ['tags', '标签'], ['emotions', '情绪'], ['uses', '用途'], ['avoid', '避免场景']] as const;
type Draft = { name: string; description: string; aliases: string; tags: string; emotions: string; uses: string; avoid: string; uncertainty: string; uncertaintyAffectsUse: boolean };
function draftFor(scope: MeaningScope): Draft {
  return { name: scope.name, description: scope.description, aliases: (scope.aliases ?? []).join('、'), tags: (scope.tags ?? []).join('、'),
    emotions: (scope.emotions ?? []).join('、'), uses: (scope.uses ?? []).join('、'), avoid: (scope.avoid ?? []).join('、'),
    uncertainty: scope.uncertainty ?? '', uncertaintyAffectsUse: scope.uncertaintyAffectsUse ?? false };
}

/** Drafts survive a conflict/refresh and scope switches; saved values remain visible for comparison. */
export function MaterialMeaningEditor({ material, disabled, onSave }: {
  material: RoleMaterial; disabled: boolean; onSave: (changes: MaterialChanges) => Promise<void>;
}) {
  const scopes: MeaningScope[] = material.meaningScopes ?? [{ ...material, id: material.meaningScopeId ?? 'current', scope: material.meaningScope ?? 'role', label: '当前说明', available: true }];
  const [selected, setSelected] = useState(material.meaningScopeId ?? scopes[0].id);
  const [drafts, setDrafts] = useState<Record<string, Draft>>(() => Object.fromEntries(scopes.map(scope => [scope.id, draftFor(scope)])));
  const scope = scopes.find(value => value.id === selected);
  const draft = drafts[selected];
  const change = (changes: Partial<Draft>) => setDrafts(current => ({ ...current, [selected]: { ...current[selected], ...changes } }));
  const switchScope = (id: string) => {
    const next = scopes.find(value => value.id === id);
    if (!next) return;
    setDrafts(current => current[id] ? current : { ...current, [id]: draftFor(next) }); setSelected(id);
  };
  return <details className="material-edit"><summary>编辑说明</summary>
    <form onSubmit={event => {
      event.preventDefault(); if (!draft || !scope?.available) return;
      const changes: MaterialChanges = { name: draft.name.trim(), description: draft.description.trim(), uncertainty: draft.uncertainty.trim(), uncertaintyAffectsUse: draft.uncertaintyAffectsUse };
      if (material.meaningScopes) changes.meaningScopeId = selected;
      for (const [key] of listFields) changes[key] = draft[key].split(/[、,，\n]/).map(value => value.trim()).filter(Boolean);
      void onSave(changes);
    }}>
      {material.meaningScopes && <label className="field"><span>说明适用范围</span><select aria-label="说明适用范围" value={selected} disabled={disabled} onChange={event => switchScope(event.target.value)}>{scopes.map(value => <option key={value.id} value={value.id}>{value.label}{value.available ? '' : '（原授权已失效）'}</option>)}</select></label>}
      <p className="field-hint">{scope?.scope === 'conversation' ? '正在编辑原收藏会话的名称与说明，修改不会共享到其他会话。' : '正在编辑当前角色共享的名称与说明，不会改动已有群专属说明。'}</p>
      {!scope?.available && <p role="alert">原会话授权已失效，这份说明不能修改或重新启用。</p>}
      {draft && <>
        <label className="field"><span>名称</span><input aria-label="素材名称" value={draft.name} maxLength={160} disabled={disabled} onChange={event => change({ name: event.target.value })} /></label>
        <label className="field"><span>说明</span><textarea aria-label="素材说明" rows={3} value={draft.description} maxLength={2000} disabled={disabled} onChange={event => change({ description: event.target.value })} /></label>
        {listFields.map(([key, label]) => <label key={key} className="field"><span>{label}（可选，顿号分隔）</span><input aria-label={`素材${label}`} value={draft[key]} maxLength={key === 'uses' || key === 'avoid' ? 5120 : 2560} disabled={disabled} onChange={event => change({ [key]: event.target.value })} /></label>)}
        <label className="field"><span>尚不确定的内容（可选）</span><textarea aria-label="尚不确定的内容" value={draft.uncertainty} maxLength={1000} rows={2} disabled={disabled} onChange={event => change({ uncertainty: event.target.value })} /></label>
        <label className="material-chat-toggle"><input type="checkbox" checked={draft.uncertaintyAffectsUse} disabled={disabled} onChange={event => change({ uncertaintyAffectsUse: event.target.checked })} />不确定性影响聊天用途，暂不自动推荐</label>
        <p className="field-hint">如仅不知道猫的品种，可留此开关关闭；不能确定是否有侮辱含义时应打开。</p>
        {scope?.scope === 'conversation' && material.meaningScopes && <button type="button" className="ghost" disabled={disabled || !scope.available} onClick={() => { setDrafts(current => ({ ...current, role: { ...draft } })); setSelected('role'); }}>将这份说明用作通用说明草稿</button>}
        <p className="field-hint">通用说明仅在素材本身可用的会话生效，不会分享原图。切换范围保留未保存的草稿。</p>
        <button className="primary" type="submit" disabled={!draft.name.trim() || disabled || !scope?.available}>保存修改</button>
      </>}
      {scope && <details><summary>已保存的说明与历史</summary><p>当前已保存：{scope.description || '未填写说明'}</p><button type="button" className="ghost" disabled={disabled} onClick={() => setDrafts(current => ({ ...current, [selected]: draftFor(scope) }))}>放弃当前草稿，载入已保存内容</button>{scope.history?.length ? <ul>{scope.history.map((entry, index) => <li key={`${entry.revision}-${index}`}>{entry.description || '未填写说明'}</li>)}</ul> : null}</details>}
    </form>
  </details>;
}
