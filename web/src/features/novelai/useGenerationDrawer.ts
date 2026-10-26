import { useEffect, useRef, useState, type FormEvent } from 'react';
import type { AgentToolConfig, ChatAttachment, Role } from '../../api/types';
import { novelai, type GenerationPage, type GenerationParameters, type GenerationSelection, type GenerationView, type NovelaiConfiguration, type NovelaiRoleConfig } from '../../api/novelai';
import { uploadChatAttachment } from '../../api/chat';
import { errorMessage } from '../../api/client';
import { generationAvailability } from './availability';

export const activeGenerationStatuses = ['prepared', 'dispatching', 'received', 'local_pending'];
export type GenerationDrawerProps = {
  role: Role; onClose: () => void; onGenerated: () => void | Promise<void>; attachments?: ChatAttachment[];
  selectedImage?: GenerationSelection; defaultDeliveryMode?: 'replacement' | 'history_only';
  onGenerationAccepted?: (generation: GenerationView) => void;
};

function serviceDefaults(config: NovelaiConfiguration) {
  const mode: GenerationParameters['mode'] = config.allowTxt2Img !== false ? 'txt2img' : config.allowImg2Img !== false ? 'img2img' : 'txt2img';
  return { mode, samples: config.defaultSamples ?? 1, model: config.nsfwEnabled ? config.nsfwModel : config.defaultModel,
    sampler: config.defaultSampler, steps: config.maxSteps };
}

export function useGenerationDrawer({ role, onGenerated, attachments = [], selectedImage, defaultDeliveryMode = 'replacement', onGenerationAccepted }: GenerationDrawerProps) {
  const [configuration, setConfiguration] = useState<NovelaiConfiguration | null>(null);
  const [configurationRefreshing, setConfigurationRefreshing] = useState(false);
  const configurationQuery = useRef(0);
  const [page, setPage] = useState<GenerationPage | null>(null);
  const [parameters, setParameters] = useState<GenerationParameters>({ prompt: '', mode: 'txt2img', negative_prompt: '', samples: 1, size_preset: 'square' });
  const [seed, setSeed] = useState('');
  const [inputs, setInputs] = useState<ChatAttachment[]>([]);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [source, setSource] = useState<{ generation: GenerationView; image: ChatAttachment } | null>(null);
  const [consent, setConsent] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [deliveryMode, setDeliveryMode] = useState<'replacement' | 'history_only'>(defaultDeliveryMode);
  const attempt = useRef<{ requestId: string; revision: number; parameters: GenerationParameters;
    source: { generation: GenerationView; image: ChatAttachment } | null; deliveryMode: 'replacement' | 'history_only' } | null>(null);
  const controller = useRef<AbortController | null>(null);
  const recordQueries = useRef(new Map<string, { stopping: boolean }>());
  const currentRole = useRef(role.id);
  const scope = useRef({ roleId: role.id, active: true });
  const generatedCallback = useRef(onGenerated);
  const acceptedCallback = useRef(onGenerationAccepted);
  generatedCallback.current = onGenerated;
  acceptedCallback.current = onGenerationAccepted;
  currentRole.current = role.id;
  const isCurrent = (owner: typeof scope.current) => owner.active && scope.current === owner && currentRole.current === owner.roleId;
  useEffect(() => {
    const identifier = role.id;
    const owner = { roleId: identifier, active: true };
    const query = ++configurationQuery.current;
    scope.current = owner;
    setPage(null); setConfiguration(null); setConfigurationRefreshing(false); setError(''); setBusy(false); setUploading(false); setInputs([]);
    attempt.current = null; setSource(null); setConsent(false); setAccepted(false); setDeliveryMode(defaultDeliveryMode);
    recordQueries.current = new Map();
    setParameters({ prompt: '', mode: 'txt2img', negative_prompt: '', size_preset: 'square' }); setSeed('');
    void Promise.all([novelai.configuration(), novelai.history(identifier)]).then(([config, history]) => {
      if (!isCurrent(owner)) return;
      setPage(history);
      if (configurationQuery.current === query) {
        setConfiguration(config);
        setParameters((value) => ({ ...value, ...serviceDefaults(config),
          ...(config.maxPixels < 1048576 ? { size_preset: 'custom', custom_width: 64, custom_height: 64 } : {}) }));
      }
      if (selectedImage?.generation.roleId === identifier) selectImage(selectedImage.generation, selectedImage.image);
    }).catch((cause) => { if (isCurrent(owner)) setError(errorMessage(cause, '无法读取生图设置和历史')); });
    return () => { owner.active = false; controller.current?.abort(); };
  }, [role.id, selectedImage?.generation.attemptId, selectedImage?.image.id]);
  const refreshConfiguration = async () => {
    const owner = scope.current;
    const query = ++configurationQuery.current;
    setConfigurationRefreshing(true);
    try {
      const config = await novelai.configuration();
      if (!isCurrent(owner) || configurationQuery.current !== query) return;
      setConfiguration(config);
      if (!attempt.current && config.revision !== configuration?.revision) {
        const previous = configuration && serviceDefaults(configuration);
        const defaults = serviceDefaults(config);
        setParameters((value) => ({ ...value,
          model: value.model === previous?.model || !config.allowedModels.includes(value.model ?? '') ? defaults.model : value.model,
          sampler: value.sampler === previous?.sampler || !config.allowedSamplers.includes(value.sampler ?? '') ? defaults.sampler : value.sampler,
          samples: value.samples === previous?.samples || value.samples === undefined ? defaults.samples : value.samples,
          steps: value.steps === previous?.steps ? defaults.steps : Math.min(value.steps ?? defaults.steps, config.maxSteps),
        }));
      }
    } catch (cause) {
      if (isCurrent(owner) && configurationQuery.current === query) setError(errorMessage(cause, '无法同步最新生图设置'));
    } finally {
      if (isCurrent(owner) && configurationQuery.current === query) setConfigurationRefreshing(false);
    }
  };
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
  const watched = page?.generations.filter((item) => item.roleId === role.id && (activeGenerationStatuses.includes(item.status)
    || item.writebackStatus === 'writing' || (item.status === 'succeeded' && ['pending', 'sending'].includes(item.deliveryStatus)))) ?? [];
  const observationKey = watched.map((item) => `${item.attemptId}:${item.status}:${item.deliveryStatus}:${item.writebackStatus ?? ''}`).join('|');
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
    if (!configuration || configurationRefreshing || busy || uploading || (source && !consent && !attempt.current)) return;
    const readiness = generationAvailability(configuration, page?.usage);
    if (!attempt.current && !readiness.available) { setError(readiness.message); return; }
    const modeAllowed = parameters.mode === 'txt2img' ? configuration.allowTxt2Img !== false : configuration.allowImg2Img !== false;
    if (!modeAllowed) {
      setError(parameters.mode === 'txt2img' ? '当前服务已关闭文生图，请切换到已启用的模式。' : '当前服务已关闭图生图，请切换到已启用的模式。');
      return;
    }
    if (source && deliveryMode === 'replacement' && (parameters.samples ?? 1) !== 1) {
      setError('替换历史中的单张图片时只能生成一张；请选择仅保存历史，或将生成张数改为 1。');
      return;
    }
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
      setAccepted(Boolean(request.parameters.background && activeGenerationStatuses.includes(result.generation.status)));
      acceptedCallback.current?.(result.generation);
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
  const availability = configuration ? generationAvailability(configuration, page?.usage) : null;
  // Rechecking an existing request is safe even after it used the last daily attempt.
  const allowed = !configurationRefreshing && Boolean(availability?.available || attempt.current);
  const unavailableMessage = availability?.message ?? '正在读取生图状态…';
  const modeAllowed = configuration ? (parameters.mode === 'txt2img' ? configuration.allowTxt2Img !== false : configuration.allowImg2Img !== false) : false;
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
    if (busy || uploading || !generation.parameters || image.kind !== 'image') return;
    loadParameters(generation);
    setSource({ generation, image }); setConsent(false); setAccepted(false); setError(''); attempt.current = null;
  };
  const loadParameters = (generation: GenerationView, reuseSeed = false) => {
    if (busy || uploading || !generation.parameters) return;
    const { width, height, prompt, negative_prompt, mode, model, sampler, steps, samples, base_attachment_id, strength, noise, background } = generation.parameters;
    const size = width === 1024 && height === 1024 ? 'square' : width === 1216 && height === 832 ? 'landscape'
      : width === 832 && height === 1216 ? 'portrait' : 'custom';
    setParameters({ prompt, negative_prompt, mode, model, sampler, steps, samples: samples ?? 1, ...(reuseSeed ? { background } : {}), size_preset: size,
      ...(mode === 'img2img' ? { base_attachment_id, strength, noise } : {}),
      ...(size === 'custom' ? { custom_width: width, custom_height: height } : {}) });
    setSeed(reuseSeed && generation.parameters.seed !== undefined ? String(generation.parameters.seed) : '');
  };
  const reuseParameters = (generation: GenerationView) => {
    if (busy || uploading) return;
    loadParameters(generation, true);
    setSource(null); setConsent(false); setAccepted(false); setError(''); attempt.current = null;
  };
  const clearSource = () => {
    if (busy || uploading) return;
    setSource(null); setConsent(false); setAccepted(false);
  };
  return { configuration, setConfiguration, page, parameters, setParameters, seed, setSeed, busy, uploading, error, setError,
    source, consent, setConsent, accepted, setAccepted, deliveryMode, setDeliveryMode, attempt, controller, scope,
    isCurrent, watched, submit, upload, policy, allowed, modeAllowed, candidates, patch, manage, selectImage, loadParameters, reuseParameters,
    clearSource, refresh, refreshConfiguration, configurationRefreshing, unavailableMessage };
}
