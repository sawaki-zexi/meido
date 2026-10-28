import type { GenerationParameters, NovelaiConfiguration } from '../../api/novelai';
import { undesiredContentPresetOptions } from './promptPreferences';

/** Historical preferences belong to this image, not today's global defaults. */
export function GenerationPromptOptions({ parameters, configuration, onPatch }: {
  parameters: GenerationParameters; configuration: NovelaiConfiguration;
  onPatch: (changes: Partial<GenerationParameters>) => void;
}) {
  return <div className="generation-prompt-options">
    <label className="checkbox-field"><input type="checkbox" checked={parameters.qualityToggle ?? configuration.addQualityTags ?? false}
      onChange={(event) => onPatch({ qualityToggle: event.target.checked })} /><span>本次自动质量标签</span></label>
    <label className="field"><span>本次负面内容预设</span><select aria-label="本次负面内容预设" value={parameters.ucPreset ?? configuration.undesiredContentPreset ?? 0}
      onChange={(event) => onPatch({ ucPreset: Number(event.target.value) })}>
      {undesiredContentPresetOptions.map(({ value, label }) => <option key={value} value={value}>{label}</option>)}
    </select></label>
    {(parameters.qualityToggle !== undefined || parameters.ucPreset !== undefined) && <><p className="field-hint">使用本次图片参数，不随公共绘图偏好变化。</p>
      <button type="button" onClick={() => onPatch({ qualityToggle: configuration.addQualityTags ?? false, ucPreset: configuration.undesiredContentPreset ?? 0 })}>复制当前绘图偏好</button></>}
  </div>;
}
