import { useEffect, useRef, useState, type FormEvent } from 'react';
import type { AgentToolConfig, ChatAttachment, Role } from '../../api/types';
import { novelai, type GenerationPage, type GenerationParameters, type GenerationSelection, type GenerationView, type NovelaiConfiguration, type NovelaiRoleConfig } from '../../api/novelai';
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
  replacement_conflict: '已生成，原消息已改变；新图仅保留在历史，未替换其他附件。',
  unexpected_multiple_images: '远端意外返回多张图片，已全部保存，请在历史查看。',
  local_media_pending: '图片本地保存尚未完成，只恢复本地步骤。',
  transport_unknown: '网络结果无法确认，原请求不会再次发送。' };
const activeStatuses = ['prepared', 'dispatching', 'received', 'local_pending'];

export function GenerationDrawer({ role, onClose, onGenerated, attachments = [], selectedImage }: {
  role: Role; onClose: () => void; onGenerated: () => void | Promise<void>; attachments?: ChatAttachment[];
  selectedImage?: GenerationSelection;
}) {
  const [configuration, setConfiguration] = useState<NovelaiConfiguration | null>(null);
  const [page, setPage] = useState<GenerationPage | null>(null);
  const [parameters, setParameters] = useState<GenerationParameters>({ prompt: '', mode: 'txt2img', negative_prompt: '', size_preset: 'square' });
  const [seed, setSeed] = useState('');
  const [inputs, setInputs] = useState<ChatAttachment[]>([]);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [source, setSource] = useState<{ generation: GenerationView; image: ChatAttachment } | null>(null);
  const [consent, setConsent] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [deliveryMode, setDeliveryMode] = useState<'replacement' | 'history_only'>('replacement');
  const attempt = useRef<{ requestId: string; revision: number; parameters: GenerationParameters;
    source: { generation: GenerationView; image: ChatAttachment } | null; deliveryMode: 'replacement' | 'history_only' } | null>(null);
  const controller = useRef<AbortController | null>(null);
  const recordQueries = useRef(new Map<string, { stopping: boolean }>());
  const currentRole = useRef(role.id);
  const scope = useRef({ roleId: role.id, active: true });
  const generatedCallback = useRef(onGenerated);
  generatedCallback.current = onGenerated;
  currentRole.current = role.id;
  const isCurrent = (owner: typeof scope.current) => owner.active && scope.current === owner && currentRole.current === owner.roleId;
  useEffect(() => {
    const identifier = role.id;
    const owner = { roleId: identifier, active: true };
    scope.current = owner;
    setPage(null); setConfiguration(null); setError(''); setBusy(false); setUploading(false); setInputs([]);
    attempt.current = null; setSource(null); setConsent(false); setAccepted(false); setDeliveryMode('replacement');
    recordQueries.current = new Map();
    setParameters({ prompt: '', mode: 'txt2img', negative_prompt: '', size_preset: 'square' }); setSeed('');
    void Promise.all([novelai.configuration(), novelai.history(identifier)]).then(([config, history]) => {
      if (!isCurrent(owner)) return;
      setConfiguration(config); setPage(history);
      setParameters((value) => ({ ...value, model: config.defaultModel, sampler: config.defaultSampler, steps: config.maxSteps,
        ...(config.maxPixels < 1048576 ? { size_preset: 'custom', custom_width: 64, custom_height: 64 } : {}) }));
      if (selectedImage?.generation.roleId === identifier) selectImage(selectedImage.generation, selectedImage.image);
    }).catch((cause) => { if (isCurrent(owner)) setError(errorMessage(cause, '无法读取生图设置和历史')); });
    return () => { owner.active = false; controller.current?.abort(); };
  }, [role.id, selectedImage?.generation.attemptId, selectedImage?.image.id]);
  const refresh = async (identifier: string, cursor?: string, owner = scope.current, retained?: GenerationView) => {
    let result = await novelai.history(identifier, cursor);
    if (!isCurrent(owner)) return;
    if (retained && !result.generations.some((item) => item.attemptId === retained.attemptId))
      result = { ...result, generations: [retained, ...result.generations] };
    setPage((previous) => cursor && previous ? { ...result, generations: [...previous.generations,
      ...result.generations.filter((item) => !previous.generations.some((old) => old.attemptId === item.attemptId))] } : result);
    return result;
  };
  const updateRecord = (generation: GenerationView) => setPage((previous) => previous ? { ...previous,
    generations: [generation, ...previous.generations.filter((item) => item.attemptId !== generation.attemptId)] } : previous);
  const watched = page?.generations.filter((item) => item.roleId === role.id && (activeStatuses.includes(item.status)
    || (item.status === 'succeeded' && ['pending', 'sending'].includes(item.deliveryStatus)))) ?? [];
  const observationKey = watched.map((item) => `${item.attemptId}:${item.status}:${item.deliveryStatus}`).join('|');
  useEffect(() => {
    if (!observationKey) return;
    const owner = scope.current;
    let active = true;
    let querying = false;
    const timer = window.setInterval(() => {
      if (querying) return;
      querying = true;
      const queries = watched.filter((item) => !recordQueries.current.get(item.attemptId)?.stopping).map((item) => {
        const token = { stopping: false };
        recordQueries.current.set(item.attemptId, token);
        return { item, token };
      });
      void Promise.allSettled(queries.map(({ item }) => novelai.get(owner.roleId, item.generationId, item.attemptId))).then(async (results) => {
        if (!active || !isCurrent(owner)) return;
        let delivered = false;
        for (const [index, result] of results.entries()) {
          if (recordQueries.current.get(queries[index].item.attemptId) !== queries[index].token) continue;
          if (result.status === 'fulfilled') {
            updateRecord(result.value.generation);
            delivered ||= result.value.generation.deliveryStatus === 'sent';
          } else setError(errorMessage(result.reason, '后台状态查询未完成，请重查原记录'));
        }
        if (delivered) await generatedCallback.current();
      }).catch((cause) => { if (active && isCurrent(owner)) setError(errorMessage(cause, '无法刷新原会话')); })
        .finally(() => { querying = false; });
    }, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [role.id, observationKey]);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!configuration || busy || uploading || (source && !consent && !attempt.current)) return;
    const identifier = role.id;
    const owner = scope.current;
    if (!attempt.current) attempt.current = { requestId: crypto.randomUUID(), revision: configuration.revision,
      parameters: { ...parameters, ...(seed ? { seed: Number(seed) } : {}) }, source, deliveryMode };
    const request = attempt.current;
    const cancellation = new AbortController();
    controller.current = cancellation;
    setBusy(true); setError('');
    try {
      const result = request.source
        ? await novelai.regenerate(identifier, request.source.generation.generationId, request.requestId, request.revision,
          request.source.generation.attemptId, request.source.image.id, request.parameters, cancellation.signal, request.deliveryMode)
        : await novelai.generate(identifier, request.requestId, request.revision, request.parameters, cancellation.signal,
          request.deliveryMode === 'history_only' ? 'history_only' : undefined);
      if (!isCurrent(owner) || cancellation.signal.aborted) return;
      setAccepted(Boolean(request.parameters.background && activeStatuses.includes(result.generation.status)));
      updateRecord(result.generation);
      const history = await refresh(identifier, undefined, owner, result.generation).catch((cause) => {
        if (isCurrent(owner)) setError(errorMessage(cause, '历史读取失败，请重查原记录'));
        return undefined;
      });
      if (!isCurrent(owner)) return;
      const latest = history?.generations.find((item) => item.attemptId === result.generation.attemptId) ?? result.generation;
      if (latest.status === 'succeeded') await generatedCallback.current();
    } catch (cause) {
      if (isCurrent(owner)) setError(cancellation.signal.aborted
        ? '已请求取消本地生成；已出网的请求可能计费，请查看原记录。'
        : errorMessage(cause, '请求结果尚未确认，请重查本次生成'));
    } finally {
      if (isCurrent(owner)) setBusy(false);
      if (controller.current === cancellation) controller.current = null;
    }
  };
  const upload = async (file: File | undefined) => {
    if (!file || busy || uploading) return;
    const identifier = role.id;
    const owner = scope.current;
    setUploading(true); setError('');
    try {
      const result = await uploadChatAttachment(identifier, file);
      if (!isCurrent(owner)) return;
      setInputs((previous) => [...previous, result]);
      setParameters((value) => ({ ...value, base_attachment_id: result.id }));
    } catch (cause) { if (isCurrent(owner)) setError(errorMessage(cause, '底图上传失败')); }
    finally { if (isCurrent(owner)) setUploading(false); }
  };
  const policy = (role.agentConfig as AgentToolConfig & { novelai?: Partial<NovelaiRoleConfig> } | undefined)?.novelai;
  const allowed = configuration?.enabled && configuration.tokenConfigured && !configuration.credentialCleanupPending
    && policy?.manualEnabled && policy.consentRevision === configuration.revision;
  const candidates = [...attachments, ...inputs].filter((item, index, all) => item.kind === 'image' && all.findIndex((other) => other.id === item.id) === index);
  const patch = (changes: Partial<GenerationParameters>) => setParameters({ ...parameters, ...changes });
  const manage = async (item: GenerationView, cancel = false) => {
    if (recordQueries.current.get(item.attemptId)?.stopping) return;
    const token = { stopping: cancel };
    recordQueries.current.set(item.attemptId, token);
    const identifier = role.id;
    const owner = scope.current;
    try {
      const result = cancel ? await novelai.cancel(identifier, item.generationId, item.attemptId)
        : await novelai.get(identifier, item.generationId, item.attemptId);
      if (!isCurrent(owner) || recordQueries.current.get(item.attemptId) !== token) return;
      updateRecord(result.generation);
      if (result.generation.status === 'succeeded' && result.generation.deliveryStatus === 'sent') await generatedCallback.current();
    } catch (cause) { if (isCurrent(owner) && recordQueries.current.get(item.attemptId) === token) setError(errorMessage(cause, cancel ? '取消尚未确认' : '查询尚未确认')); }
    finally { token.stopping = false; }
  };
  const selectImage = (generation: GenerationView, image: ChatAttachment) => {
    if (busy || uploading) return;
    const { width, height, prompt, negative_prompt, mode, model, sampler, steps, base_attachment_id, strength, noise } = generation.parameters;
    const size = width === 1024 && height === 1024 ? 'square' : width === 1216 && height === 832 ? 'landscape'
      : width === 832 && height === 1216 ? 'portrait' : 'custom';
    setParameters({ prompt, negative_prompt, mode, model, sampler, steps, size_preset: size,
      ...(mode === 'img2img' ? { base_attachment_id, strength, noise } : {}),
      ...(size === 'custom' ? { custom_width: width, custom_height: height } : {}) });
    setSeed(''); setSource({ generation, image }); setConsent(false); setAccepted(false); setError(''); attempt.current = null;
  };
  return <Dialog title={`${role.name}的生图与历史`} variant="drawer" onClose={onClose}>
    {error && <Notice>{error}</Notice>}
    {accepted && watched.length > 0 && <p role="status">后台生成已受理。关闭此面板后仍会继续，可在历史查询或停止。</p>}
    {!page && !error && <Placeholder loading>正在加载生图历史…</Placeholder>}
    {configuration && <>
      {!allowed && <p>生图尚未获准：请检查独立服务、凭据与角色当前版本的有限消费许可。</p>}
      <p>每次生成计入尝试额度；次数不是 Anlas 估价。未知结果可能已计费，不会自动再次请求。</p>
      {page && <p>今日账户尝试 {page.usage.accountAttempts}/{configuration.dailyAttemptLimit}，本角色手动尝试 {page.usage.manualAttempts}/{policy?.manualDailyAttempts ?? 10}（UTC 日）。</p>}
      <form aria-label="生成图片" onSubmit={(event) => void submit(event)}>
        <fieldset disabled={busy || uploading || !allowed || Boolean(attempt.current)}>
          {source && <><p>{deliveryMode === 'replacement' ? `替换 ${source.image.name}；其他图片和附件保持不变。` : `基于 ${source.image.name} 再次生成，仅保存在历史，不替换原消息。`}</p>
            <label><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />我确认本次重生成将再次消费一次尝试额度，可能产生费用</label></>}
          <label className="field"><span>交付方式</span><select aria-label={source ? '重生成交付方式' : '生成交付方式'} value={deliveryMode} onChange={(event) => setDeliveryMode(event.target.value as typeof deliveryMode)}>
            <option value="replacement">{source ? '替换选中图片' : '交付原会话'}</option><option value="history_only">仅保存历史</option>
          </select></label>
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
          <label className="field"><span>模型</span><select aria-label="生图模型" required value={configuration.allowedModels.includes(parameters.model ?? '') ? parameters.model : ''} onChange={(event) => patch({ model: event.target.value })}><option value="">选择当前允许的模型</option>{configuration.allowedModels.map((item) => <option key={item}>{item}</option>)}</select></label>
          <label className="field"><span>采样器</span><select aria-label="生图采样器" required value={configuration.allowedSamplers.includes(parameters.sampler ?? '') ? parameters.sampler : ''} onChange={(event) => patch({ sampler: event.target.value })}><option value="">选择当前允许的采样器</option>{configuration.allowedSamplers.map((item) => <option key={item}>{item}</option>)}</select></label>
          <label className="field"><span>采样步数</span><input aria-label="生图采样步数" type="number" min="1" max={configuration.maxSteps} step="1" required value={parameters.steps} onChange={(event) => patch({ steps: Number(event.target.value) })} /></label>
          <label className="field"><span>Seed（留空生成并保存）</span><input aria-label="生图 Seed" type="number" min="0" max="4294967295" step="1" value={seed} onChange={(event) => setSeed(event.target.value)} /></label>
          <label><input type="checkbox" checked={parameters.background ?? false} onChange={(event) => patch({ background: event.target.checked })} />{deliveryMode === 'history_only' ? '在后台生成，结果仅保存在历史' : '在后台生成，完成后交付原来源'}</label>
        </fieldset>
        <button type="submit" className="primary" disabled={busy || uploading || !allowed || Boolean(source && !consent)}>{busy ? '正在生成…' : attempt.current ? '重查本次生成' : source ? '确认并重新生成' : '生成一张图片'}</button>
        {busy && !parameters.background && <button type="button" onClick={() => controller.current?.abort()}>取消本地生成</button>}
        {attempt.current && !busy && <button type="button" onClick={() => { attempt.current = null; setConsent(false); setAccepted(false); setError(''); setSeed(''); setParameters({ ...parameters }); }}>另开一次生成</button>}
      </form>
    </>}
    <section aria-label="生图历史"><h3>历史</h3>
      {page?.generations.length === 0 && <p>暂无生图历史。</p>}
      {page?.generations.filter((item) => item.roleId === role.id).map((item) => <article key={item.attemptId} aria-label={`生成记录 ${item.attemptId}`}>
        <p>尝试 {item.attemptId}{item.parentAttemptId && ` · 重生成自 ${item.parentAttemptId}`}</p>
        <p>{labels[item.status] ?? item.status} · {new Date(item.createdAt).toLocaleString()}</p>
        <p>消费：{item.consumption === 'possible' ? '已出网，可能计费；未确认实际扣费。' : item.consumption === 'not_dispatched' ? '生成请求尚未出网。' : '消费状态尚未确认。'}</p>
        <p>{item.parameters.model} · Seed {item.parameters.seed} · {item.parameters.width}×{item.parameters.height} · {item.parameters.steps} 步</p>
        <p>投递：{deliveryLabels[item.deliveryStatus] ?? '尚未确认'}</p>
        {item.errorCode && <p>{errorLabels[item.errorCode] ?? (item.errorCode.startsWith('http_') ? '远端服务返回错误，请检查账号配置与原记录。' : '本次请求未正常完成，请检查当前许可与归属历史。')}</p>}
        <div className="message-attachments">{item.attachments.map((image) => <div key={image.id}><a href={`/api/roles/${encodeURIComponent(role.id)}/attachments/${encodeURIComponent(image.id)}`} target="_blank" rel="noreferrer"><img src={`/api/roles/${encodeURIComponent(role.id)}/attachments/${encodeURIComponent(image.id)}`} alt={image.name} /></a>
          {image.kind === 'image' && <button type="button" disabled={busy || uploading || !allowed} onClick={() => selectImage(item, image)}>重新生成 {image.name}</button>}</div>)}</div>
        <button type="button" onClick={() => void manage(item)}>查询此生成</button>
        {(activeStatuses.includes(item.status) || (item.status === 'succeeded' && item.deliveryStatus === 'pending'))
          && <button type="button" onClick={() => void manage(item, true)}>停止此生成</button>}
      </article>)}
      {page?.nextCursor && <button type="button" onClick={() => {
        const owner = scope.current;
        void refresh(role.id, page.nextCursor!, owner).catch((cause) => { if (isCurrent(owner)) setError(errorMessage(cause, '历史加载失败')); });
      }}>加载更多生图历史</button>}
    </section>
  </Dialog>;
}
