import { useState } from 'react';
import { novelai, type NovelaiConfiguration } from '../../api/novelai';
import { errorMessage } from '../../api/client';
import { Notice } from '../../ui/Status';

export function StudioGenerationPreferences({ configuration, onSaved }: {
  configuration: NovelaiConfiguration;
  onSaved: (configuration: NovelaiConfiguration) => void;
}) {
  const [addQualityTags, setAddQualityTags] = useState(configuration.addQualityTags ?? false);
  const [undesiredContentPreset, setUndesiredContentPreset] = useState(configuration.undesiredContentPreset ?? 0);
  const [confirmManualConsumption, setConfirmManualConsumption] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  const save = async () => {
    if (pending) return;
    setPending(true);
    setError('');
    setMessage('');
    try {
      const saved = await novelai.saveConfiguration({
        addQualityTags,
        undesiredContentPreset,
        confirmManualConsumption,
      }, configuration.revision);
      onSaved(saved);
      setMessage(configuration.manualEnabled && confirmManualConsumption
        ? '工作台生成偏好已保存；当前手动许可已按新配置确认。'
        : '工作台生成偏好已保存；手动生图许可需要在设置中重新确认。');
    } catch (cause) {
      setError(errorMessage(cause, '工作台生成偏好保存失败'));
    } finally {
      setPending(false);
    }
  };

  return <section className="studio-preferences" aria-label="生成偏好编辑">
    <p>这些服务级偏好会用于之后的手动生图和场景 CG。已有历史保留各自生成时的参数。</p>
    <label className="field checkbox-field"><input type="checkbox" aria-label="自动添加质量标签" checked={addQualityTags}
      disabled={pending} onChange={(event) => setAddQualityTags(event.target.checked)} />自动添加质量标签</label>
    <label className="field"><span>负向预设</span><select aria-label="负向预设" value={undesiredContentPreset} disabled={pending}
      onChange={(event) => setUndesiredContentPreset(Number(event.target.value))}>
      <option value={0}>不使用（0）</option><option value={1}>轻度（1）</option><option value={2}>重度（2）</option>
    </select></label>
    {configuration.manualEnabled && <label className="checkbox-field"><input type="checkbox"
      aria-label="我确认当前有限生图许可可用于这些设置" checked={confirmManualConsumption} disabled={pending
        || !configuration.enabled || !configuration.tokenConfigured || configuration.credentialCleanupPending}
      onChange={(event) => setConfirmManualConsumption(event.target.checked)} />我确认当前有限生图许可可用于这些设置</label>}
    {configuration.manualEnabled && !confirmManualConsumption && <p className="field-hint">
      保存会更新服务版本并使现有手动许可失效；若要继续使用手动生图，请在保存时确认当前许可。
    </p>}
    {error && <Notice>{error}</Notice>}{message && <Notice tone="success">{message}</Notice>}
    <button type="button" className="primary" disabled={pending} onClick={() => void save()}>
      {pending ? '正在保存…' : '保存生成偏好'}
    </button>
  </section>;
}
