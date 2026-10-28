import { GeneratedAssetActions } from './GeneratedAssetActions';
import { GenerationPromptOptions } from './GenerationPromptOptions';
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import type { Role } from '../../api/types';
import type { GenerationParameters, GenerationView } from '../../api/novelai';
import { errorMessage } from '../../api/client';
import { Avatar } from '../../ui/Avatar';
import { Icon } from '../../ui/Icon';
import { Notice, Placeholder } from '../../ui/Status';
import { PromptTagManager } from './PromptTagManager';
import { StudioProgress } from './StudioProgress';
import { StudioBaseImage } from './StudioBaseImage';
import { StudioPromptPreferences } from './StudioPromptPreferences';
import { undesiredContentPresetLabel } from './promptPreferences';
import { useGenerationDrawer } from './useGenerationDrawer';
import { statusLabels, deliveryLabels, generationErrorMessage, writebackLabels, libraryWritebackLabels } from './generationLabels';
import './novelaiStudio.css';

type Props = { roles: Role[]; initialRoleId?: string; selectedRoleId?: string; onRoleChange?: (roleId: string) => void;
  onGenerated?: () => void | Promise<void>; onOpenSettings?: () => void; settingsOpen?: boolean };

function imageUrl(roleId: string, attachmentId: string) {
  return `/api/roles/${encodeURIComponent(roleId)}/attachments/${encodeURIComponent(attachmentId)}`;
}

function dimensions(parameters: GenerationView['parameters']) {
  if (!parameters) return '';
  return `${parameters.width} × ${parameters.height}`;
}

function modeLabel(mode: 'txt2img' | 'img2img') {
  return mode === 'img2img' ? '图生图' : '文生图';
}

export function NovelaiStudio({ roles, initialRoleId, selectedRoleId, onRoleChange, onGenerated, onOpenSettings, settingsOpen = false }: Props) {
  const [localRoleId, setLocalRoleId] = useState(initialRoleId ?? roles[0]?.id ?? '');
  const roleId = selectedRoleId ?? localRoleId;
  const role = roles.find((item) => item.id === roleId) ?? roles[0] ?? null;
  if (!role) return <section className="novelai-studio" aria-label="NovelAI 图片工作台"><Placeholder>还没有可用角色。</Placeholder></section>;
  return <NovelaiStudioRole key={role.id} role={role} roles={roles} roleId={role.id}
    setRoleId={(id) => { setLocalRoleId(id); onRoleChange?.(id); }} onGenerated={onGenerated} onOpenSettings={onOpenSettings} settingsOpen={settingsOpen} />;
}

function NovelaiStudioRole({ role, roles, roleId, setRoleId, onGenerated, onOpenSettings, settingsOpen }: { role: Role; roles: Role[]; roleId: string; setRoleId: (id: string) => void; onGenerated?: () => void | Promise<void>; onOpenSettings?: () => void; settingsOpen: boolean }) {
  const [selectedAttemptId, setSelectedAttemptId] = useState<string | null>(null);
  const [selectedImageId, setSelectedImageId] = useState<string | null>(null);
  const [stageError, setStageError] = useState('');
  const [promptTagsOpen, setPromptTagsOpen] = useState(false);
  const [promptTagsVisited, setPromptTagsVisited] = useState(false);
  const [promptTab, setPromptTab] = useState<'positive' | 'negative'>('positive');
  const studio = useGenerationDrawer({ role, onClose: () => {}, onGenerated: onGenerated ?? (() => {}), defaultDeliveryMode: 'history_only',
    onGenerationAccepted: (generation) => setSelectedAttemptId(generation.attemptId) });
  const { configuration, page, parameters, setParameters, seed, setSeed, busy, uploading, error, setError,
    source, consent, setConsent, attempt, submit, upload, allowed, candidates, patch, reuseParameters, selectImage,
    watched, deliveryMode, setDeliveryMode, refresh, scope, isCurrent, clearSource, setAccepted, refreshConfiguration, configurationRefreshing, unavailableMessage } = studio;
  const txt2imgAllowed = configuration?.allowTxt2Img !== false;
  const img2imgAllowed = configuration?.allowImg2Img !== false;
  const modeAllowed = studio.modeAllowed;
  const channelRegeneration = source?.generation.deliveryMessageId?.startsWith('thread:');
  const replacementLabel = source ? channelRegeneration ? '向原渠道再次发送媒体' : '替换原会话选中图片' : '交付到当前角色会话';
  const previousSettingsOpen = useRef(settingsOpen);
  useEffect(() => {
    const closed = previousSettingsOpen.current && !settingsOpen;
    previousSettingsOpen.current = settingsOpen;
    if (closed) void refreshConfiguration();
  }, [settingsOpen, refreshConfiguration]);

  useEffect(() => {
    if (role.id !== roleId) setRoleId(role.id);
    setSelectedAttemptId(null);
    setStageError('');
  }, [role?.id]);

  const records = useMemo(() => page?.generations.filter((item) => item.roleId === role?.id) ?? [], [page, role?.id]);
  const selected = records.find((item) => item.attemptId === selectedAttemptId) ?? records.find((item) => item.attachments.length > 0) ?? records[0] ?? null;
  const selectedImages = selected?.attachments.filter((item) => item.kind === 'image') ?? [];
  const selectedImage = selectedImages.find((image) => image.id === selectedImageId) ?? selectedImages[0] ?? null;
  const historicalImages = records.flatMap((item) => item.attachments.filter((attachment) => attachment.kind === 'image'));
  const allCandidates = [...candidates, ...historicalImages].filter((item, index, all) => all.findIndex((other) => other.id === item.id) === index);
  const inputCandidates = parameters.mode === 'img2img' && parameters.base_attachment_id
    && !allCandidates.some((item) => item.id === parameters.base_attachment_id)
    ? [...allCandidates, { id: parameters.base_attachment_id, name: '历史底图（需仍可访问）', kind: 'image' as const, mediaType: 'image/*', size: 0 }]
    : allCandidates;

  const selectRecord = (record: GenerationView) => {
    if (busy || uploading || Boolean(attempt.current)) return;
    clearSource();
    setSelectedAttemptId(record.attemptId);
    setSelectedImageId(null);
    setStageError('');
  };
  const handleReuseParameters = (record: GenerationView) => {
    if (!record.parameters) {
      setStageError('这条记录没有可复用的图片参数。');
      return;
    }
    reuseParameters(record);
    setStageError('');
  };
  const submitForm = (event: FormEvent) => { event.preventDefault(); void submit(event); };
  const changeMode = (mode: GenerationParameters['mode']) => {
    const { base_attachment_id: _base, strength: _strength, noise: _noise, ...rest } = parameters;
    setParameters(mode === 'img2img' ? { ...rest, mode, strength: .7, noise: .2 } : { ...rest, mode });
  };

  return <section className="novelai-studio" aria-label="NovelAI 图片工作台">
    <header className="novelai-studio-header">
      <div className="studio-workspace-title"><Icon name={promptTagsOpen ? 'cards' : 'image'} size={20} /><h1>{promptTagsOpen ? '提示词库' : '图片工作台'}</h1></div>
      <div className="studio-header-controls">
        {onOpenSettings && <button type="button" className="studio-tags-button" onClick={onOpenSettings}>NovelAI 设置</button>}
        {promptTagsOpen ? <button type="button" className="studio-tags-button" onClick={() => setPromptTagsOpen(false)}><Icon name="back" size={17} /><span>返回图片工作台</span></button>
          : <button type="button" className="studio-tags-button" onClick={() => { setPromptTagsVisited(true); setPromptTagsOpen(true); }}><Icon name="cards" size={17} /><span>管理提示标签</span></button>}
      </div>
    </header>
    {(error || stageError) && <Notice onDismiss={() => { setError(''); setStageError(''); }}>{error || stageError}</Notice>}
    <div className="novelai-studio-grid" hidden={promptTagsOpen}>
      <form className="novelai-prompt-panel" aria-label="工作台生成设置" noValidate onSubmit={submitForm}>
        <div className="studio-panel-scroll">
        <div className="studio-role-bar"><Avatar id={role.id} name={role.name} avatarUrl={role.avatarUrl} />
          <label className="studio-role-picker"><select aria-label="工作台角色" value={role.id} disabled={busy || uploading} onChange={(event) => setRoleId(event.target.value)}>
            {roles.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select></label>
          {configuration && <StudioPromptPreferences configuration={configuration} disabled={busy || uploading || configurationRefreshing || Boolean(attempt.current)} onSave={refreshConfiguration} />}
        </div>
        {!configuration && !error && <Placeholder loading>正在加载生图设置…</Placeholder>}
        {configurationRefreshing && <p role="status">正在同步生图设置…</p>}
        {configuration && <>
          {allowed && <p className="muted" role="status">{unavailableMessage}</p>}
          {!allowed && <div className="studio-policy"><span>{unavailableMessage}</span>
            {onOpenSettings && <button type="button" onClick={onOpenSettings}>打开 NovelAI 设置</button>}</div>}
          {source && <div className="studio-policy"><span>将基于 {source.image.name} 再次生成；本次会重新消费一次尝试额度。</span>
            {deliveryMode === 'history_only' ? <span>仅保存历史不会向原渠道或会话发送媒体。</span>
              : channelRegeneration && <span>本次重生成会向原渠道再次发送一条新的媒体消息。</span>}
            <label className="checkbox-field"><input type="checkbox" checked={consent} disabled={busy || uploading || Boolean(attempt.current)} onChange={(event) => setConsent(event.target.checked)} />我确认本次重生成可能产生费用</label></div>}
          <fieldset disabled={busy || uploading || !allowed || Boolean(attempt.current)}>
            <div className="studio-prompt-tabs" role="tablist" aria-label="提示词类型">
              <button type="button" role="tab" id="positive-prompt-tab" aria-controls="studio-prompt-editor" aria-selected={promptTab === 'positive'} onClick={() => setPromptTab('positive')}>正向提示词</button>
              <button type="button" role="tab" id="negative-prompt-tab" aria-controls="studio-prompt-editor" aria-selected={promptTab === 'negative'} onClick={() => setPromptTab('negative')}>负向提示词{parameters.negative_prompt?.trim() && <span className="studio-tab-dot" aria-hidden="true" />}</button>
            </div>
            <div id="studio-prompt-editor" className="studio-prompt-editor" role="tabpanel" aria-labelledby={promptTab === 'positive' ? 'positive-prompt-tab' : 'negative-prompt-tab'}>
              {promptTab === 'positive'
                ? <textarea aria-label="工作台英文正向 tags" required maxLength={4000} placeholder="1girl, library, sunset light" value={parameters.prompt} onChange={(event) => patch({ prompt: event.target.value })} />
                : <textarea aria-label="工作台英文负向 tags" maxLength={4000} placeholder="lowres, bad hands, blurry" value={parameters.negative_prompt ?? ''} onChange={(event) => patch({ negative_prompt: event.target.value })} />}
            </div>
            {!modeAllowed && <p role="status" className="field-hint">当前选择的生图模式已被服务设置关闭，请切换到已启用的模式。</p>}
            {!txt2imgAllowed && !img2imgAllowed && <p role="status" className="field-hint">文生图和图生图都已关闭，当前不能提交新的生图请求。</p>}
            <StudioBaseImage roleId={role.id} parameters={parameters} candidates={inputCandidates} disabled={busy || uploading || !allowed || !img2imgAllowed || Boolean(attempt.current)}
              onPatch={patch} onUpload={(file) => void upload(file)} onRemove={() => changeMode(txt2imgAllowed ? 'txt2img' : 'img2img')} />
            <label className="field"><span>图片尺寸</span><select aria-label="工作台图片尺寸" value={parameters.size_preset} onChange={(event) => {
              const { custom_width: _width, custom_height: _height, ...rest } = parameters;
              setParameters({ ...rest, size_preset: event.target.value as GenerationParameters['size_preset'], ...(event.target.value === 'custom' ? { custom_width: 64, custom_height: 64 } : {}) });
            }}><option value="square">方形 1024 × 1024</option><option value="landscape">横图 1216 × 832</option><option value="portrait">竖图 832 × 1216</option><option value="custom">自定义</option></select></label>
            {parameters.size_preset === 'custom' && <div className="studio-size-fields">{(['custom_width', 'custom_height'] as const).map((name) => <label className="field" key={name}><span>{name === 'custom_width' ? '宽度' : '高度'}</span><input aria-label={name === 'custom_width' ? '工作台生图宽度' : '工作台生图高度'} type="number" min="64" max="1024" step="64" required value={parameters[name]} onChange={(event) => patch({ [name]: Number(event.target.value) })} /></label>)}</div>}
            <details className="studio-advanced"><summary>高级参数</summary><div className="studio-advanced-fields">
            <label className="field"><span>生成模式</span><select aria-label="工作台生成模式" value={parameters.mode} onChange={(event) => changeMode(event.target.value as GenerationParameters['mode'])}>
              <option value="txt2img" disabled={!txt2imgAllowed}>文生图{!txt2imgAllowed ? '（服务已关闭）' : ''}</option><option value="img2img" disabled={!img2imgAllowed}>图生图{!img2imgAllowed ? '（服务已关闭）' : ''}</option>
            </select></label>
            <label className="field"><span>模型</span><select aria-label="工作台生图模型" required value={configuration.allowedModels.includes(parameters.model ?? '') ? parameters.model : ''} onChange={(event) => patch({ model: event.target.value })}><option value="">选择当前允许的模型</option>{configuration.allowedModels.map((item) => <option key={item}>{item}</option>)}</select></label>
            <label className="field"><span>采样器</span><select aria-label="工作台采样器" required value={configuration.allowedSamplers.includes(parameters.sampler ?? '') ? parameters.sampler : ''} onChange={(event) => patch({ sampler: event.target.value })}><option value="">选择当前允许的采样器</option>{configuration.allowedSamplers.map((item) => <option key={item}>{item}</option>)}</select></label>
            <label className="field"><span>采样步数</span><input aria-label="工作台采样步数" type="number" min="1" max={configuration.maxSteps} step="1" required value={parameters.steps} onChange={(event) => patch({ steps: Number(event.target.value) })} /></label>
            <label className="field"><span>生成张数</span><select aria-label="工作台生成张数" value={parameters.samples ?? 1} onChange={(event) => patch({ samples: Number(event.target.value) })}>
              {[1, 2, 3, 4].map((count) => <option value={count} key={count}>{count} 张</option>)}
            </select></label>
            <GenerationPromptOptions parameters={parameters} configuration={configuration} onPatch={patch} />
            <label className="field"><span>Seed</span><input aria-label="工作台 Seed" type="number" min="0" max="4294967295" step="1" value={seed} onChange={(event) => setSeed(event.target.value)} /></label>
            <label className="field"><span>结果交付</span><select aria-label="工作台结果交付" value={deliveryMode} onChange={(event) => { setDeliveryMode(event.target.value as typeof deliveryMode); setConsent(false); }}><option value="history_only">仅保存在工作台历史</option><option value="replacement">{replacementLabel}</option></select></label>
            {source && deliveryMode === 'replacement' && (parameters.samples ?? 1) > 1 && <p role="status" className="field-hint">替换单张历史图片时只能生成一张；多张结果请选择仅保存历史。</p>}
            <label className="checkbox-field"><input type="checkbox" checked={parameters.background ?? false} onChange={(event) => patch({ background: event.target.checked })} /><span>{deliveryMode === 'history_only' ? '后台生成，结果仅保存在历史' : `后台生成，完成后${replacementLabel}`}</span></label>
            </div></details>
          </fieldset>
        </>}
        </div>
        {configuration && <div className="studio-panel-footer">
          <div className="form-actions studio-actions"><button type="submit" className="primary" disabled={busy || uploading || !allowed || !modeAllowed || Boolean(source && !consent)}>{busy ? '正在生成…' : attempt.current ? '重查本次生成' : source ? '确认并重新生成' : (parameters.samples ?? 1) === 1 ? '生成一张图片' : `生成 ${parameters.samples} 张图片`}</button>
            {attempt.current && !busy && <button type="button" onClick={() => { studio.attempt.current = null; setConsent(false); setAccepted(false); setError(''); setSeed(''); setParameters({ ...parameters }); }}>另开一次生成</button>}</div>
          <p className="studio-usage">今日账户尝试 {page?.usage.accountAttempts ?? 0}/{configuration.dailyAttemptLimit}，本角色手动尝试 {page?.usage.manualAttempts ?? 0}/{configuration.manualDailyAttempts ?? 10}。</p>
        </div>}
      </form>
      <div className="novelai-stage-column">
        <div className="novelai-stage" aria-label="当前作品" aria-busy={busy || Boolean(selected && ['prepared', 'dispatching', 'received', 'local_pending'].includes(selected.status))}>
          {(busy || (selected && ['prepared', 'dispatching', 'received', 'local_pending'].includes(selected.status))) && <StudioProgress key={selected?.attemptId ?? 'submitting'} />}
          {selectedImages.length > 0 && role ? <div className="novelai-stage-gallery" aria-label={`当前作品 ${selectedImages.length} 张图片`}>{selectedImages.map((image) => <a href={imageUrl(role.id, image.id)} target="_blank" rel="noreferrer" key={image.id}><img src={imageUrl(role.id, image.id)} alt={image.name} /></a>)}</div>
            : selected ? <div className="studio-stage-status"><Icon name="image" size={34} /><strong>{statusLabels[selected.status] ?? selected.status}</strong><span>{generationErrorMessage(selected)}</span></div>
            : page ? <div className="studio-stage-status"><Icon name="image" size={34} /><strong>选择一张历史作品</strong><span>生成完成后，图片会出现在这里。</span></div>
            : error ? <div className="studio-stage-status" role="status"><Icon name="image" size={34} /><strong>作品读取失败</strong><span>{error}</span></div>
            : <Placeholder loading>正在加载作品…</Placeholder>}
        </div>
        {selected?.parameters && <div className="studio-stage-summary">
              <div className="studio-stage-facts"><span>{dimensions(selected.parameters)}</span><span>{modeLabel(selected.parameters.mode)}</span><span>{selected.parameters.samples ?? 1} 张</span><span>{selected.parameters.model ?? '模型未记录'}</span><span>{selected.parameters.sampler ?? '采样器未记录'} · {selected.parameters.steps ?? '未知'} 步</span><span>Seed {selected.parameters.seed}</span></div>
              <div className="studio-stage-actions">
                {selectedImages.length > 1 && <label className="studio-role-picker"><span>当前图片</span><select aria-label="当前作品图片" value={selectedImage?.id ?? ''} disabled={busy || uploading || Boolean(attempt.current)} onChange={(event) => { setSelectedImageId(event.target.value); clearSource(); }}>
                  {selectedImages.map((image, index) => <option key={image.id} value={image.id}>{index + 1}. {image.name}</option>)}
                </select></label>}
                {selectedImage && <a href={imageUrl(role.id, selectedImage.id)} download={selectedImage.name}>下载</a>}
                <button type="button" disabled={busy || uploading || Boolean(attempt.current)} onClick={() => handleReuseParameters(selected)}>复用参数</button>
                {selectedImage && <button type="button" disabled={busy || uploading || Boolean(attempt.current)} onClick={() => selectImage(selected, selectedImage)}>重新生成</button>}
                {selectedImage && <GeneratedAssetActions key={selectedImage.id} roleId={role.id} attachmentId={selectedImage.id} disabled={busy || uploading} />}
              </div>
            </div>}
        {selected && <details className="studio-image-info">
          <summary>图片信息</summary>
        {selected && <div className="studio-stage-meta" role="status" aria-label="当前作品状态">
          <p>生成：{statusLabels[selected.status] ?? selected.status}</p>
          <p>投递：{deliveryLabels[selected.deliveryStatus] ?? '尚未确认'}</p>
          <p>消费：{selected.consumption === 'possible' ? '已出网，可能计费；未确认实际扣费。' : selected.consumption === 'not_dispatched' ? '生成请求尚未出网。' : '消费状态尚未确认。'}</p>
          {selected.purpose === 'scene_cg' && <p>{selected.auxiliaryConsumption === 'possible' ? '辅助请求已出网，可能计费；未出图仍计入 CG 与账户尝试。' : selected.auxiliaryConsumption === 'not_dispatched' ? '辅助请求尚未出网。' : '辅助消费状态尚未确认。'}</p>}
          {selected.writebackStatus && (selected.purpose === 'scene_cg' ? writebackLabels : libraryWritebackLabels)[selected.writebackStatus] && <p>{(selected.purpose === 'scene_cg' ? writebackLabels : libraryWritebackLabels)[selected.writebackStatus]}</p>}
          {selected.errorCode && <p>{generationErrorMessage(selected)}</p>}
        </div>}
        {selected?.parameters && <section className="studio-stage-meta" aria-label="当前作品参数">
            <div className="studio-full-parameters">
              <dl className="studio-parameter-list">
                <div className="studio-parameter-wide"><dt>正向 tags</dt><dd>{selected.parameters.prompt || '（空）'}</dd></div>
                <div className="studio-parameter-wide"><dt>负向 tags</dt><dd>{selected.parameters.negative_prompt || '（空）'}</dd></div>
                <div><dt>生成模式</dt><dd>{modeLabel(selected.parameters.mode)}</dd></div>
                <div><dt>模型</dt><dd>{selected.parameters.model ?? '未记录'}</dd></div>
                <div><dt>采样器</dt><dd>{selected.parameters.sampler ?? '未记录'}</dd></div>
                <div><dt>采样步数</dt><dd>{selected.parameters.steps ?? '未记录'}</dd></div>
                <div><dt>生成张数</dt><dd>{selected.parameters.samples ?? 1}</dd></div>
                <div><dt>Seed</dt><dd>{selected.parameters.seed}</dd></div>
                <div><dt>自动质量标签</dt><dd>{selected.parameters.qualityToggle === undefined ? '未记录' : selected.parameters.qualityToggle ? '开启' : '关闭'}</dd></div>
                <div><dt>负向预设</dt><dd>{undesiredContentPresetLabel(selected.parameters.ucPreset)}</dd></div>
                {selected.parameters.mode === 'img2img' && <>
                  <div><dt>图生图底图</dt><dd>{allCandidates.find((item) => item.id === selected.parameters?.base_attachment_id)?.name ?? selected.parameters.base_attachment_id ?? '未设置'}</dd></div>
                  <div><dt>图生图强度</dt><dd>{selected.parameters.strength ?? '未记录'}</dd></div>
                  <div><dt>图生图噪声</dt><dd>{selected.parameters.noise ?? '未记录'}</dd></div>
                </>}
                <div><dt>后台生成</dt><dd>{selected.parameters.background ? '是' : '否'}</dd></div>
              </dl>
            </div>
        </section>}
        </details>}
        <nav className="novelai-filmstrip" aria-label="历史作品">
          <div className="filmstrip-heading"><h2>历史</h2><span>{records.length} 条</span></div>
          <div className="filmstrip-scroll">{records.length ? records.map((record) => {
            const preview = record.attachments.find((item) => item.kind === 'image');
            return <button type="button" key={record.attemptId} disabled={busy || uploading || Boolean(attempt.current)} className={record.attemptId === selected?.attemptId ? 'filmstrip-item selected' : 'filmstrip-item'} aria-label={`选择生成记录 ${record.attemptId}`} aria-pressed={record.attemptId === selected?.attemptId} onClick={() => selectRecord(record)}>
              {preview ? <img src={imageUrl(role.id, preview.id)} alt="" /> : <span>{statusLabels[record.status] ?? record.status}</span>}
            </button>;
          }) : page ? <span className="muted">暂无生图历史。</span> : error ? <span role="status">历史读取失败</span> : <span className="muted">正在加载…</span>}</div>
          {page?.nextCursor && <button type="button" className="filmstrip-more" disabled={busy || uploading} onClick={() => {
            const owner = scope.current;
            void refresh(role.id, page.nextCursor!, owner).catch((cause) => { if (isCurrent(owner)) setError(errorMessage(cause, '历史加载失败')); });
          }}>加载更多</button>}
        </nav>
      </div>
    </div>
    {watched.length > 0 && <p className="studio-background-status" role="status">有 {watched.length} 条生成或交付仍在处理中，历史会自动更新。</p>}
    {promptTagsVisited && <div className="studio-tag-page" hidden={!promptTagsOpen} aria-label={`${role.name}的提示标签库`}>
      <PromptTagManager key={role.id} roleId={role.id} expanded />
    </div>}
  </section>;
}
