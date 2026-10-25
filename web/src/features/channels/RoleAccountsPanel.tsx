import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { IconButton } from "../../ui/Icon";
import { Notice } from "../../ui/Status";
import { AccountDetail } from "./AccountDetail";
import { accountStatus, channels, type Account, type Channel, type ChannelSettings } from "./channelAccounts";
import "./role-accounts.css";

type RoleAccountsProps = { roleId: string; onOpenSettings?: () => void } & (
  { presentation: "page"; onBack: () => void } | { presentation?: "dialog"; onBack?: never }
);

export function RoleAccountsPanel(props: RoleAccountsProps) {
  return <RoleAccountsForRole key={props.roleId} {...props} />;
}

function RoleAccountsForRole({ roleId, onOpenSettings, presentation = "dialog", onBack }: RoleAccountsProps) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [settings, setSettings] = useState<ChannelSettings[]>([]);
  const [selected, setSelected] = useState<Channel | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const request = useRef(0);
  const accountRows = useRef(new Map<Channel, HTMLButtonElement>());
  const returnToChannel = useRef<Channel | null>(null);
  useLayoutEffect(() => {
    if (!selected && returnToChannel.current) {
      accountRows.current.get(returnToChannel.current)?.focus();
      returnToChannel.current = null;
    }
  }, [selected]);
  const load = useCallback(async () => {
    const sequence = ++request.current;
    setLoading(true);
    try {
      const [accountData, settingData] = await Promise.all([
        api<{ accounts: Account[] }>(`/api/roles/${encodeURIComponent(roleId)}/channels/accounts`),
        api<{ channels: ChannelSettings[] }>("/api/channels/settings"),
      ]);
      if (sequence !== request.current) return;
      setAccounts(accountData.accounts);
      setSettings(settingData.channels);
      setError("");
    } catch (cause) {
      if (sequence === request.current) setError(errorMessage(cause, "无法读取渠道账号"));
    } finally {
      if (sequence === request.current) setLoading(false);
    }
  }, [roleId]);

  useEffect(() => {
    void load();
    if (typeof EventSource === "undefined") return () => { request.current += 1; };
    const source = new EventSource(`/api/roles/${encodeURIComponent(roleId)}/channels/events`);
    source.addEventListener("channel_update", () => { void load(); });
    return () => { request.current += 1; source.close(); };
  }, [load, roleId]);

  const selectedAccount = accounts.find((item) => item.channel === selected) ?? null;
  const platformEnabled = (channel: Channel) => settings.find((item) => item.channel === channel)?.enabled === true;
  const detail = selected && <AccountDetail key={`${roleId}:${selected}`} roleId={roleId} channel={selected} account={selectedAccount} platformEnabled={platformEnabled(selected)} onOpenSettings={onOpenSettings} presentation={presentation} onClose={() => {
    if (presentation === "page") returnToChannel.current = selected;
    setSelected(null);
  }} onAccountChange={(account) => {
    setAccounts((current) => [...current.filter((item) => item.channel !== selected), ...(account ? [account] : [])]);
  }} />;
  if (presentation === "page" && detail) return detail;
  return <section className="role-accounts" aria-label="角色账号">
    {presentation === "page" && onBack && <header className="channel-phone-settings-header"><IconButton icon="back" label="返回多端会话" onClick={onBack} /><h2>设置</h2></header>}
    <div className="role-accounts-content">
    <div className="role-accounts-heading"><div><h3>渠道账号</h3><p>为角色连接各平台账号，在对应平台与她聊天。</p></div><div className="role-accounts-actions">
      {onOpenSettings && <button type="button" onClick={onOpenSettings}>平台总开关</button>}
      <IconButton icon="refresh" label="刷新账号" disabled={loading} onClick={() => void load()} />
    </div></div>
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    {loading && settings.length === 0 ? <p role="status" className="role-accounts-empty">正在读取账号…</p> : <div className="role-account-list">
      {channels.map((channel) => {
        const account = accounts.find((item) => item.channel === channel.id) ?? null;
        return <button type="button" className="role-account-row" key={channel.id} ref={node => { if (node) accountRows.current.set(channel.id, node); else accountRows.current.delete(channel.id); }} aria-label={`${channel.name}${account?.displayName ? ` ${account.displayName}` : ""} ${accountStatus(account, platformEnabled(channel.id))}`} disabled={settings.length === 0} onClick={() => setSelected(channel.id)}>
          {account?.avatarUrl ? <img src={account.avatarUrl} alt="" className="role-account-avatar" /> : <span className={`role-account-mark ${channel.id}`}>{channel.id === "telegram" ? "T" : channel.id === "qq" ? "Q" : channel.id === "qqbot" ? "B" : "飞"}</span>}
          <span className="role-account-copy"><strong>{channel.name}</strong><small>{account?.displayName || channel.summary}</small></span>
          <span className={`role-account-status${platformEnabled(channel.id) && account?.enabled && account.connectionStatus === "connected" ? " connected" : ""}`}>{accountStatus(account, platformEnabled(channel.id))}</span><span aria-hidden="true">›</span>
        </button>;
      })}
    </div>}
    <p className="role-accounts-hint">主人身份和配对码在设置 → 我的身份中管理。</p>
    </div>
    {detail}
  </section>;
}
