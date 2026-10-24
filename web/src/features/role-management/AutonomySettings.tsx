import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from '../../api/client';
import { proactive, type ProactiveSettings, type ProactiveState } from '../../api/proactive';

type Props = { roleId: string; disabled: boolean };
type NumberField = { key: keyof Omit<ProactiveSettings, 'revision' | 'enabled' | 'profile'>;
  label: string; min: number; max: number };
const pushFields: NumberField[] = [
  { key: 'agentMaxSteps', label: '每次推送最大步数', min: 1, max: 35 },
  { key: 'agentContentLimit', label: '候选内容数', min: 1, max: 5 },
  { key: 'agentWebFetchMaxChars', label: '网页上下文字符数', min: 256, max: 8000 },
];
const idleFields: NumberField[] = [
  { key: 'driftMaxSteps', label: '空闲活动最大步数', min: 1, max: 20 },
  { key: 'driftMinIntervalHours', label: '空闲活动最小间隔（小时）', min: 3, max: 168 },
];

export function AutonomySettings(props: Props) {
  return <ProactiveForm key={props.roleId} {...props} />;
}

function ProactiveForm({ roleId, disabled }: Props) {
  const [state, setState] = useState<ProactiveState | null>(null);
  const [draft, setDraft] = useState<ProactiveSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState('');
  const [notice, setNotice] = useState('');
  const generation = useRef(0);
  const load = useCallback(async () => {
    const request = ++generation.current;
    setBusy(true); setProblem(''); setNotice('');
    try {
      const value = await proactive.get(roleId);
      if (request === generation.current) { setState(value); setDraft(value.settings); }
    } catch (cause) {
      if (request === generation.current) setProblem(errorMessage(cause, '无法读取主动推送设置'));
    } finally { if (request === generation.current) setBusy(false); }
  }, [roleId]);
  useEffect(() => { void load(); return () => { ++generation.current; }; }, [load]);
  function change(patch: Partial<ProactiveSettings>) {
    setDraft(value => value && { ...value, ...patch }); setNotice('');
  }
  async function save() {
    if (!draft || busy || disabled) return;
    const request = ++generation.current;
    setBusy(true); setProblem(''); setNotice('');
    try {
      const value = await proactive.save(roleId, draft);
      if (request === generation.current) { setState(value); setDraft(value.settings); setNotice('已保存'); }
    } catch (cause) {
      if (request === generation.current) setProblem(errorMessage(cause, '无法保存主动推送设置'));
    } finally { if (request === generation.current) setBusy(false); }
  }
  const needsModel = draft?.enabled && !state?.modelReady;
  const fields = (items: NumberField[]) => <div className="settings-field-grid">{items.map(field =>
    <label className="field" key={field.key}>{field.label}<input type="number" required min={field.min} max={field.max}
      step={field.key === 'driftMinIntervalHours' ? 'any' : 1}
      value={Number.isFinite(draft?.[field.key]) ? draft![field.key] : ''}
      onChange={event => change({ [field.key]: event.target.value === '' ? NaN : Number(event.target.value) })} />
    </label>)}</div>;
  const invalid = draft && [...pushFields, ...idleFields].some(field => !Number.isFinite(draft[field.key])
    || draft[field.key] < field.min || draft[field.key] > field.max
    || field.key !== 'driftMinIntervalHours' && !Number.isInteger(draft[field.key]));
  return <form className="proactive-settings" aria-label="主动推送设置" onSubmit={event => { event.preventDefault(); void save(); }}>
    {problem && <p role="alert">{problem}</p>}
    {notice && <p role="status">{notice}</p>}
    {!draft && !problem && <p role="status">正在读取…</p>}
    {draft && <fieldset disabled={disabled || busy} className="settings-card proactive-settings-card">
      <legend className="settings-sr-only">主动推送</legend>
      <div className="settings-preference">
        <span><strong>主动推送</strong>{draft.enabled && <small>使用角色聊天模型，会产生模型用量。</small>}</span>
        <button type="button" role="switch" aria-label="主动推送" aria-checked={draft.enabled} className="settings-switch"
          onClick={() => change({ enabled: !draft.enabled })}><span /></button>
      </div>
      {needsModel && <p role="alert">请先配置角色的聊天模型。</p>}
      {draft.enabled && state?.activityNote && <p className="field-hint">{state.activityNote}</p>}
      <label className="field proactive-profile">推送策略<select value={draft.profile}
        onChange={event => change({ profile: event.target.value as ProactiveSettings['profile'] })}>
        <option value="daily">日常</option><option value="quiet">低打扰</option>
      </select></label>
      <details className="settings-disclosure proactive-execution">
        <summary>执行参数</summary>
        <div>
          {fields(pushFields)}
          <section className="settings-section"><h3>空闲活动</h3>{fields(idleFields)}</section>
        </div>
      </details>
      {invalid && <p role="alert">请填写有效的执行参数。</p>}
      {state?.cleanupPending && <p role="status">正在停止上一次活动…</p>}
      {state?.problem && <p role="alert">{state.problem}</p>}
      <div className="form-actions settings-save-actions"><button type="submit" className="primary"
        disabled={Boolean(needsModel || invalid)}>{busy ? '保存中…' : '保存'}</button></div>
    </fieldset>}
    {problem && <button type="button" disabled={busy || disabled} onClick={() => void load()}>重新读取</button>}
  </form>;
}
