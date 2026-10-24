import type { RoleDraft } from '../role-management/draft';
import type { PromptTagRating } from '../../api/novelai';
import { Notice, Placeholder } from '../../ui/Status';
import { useNovelaiRoleSettings } from './useNovelaiRoleSettings';

export function NovelaiRoleSettings({ draft, onChange, disabled = false, sceneOnly = false }: {
  draft: RoleDraft; onChange: (draft: RoleDraft) => void; disabled?: boolean; sceneOnly?: boolean;
}) {
  const { configuration, models, modelLoading, error, modelError, config, patch, available, consent,
    selectedModel, dismissError, dismissModelError, refreshModels } = useNovelaiRoleSettings(draft, onChange);
  return <fieldset aria-label="角色生图许可" disabled={disabled}>
    <legend>生图</legend>
    <p>{sceneOnly ? '聊天中的表情生图由 NovelAI 插件设置统一启用，角色会自行判断是否需要调用；这里继续配置本角色的场景 CG、标签和资产写回。' : '角色可以在聊天中自行判断是否生成当前表情；图片只属于本角色和原来源。主人需要先在 NovelAI 插件设置中确认有限消费许可。'}</p>
    {error && <Notice onDismiss={dismissError}>{error}</Notice>}
    {!configuration ? <Placeholder loading>正在读取独立服务设置…</Placeholder> : !available ? <p>请先在设置中开启 NovelAI 服务并配置独立凭据。</p> : <p>账户每日最多 {configuration.dailyAttemptLimit} 次，每张最多 {configuration.maxPixels.toLocaleString()} 像素、{configuration.maxSteps} 步。</p>}
    {!sceneOnly && <><label className="field checkbox-field"><input type="checkbox" checked={config.manualEnabled}
      disabled={disabled || (!available && !config.manualEnabled)}
      onChange={(event) => patch({ manualEnabled: event.target.checked, consentRevision: 0 })} />允许本角色在聊天中自主生成表情</label>
    {config.manualEnabled && <label className="field"><span>本角色聊天表情每日最多尝试</span><input aria-label="本角色聊天表情每日最多尝试" type="number" min="1" max="100" step="1" required value={config.manualDailyAttempts}
      onChange={(event) => patch({ manualDailyAttempts: Number(event.target.value), consentRevision: 0 })} /></label>}</>}
    <label className="field checkbox-field"><input type="checkbox" checked={config.sceneCgEnabled}
      disabled={disabled || (!available && !config.sceneCgEnabled)}
      onChange={(event) => patch({ sceneCgEnabled: event.target.checked, ...(!event.target.checked ? { autoWritebackRoleAssets: false } : {}), consentRevision: 0 })} />允许主人回合场景 CG</label>
    {config.sceneCgEnabled && <>
      <p>只有真实主人输入回合完成后才判断场景。新主人输入会撤销旧候选，已出网请求仍可能计费。辅助调用本身也计入 CG 与账户尝试；未出图不表示没有费用。</p>
      <label className="field"><span>CG 辅助模型连接</span><select aria-label="CG 辅助模型连接" required value={config.scenePromptModelConfigurationId ?? ''}
        ref={(node) => node?.setCustomValidity(modelLoading || modelError || !selectedModel ? '请明确选择仍存在的 CG 辅助模型连接' : '')}
        onChange={(event) => patch({ scenePromptModelConfigurationId: event.target.value || null, consentRevision: 0 })}>
        <option value="">请选择已保存的辅助模型连接</option>
        {config.scenePromptModelConfigurationId && !selectedModel && <option value={config.scenePromptModelConfigurationId} disabled>原辅助连接已不存在（{config.scenePromptModelConfigurationId}）</option>}
        {models.map((model) => <option key={model.id} value={model.id}>{model.provider} · {model.model} · {model.id}</option>)}
      </select><small className="field-hint">请明确选择辅助连接；不会自动选择当前聊天连接。</small></label>
      {modelLoading && <Placeholder loading>正在读取 CG 辅助连接…</Placeholder>}
      {modelError && <Notice onDismiss={dismissModelError}>{modelError}</Notice>}
      <button type="button" disabled={disabled || modelLoading} onClick={refreshModels}>刷新 CG 辅助连接</button>
      <label className="field"><span>角色 CG 补充要求</span><textarea aria-label="角色 CG 补充要求" rows={5} maxLength={2000}
        value={config.sceneCgPromptInstructions} onChange={(event) => patch({ sceneCgPromptInstructions: event.target.value, consentRevision: 0 })} />
        <small className="field-hint">仅作为本角色的绘图偏好；场景 CG 会采用第三人称电影构图、让角色入镜，并排除第一人称、POV 与自拍视角。</small></label>
      <label className="field"><span>角色 CG 每日最多尝试</span><input aria-label="角色 CG 每日最多尝试" type="number" min="1" max="100" step="1" required value={config.sceneCgDailyAttempts}
        onChange={(event) => patch({ sceneCgDailyAttempts: Number(event.target.value), consentRevision: 0 })} /></label>
      <label className="field checkbox-field"><input type="checkbox" checked={config.autoWritebackRoleAssets}
        onChange={(event) => patch({ autoWritebackRoleAssets: event.target.checked, consentRevision: 0 })} />将本角色最新获准 CG 设为角色卡片图</label>
      <p>卡片图写回使用独立许可；头像保持原样。写回失败时生成图仍保留历史。</p>
    </>}
    <label className="field checkbox-field"><input type="checkbox" checked={config.promptTagsEnabled}
      onChange={(event) => patch({ promptTagsEnabled: event.target.checked, consentRevision: 0 })} />允许匹配本角色提示标签</label>
    {config.promptTagsEnabled && <>
      <p>角色聊天生图会匹配当前主人输入；预览不调用模型或生成图片。</p>
      {(['general', 'mature', 'explicit'] as PromptTagRating[]).map((rating) => <label className="field checkbox-field" key={rating}>
        <input type="checkbox" checked={config.allowedPromptTagRatings.includes(rating)}
          disabled={disabled || (config.allowedPromptTagRatings.length === 1 && config.allowedPromptTagRatings.includes(rating))}
          onChange={(event) => patch({ allowedPromptTagRatings: event.target.checked ? [...config.allowedPromptTagRatings, rating]
            : config.allowedPromptTagRatings.filter((item) => item !== rating), consentRevision: 0 })} />允许 {rating} 标签</label>)}
    </>}
    {((!sceneOnly && config.manualEnabled) || config.sceneCgEnabled) && <>
      <label className="field checkbox-field"><input type="checkbox" required checked={consent} disabled={disabled || !available}
        onChange={(event) => patch({ consentRevision: event.target.checked ? configuration!.revision : 0 })} />我确认当前设置的有限生图消费；次数不是 Anlas 估价，取消或未知结果可能已计费</label>
      {configuration && !consent && <p>请确认当前版本的有限许可后保存角色。</p>}
    </>}
  </fieldset>;
}
