import { useEffect, useRef, useState } from 'react';
import type { NovelaiConfiguration } from '../../api/novelai';
import { Icon } from '../../ui/Icon';
import { undesiredContentPresetOptions } from './promptPreferences';

export type DrawingPreferences = Pick<NovelaiConfiguration, 'nsfwEnabled' | 'addQualityTags' | 'undesiredContentPreset'>;

export function StudioPromptPreferences({ configuration, disabled, onSave }: {
  configuration: NovelaiConfiguration; disabled: boolean; onSave: (changes: Partial<DrawingPreferences>) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const firstInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!open) return;
    firstInput.current?.focus();
    const outside = (event: PointerEvent) => { if (!container.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); setOpen(false); trigger.current?.focus(); }
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape); };
  }, [open]);
  const nsfwModelAllowed = configuration.allowedModels.includes(configuration.nsfwModel);
  return <div className="studio-preferences" ref={container}>
    <button type="button" ref={trigger} className="studio-preferences-trigger" aria-label="绘图偏好" title="绘图偏好" aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(!open)}><Icon name="settings" size={18} /></button>
    {open && <div className="studio-preferences-popover" role="dialog" aria-label="绘图偏好">
      <strong>绘图偏好</strong><small>自动保存，应用于所有角色的后续生成。</small>
      <label className="checkbox-field"><input ref={firstInput} type="checkbox" checked={configuration.addQualityTags} disabled={disabled} onChange={(event) => void onSave({ addQualityTags: event.target.checked })} />自动质量标签</label>
      <label className="checkbox-field"><input type="checkbox" checked={configuration.nsfwEnabled} disabled={disabled || (!configuration.nsfwEnabled && !nsfwModelAllowed)} onChange={(event) => void onSave({ nsfwEnabled: event.target.checked })} />NSFW 模式</label>
      {!nsfwModelAllowed && <small>请先在 NovelAI 设置中允许 NSFW 模型。</small>}
      <label className="field"><span>负向预设</span><select aria-label="工作台负向预设" value={configuration.undesiredContentPreset} disabled={disabled} onChange={(event) => void onSave({ undesiredContentPreset: Number(event.target.value) })}>
        {undesiredContentPresetOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select></label>
      <div className="studio-preferences-model"><span>默认模型</span><code>{configuration.nsfwEnabled ? configuration.nsfwModel : configuration.defaultModel}</code></div>
    </div>}
  </div>;
}
