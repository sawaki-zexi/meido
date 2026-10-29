import { useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { IconButton } from "../../ui/Icon";
import type { Account } from "./channelAccounts";
import { GroupListeningPanel } from "./GroupListeningPanel";
import "./channels.css";

export type GroupSummary = { chatId: string; title: string; threadId?: string | null };
type GroupState = GroupSummary & { repliesEnabled: boolean; listening: boolean; naturalEnabled: boolean; naturalEffective?: boolean };
type Props = {
  roleId: string;
  account: Account | null;
  initialGroup?: GroupSummary;
  onBack: () => void;
  onAccountSettings?: (section?: "rules" | "connection") => void;
  onOpenSettings?: () => void;
};

// Changing roles/accounts must also discard an in-flight directory or save.
export function GroupSettingsPanel(props: Props) {
  return <GroupSettingsForAccount key={`${props.roleId}:${props.account?.accountId ?? "removed"}:${props.initialGroup?.chatId ?? "directory"}`} {...props} />;
}

function GroupSettingsForAccount({ roleId, account, initialGroup, onBack, onAccountSettings, onOpenSettings }: Props) {
  const [groups, setGroups] = useState<GroupSummary[]>([]);
  const [selected, setSelected] = useState<GroupSummary | null>(initialGroup ?? null);
  const [manualId, setManualId] = useState("");
  const [loading, setLoading] = useState(Boolean(account));
  const [error, setError] = useState("");
  const [warning, setWarning] = useState("");
  const [reload, setReload] = useState(0);
  const backButton = useRef<HTMLButtonElement>(null);
  const base = `/api/roles/${encodeURIComponent(roleId)}/channels/accounts/${encodeURIComponent(account?.accountId ?? "")}/groups`;
  useEffect(() => { backButton.current?.focus(); }, []);
  useEffect(() => {
    if (!account) return;
    let live = true;
    setLoading(true); setError("");
    void api<{ groups: GroupSummary[]; directoryWarning?: string }>(base).then(data => {
      if (!live) return;
      setGroups(data.groups); setWarning(data.directoryWarning ?? "");
    }).catch(cause => { if (live) setError(errorMessage(cause, "无法读取群聊目录")); })
      .finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [base, reload, Boolean(account)]);

  return <section className="group-settings-page" aria-label="群聊设置">
    <header className="channel-phone-settings-header">
      <button type="button" ref={backButton} className="group-settings-back" onClick={onBack} aria-label="返回上一页">‹ 返回</button>
      <h2>群聊设置</h2>
    </header>
    <div className="group-settings-scroll">
      {selected ? <>
        {account && <button type="button" className="group-settings-choose" onClick={() => setSelected(null)}>选择其他群聊</button>}
        <SelectedGroupSettings key={selected.chatId} roleId={roleId} account={account} group={selected} base={base} onAccountSettings={onAccountSettings} onOpenSettings={onOpenSettings} />
      </> : <>
        <div className="group-settings-intro"><h3>选择要设置的 QQ 群</h3><p>按群控制回复、主动接话与旁听，不需要先在群里 @ 角色。</p></div>
        {warning && <p className="group-settings-hint" role="status">{warning}</p>}
        {error && <p className="group-settings-error" role="alert">{error}</p>}
        <div className="group-settings-directory-heading"><span>已知群聊</span><IconButton icon="refresh" label="刷新群聊" disabled={loading} onClick={() => setReload(value => value + 1)} /></div>
        {loading ? <p role="status">正在读取群聊…</p> : groups.length ? <ul className="group-settings-directory">{groups.map(group => <li key={group.chatId}><button type="button" onClick={() => setSelected(group)}><strong>{group.title || group.chatId}</strong><small>群号 {group.chatId}</small><span aria-hidden="true">›</span></button></li>)}</ul> : !error && <p className="group-settings-hint">暂无已知群聊。可刷新目录，或输入 QQ 群号设置。</p>}
        {account && <form className="group-settings-manual" onSubmit={event => { event.preventDefault(); const chatId = manualId.trim(); if (/^[1-9][0-9]{0,19}$/.test(chatId)) setSelected(groups.find(item => item.chatId === chatId) ?? { chatId, title: chatId }); }}>
          <label className="field">群号<input aria-label="群号" inputMode="numeric" value={manualId} onChange={event => setManualId(event.target.value)} placeholder="输入此 QQ 账号已加入的群号" /></label>
          <button type="submit" disabled={!/^[1-9][0-9]{0,19}$/.test(manualId.trim())}>打开群聊设置</button>
          <p>只保存设置，不会加入群聊或发送消息，也不创建聊天记录；账号须已加入该群才能生效。</p>
        </form>}
      </>}
    </div>
  </section>;
}

function SelectedGroupSettings({ roleId, account, group, base, onAccountSettings, onOpenSettings }: {
  roleId: string; account: Account | null; group: GroupSummary; base: string;
  onAccountSettings?: (section?: "rules" | "connection") => void; onOpenSettings?: () => void;
}) {
  const [state, setState] = useState<GroupState | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const live = useRef(false);
  const saving = useRef(false);
  const revision = useRef(0);
  const groupBase = `${base}/${encodeURIComponent(group.chatId)}`;
  useEffect(() => {
    live.current = true;
    const request = ++revision.current;
    setLoading(true); setError("");
    const read = account ? api<GroupState>(`${groupBase}/settings`)
      : api<{ enabled: boolean; repliesEnabled?: boolean; naturalEnabled?: boolean }>(`/api/roles/${encodeURIComponent(roleId)}/channels/threads/${encodeURIComponent(group.threadId ?? "")}/listening`).then(value => ({ ...group, repliesEnabled: value.repliesEnabled ?? true, listening: value.enabled, naturalEnabled: value.naturalEnabled ?? false, naturalEffective: false }));
    void read.then(value => { if (live.current && request === revision.current) setState(value); })
      .catch(cause => { if (live.current && request === revision.current) setError(errorMessage(cause, "无法读取群聊设置")); })
      .finally(() => { if (live.current && request === revision.current) setLoading(false); });
    return () => { live.current = false; revision.current += 1; };
  }, [groupBase, roleId, reload]);

  const change = async (kind: "replies" | "natural" | "listening", enabled: boolean) => {
    if (!account || !state || saving.current) return;
    if (kind === "natural" && enabled && (!state.listening || !account.allowGroup || state.repliesEnabled === false)) return;
    saving.current = true; setBusy(true); setError("");
    const request = ++revision.current;
    try {
      const saved = await api<GroupState>(`${groupBase}/${kind}`, { method: "PUT", body: JSON.stringify({ enabled }) });
      if (live.current && request === revision.current) setState(saved);
    } catch (cause) {
      if (live.current && request === revision.current) setError(errorMessage(cause, "保存群聊设置失败"));
    } finally {
      if (live.current && request === revision.current) { saving.current = false; setBusy(false); }
    }
  };

  const blockers: { text: string; action?: () => void; label?: string }[] = [];
  if (!account) blockers.push({ text: "账号已移除，历史设置仅供查看。" });
  else {
    if (!account.allowGroup) blockers.push({ text: "账号未开启“响应群聊”，请在响应规则中开启并保存。", action: onAccountSettings ? () => onAccountSettings("rules") : undefined, label: "打开响应规则" });
    if (account.platformEnabled === false) blockers.push({ text: "QQ 平台已暂停，恢复后才会接收和回复消息。", action: onOpenSettings, label: "打开平台设置" });
    if (!account.enabled) blockers.push({ text: "账号已暂停，连接后才会接收和回复消息。", action: onAccountSettings ? () => onAccountSettings("connection") : undefined, label: "打开账号设置" });
    else if (account.platformEnabled !== false && account.connectionStatus !== "connected") blockers.push({ text: account.connectionStatus === "connecting" ? "账号正在连接，连接成功后生效。" : account.connectionStatus === "error" ? "账号连接异常，请检查连接设置。" : account.connectionStatus === "unknown" || !account.connectionStatus ? "尚未确认账号连接状态，连接成功后生效。" : "账号离线，重新连接后生效。", action: onAccountSettings ? () => onAccountSettings("connection") : undefined, label: "打开账号设置" });
  }
  if (state && !state.listening) blockers.push({ text: "请先开启下方“本群旁听”，主动接话需要当前群的近期上下文。" });
  if (state?.repliesEnabled === false) blockers.push({ text: "本群回复已关闭，开启上方“允许本群回复”后才能主动接话。" });
  const effective = state?.naturalEnabled && blockers.length === 0;

  return <>
    <div className="group-settings-title"><h3>{state?.title || group.title || group.chatId}</h3><p>QQ 群 · {group.chatId}</p></div>
    {error && <p role="alert" className="group-settings-error">{error}</p>}
    {loading ? <p role="status">正在读取群聊设置…</p> : !state ? <button type="button" onClick={() => setReload(value => value + 1)}>重试读取群聊设置</button> : <>
      <section className="group-natural-card" aria-label="本群回复设置">
        <label className="group-settings-toggle"><span><strong>允许本群回复</strong><small>只控制当前群，不影响私聊和其他群。</small></span><input type="checkbox" aria-label="允许本群回复" checked={state.repliesEnabled !== false} disabled={!account || busy} onChange={event => void change("replies", event.target.checked)} /></label>
        <p className="group-settings-state" role="status">{state.repliesEnabled === false ? "已关闭 · @、引用和主动接话均不发送" : "已允许 · 回复仍遵守账号响应规则"}</p>
        <p className="group-settings-hint">关闭后，尚未发出的回复不再发送；重新开启不会补发旧回复。旁听与主动接话偏好会保留。</p>
      </section>
      <section className="group-natural-card" aria-label="群聊主动接话设置">
        <label className="group-settings-toggle"><span><strong>群聊主动接话（无需 @）</strong><small>角色可根据群聊内容主动接话，不保证每条都回复。</small></span><input type="checkbox" aria-label="群聊主动接话（无需 @）" checked={state.naturalEnabled} disabled={!account || busy || (!state.naturalEnabled && (!state.listening || !account.allowGroup || state.repliesEnabled === false))} onChange={event => void change("natural", event.target.checked)} /></label>
        <p className={`group-settings-state${effective ? " is-effective" : ""}`} role="status">{state.naturalEnabled ? effective ? "已开启 · 当前可主动接话" : "已保存开启 · 当前未生效" : "已关闭 · 不会主动接话"}</p>
        {blockers.length > 0 && <ul className="group-settings-blockers">{blockers.map(({ text, action, label }) => <li key={text}>{text}{action && <button type="button" onClick={action}>{label}</button>}</li>)}</ul>}
        <p className="group-settings-hint">主动接话不改变主人工具权限。仅关闭主动接话时，账号与本群都允许回复才能通过 @ 或引用角色消息聊天。</p>
      </section>
      <section className="group-listening-card" aria-label="旁听设置">
        <label className="group-settings-toggle"><span><strong>本群旁听</strong><small>保存群内未触发回复的消息；本群回复关闭时仍可旁听。</small></span><input type="checkbox" aria-label="开启本群旁听" checked={state.listening} disabled={!account || busy} onChange={event => void change("listening", event.target.checked)} /></label>
        <p className="group-settings-hint">关闭旁听会暂停主动接话，但保留主动接话的开关偏好。</p>
      </section>
      {state.threadId ? <GroupListeningPanel key={state.threadId} roleId={roleId} threadId={state.threadId} accountId={account?.accountId} chatId={group.chatId} editable={Boolean(account)} settingsManaged /> : <p className="group-settings-hint">暂无旁听记录。开启旁听后，新收到的群消息才会记录。</p>}
    </>}
  </>;
}
