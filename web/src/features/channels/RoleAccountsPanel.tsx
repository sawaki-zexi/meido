import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { IconButton } from "../../ui/Icon";
import { Notice } from "../../ui/Status";
import { AccountDetailDialog } from "./AccountDetailDialog";
import { accountStatus, channels, type Account, type Channel, type ChannelSettings } from "./channelAccounts";
import "./role-accounts.css";

export function RoleAccountsPanel({ roleId, onOpenSettings }: { roleId: string; onOpenSettings?: () => void }) {
  return <RoleAccountsForRole key={roleId} roleId={roleId} onOpenSettings={onOpenSettings} />;
}

function RoleAccountsForRole({ roleId, onOpenSettings }: { roleId: string; onOpenSettings?: () => void }) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [settings, setSettings] = useState<ChannelSettings[]>([]);
  const [selected, setSelected] = useState<Channel | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const request = useRef(0);
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
  return <section className="role-accounts" aria-label="角色账号">
    <div className="role-accounts-heading"><div><h3>渠道账号</h3><p>为角色连接各平台账号，在对应平台与她聊天。</p></div><div className="role-accounts-actions">
      {onOpenSettings && <button type="button" onClick={onOpenSettings}>平台总开关</button>}
      <IconButton icon="refresh" label="刷新账号" disabled={loading} onClick={() => void load()} />
    </div></div>
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    {loading && settings.length === 0 ? <p role="status" className="role-accounts-empty">正在读取账号…</p> : <div className="role-account-list">
      {channels.map((channel) => {
        const account = accounts.find((item) => item.channel === channel.id) ?? null;
        return <button type="button" className="role-account-row" key={channel.id} aria-label={`${channel.name}${account?.displayName ? ` ${account.displayName}` : ""} ${accountStatus(account, platformEnabled(channel.id))}`} disabled={settings.length === 0} onClick={() => setSelected(channel.id)}>
          {account?.avatarUrl ? <img src={account.avatarUrl} alt="" className="role-account-avatar" /> : <span className={`role-account-mark ${channel.id}`}>{channel.id === "telegram" ? "T" : channel.id === "qq" ? "Q" : channel.id === "qqbot" ? "B" : "飞"}</span>}
          <span className="role-account-copy"><strong>{channel.name}</strong><small>{account?.displayName || channel.summary}</small></span>
          <span className={`role-account-status${platformEnabled(channel.id) && account?.enabled && account.connectionStatus === "connected" ? " connected" : ""}`}>{accountStatus(account, platformEnabled(channel.id))}</span><span aria-hidden="true">›</span>
        </button>;
      })}
    </div>}
    <p className="role-accounts-hint">主人身份和配对码在设置 → 我的身份中管理。</p>
    {selected && <AccountDetailDialog key={`${roleId}:${selected}`} roleId={roleId} channel={selected} account={selectedAccount} platformEnabled={platformEnabled(selected)} onOpenSettings={onOpenSettings} onClose={() => setSelected(null)} onAccountChange={(account) => {
      setAccounts((current) => [...current.filter((item) => item.channel !== selected), ...(account ? [account] : [])]);
    }} />}
  </section>;
}
