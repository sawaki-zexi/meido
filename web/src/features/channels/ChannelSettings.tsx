import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import "./channel-settings.css";

type ChannelState = { channel: string; name: string; enabled: boolean; configuredAccounts: number; connectedAccounts: number };

export function ChannelSettings() {
  const [channels, setChannels] = useState<ChannelState[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const request = useRef(0);
  const load = useCallback(async () => {
    const current = ++request.current;
    try {
      const result = await api<{ channels: ChannelState[] }>("/api/channels/settings");
      if (current === request.current) { setChannels(result.channels); setError(""); }
    } catch (cause) { if (current === request.current) setError(errorMessage(cause, "无法读取渠道设置")); }
  }, []);
  useEffect(() => { void load(); return () => { request.current += 1; }; }, [load]);
  const toggle = async (channel: ChannelState) => {
    const current = ++request.current;
    setBusy(channel.channel); setError("");
    try {
      const result = await api<{ channels: ChannelState[] }>("/api/channels/settings", { method: "POST", body: JSON.stringify({ channel: channel.channel, enabled: !channel.enabled }) });
      if (current === request.current) setChannels(result.channels);
    } catch (cause) {
      // A failed shutdown can still have persisted the paused state.
      const message = errorMessage(cause, "修改渠道状态失败，请重试");
      try {
        const result = await api<{ channels: ChannelState[] }>("/api/channels/settings");
        if (current === request.current) setChannels(result.channels);
      } catch { /* Keep the last known state and the actionable error. */ }
      if (current === request.current) setError(message);
    } finally { if (current === request.current) setBusy(null); }
  };
  return <section className="channel-settings" aria-label="多端连接">
    <header><h3>平台总开关</h3><p>分别控制各平台的全部角色连接。账号在角色资料 → 账号中添加。</p></header>
    {error && <p role="alert">{error} <button type="button" disabled={busy !== null} onClick={() => void load()}>重新读取</button></p>}
    {channels === null && !error && <p role="status">正在读取渠道…</p>}
    {channels && <div className="channel-setting-card">{channels.map((channel) => <div className="channel-setting-row" role="group" aria-label={`${channel.name} 渠道`} key={channel.channel}>
      <span><strong>{channel.name}</strong><small>{channel.configuredAccounts} 个账号 · {channel.connectedAccounts} 个已连接</small></span>
      <div className="channel-setting-actions">
        <span className={`channel-enabled-state${channel.enabled ? " enabled" : ""}`} aria-live="polite">{channel.enabled ? "已启用" : "已暂停"}</span>
        <button type="button" className={channel.enabled ? "" : "primary"} aria-label={`${channel.enabled ? "暂停" : "启用"} ${channel.name} 渠道`} disabled={busy !== null} onClick={() => void toggle(channel)}>{busy === channel.channel ? "切换中…" : channel.enabled ? "暂停" : "启用"}</button>
      </div>
    </div>)}</div>}
    <p>暂停保留账号配置、配对身份和聊天记录；恢复后按各账号的连接状态重新连接。</p>
  </section>;
}
