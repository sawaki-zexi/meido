import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../../api/client';
import { autonomy, AutonomyOperationError, type AutonomyDelivery, type AutonomyReconciliationRequest,
  type AutonomyResolutionDecision, type AutonomyState } from '../../api/autonomy';
import { Notice } from '../../ui/Status';

const deliveryLabels = { dispatching: '正在核对原投递', pending: '等待原投递回执', uncertain: '结果不确定，不会再次发送',
  sent: '已发送，平台回执已确认', failed: '确定失败，不会再次发送' };
const decisionLabels = { reported_sent: '主人报告：已收到', reported_not_sent: '主人报告：未收到', keep_pending: '主人决定：继续等待回执' };

function DeliveryReconciliation({ roleId, revision, delivery, onState, refresh }: {
  roleId: string; revision: number; delivery: AutonomyDelivery; onState: (value: AutonomyState) => void; refresh: () => Promise<void>;
}) {
  const [decision, setDecision] = useState<AutonomyResolutionDecision>('keep_pending');
  const [allowFuture, setAllowFuture] = useState(false);
  const [note, setNote] = useState('');
  const [pending, setPending] = useState<AutonomyReconciliationRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState('');
  const pendingRequest = useRef<AutonomyReconciliationRequest | null>(null);
  const working = useRef(false);
  const generation = useRef(0);
  const unresolved = ['dispatching', 'pending', 'uncertain'].includes(delivery.status);

  function remember(request: AutonomyReconciliationRequest | null) { pendingRequest.current = request; setPending(request); }
  function clearForm() { setNote(''); setDecision('keep_pending'); setAllowFuture(false); }
  useEffect(() => {
    ++generation.current; working.current = false; setBusy(false);
    return () => { ++generation.current; };
  }, [roleId, revision]);
  useEffect(() => {
    const request = pendingRequest.current;
    if (!request) return;
    const confirmed = delivery.resolution?.requestId === request.requestId;
    const changed = delivery.status !== request.expectedStatus || delivery.resolutionRevision !== request.expectedResolutionRevision;
    if (confirmed || changed) { ++generation.current; working.current = false; setBusy(false); }
    if (confirmed) { remember(null); clearForm(); setError(''); }
    else if (changed) {
      remember(null); setConflict(true); setError('原投递状态或处置版本已变化，原请求已失效；请核对最新回执后重新填写。');
    }
  }, [delivery.status, delivery.resolutionRevision, delivery.resolution?.requestId]);

  async function submit() {
    if (working.current || conflict || !unresolved || !pendingRequest.current && (!note.trim() || note.trim().length > 1000)) return;
    const current = generation.current;
    const request = pendingRequest.current ?? { requestId: crypto.randomUUID(), expectedResolutionRevision: delivery.resolutionRevision,
      expectedStatus: delivery.status, decision, allowFuture: decision === 'keep_pending' ? false : allowFuture, note: note.trim() };
    working.current = true; setBusy(true); setError(''); remember(request);
    try {
      const value = await autonomy.reconcileDelivery(roleId, delivery.intentId, request);
      if (current !== generation.current) return;
      remember(null); clearForm(); onState(value);
    } catch (cause) {
      if (current !== generation.current) return;
      if (cause instanceof AutonomyOperationError && cause.status === 409) {
        remember(null); setConflict(true);
        setError(`${cause.message}；原请求已失效，请刷新核对后重新填写，不会自动重试。`);
        await refresh();
      } else if (cause instanceof AutonomyOperationError && cause.status >= 400 && cause.status < 500) {
        remember(null); setError(cause.message);
      } else {
        setError(`${errorMessage(cause, '处置结果尚未确认')}；已保留原请求身份与正文，可先核对状态，再手动重试原请求。`);
      }
    } finally { if (current === generation.current) { working.current = false; setBusy(false); } }
  }

  return <fieldset aria-label={`投递处置 ${delivery.intentId}`}>
    <legend>投递 {delivery.intentId}</legend>
    <p>{delivery.status === 'sent' && delivery.target.channel === 'desktop' ? '已发送，桌面回执已确认' : deliveryLabels[delivery.status]}</p>
    <p>原目标：{delivery.target.displayName || delivery.target.ref} · {delivery.target.channel}
      {delivery.target.accountId ? ` · 账号 ${delivery.target.accountId}` : ''}</p>
    <p>原 attempt：{delivery.attemptId || '尚无本地回执'} · 平台消息 ID：{delivery.platformMessageId || '尚未确认'}</p>
    {delivery.messageId && <p>本地消息：{delivery.messageId}</p>}
    {delivery.projectionPending && <p role="status">原意图仍有本地恢复责任；只核对原回执并恢复本地记录，不会重新发送。</p>}
    {delivery.error && <p>{delivery.error}</p>}
    {delivery.resolution && <div aria-label="已记录主人处置">
      <p>{decisionLabels[delivery.resolution.decision]} · {delivery.resolution.allowFuture ? '允许后续新投递' : '保留后续投递门'}</p>
      <p>{delivery.resolution.note} · {new Date(delivery.resolution.reportedAt * 1000).toLocaleString()} · 处置版本 {delivery.resolution.revision}</p>
    </div>}
    {error && <Notice>{error}</Notice>}
    {unresolved && <>
      <p>主人报告不等于平台送达确认。处置不会重发这条消息，不会将未知结果记为平台已发送，也不会补造来源 ACK。</p>
      {pending && <p role="status">待核对原请求：{pending.requestId}；正文已冻结。</p>}
      <fieldset disabled={busy || Boolean(pending) || conflict} aria-label="主人处置草稿">
        <label className="field"><span>主人处置决定</span><select aria-label="主人处置决定" value={decision}
          onChange={(event) => { const value = event.target.value as AutonomyResolutionDecision; setDecision(value); if (value === 'keep_pending') setAllowFuture(false); }}>
          <option value="keep_pending">继续等待原回执</option><option value="reported_sent">主人报告已收到</option><option value="reported_not_sent">主人报告未收到</option>
        </select></label>
        <label className="field"><span>处置说明</span><textarea aria-label="处置说明" value={note} maxLength={1000}
          onChange={(event) => setNote(event.target.value)} /></label>
        <label className="field checkbox-field"><input type="checkbox" aria-label="允许后续新的自主投递" checked={allowFuture}
          disabled={decision === 'keep_pending'} onChange={(event) => setAllowFuture(event.target.checked)} />允许后续新的自主投递</label>
      </fieldset>
      {conflict ? <button type="button" disabled={busy} onClick={() => { setConflict(false); setError(''); clearForm(); }}>重新填写处置</button>
        : <button type="button" disabled={busy || !pending && (!note.trim() || note.trim().length > 1000)}
          onClick={() => void submit()}>{pending ? '重试原处置请求' : '记录主人处置'}</button>}
      <button type="button" disabled={busy} onClick={() => void refresh()}>核对原处置请求</button>
    </>}
  </fieldset>;
}

export function AutonomyReconciliation({ roleId, revision, deliveries, onState, refresh }: {
  roleId: string; revision: number; deliveries: AutonomyDelivery[]; onState: (value: AutonomyState) => void; refresh: () => Promise<void>;
}) {
  return <section aria-label="自主投递与对账">
    {deliveries.map((delivery) => <DeliveryReconciliation key={`${roleId}-${delivery.intentId}`} roleId={roleId} revision={revision}
      delivery={delivery} onState={onState} refresh={refresh} />)}
  </section>;
}
