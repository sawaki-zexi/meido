import { useEffect, useRef, useState, type FormEvent } from 'react';
import type { AgentToolConfig, ChatAttachment, Role } from '../../api/types';
import { novelai, type GenerationPage, type GenerationParameters, type NovelaiConfiguration, type NovelaiRoleConfig } from '../../api/novelai';
import { uploadChatAttachment } from '../../api/chat';
import { errorMessage } from '../../api/client';
import { Dialog } from '../../ui/Dialog';
import { Notice, Placeholder } from '../../ui/Status';

const labels: Record<string, string> = { prepared: '等待生成', dispatching: '生成中', received: '已收图，等待保存',
  succeeded: '已生成', unknown: '消费结果不确定', failed_response: '远端返回错误，可能已计费',
  local_pending: '本地保存待恢复', cancelled_before_dispatch: '出网前已取消', rejected: '未准入' };
const deliveryLabels: Record<string, string> = { sent: '已交付原来源', uncertain: '交付结果不确定，不会再次发送',
  failed: '交付失败，图片保留在历史', suppressed: '已取消展示，图片保留在历史',
  history_only: '仅保存在历史', pending: '尚未交付', sending: '正在交付原来源' };
const errorLabels: Record<string, string> = { response_not_verifiable: '无法完整验证远端图片，已保留可验证的图片。',
  unexpected_multiple_images: '远端意外返回多张图片，已全部保存，请在历史查看。',
  local_media_pending: '图片本地保存尚未完成，只恢复本地步骤。',
  transport_unknown: '网络结果无法确认，原请求不会再次发送。' };

export function GenerationDrawer({ role, onClose, onGenerated, attachments = [] }: {
  role: Role; onClose: () => void; onGenerated: () => void | Promise<void>; attachments?: ChatAttachment[];
}) {
  const [configuration, setConfiguration] = useState<NovelaiConfiguration | null>(null);
  const [page, setPage] = useState<GenerationPage | null>(null);
  const [parameters, setParameters] = useState<GenerationParameters>({ prompt: '', mode: 'txt2img', negative_prompt: '', size_preset: 'square' });
  const [seed, setSeed] = useState('');
  const [inputs, setInputs] = useState<ChatAttachment[]>([]);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const attempt = useRef<{ requestId: string; revision: number; parameters: GenerationParameters } | null>(null);
  const controller = useRef<AbortController | null>(null);
  const currentRole = useRef(role.id);
  currentRole.current = role.id;
  useEffect(() => {
    const identifier = role.id;
    let active = true;
    setPage(null); setConfiguration(null); setError(''); setBusy(false); setUploading(false); setInputs([]);
    attempt.current = null;
    setParameters({ prompt: '', mode: 'txt2img', negative_prompt: '', size_preset: 'square' }); setSeed('');
    void Promise.all([novelai.configuration(), novelai.history(identifier)]).then(([config, history]) => {
      if (!active || currentRole.current !== identifier) return;
      setConfiguration(config); setPage(history);
      setParameters((value) => ({ ...value, model: config.defaultModel, sampler: config.defaultSampler, steps: config.maxSteps,
        ...(config.maxPixels < 1048576 ? { size_preset: 'custom', custom_width: 64, custom_height: 64 } : {}) }));
    }).catch((cause) => { if (active) setError(errorMessage(cause, '无法读取生图设置和历史')); });
    return () => { active = false; controller.current?.abort(); };
  }, [role.id]);
  const refresh = async (identifier: string, cursor?: string) => {
    const result = await novelai.history(identifier, cursor);
    if (currentRole.current !== identifier) return;
    setPage((previous) => cursor && previous ? { ...result, generations: [...previous.generations,
      ...result.generations.filter((item) => !previous.generations.some((old) => old.attemptId === item.attemptId))] } : result);
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!configuration || busy || uploading) return;
    const identifier = role.id;
    if (!attempt.current) attempt.current = { requestId: crypto.randomUUID(), revision: configuration.revision,
      parameters: { ...parameters, ...(seed ? { seed: Number(seed) } : {}) } };
    const request = attempt.current;
    const cancellation = new AbortController();
    controller.current = cancellation;
    setBusy(true); setError('');
    try {
      const result = await novelai.generate(identifier, request.requestId, request.revision, request.parameters, cancellation.signal);
      if (currentRole.current !== identifier || cancellation.signal.aborted) return;
      await refresh(identifier);
      if (result.generation.status === 'succeeded') await onGenerated();
    } catch (cause) {
      if (currentRole.current === identifier) setError(cancellation.signal.aborted
        ? '已请求取消本地生成；已出网的请求可能计费，请查看原记录。'
        : errorMessage(cause, '请求结果尚未确认，请重查本次生成'));
    } finally {
      if (currentRole.current === identifier) setBusy(false);
      if (controller.current === cancellation) controller.current = null;
    }
  };
  const upload = async (file: File | undefined) => {
    if (!file || busy || uploading) return;
    const identifier = role.id;
    setUploading(true); setError('');
    try {
      const result = await uploadChatAttachment(identifier, file);
      if (currentRole.current !== identifier) return;
      setInputs((previous) => [...previous, result]);
      setParameters((value) => ({ ...value, base_attachment_id: result.id }));
    } catch (cause) { if (currentRole.current === identifier) setError(errorMessage(cause, '底图上传失败')); }
    finally { if (currentRole.current === identifier) setUploading(false); }
  };
  const policy = (role.agentConfig as AgentToolConfig & { novelai?: Partial<NovelaiRoleConfig> } | undefined)?.novelai;
  const allowed = configuration?.enabled && configuration.tokenConfigured && !configuration.credentialCleanupPending
    && policy?.manualEnabled && policy.consentRevision === configuration.revision;
  const candidates = [...attachments, ...inputs].filter((item, index, all) => item.kind === 'image' && all.findIndex((other) => other.id === item.id) === index);
  const patch = (changes: Partial<GenerationParameters>) => setParameters({ ...parameters, ...changes });
  return <Dialog title={`${role.name}的生图与历史`} variant="drawer" onClose={onClose}>
    {error && <Notice>{error}</Notice>}
    {!page && !error && <Placeholder loading>正在加载生图历史…</Placeholder>}
    {configuration && <>
      {!allowed && <p>生图尚未获准：请检查独立服务、凭据与角色当前版本的有限消费许可。</p>}
      <p>每次生成计入尝试额度；次数不是 Anlas 估价。未知结果可能已计费，不会自动再次请求。</p>
      {page && <p>今日账户尝试 {page.usage.accountAttempts}/{configuration.dailyAttemptLimit}，本角色手动尝试 {page.usage.manualAttempts}/{policy?.manualDailyAttempts ?? 10}（UTC 日）。</p>}
      <form aria-label="生成图片" onSubmit={(event) => void submit(event)}>
        <fieldset disabled={busy || uploading || !allowed || Boolean(attempt.current)}>
          <label className="field"><span>英文正向 tags</span><textarea aria-label="英文正向 tags" required maxLength={4000} value={parameters.prompt} onChange={(event) => patch({ prompt: event.target.value })} /></label>
          <label className="field"><span>英文负向 tags</span><textarea aria-label="英文负向 tags" maxLength={4000} value={parameters.negative_prompt ?? ''} onChange={(event) => patch({ negative_prompt: event.target.value })} /></label>
          <label className="field"><span>生成模式</span><select aria-label="生成模式" value={parameters.mode} onChange={(event) => {
            const { base_attachment_id: _base, strength: _strength, noise: _noise, ...rest } = parameters;
            setParameters(event.target.value === 'img2img' ? { ...rest, mode: 'img2img', strength: .7, noise: .2 } : { ...rest, mode: 'txt2img' });
          }}><option value="txt2img">文生图</option><option value="img2img">图生图</option></select></label>
          {parameters.mode === 'img2img' && <>
            <label className="field"><span>本角色底图</span><select aria-label="本角色底图" required value={parameters.base_attachment_id ?? ''} onChange={(event) => patch({ base_attachment_id: event.target.value })}><option value="">选择底图</option>{candidates.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
            <label className="field"><span>上传底图（最多 10 MiB）</span><input aria-label="上传生图底图" type="file" accept="image/png,image/jpeg,image/webp" onChange={(event) => { void upload(event.target.files?.[0]); event.target.value = ''; }} /></label>
            <label className="field"><span>图生图强度</span><input type="number" aria-label="图生图强度" min="0.01" max="1" step="0.01" required value={parameters.strength} onChange={(event) => patch({ strength: Number(event.target.value) })} /></label>
            <label className="field"><span>图生图噪声</span><input type="number" aria-label="图生图噪声" min="0" max="0.99" step="0.01" required value={parameters.noise} onChange={(event) => patch({ noise: Number(event.target.value) })} /></label>
          </>}
          <label className="field"><span>图片尺寸</span><select aria-label="图片尺寸" value={parameters.size_preset} onChange={(event) => {
            const { custom_width: _width, custom_height: _height, ...rest } = parameters;
            setParameters({ ...rest, size_preset: event.target.value as GenerationParameters['size_preset'], ...(event.target.value === 'custom' ? { custom_width: 64, custom_height: 64 } : {}) });
          }}><option value="square">方形 1024×1024</option><option value="landscape">横图 1216×832</option><option value="portrait">竖图 832×1216</option><option value="custom">自定义</option></select></label>
          {parameters.size_preset === 'custom' && <>{(['custom_width', 'custom_height'] as const).map((name) => <label className="field" key={name}><span>{name === 'custom_width' ? '宽度' : '高度'}</span><input aria-label={name === 'custom_width' ? '生图宽度' : '生图高度'} type="number" min="64" max="1024" step="64" required value={parameters[name]} onChange={(event) => patch({ [name]: Number(event.target.value) })} /></label>)}</>}
          <label className="field"><span>模型</span><select aria-label="生图模型" value={parameters.model} onChange={(event) => patch({ model: event.target.value })}>{configuration.allowedModels.map((item) => <option key={item}>{item}</option>)}</select></label>
          <label className="field"><span>采样器</span><select aria-label="生图采样器" value={parameters.sampler} onChange={(event) => patch({ sampler: event.target.value })}>{configuration.allowedSamplers.map((item) => <option key={item}>{item}</option>)}</select></label>
          <label className="field"><span>采样步数</span><input aria-label="生图采样步数" type="number" min="1" max={configuration.maxSteps} step="1" required value={parameters.steps} onChange={(event) => patch({ steps: Number(event.target.value) })} /></label>
          <label className="field"><span>Seed（留空生成并保存）</span><input aria-label="生图 Seed" type="number" min="0" max="4294967295" step="1" value={seed} onChange={(event) => setSeed(event.target.value)} /></label>
        </fieldset>
        <button type="submit" className="primary" disabled={busy || uploading || !allowed}>{busy ? '正在生成…' : attempt.current ? '重查本次生成' : '生成一张图片'}</button>
        {busy && <button type="button" onClick={() => controller.current?.abort()}>取消本地生成</button>}
        {attempt.current && !busy && <button type="button" onClick={() => { attempt.current = null; setError(''); setParameters({ ...parameters }); }}>另开一次生成</button>}
      </form>
    </>}
    <section aria-label="生图历史"><h3>历史</h3>
      {page?.generations.length === 0 && <p>暂无生图历史。</p>}
      {page?.generations.filter((item) => item.roleId === role.id).map((item) => <article key={item.attemptId}>
        <p>{labels[item.status] ?? item.status} · {new Date(item.createdAt).toLocaleString()}</p>
        <p>{item.parameters.model} · Seed {item.parameters.seed} · {item.parameters.width}×{item.parameters.height} · {item.parameters.steps} 步</p>
        <p>投递：{deliveryLabels[item.deliveryStatus] ?? '尚未确认'}</p>
        {item.errorCode && <p>{errorLabels[item.errorCode] ?? (item.errorCode.startsWith('http_') ? '远端服务返回错误，请检查账号配置与原记录。' : '本次请求未正常完成，请检查当前许可与归属历史。')}</p>}
        <div className="message-attachments">{item.attachments.map((image) => <a key={image.id} href={`/api/roles/${encodeURIComponent(role.id)}/attachments/${encodeURIComponent(image.id)}`} target="_blank" rel="noreferrer"><img src={`/api/roles/${encodeURIComponent(role.id)}/attachments/${encodeURIComponent(image.id)}`} alt={image.name} /></a>)}</div>
        {['prepared', 'dispatching', 'received', 'local_pending'].includes(item.status) && <button type="button" onClick={() => { void novelai.cancel(role.id, item.generationId).then(() => refresh(role.id)).catch((cause) => setError(errorMessage(cause, '取消尚未确认'))); }}>停止此生成</button>}
      </article>)}
      {page?.nextCursor && <button type="button" onClick={() => { void refresh(role.id, page.nextCursor!).catch((cause) => setError(errorMessage(cause, '历史加载失败'))); }}>加载更多生图历史</button>}
    </section>
  </Dialog>;
}
