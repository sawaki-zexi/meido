import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";

type Heard = { id: string; sequence: number; content: string; createdAt: string; metadata: Record<string, unknown> };
type State = { enabled: boolean; dailyCap: number | null; defaultDailyCap: number; toggles: { enabled: boolean; operator: string; at: string }[] };
type Page = { messages: Heard[]; hasMore: boolean; nextBefore: number | null };

export function GroupListeningPanel({ roleId, threadId, editable, accountId, chatId }: { roleId: string; threadId: string; editable: boolean; accountId?: string; chatId?: string }) {
  const [state, setState] = useState<State | null>(null);
  const [messages, setMessages] = useState<Heard[]>([]);
  const [cap, setCap] = useState("");
  const [defaultCap, setDefaultCap] = useState("");
  const [cursor, setCursor] = useState<number | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  const refreshRequest = useRef(0);
  const initialized = useRef(false);
  const settingsInitialized = useRef(false);
  const base = `/api/roles/${encodeURIComponent(roleId)}/channels/threads/${encodeURIComponent(threadId)}/listening`;
  const load = useCallback(async () => {
    const current = generation.current;
    const request = ++refreshRequest.current;
    try {
      const [settings, page] = await Promise.all([api<State>(base), api<Page>(`${base}/messages`)]);
      if (current !== generation.current || request !== refreshRequest.current) return;
      setState(settings);
      if (!settingsInitialized.current) {
        setCap(settings.dailyCap === null ? "" : String(settings.dailyCap)); setDefaultCap(String(settings.defaultDailyCap));
        settingsInitialized.current = true;
      }
      if (!initialized.current) {
        setCursor(page.nextBefore); setHasMore(page.hasMore);
      }
      setMessages((items) => [...new Map([...items, ...page.messages].map((item) => [item.id, item])).values()].sort((a, b) => a.sequence - b.sequence));
      initialized.current ||= page.messages.length > 0;
      setError("");
    } catch (cause) { if (current === generation.current && request === refreshRequest.current) setError(errorMessage(cause, "无法读取旁听记录")); }
  }, [base]);
  useEffect(() => {
    generation.current += 1;
    initialized.current = false;
    settingsInitialized.current = false;
    setMessages([]); setState(null); setHasMore(false); setCursor(null);
    void load();
    const events = typeof EventSource === "undefined" ? null : new EventSource(`/api/roles/${encodeURIComponent(roleId)}/channels/events`);
    events?.addEventListener("channel_update", (event) => {
      try {
        const payload = JSON.parse((event as MessageEvent).data ?? "{}");
        if (payload.type === "listening_cleared" && payload.threadId === threadId) {
          generation.current += 1; initialized.current = false;
          setMessages([]); setCursor(null); setHasMore(false); setBusy(false);
        }
      } catch { /* A transport reconnect still refreshes the current thread. */ }
      void load();
    });
    return () => { generation.current += 1; events?.close(); };
  }, [load, roleId, threadId]);
  const older = async () => {
    if (busy || cursor === null) return;
    setBusy(true);
    const current = generation.current;
    try {
      const page = await api<Page>(`${base}/messages?before=${cursor}`);
      if (current !== generation.current) return;
      setMessages((items) => [...new Map([...page.messages, ...items].map((item) => [item.id, item])).values()].sort((a, b) => a.sequence - b.sequence));
      setCursor(page.nextBefore); setHasMore(page.hasMore);
    } catch (cause) { if (current === generation.current) setError(errorMessage(cause, "无法读取更早旁听记录")); }
    finally { if (current === generation.current) setBusy(false); }
  };
  const save = async () => {
    const groupValue = cap.trim() === "" ? null : Number(cap);
    const defaultValue = Number(defaultCap);
    if ((groupValue !== null && (!Number.isInteger(groupValue) || groupValue < 1)) || !Number.isInteger(defaultValue) || defaultValue < 1) { setError("每日入库上限必须是正整数"); return; }
    setBusy(true);
    try {
      await api(`${base}/cap`, { method: "PUT", body: JSON.stringify({ dailyCap: groupValue }) });
      await api("/api/channels/listening/defaults", { method: "PUT", body: JSON.stringify({ defaultDailyCap: defaultValue }) });
      await load();
    } catch (cause) { setError(errorMessage(cause, "保存旁听上限失败")); }
    finally { setBusy(false); }
  };
  const change = async (enabled?: boolean) => {
    if (!accountId || !chatId || !editable || busy) return;
    if (enabled === undefined && !window.confirm("清理本群旁听记录？正常对话会保留。")) return;
    setBusy(true); setError("");
    const current = generation.current;
    const group = `/api/roles/${encodeURIComponent(roleId)}/channels/accounts/${encodeURIComponent(accountId)}/groups/${encodeURIComponent(chatId)}/listening`;
    try {
      if (enabled === undefined) {
        await api(`${group}/messages`, { method: "DELETE" });
        if (current !== generation.current) return;
        generation.current += 1; initialized.current = false;
        setMessages([]); setCursor(null); setHasMore(false);
      } else {
        await api(group, { method: "PUT", body: JSON.stringify({ enabled }) });
        if (current !== generation.current) return;
        setState((value) => value ? { ...value, enabled } : value);
      }
    } catch (cause) { if (current === generation.current) setError(errorMessage(cause, "更新本群旁听失败")); }
    finally { setBusy(false); }
  };
  return <details className="channel-listening-panel">
    <summary>群旁听记录{state ? ` · ${state.enabled ? "已开启" : "已暂停"}` : ""}</summary>
    <p>未点名角色的消息单独保存。超过每日上限的消息只保留在最近窗口，不在此处显示。</p>
    {error && <p role="status">{error}</p>}
    {state && accountId && chatId && <div className="channel-listening-actions">
      <label><input type="checkbox" aria-label="开启本群旁听" checked={state.enabled} disabled={!editable || busy} onChange={(event) => void change(event.target.checked)} />开启本群旁听</label>
      <button type="button" disabled={!editable || busy} onClick={() => void change()}>清理本群旁听记录</button>
    </div>}
    {state && <div className="channel-listening-caps">
      <label>本群每日上限<input aria-label="本群每日旁听上限" type="number" min="1" value={cap} onChange={(e) => setCap(e.target.value)} placeholder={`默认 ${state.defaultDailyCap}`} disabled={!editable || busy} /></label>
      <label>默认每日上限<input aria-label="默认每日旁听上限" type="number" min="1" value={defaultCap} onChange={(e) => setDefaultCap(e.target.value)} disabled={!editable || busy} /></label>
      <button type="button" disabled={!editable || busy} onClick={() => void save()}>保存旁听上限</button>
    </div>}
    {hasMore && <button type="button" disabled={busy} onClick={() => void older()}>读取更早旁听记录</button>}
    <ol>{messages.map((message) => <li key={message.id}><strong>{String(message.metadata.senderName || message.metadata.senderId || "成员")}</strong><p>{message.content || "（无内容）"}</p><time>{new Date(message.createdAt).toLocaleString()}</time></li>)}</ol>
    {messages.length === 0 && <p>暂无旁听记录</p>}
    {state && state.toggles.length > 0 && <details><summary>旁听开关记录</summary><ul>{state.toggles.map((toggle, index) => <li key={`${toggle.at}:${index}`}>{toggle.operator === "role" ? "角色" : "主人"}于 {new Date(toggle.at).toLocaleString()} {toggle.enabled ? "开启" : "暂停"}旁听</li>)}</ul></details>}
  </details>;
}
