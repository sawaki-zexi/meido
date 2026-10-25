import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import type { ModelConfiguration } from "../../api/types";
import { normalizeAutonomyConfiguration, normalizeAutonomyState, type AutonomyConfiguration as Configuration,
  type AutonomyState, type AutonomyTargetChoice } from "../../api/autonomy";
import { SourceVerificationPanel, type SourceVerification } from "./SourceVerificationPanel";
import { DriftSettings } from "./DriftSettings";
import { DriftMemeSettings, driftMemeConfigurationError } from "./DriftMemeSettings";
import { DriftSkillsPanel } from "./DriftSkillsPanel";
import { AutonomyTargets } from "./AutonomyTargets";
import { AutonomyReconciliation } from "./AutonomyReconciliation";
import { Notice, Placeholder } from "../../ui/Status";

type State = AutonomyState & { sourceVerifications?: SourceVerification[] };
function observedState(current: State | null, incoming: State): State {
  if (current && incoming.config.revision < current.config.revision) return current;
  const deliveries = new Map(incoming.deliveries?.map((item) => [item.intentId, item]));
  // Independent requests can return full snapshots in the same React batch.
  if (current?.deliveries?.some((known) => {
    const next = deliveries.get(known.intentId);
    return next && (next.resolutionRevision < known.resolutionRevision || known.status === "sent" && next.status !== "sent");
  })) return current;
  return normalizeAutonomyState(incoming);
}
const statusLabels: Record<string, string> = { sent: "已发送", skipped: "已跳过", no_events: "无新事件",
  aborted: "已中止", running: "运行中", preparing: "准备中", polled: "已轮询", rejected: "任务未获准",
  sources_unavailable: "来源不可用", duplicate: "重复消息已抑制", guard_unavailable: "重复检查不可用",
  blocked_uncertain: "等待投递对账", pending: "等待处理", inflight: "等待来源回执", retry_wait: "等待有限重试",
  acknowledged: "来源已确认", ack_uncertain: "来源确认不明", revoked: "原来源许可已撤销", expired: "原期限已过",
  unsupported: "未授权来源 ACK", decision_disabled: "主动决策已关闭", drift_dependency_pending: "Drift 尚未就绪",
  finished_silent: "合法静默结束", finished_sent: "合法发送结束", finish_failed: "合法结束，发送确定失败",
  finish_uncertain: "合法结束，投递结果不确定", failed: "运行失败，未合法 finish", cleanup_pending: "等待资源收尾" };
const deliveryLabels: Record<string, string> = { sent: "已发送，回执已确认", pending: "等待原投递回执",
  failed: "确定失败，不会再次发送", uncertain: "结果不确定，不会再次发送", sending: "正在核对原投递", dispatching: "正在核对原投递" };

export function AutonomyManagement({ roleId, disabled }: { roleId: string; disabled: boolean }) {
  const [state, setState] = useState<State | null>(null);
  const [draft, setDraft] = useState<Configuration | null>(null);
  const [sources, setSources] = useState("[]");
  const [models, setModels] = useState<ModelConfiguration[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [targetChoices, setTargetChoices] = useState<AutonomyTargetChoice[]>([]);
  const generation = useRef(0);
  const stateRequest = useRef(0);
  const operating = useRef(false);
  const stateRef = useRef(state);
  stateRef.current = state;
  const path = `/api/roles/${encodeURIComponent(roleId)}/autonomy`;

  const load = useCallback(async () => {
    const current = ++generation.current;
    ++stateRequest.current; operating.current = false;
    setState(null); setDraft(null); setTargetChoices([]); setError(""); setBusy(true); setModels([]);
    try {
      const [value, connections] = await Promise.all([api<State>(path), api<{ configurations: ModelConfiguration[] }>("/api/model/configurations")]);
      if (current !== generation.current) return;
      setState((currentState) => observedState(currentState, value)); setDraft(normalizeAutonomyConfiguration(value.config)); setSources(JSON.stringify(value.config.sourceGrants, null, 2));
      setModels(connections.configurations);
    } catch (cause) {
      if (current === generation.current) setError(errorMessage(cause, "自主设置暂不可读"));
    } finally {
      if (current === generation.current) setBusy(false);
    }
  }, [path]);

  useEffect(() => {
    void load();
    return () => { ++generation.current; };
  }, [load]);

  async function operate(stop = false) {
    if (!draft || disabled || busy || operating.current || (!stop && !ready)) return;
    const current = generation.current;
    const request = ++stateRequest.current;
    operating.current = true;
    setBusy(true); setError("");
    try {
      let value: State;
      if (stop) {
        value = await api<State>(`${path}/stop`, { method: "POST" });
      } else {
        const parsed: unknown = JSON.parse(sources);
        if (!Array.isArray(parsed) || parsed.length > 8 || parsed.some((item) => !item || typeof item !== "object" || Array.isArray(item))) {
          throw new Error("来源草稿须为最多8项的配置数组");
        }
        value = await api<State>(path, { method: "PUT", body: JSON.stringify({ expectedRevision: draft.revision,
          config: { ...draft, sourceGrants: parsed }, ...(targetChoices.length ? { targetSelections: targetChoices.map((choice) => choice.selection) } : {}) }) });
      }
      if (current !== generation.current || request !== stateRequest.current) return;
      setState((currentState) => observedState(currentState, value));
      if (!stop) { setTargetChoices([]); setDraft(normalizeAutonomyConfiguration(value.config)); setSources(JSON.stringify(value.config.sourceGrants, null, 2)); }
    } catch (cause) {
      if (current === generation.current) setError(errorMessage(cause, "自主操作失败，请刷新状态后重试"));
    } finally {
      if (current === generation.current) { operating.current = false; setBusy(false); }
    }
  }

  async function refresh() {
    const current = generation.current;
    const request = ++stateRequest.current;
    const revision = stateRef.current?.config.revision;
    try {
      const value = await api<State>(path);
      if (current === generation.current && request === stateRequest.current && stateRef.current?.config.revision === revision
        && value.config.revision >= (revision ?? 0)) { setState((currentState) => observedState(currentState, value)); setError(""); }
    } catch (cause) {
      if (current === generation.current && request === stateRequest.current) setError(errorMessage(cause, "自主状态暂不可读"));
    }
  }

  const enabled = Boolean(draft?.activeDecisionEnabled || draft?.driftEnabled);
  const configurationChanged = Boolean(draft && state && draft.revision !== state.config.revision);
  let hasUnsavedChanges = true;
  try { hasUnsavedChanges = !draft || !state || JSON.stringify({ ...draft, sourceGrants: JSON.parse(sources) })
    !== JSON.stringify(normalizeAutonomyConfiguration(state.config)); } catch { /* Invalid source JSON remains an unsaved draft. */ }
  const targetsReady = Boolean(draft && draft.allowedTargetRefs.length > 0 && draft.allowedTargetRefs.length <= 4
    && draft.allowedTargetRefs.includes(draft.defaultTargetRef)
    && draft.allowedTargetRefs.every((ref) => ref === "desktop" || targetChoices.some((choice) => choice.ref === ref)
      || state?.targetGrants?.some((grant) => grant.ref === ref && grant.current)));
  const memeError = draft ? driftMemeConfigurationError(draft, models) : "";
  const ready = !configurationChanged && !memeError && targetsReady && (!enabled || Boolean(draft?.consentRevision
    && (draft.inheritRoleModel || !draft.activeDecisionEnabled || models.some((model) => model.id === draft.decisionModelConfigurationId))
    && (draft.inheritRoleModel || !draft.driftEnabled || models.some((model) => model.id === draft.driftModelConfigurationId))));
  return <fieldset className="autonomy-settings" disabled={disabled || busy} aria-label="自主行为设置">
    <legend className="settings-sr-only">自主行为</legend>
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    {!draft || !state ? <>
      {busy ? <Placeholder loading>正在读取自主设置…</Placeholder> : !error && <Placeholder>尚未读取自主设置，请重新读取。</Placeholder>}
      <button type="button" onClick={() => void load()}>重新读取自主设置</button>
    </> : <>
      <label className="field checkbox-field"><input type="checkbox" checked={draft.inheritRoleModel ?? false}
        onChange={event => setDraft({ ...draft, inheritRoleModel: event.target.checked })} />使用角色聊天模型</label>
      <div className="autonomy-core-grid">
      <section className="settings-section" aria-label="主动推送配置">
      <h3>主动推送</h3>
      <label className="field checkbox-field"><input type="checkbox" aria-label="开启主动决策"
        checked={draft.activeDecisionEnabled} onChange={(event) => setDraft({ ...draft, activeDecisionEnabled: event.target.checked })} />
        <span>开启主动决策</span></label>
      {!draft.inheritRoleModel && <label className="field"><span>主动决策模型</span><select aria-label="主动决策模型" value={draft.decisionModelConfigurationId ?? ""}
        onChange={(event) => setDraft({ ...draft, decisionModelConfigurationId: event.target.value || null })}>
        <option value="">请选择已保存的模型连接</option>
        {draft.decisionModelConfigurationId && !models.some((model) => model.id === draft.decisionModelConfigurationId)
          && <option value={draft.decisionModelConfigurationId} disabled>原连接已不存在</option>}
        {models.map((model) => <option value={model.id} key={model.id}>{model.provider} · {model.model}（{model.id}）</option>)}
      </select></label>}
      </section>
      <DriftSettings key={`drift-${roleId}`} roleId={roleId} config={draft} models={models} onChange={(changes) => setDraft({ ...draft, ...changes })} />
      </div>
      <section className="settings-section">
      <h3>发送目标</h3>
      <AutonomyTargets key={`targets-${roleId}-${state.config.revision}`} roleId={roleId} revision={state.config.revision}
        config={draft} grants={state.targetGrants ?? []} choices={targetChoices}
        onChange={(changes, choices) => { setTargetChoices(choices); setDraft({ ...draft, ...changes }); }} />
      </section>
      <details className="settings-disclosure">
      <summary><span>表情库维护</span><small>{draft.driftMeme?.enabled ? "已开启" : "未开启"}</small></summary>
      <div>
      <DriftMemeSettings key={`memes-${roleId}-${draft.revision}`} config={draft} models={models} onChange={(changes) => setDraft({ ...draft, ...changes })} />
      </div>
      </details>
      {memeError && <p className="settings-validation" role="status">表情库维护：{memeError}</p>}
      <DriftSkillsPanel key={`skills-${roleId}`} roleId={roleId} config={normalizeAutonomyConfiguration(state.config)} hasUnsavedChanges={hasUnsavedChanges} />
      <details className="settings-disclosure">
      <summary><span>来源与验证</span><small>{state.config.sourceGrants.length} 个来源</small></summary>
      <div>
      <label className="field"><span>来源许可草稿</span><textarea aria-label="来源许可草稿" rows={7} value={sources}
        onChange={(event) => setSources(event.target.value)} /><small className="field-hint">填写 JSON 数组，绑定已授权的 MCP 来源；保存与预览不连接服务。</small></label>
      <SourceVerificationPanel key={roleId} roleId={roleId} revision={state.config.revision}
        sources={state.config.sourceGrants} attempts={state.sourceVerifications ?? []} refresh={refresh}
        importSources={(incoming) => {
          try {
            const existing: Record<string, unknown>[] = JSON.parse(sources);
            if (!Array.isArray(existing) || existing.some((item) => !item || typeof item !== "object" || Array.isArray(item))) {
              throw new Error("来源草稿无效，请先修正后再导入");
            }
            const identifiers = new Set(incoming.map((item) => item.sourceId));
            if (existing.some((item) => identifiers.has(item.sourceId))) throw new Error("来源 ID 已存在，请核对原许可并手动更新，避免覆盖");
            if (existing.length + incoming.length > 8) throw new Error("每角色最多8项来源");
            setSources(JSON.stringify([...existing, ...incoming], null, 2));
            return true;
          } catch (cause) { setError(errorMessage(cause, "来源草稿无法导入")); return false; }
        }}
        importProof={(proof) => {
          try {
            const parsed: Record<string, unknown>[] = JSON.parse(sources);
            if (!Array.isArray(parsed) || !parsed.some((source) => source.sourceId === proof.sourceId && source.revision === proof.sourceRevision)) {
              throw new Error("来源已变化，请保存配置后重新验证");
            }
            setSources(JSON.stringify(parsed.map((source) => source.sourceId === proof.sourceId
              ? { ...source, ackProofId: proof.proofId } : source), null, 2));
            return true;
          } catch (cause) { setError(errorMessage(cause, "来源草稿无效，请重新读取")); return false; }
        }} />
      </div>
      </details>
      <section className="settings-section">
      <h3>使用许可</h3>
      <label className="field checkbox-field"><input type="checkbox" aria-label="同意有限自主读取、消息和来源 ACK，可能产生模型费用"
        checked={draft.consentRevision > 0} onChange={(event) => setDraft({ ...draft, consentRevision: event.target.checked ? 1 : 0 })} />
        <span>同意有限自主读取、消息和来源 ACK，可能产生模型费用</span></label>
      <p className="field-hint">每天最多24次（全应用120次），单次最多12288输出 token；不代表实际费用。</p>
      </section>
      {configurationChanged && <p className="settings-validation" role="status">配置版本已变化；重新读取自主设置会替换未保存草稿，请核对后继续。</p>}
      <div className="form-actions settings-save-actions">
        <button type="button" className="ghost" onClick={() => void load()}>重新读取自主设置</button>
        <button type="button" className="primary" disabled={!ready} onClick={() => void operate()}>保存自主设置</button>
      </div>
      <div className="settings-inline-actions autonomy-runtime-actions">
        <button type="button" className="ghost" onClick={() => void refresh()}>刷新自主状态</button>
        <button type="button" className="danger ghost" onClick={() => void operate(true)}>停止当前自主运行</button>
      </div>
      {state.cleanupPending && <p role="status">正在回收自有资源；完成前不会开始新的自主运行。</p>}
      {state.humanBusy && <p role="status">主人回合正在进行，自主运行暂停。</p>}
      {state.admissionError && <p role="status">{state.admissionError}</p>}
      {Boolean(state.deliveries?.length) && <section className="settings-section">
      <h3>投递与对账</h3>
      <AutonomyReconciliation key={`reconciliation-${roleId}`} roleId={roleId} revision={state.config.revision}
        deliveries={state.deliveries ?? []} refresh={refresh} onState={(value) => {
          if (value.config.revision < (stateRef.current?.config.revision ?? 0)) return;
          ++stateRequest.current; setState((currentState) => observedState(currentState, value));
        }} />
      </section>}
      <details className="settings-disclosure">
      <summary><span>运行记录</span><small>{state.runs.length} 次运行</small></summary>
      <div className="autonomy-records">
      <div aria-label="最近自主运行">{state.runs.length === 0 ? <p>暂无自主运行</p> : <ul>{state.runs.slice(0, 20).map((run) =>
        <li key={run.id} aria-label={`自主运行 ${run.id}`}>
          <p>{run.mode === "drift" ? "Drift" : run.mode === "source_poll" ? "来源轮询" : "主动决策"} · {run.id} · {run.mode === "drift" && run.status === "aborted" ? "已中止，未合法 finish" : statusLabels[run.status] ?? run.status}</p>
          <p>{run.runtimeAcknowledged ? "Runtime 结果已交接" : "Runtime 结果尚未确认交接；仍在结算任务资源"}</p>
          {run.delivery && <p>消息投递：{deliveryLabels[run.delivery.status] ?? "尚未确认"}{run.delivery.messageId ? ` · 消息 ${run.delivery.messageId}` : ""}</p>}
          {run.delivery?.target && <p>冻结目标：{run.delivery.target.displayName || run.delivery.target.ref} · {run.delivery.target.channel}
            {run.delivery.target.accountId ? ` · 账号 ${run.delivery.target.accountId}` : ""}</p>}
          {(run.delivery?.attemptId || run.delivery?.platformMessageId) && <p>原 attempt：{run.delivery.attemptId || "尚无本地回执"} · 平台消息 ID：{run.delivery.platformMessageId || "尚未确认"}</p>}
        </li>)}</ul>}</div>
      <div aria-label="来源 ACK 状态">{state.acknowledgements.map((ack) => <p key={ack.id}>{ack.sourceId}：{statusLabels[ack.status] ?? ack.status}
        {` · 已尝试${ack.attempts}次 · `}{ack.permanent ? "永久处理记录" : ack.expiresAt ? `原期限 ${new Date(ack.expiresAt * 1000).toLocaleString()}` : "无期限"}</p>)}</div>
      </div>
      </details>
    </>}
  </fieldset>;
}
