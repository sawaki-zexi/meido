import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { Icon, IconButton, type IconName } from "../../ui/Icon";
import { Notice } from "../../ui/Status";
import { AccountDetail } from "./AccountDetail";
import { ChannelLogo } from "./ChannelLogo";
import { accountStatus, channels, type Account, type Channel, type ChannelSettings } from "./channelAccounts";
import "./role-accounts.css";

type RoleAccountsProps = { roleId: string; onOpenSettings?: () => void; workspace?: boolean } & (
  { presentation: "page"; onBack: () => void } | { presentation?: "dialog"; onBack?: never }
);

export function RoleAccountsPanel(props: RoleAccountsProps) {
  return <RoleAccountsForRole key={props.roleId} {...props} />;
}

const workspaceChannels: { id: Channel; name: string; icon: IconName; summary?: string }[] = [
  { id: 'feishu', name: '飞书 / Lark', icon: 'building' }, { id: 'qq', name: 'QQ', icon: 'chat' },
  { id: 'qqbot', name: 'QQ 官方机器人', icon: 'bot' }, { id: 'telegram', name: 'Telegram', icon: 'send' },
];

function RoleAccountsForRole({ roleId, onOpenSettings, presentation = "dialog", onBack, workspace = false }: RoleAccountsProps) {
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
  return <section className={`role-accounts${workspace ? ' is-workspace' : ''}`} aria-label="角色账号">
    {presentation === "page" && onBack && <header className="channel-phone-settings-header"><IconButton icon="back" label="返回多端会话" onClick={onBack} /><h2>设置</h2></header>}
    <div className="role-accounts-content">
    <div className="role-accounts-heading"><div><h3>{workspace ? '账号' : '渠道账号'}</h3>{!workspace && <p>为角色连接各平台账号，在对应平台与她聊天。</p>}</div><div className="role-accounts-actions">
      {onOpenSettings && (workspace ? <IconButton icon="settings" label="平台总开关" onClick={onOpenSettings} /> : <button type="button" onClick={onOpenSettings}>平台总开关</button>)}
      <IconButton icon="refresh" label="刷新账号" disabled={loading} onClick={() => void load()} />
    </div></div>
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    {loading && settings.length === 0 ? <p role="status" className="role-accounts-empty">正在读取账号…</p> : <div className="role-account-list">
      {(workspace ? workspaceChannels : channels).map((channel) => {
        const account = accounts.find((item) => item.channel === channel.id) ?? null;
        const status = workspace && !account ? '未添加' : accountStatus(account, platformEnabled(channel.id));
        return <button type="button" className="role-account-row" key={channel.id} ref={node => { if (node) accountRows.current.set(channel.id, node); else accountRows.current.delete(channel.id); }} aria-label={`${channel.name}${account?.displayName ? ` ${account.displayName}` : ""} ${status}`} disabled={settings.length === 0} onClick={() => setSelected(channel.id)}>
          {account?.avatarUrl ? <img src={account.avatarUrl} alt="" className="role-account-avatar" /> : workspace ? <span className="role-account-icon"><Icon name={workspaceChannels.find(item => item.id === channel.id)!.icon} size={23} /></span> : <span className={`role-account-mark channel-brand-mark ${channel.id}`}><ChannelLogo channel={channel.id} /></span>}
          <span className="role-account-copy"><strong>{channel.name}</strong><small>{workspace ? [account?.displayName, status].filter(Boolean).join(' · ') : account?.displayName || channel.summary}</small></span>
          {workspace ? <span className="role-account-trailing"><Icon name={account ? 'settings' : 'plus'} size={20} /></span> : <><span className={`role-account-status${platformEnabled(channel.id) && account?.enabled && account.connectionStatus === "connected" ? " connected" : ""}`}>{status}</span><span aria-hidden="true">›</span></>}
        </button>;
      })}
    </div>}
    {!workspace && <p className="role-accounts-hint">主人身份和配对码在设置 → 我的身份中管理。</p>}
    </div>
    {detail}
  </section>;
}
