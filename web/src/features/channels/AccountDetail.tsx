import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { Dialog } from "../../ui/Dialog";
import { Icon } from "../../ui/Icon";
import { Notice } from "../../ui/Status";
import { accountStatus, channelName, channels, defaultCapabilities, type Account, type Channel } from "./channelAccounts";
import { ManagedQQLogin } from "./ManagedQQLogin";
import { ChannelLogo } from "./ChannelLogo";
import { GroupSettingsPanel } from "./GroupSettingsPanel";

type ConnectionForm = {
  displayName: string;
  endpoint: string;
  appId: string;
  token: string;
  accessToken: string;
  clientSecret: string;
  appSecret: string;
  verificationToken: string;
  encryptKey: string;
  webhookKey: string;
  maxMessages: number;
  allowPrivate: boolean;
  allowGroup: boolean;
  blockedSenders: string;
};

export function AccountDetail({ roleId, channel, account, platformEnabled, onAccountChange, onClose, onOpenSettings, presentation = "dialog" }: {
  roleId: string;
  channel: Channel;
  account: Account | null;
  platformEnabled: boolean;
  onAccountChange: (account: Account | null) => void;
  onClose: () => void;
  onOpenSettings?: () => void;
  presentation?: "dialog" | "page";
}) {
  const [form, setForm] = useState<ConnectionForm>(() => ({
    displayName: account?.displayName ?? "", endpoint: account?.endpoint ?? "", appId: account?.appId ?? account?.platformId ?? "",
    token: "", accessToken: "", clientSecret: "", appSecret: "", verificationToken: "", encryptKey: "",
    webhookKey: "", maxMessages: account?.maxMessages ?? 12,
    allowPrivate: account?.allowPrivate ?? true, allowGroup: account?.allowGroup ?? Boolean(defaultCapabilities[channel].group),
    blockedSenders: account?.blockedSenders.join("\n") ?? "",
  }));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [groupSettingsOpen, setGroupSettingsOpen] = useState(false);
  const groupSettingsButton = useRef<HTMLButtonElement>(null);
  const connectionButton = useRef<HTMLButtonElement>(null);
  const rulesSection = useRef<HTMLElement>(null);
  const returnFromGroup = useRef<"button" | "rules" | "connection" | null>(null);
  useLayoutEffect(() => {
    if (!groupSettingsOpen && returnFromGroup.current) {
      if (returnFromGroup.current === "rules") { rulesSection.current?.scrollIntoView?.({ block: "center" }); rulesSection.current?.querySelector<HTMLInputElement>('input[aria-label="响应群聊"]')?.focus(); }
      else if (returnFromGroup.current === "connection") connectionButton.current?.focus();
      else groupSettingsButton.current?.focus();
      returnFromGroup.current = null;
    }
  }, [groupSettingsOpen]);
  const [manualQQ, setManualQQ] = useState(false);
  const live = useRef(true);
  const credentialsDetails = useRef<HTMLDetailsElement>(null);
  const previousAccountId = useRef(account?.accountId);
  const backButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  useEffect(() => { if (presentation === "page") backButton.current?.focus(); }, [presentation]);
  useEffect(() => {
    if (previousAccountId.current === account?.accountId) return;
    previousAccountId.current = account?.accountId;
    if (account) setForm((current) => ({ ...current,
      displayName: account.displayName, endpoint: account.endpoint ?? "", appId: account.appId ?? account.platformId ?? "",
      allowPrivate: account.allowPrivate, allowGroup: account.allowGroup, blockedSenders: account.blockedSenders.join("\n"),
    }));
  }, [account]);
  const capabilities = { ...defaultCapabilities[channel], ...account?.capabilities };
  const canConnect = platformEnabled && account?.platformEnabled !== false;
  const connected = canConnect && account?.enabled && account.connectionStatus === "connected";
  const base = `/api/roles/${encodeURIComponent(roleId)}/channels`;
  const update = <K extends keyof ConnectionForm>(key: K, value: ConnectionForm[K]) => setForm((current) => ({ ...current, [key]: value }));
  const rules = () => ({
    displayName: form.displayName.trim(), allowPrivate: form.allowPrivate,
    allowGroup: Boolean(capabilities.group && form.allowGroup),
    blockedSenders: form.blockedSenders.split(/[\n,]/).map((item) => item.trim()).filter(Boolean),
  });
  const valid = channel === "telegram" ? Boolean(form.token.trim() || account?.tokenConfigured)
    : channel === "grok_bot" ? Boolean(form.endpoint.trim() && (form.webhookKey.trim() || account?.credentialsConfigured) && Number.isInteger(form.maxMessages) && form.maxMessages >= 2 && form.maxMessages <= 100)
    : channel === "qq" ? Boolean(form.endpoint.trim())
      : Boolean(form.appId.trim() && (account?.tokenConfigured || (channel === "qqbot" ? form.clientSecret.trim() : form.appSecret.trim())));

  async function action(task: () => Promise<void>, fallback: string) {
    setBusy(true); setError(""); setNotice("");
    try { await task(); }
    catch (cause) { if (live.current) setError(errorMessage(cause, fallback)); }
    finally { if (live.current) setBusy(false); }
  }

  const saveConnection = () => action(async () => {
    const body: Record<string, unknown> = { ...rules(), enabled: account?.enabled ?? false, ...(account ? { accountId: account.accountId } : {}) };
    const keys: (keyof Pick<ConnectionForm, "token" | "endpoint" | "appId" | "accessToken" | "clientSecret" | "appSecret" | "verificationToken" | "encryptKey" | "webhookKey">)[] = channel === "telegram" ? ["token"] : channel === "qq" ? ["endpoint", "accessToken"] : channel === "qqbot" ? ["endpoint", "appId", "clientSecret"] : channel === "grok_bot" ? ["endpoint", "webhookKey"] : ["endpoint", "appId", "appSecret", "verificationToken", "encryptKey"];
    if (channel === "grok_bot") body.maxMessages = form.maxMessages;
    for (const key of keys) {
      if (form[key].trim()) body[key] = form[key].trim();
    }
    const result = await api<{ account: Account }>(`${base}/${channel}`, { method: "PUT", body: JSON.stringify(body) });
    if (!live.current) return;
    onAccountChange(result.account);
    setForm((current) => ({ ...current, token: "", accessToken: "", clientSecret: "", appSecret: "", verificationToken: "", encryptKey: "", webhookKey: "" }));
    if (credentialsDetails.current) credentialsDetails.current.open = false;
    if (!account && !result.account.enabled) {
      const enabled = await api<{ account: Account }>(`${base}/accounts/${encodeURIComponent(result.account.accountId)}/state`, { method: "POST", body: JSON.stringify({ enabled: true }) });
      if (!live.current) return;
      onAccountChange(enabled.account);
    }
    setNotice("连接配置已保存");
  }, "保存连接配置失败");

  const setConnection = (enabled: boolean) => account && action(async () => {
    const result = await api<{ account: Account }>(`${base}/accounts/${encodeURIComponent(account.accountId)}/state`, { method: "POST", body: JSON.stringify({ enabled }) });
    if (live.current) onAccountChange(result.account);
  }, "修改连接状态失败");

  const saveRules = () => account && action(async () => {
    const result = await api<{ account: Account }>(`${base}/${channel}`, { method: "PUT", body: JSON.stringify({ ...rules(), accountId: account.accountId, enabled: account.enabled }) });
    if (live.current) { onAccountChange(result.account); setNotice("响应规则已保存"); }
  }, "保存响应规则失败");

  const test = () => account && action(async () => {
    const result = await api<{ account: Account }>(`${base}/${channel}/${encodeURIComponent(account.accountId)}/test`, { method: "POST" });
    if (live.current) { onAccountChange(result.account); setNotice(channel === "grok_bot" ? "本机入口已启用；尚未验证远端收发。" : "连接测试通过"); }
  }, "连接测试失败");

  const remove = () => account && window.confirm(`移除${channelName[channel]}账号？历史会话会保留。`) && action(async () => {
    await api(`${base}/accounts/${encodeURIComponent(account.accountId)}`, { method: "DELETE" });
    if (live.current) { onAccountChange(null); onClose(); }
  }, "移除账号失败");

  const fields = <fieldset disabled={busy || !canConnect} className="account-connection-fields">
    {channel !== "telegram" && account?.mode !== "managed" && <label className="field">{channel === "qq" ? "NapCat WebSocket 地址" : channel === "grok_bot" ? "Grok Webhook 地址" : "平台 API 地址"}<input type="url" value={form.endpoint} onChange={(event) => update("endpoint", event.target.value)} placeholder={channel === "qq" ? "ws://127.0.0.1:6700" : channel === "qqbot" ? "https://api.sgroup.qq.com" : channel === "grok_bot" ? "https://…" : "https://open.feishu.cn/open-apis"} /></label>}
    {channel === "grok_bot" && <><label className="field">Webhook 密钥<input type="password" autoComplete="new-password" value={form.webhookKey} onChange={(event) => update("webhookKey", event.target.value)} placeholder={account?.credentialsConfigured ? "已保存；留空则不更换" : "填写 Bot 的 Webhook 密钥"} /></label><label className="field">单次交流消息上限<input type="number" min={2} max={100} step={1} value={form.maxMessages} onChange={(event) => update("maxMessages", Number(event.target.value))} /></label><p>双方消息合计，默认 12 条。更换 Webhook 会建立新会话，旧历史保留。</p></>}
    {(channel === "qqbot" || channel === "feishu") && <label className="field">{channel === "qqbot" ? "QQ Bot App ID" : "飞书 App ID"}<input autoComplete="off" value={form.appId} onChange={(event) => update("appId", event.target.value)} /></label>}
    {channel === "telegram" && <label className="field">Telegram Bot Token<input type="password" autoComplete="new-password" value={form.token} onChange={(event) => update("token", event.target.value)} placeholder={account?.tokenConfigured ? "已保存；留空则不更换" : "从 BotFather 获取"} /></label>}
    {channel === "qq" && account?.mode !== "managed" && <label className="field">NapCat Access Token<input type="password" autoComplete="new-password" value={form.accessToken} onChange={(event) => update("accessToken", event.target.value)} placeholder={account?.tokenConfigured ? "已保存；留空则不更换" : "如 NapCat 配置了访问令牌"} /></label>}
    {channel === "qqbot" && <label className="field">QQ Bot Client Secret<input type="password" autoComplete="new-password" value={form.clientSecret} onChange={(event) => update("clientSecret", event.target.value)} placeholder={account?.tokenConfigured ? "已保存；留空则不更换" : "从 QQ 开放平台获取"} /></label>}
    {channel === "feishu" && <><label className="field">飞书 App Secret<input type="password" autoComplete="new-password" value={form.appSecret} onChange={(event) => update("appSecret", event.target.value)} placeholder={account?.tokenConfigured ? "已保存；留空则不更换" : "从飞书开放平台获取"} /></label><details className="account-disclosure"><summary>Webhook 设置（可选）</summary><div className="account-connection-fields"><label className="field">Verification Token<input type="password" autoComplete="new-password" value={form.verificationToken} onChange={(event) => update("verificationToken", event.target.value)} /></label><label className="field">Encrypt Key<input type="password" autoComplete="new-password" value={form.encryptKey} onChange={(event) => update("encryptKey", event.target.value)} /></label></div></details></>}
    <label className="field">显示名称<input value={form.displayName} onChange={(event) => update("displayName", event.target.value)} placeholder="可选" /></label>
    <div className="account-actions"><button type="button" className="primary" disabled={!valid && account?.mode !== "managed"} onClick={() => void saveConnection()}>{busy ? "正在保存…" : account ? "保存连接配置" : "保存并连接"}</button></div>
  </fieldset>;

  const body = <div className="account-detail">
      {account && presentation === "dialog" && <div className="account-summary">{account.avatarUrl && <img src={account.avatarUrl} alt="账号头像" className="role-account-avatar" />}<div><strong>{account.displayName || channelName[channel]}</strong>{account.platformId && <small>{account.platformId}</small>}</div><span className={`role-account-status${connected ? " connected" : ""}`}>{accountStatus(account, platformEnabled)}</span></div>}
      {!canConnect && <div className="account-platform-paused" role="status"><p>此平台已在全局设置中暂停。启用后才能连接账号。</p>{onOpenSettings && <button type="button" onClick={() => { onClose(); onOpenSettings(); }}>打开渠道设置</button>}</div>}
      {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
      {notice && <p className="account-feedback" role="status">{notice}</p>}
      {account?.lastError && <p role="status" className="account-error">连接错误：{account.lastError}</p>}
      {channel === "grok_bot" && <section className="account-response-rules" aria-label="Grok 回程配置"><h3>同机回程</h3><p>启用仅开放本机入口。需让 Grok 在已授权的同一台电脑执行回程客户端；不会自动向 Bot 发送测试任务。</p>{account?.routine && <><label className="field">Routine 配置<textarea readOnly rows={9} value={account.routine} /></label><button type="button" onClick={() => void action(async () => { await navigator.clipboard.writeText(account.routine!); if (live.current) setNotice("Routine 配置已复制"); }, "复制失败，可手动选择并复制")}>复制 Routine 配置</button></>}</section>}
      {account ? <><div className="account-actions"><button ref={connectionButton} type="button" disabled={busy || !canConnect} onClick={() => void setConnection(!account.enabled)}>{account.enabled ? "断开连接" : "连接"}</button>{(account.lastError || account.connectionStatus === "error") && <button type="button" disabled={busy || !canConnect} onClick={() => void setConnection(true)}>重试连接</button>}<button type="button" disabled={busy || !canConnect} onClick={() => void test()}>{channel === "grok_bot" ? "检查本机入口" : "测试连接"}</button></div><details className="account-disclosure" ref={credentialsDetails}><summary>更改连接配置</summary>{fields}</details></>
        : channel === "qq" ? <><ManagedQQLogin roleId={roleId} canConnect={canConnect} getRules={rules} onConnected={onAccountChange} /><details className="account-disclosure" onToggle={(event) => setManualQQ(event.currentTarget.open)}><summary>高级：手动连接已有 NapCat</summary>{manualQQ && fields}</details></> : fields}
      {channel === "qq" && account?.mode === "managed" && account.managedRef && !connected && (account.enabled ? <ManagedQQLogin roleId={roleId} existingAccount={{ managedRef: account.managedRef, accountId: account.accountId }} canConnect={canConnect} getRules={rules} onConnected={onAccountChange} /> : <p className="role-accounts-hint">点击连接以启动 QQ 扫码登录。</p>)}
      {account && channel === "qq" && <section className="account-group-entry"><div><h3>群聊</h3><p>按群设置群聊主动接话（无需 @）与旁听记录。</p></div><button ref={groupSettingsButton} type="button" onClick={() => setGroupSettingsOpen(true)}>群聊设置</button></section>}
      {account && (connected || channel === "qq") && <section ref={rulesSection} className="account-response-rules" aria-label="响应规则"><h3>响应规则</h3>{capabilities.group && <p>{channel === "qq" ? "默认在 @ 角色或回复角色消息时响应；也可在群聊设置中按群开启群聊主动接话（无需 @）。" : "群聊在 @ 角色或回复角色消息时响应。"}</p>}<fieldset disabled={busy}><label className="account-checkbox"><input type="checkbox" checked={form.allowPrivate} disabled={!capabilities.private} onChange={(event) => update("allowPrivate", event.target.checked)} />响应私聊</label>{capabilities.group && <label className="account-checkbox"><input type="checkbox" aria-label="响应群聊" checked={form.allowGroup} onChange={(event) => update("allowGroup", event.target.checked)} />响应群聊</label>}<label className="field">屏蔽发送者（ID 或平台别名）<textarea rows={3} value={form.blockedSenders} onChange={(event) => update("blockedSenders", event.target.value)} placeholder="每行一个 ID 或别名" /></label><div className="account-actions"><button type="button" onClick={() => void saveRules()}>保存响应规则</button></div></fieldset></section>}

      {account && <div className="account-remove"><button type="button" className="danger" disabled={busy} onClick={() => void remove()}>移除账号</button><small>历史会话会保留。</small></div>}
    </div>;

  if (groupSettingsOpen && account) {
    const groups = <GroupSettingsPanel roleId={roleId} account={{ ...account, platformEnabled: canConnect }} onBack={() => { returnFromGroup.current = "button"; setGroupSettingsOpen(false); }} onAccountSettings={(section) => { returnFromGroup.current = section === "rules" ? "rules" : "connection"; setGroupSettingsOpen(false); }} onOpenSettings={onOpenSettings ? () => { onClose(); onOpenSettings(); } : undefined} />;
    return presentation === "page" ? groups : <Dialog title="QQ 群聊设置" variant="compact" modalFocus onClose={() => { returnFromGroup.current = "button"; setGroupSettingsOpen(false); }}>{groups}</Dialog>;
  }

  if (presentation === "page") return <section className="channel-account-page" aria-label={`${channelName[channel]} 账号`}>
    <header className="channel-phone-settings-header">
      <button ref={backButton} type="button" className="icon-button" aria-label="返回渠道账号" onClick={onClose}><Icon name="back" size={20} /></button>
      <h2>{channelName[channel]} 账号</h2>
    </header>
    <div className="channel-account-scroll">
      <div className="channel-account-hero">
        {account?.avatarUrl ? <img src={account.avatarUrl} alt="账号头像" className="role-account-avatar" /> : <span className={`role-account-mark channel-brand-mark ${channel}`}><ChannelLogo channel={channel} /></span>}
        <h3>{account?.displayName || channelName[channel]}</h3>
        <p>{channels.find(item => item.id === channel)?.summary}</p>
        <span className={`role-account-status${connected ? " connected" : ""}`}>{accountStatus(account, platformEnabled)}</span>
      </div>
      {body}
    </div>
  </section>;
  return <Dialog title={`${channelName[channel]} 账号`} variant="compact" modalFocus onClose={onClose}>{body}</Dialog>;
}
