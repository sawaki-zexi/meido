import { defaultDriftMeme, type AutonomyConfiguration, type DriftMemeGrant } from '../../api/autonomy';
import type { ModelConfiguration } from '../../api/types';
import { useState } from 'react';

export function driftMemeConfigurationError(config: AutonomyConfiguration, models: ModelConfiguration[]): string {
  const grant = config.driftMeme ?? defaultDriftMeme;
  if (grant.allowedCategories.length > 32 || grant.allowedCategories.some((value) => !value || value.length > 128)) {
    return '最多允许32个类别，每项须为1至128字符。';
  }
  if (new Set(grant.allowedCategories).size !== grant.allowedCategories.length) return '类别不能重复，请保留每个类别一行。';
  if (grant.allowedCategories.some((value) => value === '.' || value === '..' || /[\\/:\u0000]/.test(value) || /[ .]$/.test(value))) {
    return '类别须为单个目录名，不能包含路径、冒号、尾部点号或空格。';
  }
  if (grant.sourceAttachmentIds.length > 16 || grant.sourceAttachmentIds.some((value) => !value || value.length > 128)) {
    return '最多允许16个已登记素材 ID，每项须为1至128字符。';
  }
  if (new Set(grant.sourceAttachmentIds).size !== grant.sourceAttachmentIds.length) return '素材 ID 不能重复，请保留每个 ID 一行。';
  if (!grant.enabled) return '';
  if (!grant.visionModelConfigurationId || grant.visionModelConfigurationId !== config.driftModelConfigurationId
    || !models.some((model) => model.id === grant.visionModelConfigurationId)) {
    return '请明确选择当前已保存的 Drift 模型作为表情库视觉模型；更换连接后须重新选择。';
  }
  if (!grant.readEnabled) return '请允许查看当前角色表情库图片，再开启维护。';
  if (!grant.allowedCategories.length) return '请至少填写一个允许维护的类别。';
  return '';
}

export function DriftMemeSettings({ config, models, onChange }: {
  config: AutonomyConfiguration; models: ModelConfiguration[];
  onChange: (changes: Partial<AutonomyConfiguration>) => void;
}) {
  const grant = config.driftMeme ?? defaultDriftMeme;
  const [categories, setCategories] = useState(grant.allowedCategories.join('\n'));
  const [sources, setSources] = useState(grant.sourceAttachmentIds.join('\n'));
  const model = models.find((item) => item.id === config.driftModelConfigurationId);
  const update = (changes: Partial<Omit<DriftMemeGrant, 'managedEpoch' | 'revision'>>) => onChange({ driftMeme: { ...grant, ...changes } });
  const lines = (text: string) => text.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  return <fieldset aria-label="Drift 表情库维护">
    <label className="field checkbox-field"><input type="checkbox" aria-label="允许 Drift 维护当前角色表情库"
      checked={grant.enabled} onChange={(event) => update({ enabled: event.target.checked })} />允许 Drift 维护当前角色表情库</label>
    <p className="field-hint">仅维护当前角色的表情库；维护不授予图片发送许可。</p>
    <label className="field"><span>表情库视觉模型</span><select aria-label="表情库视觉模型"
      value={grant.visionModelConfigurationId ?? ''} onChange={(event) => update({ visionModelConfigurationId: event.target.value || null })}>
      <option value="">请明确选择当前 Drift 模型的视觉用途</option>
      {grant.visionModelConfigurationId && grant.visionModelConfigurationId !== model?.id
        && <option value={grant.visionModelConfigurationId} disabled>原视觉连接已不是当前 Drift 模型</option>}
      {model && <option value={model.id}>{model.provider} · {model.model}（{model.id}）</option>}
    </select></label>
    <p className="field-hint">需支持图片；更换 Drift 模型后请重新选择。</p>
    <label className="field checkbox-field"><input type="checkbox" aria-label="允许 Drift 查看图片"
      checked={grant.readEnabled} onChange={(event) => update({ readEnabled: event.target.checked })} />允许 Drift 查看图片</label>
    <p className="field-hint">这是 Drift 的自主读图许可；主人可直接在角色工作区的“素材库”查看素材，不需要开启此项。</p>
    <label className="field checkbox-field"><input type="checkbox" aria-label="编辑类别、描述和别名"
      checked={grant.editEnabled} onChange={(event) => update({ editEnabled: event.target.checked })} />编辑类别、描述和别名</label>
    <label className="field checkbox-field"><input type="checkbox" aria-label="复制已登记素材到表情库"
      checked={grant.copyEnabled} onChange={(event) => update({ copyEnabled: event.target.checked })} />复制已登记素材到表情库</label>
    <div className="settings-field-grid">
    <label className="field"><span>允许维护的类别（每行一个）</span><textarea aria-label="允许维护的类别（每行一个）" rows={3}
      value={categories} onChange={(event) => { setCategories(event.target.value); update({ allowedCategories: lines(event.target.value) }); }} />
      <small>最多32项，填写类别名。</small></label>
    <label className="field"><span>允许复制的已登记素材 ID（每行一个）</span><textarea aria-label="允许复制的已登记素材 ID（每行一个）" rows={3}
      value={sources} onChange={(event) => { setSources(event.target.value); update({ sourceAttachmentIds: lines(event.target.value) }); }} />
      <small>最多16项，仅限当前角色已登记素材。</small></label>
    </div>
    <p className="field-hint">还需相应的文件读写和 Drift 工具许可，点击“保存自主设置”后生效。</p>
    <p className="field-hint">关闭后，主人仍可按原方式编辑表情库；重新开启要等待原运行资源收尾。</p>
  </fieldset>;
}
