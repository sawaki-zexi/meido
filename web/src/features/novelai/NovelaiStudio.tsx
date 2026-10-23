import { useEffect, useMemo, useState, type FormEvent } from 'react';
import type { Role } from '../../api/types';
import type { GenerationParameters, GenerationView } from '../../api/novelai';
import { errorMessage } from '../../api/client';
import { Avatar } from '../../ui/Avatar';
import { Dialog } from '../../ui/Dialog';
import { Icon } from '../../ui/Icon';
import { Notice, Placeholder } from '../../ui/Status';
import { PromptTagManager } from './PromptTagManager';
import { useGenerationDrawer } from './useGenerationDrawer';

const statusLabels: Record<string, string> = {
  prepared: '等待生成', dispatching: '生成中', received: '已收图，等待保存', succeeded: '已生成',
  unknown: '消费结果不确定', failed_response: '远端返回错误', local_pending: '本地保存待恢复',
  cancelled_before_dispatch: '出网前已取消', rejected: '未准入', completed_without_image: '没有图片',
};
const errorLabels: Record<string, string> = {
  response_not_verifiable: '无法验证远端图片；本次不会重试。', local_media_pending: '图片保留在本地恢复流程。',
  transport_unknown: '远端结果尚不能确认；原请求不会再次发送。', scene_cooldown: '当前场景仍在冷却期。',
  scene_visual_duplicate: '此画面已处理，本次未重复生成。', scene_observation_failed: '场景观察未完成，辅助请求可能已计费。',
};

type Props = { roles: Role[]; initialRoleId?: string; selectedRoleId?: string; onRoleChange?: (roleId: string) => void;
  onGenerated?: () => void | Promise<void>; onOpenSettings?: () => void };

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

export function NovelaiStudio({ roles, initialRoleId, selectedRoleId, onRoleChange, onGenerated, onOpenSettings }: Props) {
  const [localRoleId, setLocalRoleId] = useState(initialRoleId ?? roles[0]?.id ?? '');
  const roleId = selectedRoleId ?? localRoleId;
  const role = roles.find((item) => item.id === roleId) ?? roles[0] ?? null;
  if (!role) return <section className="novelai-studio" aria-label="NovelAI 图片工作台"><Placeholder>还没有可用角色。</Placeholder></section>;
  return <NovelaiStudioRole key={role.id} role={role} roles={roles} roleId={role.id}
    setRoleId={(id) => { setLocalRoleId(id); onRoleChange?.(id); }} onGenerated={onGenerated} onOpenSettings={onOpenSettings} />;
}

function NovelaiStudioRole({ role, roles, roleId, setRoleId, onGenerated, onOpenSettings }: { role: Role; roles: Role[]; roleId: string; setRoleId: (id: string) => void; onGenerated?: () => void | Promise<void>; onOpenSettings?: () => void }) {
  const [selectedAttemptId, setSelectedAttemptId] = useState<string | null>(null);
  const [stageError, setStageError] = useState('');
  const [promptTagsOpen, setPromptTagsOpen] = useState(false);
  const studio = useGenerationDrawer({ role, onClose: () => {}, onGenerated: onGenerated ?? (() => {}), defaultDeliveryMode: 'history_only',
    onGenerationAccepted: (generation) => setSelectedAttemptId(generation.attemptId) });
  const { configuration, page, parameters, setParameters, seed, setSeed, busy, uploading, error, setError,
    source, consent, setConsent, attempt, submit, upload, allowed, candidates, patch, reuseParameters, selectImage,
    watched, deliveryMode, setDeliveryMode, refresh, scope, isCurrent, clearSource, setAccepted } = studio;

  useEffect(() => {
    if (role.id !== roleId) setRoleId(role.id);
    setSelectedAttemptId(null);
    setStageError('');
  }, [role?.id]);

  const records = useMemo(() => page?.generations.filter((item) => item.roleId === role?.id) ?? [], [page, role?.id]);
  const selected = records.find((item) => item.attemptId === selectedAttemptId) ?? records.find((item) => item.attachments.length > 0) ?? records[0] ?? null;
  const selectedImage = selected?.attachments.find((item) => item.kind === 'image') ?? null;
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

  return <section className="novelai-studio" aria-label="NovelAI 图片工作台">
    <header className="novelai-studio-header">
      <div>
        <p className="eyebrow">NovelAI</p>
        <h1>图片工作台</h1>
        <p className="muted">在角色范围内浏览作品、复用参数并开始新的生成。</p>
      </div>
      <div className="studio-header-controls">
        <button type="button" className="studio-tags-button" onClick={() => setPromptTagsOpen(true)}><Icon name="cards" size={17} /><span>管理提示标签</span></button>
        <label className="studio-role-picker"><span>当前角色</span><select aria-label="工作台角色" value={role.id} disabled={busy || uploading} onChange={(event) => setRoleId(event.target.value)}>
          {roles.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select></label>
      </div>
    </header>
    {(error || stageError) && <Notice onDismiss={() => { setError(''); setStageError(''); }}>{error || stageError}</Notice>}
    <div className="novelai-studio-grid">
      <form className="novelai-prompt-panel" aria-label="工作台生成设置" onSubmit={submitForm}>
        <div className="studio-panel-heading"><div><h2>生成设置</h2><p className="muted">提交仍需满足当前服务、角色许可和有限额度。</p></div><Avatar id={role.id} name={role.name} avatarUrl={role.avatarUrl} /></div>
        {!configuration && !error && <Placeholder loading>正在加载生图设置…</Placeholder>}
        {configuration && <>
          {!allowed && <div className="studio-policy"><span>手动生图尚未获准，请先完成独立服务、凭据和有限消费确认。</span>
            {onOpenSettings && <button type="button" onClick={onOpenSettings}>打开 NovelAI 设置</button>}</div>}
          {source && <div className="studio-policy"><span>将基于 {source.image.name} 再次生成；本次会重新消费一次尝试额度。</span>
            <label className="checkbox-field"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />我确认本次重生成可能产生费用</label></div>}
          <fieldset disabled={busy || uploading || !allowed || Boolean(attempt.current)}>
            <label className="field"><span>英文正向 tags</span><textarea aria-label="工作台英文正向 tags" required maxLength={4000} value={parameters.prompt} onChange={(event) => patch({ prompt: event.target.value })} /></label>
            <label className="field"><span>英文负向 tags</span><textarea aria-label="工作台英文负向 tags" maxLength={4000} value={parameters.negative_prompt ?? ''} onChange={(event) => patch({ negative_prompt: event.target.value })} /></label>
            <label className="field"><span>生成模式</span><select aria-label="工作台生成模式" value={parameters.mode} onChange={(event) => {
              const { base_attachment_id: _base, strength: _strength, noise: _noise, ...rest } = parameters;
              setParameters(event.target.value === 'img2img' ? { ...rest, mode: 'img2img', strength: .7, noise: .2 } : { ...rest, mode: 'txt2img' });
            }}><option value="txt2img">文生图</option><option value="img2img">图生图</option></select></label>
            {parameters.mode === 'img2img' && <>
              <label className="field"><span>本角色底图</span><select aria-label="工作台本角色底图" required value={parameters.base_attachment_id ?? ''} onChange={(event) => patch({ base_attachment_id: event.target.value })}><option value="">选择底图</option>{inputCandidates.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
              <label className="field"><span>上传底图（最多 10 MiB）</span><input aria-label="工作台上传生图底图" type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => { void upload(event.target.files?.[0]); event.target.value = ''; }} /></label>
              <label className="field"><span>图生图强度</span><input type="number" aria-label="工作台图生图强度" min="0.01" max="1" step="0.01" required value={parameters.strength} onChange={(event) => patch({ strength: Number(event.target.value) })} /></label>
              <label className="field"><span>图生图噪声</span><input type="number" aria-label="工作台图生图噪声" min="0" max="0.99" step="0.01" required value={parameters.noise} onChange={(event) => patch({ noise: Number(event.target.value) })} /></label>
            </>}
            <label className="field"><span>图片尺寸</span><select aria-label="工作台图片尺寸" value={parameters.size_preset} onChange={(event) => {
              const { custom_width: _width, custom_height: _height, ...rest } = parameters;
              setParameters({ ...rest, size_preset: event.target.value as GenerationParameters['size_preset'], ...(event.target.value === 'custom' ? { custom_width: 64, custom_height: 64 } : {}) });
            }}><option value="square">方形 1024 × 1024</option><option value="landscape">横图 1216 × 832</option><option value="portrait">竖图 832 × 1216</option><option value="custom">自定义</option></select></label>
            {parameters.size_preset === 'custom' && <div className="studio-size-fields">{(['custom_width', 'custom_height'] as const).map((name) => <label className="field" key={name}><span>{name === 'custom_width' ? '宽度' : '高度'}</span><input aria-label={name === 'custom_width' ? '工作台生图宽度' : '工作台生图高度'} type="number" min="64" max="1024" step="64" required value={parameters[name]} onChange={(event) => patch({ [name]: Number(event.target.value) })} /></label>)}</div>}
            <label className="field"><span>模型</span><select aria-label="工作台生图模型" required value={configuration.allowedModels.includes(parameters.model ?? '') ? parameters.model : ''} onChange={(event) => patch({ model: event.target.value })}><option value="">选择当前允许的模型</option>{configuration.allowedModels.map((item) => <option key={item}>{item}</option>)}</select></label>
            <label className="field"><span>采样器</span><select aria-label="工作台采样器" required value={configuration.allowedSamplers.includes(parameters.sampler ?? '') ? parameters.sampler : ''} onChange={(event) => patch({ sampler: event.target.value })}><option value="">选择当前允许的采样器</option>{configuration.allowedSamplers.map((item) => <option key={item}>{item}</option>)}</select></label>
            <label className="field"><span>采样步数</span><input aria-label="工作台采样步数" type="number" min="1" max={configuration.maxSteps} step="1" required value={parameters.steps} onChange={(event) => patch({ steps: Number(event.target.value) })} /></label>
            <label className="field"><span>Seed</span><input aria-label="工作台 Seed" type="number" min="0" max="4294967295" step="1" value={seed} onChange={(event) => setSeed(event.target.value)} /></label>
            <label className="field"><span>结果交付</span><select aria-label="工作台结果交付" value={deliveryMode} onChange={(event) => setDeliveryMode(event.target.value as typeof deliveryMode)}><option value="history_only">仅保存在工作台历史</option><option value="replacement">交付到当前角色会话</option></select></label>
            <label className="checkbox-field"><input type="checkbox" checked={parameters.background ?? false} onChange={(event) => patch({ background: event.target.checked })} /><span>{deliveryMode === 'history_only' ? '后台生成，结果仅保存在历史' : '后台生成，完成后交付当前角色会话'}</span></label>
          </fieldset>
          <div className="form-actions studio-actions"><button type="submit" className="primary" disabled={busy || uploading || !allowed || Boolean(source && !consent)}>{busy ? '正在生成…' : attempt.current ? '重查本次生成' : source ? '确认并重新生成' : '生成一张图片'}</button>
            {attempt.current && !busy && <button type="button" onClick={() => { studio.attempt.current = null; setConsent(false); setAccepted(false); setError(''); setSeed(''); setParameters({ ...parameters }); }}>另开一次生成</button>}</div>
          <p className="studio-usage">今日账户尝试 {page?.usage.accountAttempts ?? 0}/{configuration.dailyAttemptLimit}，本角色手动尝试 {page?.usage.manualAttempts ?? 0}/{configuration.manualDailyAttempts ?? 10}。</p>
        </>}
      </form>
      <div className="novelai-stage-column">
        <div className="novelai-stage" aria-label="当前作品">
          {selectedImage && role ? <img src={imageUrl(role.id, selectedImage.id)} alt={selectedImage.name} />
            : selected ? <div className="studio-stage-status"><Icon name="image" size={34} /><strong>{statusLabels[selected.status] ?? selected.status}</strong><span>{errorLabels[selected.errorCode] ?? '这条记录暂时没有可预览图片。'}</span></div>
            : page ? <div className="studio-stage-status"><Icon name="image" size={34} /><strong>选择一张历史作品</strong><span>生成完成后，图片会出现在这里。</span></div>
            : error ? <div className="studio-stage-status" role="status"><Icon name="image" size={34} /><strong>作品读取失败</strong><span>{error}</span></div>
            : <Placeholder loading>正在加载作品…</Placeholder>}
        </div>
        {selected?.parameters && <section className="studio-stage-meta" aria-label="当前作品参数">
            <div className="studio-stage-summary">
              <div className="studio-stage-facts"><span>{dimensions(selected.parameters)}</span><span>{modeLabel(selected.parameters.mode)}</span><span>{selected.parameters.model ?? '模型未记录'}</span><span>{selected.parameters.sampler ?? '采样器未记录'} · {selected.parameters.steps ?? '未知'} 步</span><span>Seed {selected.parameters.seed}</span></div>
              <div className="studio-stage-actions">
                {selectedImage && <a href={imageUrl(role.id, selectedImage.id)} download={selectedImage.name}>下载</a>}
                <button type="button" disabled={busy || uploading || Boolean(attempt.current)} onClick={() => handleReuseParameters(selected)}>复用参数</button>
                {selectedImage && <button type="button" disabled={busy || uploading || Boolean(attempt.current)} onClick={() => selectImage(selected, selectedImage)}>重新生成</button>}
              </div>
            </div>
            <details open>
              <summary>完整参数</summary>
              <dl className="studio-parameter-list">
                <div className="studio-parameter-wide"><dt>正向 tags</dt><dd>{selected.parameters.prompt || '（空）'}</dd></div>
                <div className="studio-parameter-wide"><dt>负向 tags</dt><dd>{selected.parameters.negative_prompt || '（空）'}</dd></div>
                <div><dt>生成模式</dt><dd>{modeLabel(selected.parameters.mode)}</dd></div>
                <div><dt>模型</dt><dd>{selected.parameters.model ?? '未记录'}</dd></div>
                <div><dt>采样器</dt><dd>{selected.parameters.sampler ?? '未记录'}</dd></div>
                <div><dt>采样步数</dt><dd>{selected.parameters.steps ?? '未记录'}</dd></div>
                <div><dt>Seed</dt><dd>{selected.parameters.seed}</dd></div>
                {selected.parameters.mode === 'img2img' && <>
                  <div><dt>图生图底图</dt><dd>{allCandidates.find((item) => item.id === selected.parameters?.base_attachment_id)?.name ?? selected.parameters.base_attachment_id ?? '未设置'}</dd></div>
                  <div><dt>图生图强度</dt><dd>{selected.parameters.strength ?? '未记录'}</dd></div>
                  <div><dt>图生图噪声</dt><dd>{selected.parameters.noise ?? '未记录'}</dd></div>
                </>}
                <div><dt>后台生成</dt><dd>{selected.parameters.background ? '是' : '否'}</dd></div>
              </dl>
            </details>
        </section>}
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
    {promptTagsOpen && <Dialog title={`${role.name}的提示标签库`} variant="modal" modalFocus onClose={() => setPromptTagsOpen(false)}>
      <PromptTagManager key={role.id} roleId={role.id} expanded />
    </Dialog>}
  </section>;
}
