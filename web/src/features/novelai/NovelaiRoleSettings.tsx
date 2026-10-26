import type { RoleDraft } from '../role-management/draft';
import type { PromptTagRating } from '../../api/novelai';
import { Notice, Placeholder } from '../../ui/Status';
import { useNovelaiRoleSettings } from './useNovelaiRoleSettings';

export function NovelaiRoleSettings({ draft, onChange, disabled = false }: {
  draft: RoleDraft; onChange: (draft: RoleDraft) => void; disabled?: boolean;
}) {
  const { configuration, models, modelLoading, error, modelError, config, patch, available,
    selectedModel, dismissError, dismissModelError, refreshModels } = useNovelaiRoleSettings(draft, onChange);
  return <fieldset aria-label="角色生图许可" disabled={disabled}>
    <legend>生图</legend>
    {error && <Notice onDismiss={dismissError}>{error}</Notice>}
    {!configuration ? <Placeholder loading>正在读取独立服务设置…</Placeholder> : !available ? <p>请先在设置中开启 NovelAI 服务并配置独立凭据。</p> : <p>账户每日最多 {configuration.dailyAttemptLimit} 次，每张最多 {configuration.maxPixels.toLocaleString()} 像素、{configuration.maxSteps} 步。</p>}
    <p className="field-hint">聊天和手动生图由插件设置统一启用。自动场景 CG 需在此开启，并选择辅助模型。</p>
    <label className="field checkbox-field"><input type="checkbox" checked={config.sceneCgEnabled}
      disabled={disabled || (!available && !config.sceneCgEnabled)}
      onChange={(event) => patch({ sceneCgEnabled: event.target.checked, ...(!event.target.checked ? { autoWritebackRoleAssets: false } : {}), consentRevision: 0 })} />允许主人回合场景 CG</label>
    {config.sceneCgEnabled && <>
      <label className="field"><span>CG 辅助模型连接</span><select aria-label="CG 辅助模型连接" required value={config.scenePromptModelConfigurationId ?? ''}
        ref={(node) => node?.setCustomValidity(modelLoading || modelError || !selectedModel ? '请明确选择仍存在的 CG 辅助模型连接' : '')}
        onChange={(event) => patch({ scenePromptModelConfigurationId: event.target.value || null, consentRevision: 0 })}>
        <option value="">请选择已保存的辅助模型连接</option>
        {config.scenePromptModelConfigurationId && !selectedModel && <option value={config.scenePromptModelConfigurationId} disabled>原辅助连接已不存在（{config.scenePromptModelConfigurationId}）</option>}
        {models.map((model) => <option key={model.id} value={model.id}>{model.provider} · {model.model} · {model.id}</option>)}
      </select><small className="field-hint">辅助调用也计入额度，未出图可能计费。</small></label>
      {modelLoading && <Placeholder loading>正在读取 CG 辅助连接…</Placeholder>}
      {modelError && <Notice onDismiss={dismissModelError}>{modelError}</Notice>}
      <button type="button" disabled={disabled || modelLoading} onClick={refreshModels}>刷新 CG 辅助连接</button>
      <label className="field"><span>角色 CG 补充要求</span><textarea aria-label="角色 CG 补充要求" rows={5} maxLength={2000} placeholder="画面风格、服装或构图偏好"
        value={config.sceneCgPromptInstructions} onChange={(event) => patch({ sceneCgPromptInstructions: event.target.value, consentRevision: 0 })} /></label>
      <label className="field"><span>角色 CG 每日最多尝试</span><input aria-label="角色 CG 每日最多尝试" type="number" min="1" max="100" step="1" required value={config.sceneCgDailyAttempts}
        onChange={(event) => patch({ sceneCgDailyAttempts: Number(event.target.value), consentRevision: 0 })} /></label>
      <label className="field checkbox-field"><input type="checkbox" checked={config.autoWritebackRoleAssets}
        onChange={(event) => patch({ autoWritebackRoleAssets: event.target.checked, consentRevision: 0 })} />将本角色最新获准 CG 设为角色卡片图</label>
    </>}
    <label className="field checkbox-field"><input type="checkbox" checked={config.promptTagsEnabled}
      onChange={(event) => patch({ promptTagsEnabled: event.target.checked, consentRevision: 0 })} />允许匹配本角色提示标签</label>
    {config.promptTagsEnabled && <>
      {(['general', 'mature', 'explicit'] as PromptTagRating[]).map((rating) => <label className="field checkbox-field" key={rating}>
        <input type="checkbox" checked={config.allowedPromptTagRatings.includes(rating)}
          disabled={disabled || (config.allowedPromptTagRatings.length === 1 && config.allowedPromptTagRatings.includes(rating))}
          onChange={(event) => patch({ allowedPromptTagRatings: event.target.checked ? [...config.allowedPromptTagRatings, rating]
            : config.allowedPromptTagRatings.filter((item) => item !== rating), consentRevision: 0 })} />允许 {rating} 标签</label>)}
    </>}
    {config.sceneCgEnabled && <p className="field-hint">启用 CG 后会自动观察主人回合并按需生成图片；辅助模型和生图可能计费，受每日次数上限约束。</p>}
  </fieldset>;
}
