import type { GenerationParameters } from '../../api/novelai';
import { errorMessage } from '../../api/client';
import { Dialog } from '../../ui/Dialog';
import { Notice, Placeholder } from '../../ui/Status';
import { useGenerationDrawer, activeGenerationStatuses as activeStatuses, type GenerationDrawerProps } from './useGenerationDrawer';
import { PromptTagManager } from './PromptTagManager';

const labels: Record<string, string> = { completed_without_image: '观察完成，未生成图片', prepared: '等待生成', dispatching: '生成中', received: '已收图，等待保存',
  succeeded: '已生成', unknown: '消费结果不确定', failed_response: '远端返回错误，可能已计费',
  local_pending: '本地保存待恢复', cancelled_before_dispatch: '出网前已取消', rejected: '未准入' };
const deliveryLabels: Record<string, string> = { sent: '已交付原来源', uncertain: '交付结果不确定，不会再次发送',
  failed: '交付失败，图片保留在历史', suppressed: '已取消展示，图片保留在历史',
  history_only: '仅保存在历史', pending: '尚未交付', sending: '正在交付原来源' };
const errorLabels: Record<string, string> = { response_not_verifiable: '无法完整验证远端图片，已保留可验证的图片。',
  replacement_conflict: '已生成，原消息已改变；新图仅保留在历史，未替换其他附件。',
  unexpected_multiple_images: '远端意外返回多张图片，已全部保存，请在历史查看。',
  local_media_pending: '图片本地保存尚未完成，只恢复本地步骤。',
  transport_unknown: '网络结果无法确认，原请求不会再次发送。',
  scene_none: '当前没有需要生成图片的场景。', scene_closed: '场景已结束，本次不生成图片。',
  scene_cooldown: '当前场景仍在五个主人回合的冷却期内。', scene_visual_duplicate: '此画面已经处理，本次不重复生成。',
  scene_observation_failed: '场景观察未完成；辅助请求可能已经计费，不会自动重试。' };
const writebackLabels: Record<string, string> = { writing: '正在写回或核对本地卡片图。',
  uncertain: '卡片图写回结果尚未确认，生成图已保留历史。', applied: '已更新本角色卡片图。',
  failed: '卡片图写回失败，生成图已保留历史；不会重新生成。' };
export function GenerationDrawer(props: GenerationDrawerProps) {
  const { role, onClose } = props;
  const { configuration, page, parameters, setParameters, seed, setSeed, busy, uploading, error, setError,
    source, consent, setConsent, accepted, setAccepted, deliveryMode, setDeliveryMode, attempt, controller, scope,
    isCurrent, watched, submit, upload, policy, allowed, candidates, patch, manage, selectImage, refresh } = useGenerationDrawer(props);
  return <Dialog title={`${role.name}的生图与历史`} variant="drawer" onClose={onClose}>
    {error && <Notice onDismiss={() => setError('')}>{error}</Notice>}
    {accepted && watched.length > 0 && <p role="status">后台生成已受理。关闭此面板后仍会继续，可在历史查询或停止。</p>}
    {!page && !error && <Placeholder loading>正在加载生图历史…</Placeholder>}
    {configuration && <>
      {!allowed && <p>手动生图尚未获准：请检查独立服务、凭据与公共参数当前版本的有限消费许可。</p>}
      <p>场景 CG：{policy?.sceneCgEnabled ? '角色已开启；每次仍需校验当前服务、辅助连接和有限消费许可。' : '角色未开启。'}</p>
      <p>每次生成计入尝试额度；次数不是 Anlas 估价。未知结果可能已计费，不会自动再次请求。</p>
      {page && <p>今日账户尝试 {page.usage.accountAttempts}/{configuration.dailyAttemptLimit}，本角色手动尝试 {page.usage.manualAttempts}/{configuration.manualDailyAttempts ?? 10}，本角色 CG 尝试 {page.usage.cgAttempts ?? 0}/{policy?.sceneCgDailyAttempts ?? 3}（UTC 日）。</p>}
      <form aria-label="生成图片" onSubmit={(event) => void submit(event)}>
        <fieldset disabled={busy || uploading || !allowed || Boolean(attempt.current)}>
          {source && <><p>{deliveryMode === 'replacement' ? `替换 ${source.image.name}；其他图片和附件保持不变。` : `基于 ${source.image.name} 再次生成，仅保存在历史，不替换原消息。`}</p>
            {deliveryMode === 'history_only' ? <p>仅保存历史不会向原渠道或会话发送媒体。</p>
              : source.generation.deliveryMessageId?.startsWith('thread:') && <p>本次重生成会向原渠道再次发送一条新的媒体消息。</p>}
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
    <PromptTagManager key={role.id} roleId={role.id} />
    <section aria-label="生图历史"><h3>历史</h3>
      {page?.generations.length === 0 && <p>暂无生图历史。</p>}
      {page?.generations.filter((item) => item.roleId === role.id).map((item) => <article key={item.attemptId} aria-label={`生成记录 ${item.attemptId}`}>
        <p>尝试 {item.attemptId}{item.parentAttemptId && ` · 重生成自 ${item.parentAttemptId}`}</p>
        <p>{item.purpose === 'scene_cg' ? '主人回合场景 CG' : '手动生成'}{item.phase === 'auxiliary' ? ' · 辅助场景观察中' : item.phase === 'generation' ? ' · 图片生成阶段' : item.phase === 'completed' ? ' · 本次已结束' : ''}</p>
        <p>{labels[item.status] ?? item.status} · {new Date(item.createdAt).toLocaleString()}</p>
        <p>消费：{item.consumption === 'possible' ? '已出网，可能计费；未确认实际扣费。' : item.consumption === 'not_dispatched' ? '生成请求尚未出网。' : '消费状态尚未确认。'}</p>
        {item.purpose === 'scene_cg' && <p>{item.auxiliaryConsumption === 'possible' ? '辅助请求已出网，可能计费；未出图仍计入 CG 与账户尝试。' : item.auxiliaryConsumption === 'not_dispatched' ? '辅助请求尚未出网。' : '辅助消费状态尚未确认。'}</p>}
        {item.parameters ? <p>{item.parameters.model} · Seed {item.parameters.seed} · {item.parameters.width}×{item.parameters.height} · {item.parameters.steps} 步</p> : <p>尚无生图参数，本次可能仅进行场景观察。</p>}
        {item.writebackStatus && writebackLabels[item.writebackStatus] && <p>{writebackLabels[item.writebackStatus]}</p>}
        <p>投递：{deliveryLabels[item.deliveryStatus] ?? '尚未确认'}</p>
        {item.errorCode && <p>{errorLabels[item.errorCode] ?? (item.errorCode.startsWith('http_') ? '远端服务返回错误，请检查账号配置与原记录。' : '本次请求未正常完成，请检查当前许可与归属历史。')}</p>}
        <div className="message-attachments">{item.attachments.map((image) => <div key={image.id}><a href={`/api/roles/${encodeURIComponent(role.id)}/attachments/${encodeURIComponent(image.id)}`} target="_blank" rel="noreferrer"><img src={`/api/roles/${encodeURIComponent(role.id)}/attachments/${encodeURIComponent(image.id)}`} alt={image.name} /></a>
          {image.kind === 'image' && item.parameters && <button type="button" disabled={busy || uploading || !allowed} onClick={() => selectImage(item, image)}>重新生成 {image.name}</button>}</div>)}</div>
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
