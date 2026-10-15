import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Role } from "../../api/types";
import { api, errorMessage } from "../../api/client";
import { Icon, IconButton } from "../../ui/Icon";
import { Notice } from "../../ui/Status";
import { GroupListeningPanel } from "./GroupListeningPanel";
import { IdentityBindings } from "./IdentityBindings";
import { PushTargets } from "./PushTargets";
import type { MemoryOrigin } from "../memory/MemoryPanel";
import "./channels.css";

export type ChannelMemorySource = { origin: MemoryOrigin; messageId?: string };

type Channel = "telegram" | "qq" | "qqbot" | "feishu";
type Capabilities = {
  private?: boolean;
  group?: boolean;
  groupListening?: boolean;
  media?: boolean;
  replies?: boolean;
  streaming?: boolean;
  targets?: string[];
};
type Account = {
  accountId: string;
  channel: Channel;
  enabled: boolean;
  allowPrivate: boolean;
  allowGroup: boolean;
  blockedSenders: string[];
  displayName: string;
  platformId: string | null;
  endpoint?: string;
  appId?: string;
  lastError: string | null;
  tokenConfigured: boolean;
  credentialsConfigured?: boolean;
  capabilities?: Capabilities;
  connectionStatus?: "connected" | "disconnected" | "connecting" | "error" | "unknown";
  avatarUrl?: string | null;
};
type Thread = {
  threadId: string;
  sessionKey: string;
  channel: string;
  chatId: string;
  chatType: string;
  title: string;
  lastActivity: string;
  viaAccount?: ViaAccount | null;
  accountRemoved?: boolean;
  latestMessage?: LatestMessage | null;
  avatarUrl?: string | null;
};
type ViaAccount = { platform?: string; platformAccountId?: string; accountId?: string; displayName?: string; avatarUrl?: string | null };
type LatestMessage = { content: string; role: string; status?: string; createdAt: string };
type ConversationMessage = {
  id: string;
  sequence: number;
  role: string;
  content: string;
  createdAt: string;
  metadata: Record<string, unknown>;
};
type ChannelAttachment = { id: string; name: string; kind?: string };
type Group = { chatId: string; title: string; listening: boolean };
type AccountForm = {
  displayName: string;
  endpoint: string;
  appId: string;
  token: string;
  accessToken: string;
  clientSecret: string;
  appSecret: string;
  verificationToken: string;
  encryptKey: string;
  allowPrivate: boolean;
  allowGroup: boolean;
  blockedSenders: string;
};

const channels: { id: Channel; name: string; summary: string }[] = [
  { id: "telegram", name: "Telegram", summary: "Bot 私聊与群聊" },
  { id: "qq", name: "NapCat QQ", summary: "私聊、群聊与群旁听" },
  { id: "qqbot", name: "官方 QQBot", summary: "C2C 私聊" },
  { id: "feishu", name: "飞书", summary: "Bot 私聊" },
];
const channelName: Record<string, string> = {
  desktop: "桌面", telegram: "Telegram", qq: "NapCat QQ", qqbot: "官方 QQBot", feishu: "飞书",
};
const defaultCapabilities: Record<Channel, Capabilities> = {
  telegram: { private: true, group: true, groupListening: false, media: true, replies: true, streaming: false, targets: ["private", "group"] },
  qq: { private: true, group: true, groupListening: true, media: true, replies: true, streaming: false, targets: ["private", "group", "group_member"] },
  qqbot: { private: true, group: false, groupListening: false, media: true, replies: true, streaming: true, targets: ["private"] },
  feishu: { private: true, group: false, groupListening: false, media: true, replies: true, streaming: true, targets: ["private"] },
};
const newForm = (): AccountForm => ({
  displayName: "", endpoint: "", appId: "", token: "", accessToken: "", clientSecret: "", appSecret: "",
  verificationToken: "", encryptKey: "", allowPrivate: true, allowGroup: true, blockedSenders: "",
});

function accountStatus(account: Account | null, recentTest: boolean | null): string {
  if (!account) return "未配置";
  if (account.connectionStatus === "connected") return "已连接";
  if (account.connectionStatus === "connecting") return "正在连接";
  if (account.connectionStatus === "error" || account.lastError) return "连接异常";
  if (recentTest === true) return account.enabled ? "最近测试通过" : "最近测试通过 · 已暂停";
  if (!account.enabled || account.connectionStatus === "disconnected") return "已暂停";
  return "已启用 · 尚未确认连接";
}

function capabilityLabels(capabilities: Capabilities) {
  const targets = (capabilities.targets ?? []).map((target) => ({
    private: "私聊目标",
    group: "群聊目标",
    group_member: "群成员目标",
  }[target])).filter((item): item is string => Boolean(item));
  return [
    capabilities.private && "私聊",
    capabilities.group && "群聊",
    capabilities.groupListening && "按群旁听",
    capabilities.media && "媒体",
    capabilities.replies && "引用回复",
    capabilities.streaming && "流式回复",
    targets.length > 0 && `可发送：${targets.join("、")}`,
  ].filter((item): item is string => Boolean(item));
}

function sourceLabel(source: ViaAccount | null | undefined): string | null {
  if (!source) return null;
  const name = source.displayName || "渠道账号";
  const platform = channelName[source.platform ?? ""] ?? source.platform ?? "渠道";
  return `来源：${name} · ${platform}`;
}

function deliveryLabel(status: unknown): string | null {
  return ({ pending: "发送中", sent: "已发送", failed: "发送失败", uncertain: "发送结果不确定" } as Record<string, string>)[String(status)] ?? null;
}

function messageAttachments(value: unknown): ChannelAttachment[] {
  return Array.isArray(value) ? value.filter((item): item is ChannelAttachment => Boolean(
    item && typeof item === "object" && typeof item.id === "string" && typeof item.name === "string",
  )) : [];
}

function ChannelAttachments({ roleId, attachments }: { roleId: string; attachments: ChannelAttachment[] }) {
  return <>{attachments.map((attachment) => {
    const href = `/api/roles/${encodeURIComponent(roleId)}/attachments/${encodeURIComponent(attachment.id)}`;
    return attachment.kind === "image"
      ? <a href={href} target="_blank" rel="noreferrer" key={attachment.id}><img className="channel-attachment-image" src={href} alt={attachment.name} /></a>
      : <a className="channel-attachment-file" href={href} target="_blank" rel="noreferrer" key={attachment.id}>{attachment.name}</a>;
  })}</>;
}

export function ChannelView({ role, onBack, initialSource }: { role: Role; onBack: () => void; initialSource?: ChannelMemorySource | null }) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [channel, setChannel] = useState<Channel>("telegram");
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadsLoaded, setThreadsLoaded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [hasOlderMessages, setHasOlderMessages] = useState(false);
  const [olderCursor, setOlderCursor] = useState<number | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const messageThreadId = useRef<string | null>(null);
  const messageRoleId = useRef(role.id);
  const olderRequest = useRef(0);
  const [form, setForm] = useState<AccountForm>(newForm);
  const [pairingCode, setPairingCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sourceError, setSourceError] = useState("");
  const [sourceHighlightedId, setSourceHighlightedId] = useState<string | null>(null);
  const [listeningSource, setListeningSource] = useState<ConversationMessage | null>(null);
  const sourceRequest = useRef(0);
  const verifiedSourceThread = useRef<Thread | null>(null);
  const sourceElement = useRef<HTMLElement | null>(null);
  const listeningPanel = useRef<HTMLDivElement | null>(null);
  const [groupError, setGroupError] = useState("");
  const [groups, setGroups] = useState<Group[]>([]);
  const [loadingGroups, setLoadingGroups] = useState(false);
  const [busyGroup, setBusyGroup] = useState<string | null>(null);
  const [lastTest, setLastTest] = useState<{ accountId: string; ok: boolean } | null>(null);
  const account = accounts.find((item) => item.channel === channel) ?? null;
  const selectedThread = useMemo(() => threads.find((thread) => thread.threadId === selected) ?? null, [threads, selected]);
  const capabilities = account?.capabilities && Object.keys(account.capabilities).length
    ? { ...defaultCapabilities[channel], ...account.capabilities }
    : defaultCapabilities[channel];
  const testResult = account && lastTest?.accountId === account.accountId ? lastTest.ok : null;

  const load = useCallback(async () => {
    try {
      const [accountsData, threadsData] = await Promise.all([
        api<{ accounts: Account[] }>(`/api/roles/${encodeURIComponent(role.id)}/channels/accounts`),
        api<{ threads: Thread[] }>(`/api/roles/${encodeURIComponent(role.id)}/channels/threads`),
      ]);
      setAccounts(accountsData.accounts);
      const verified = verifiedSourceThread.current;
      const visibleThreads = verified && !threadsData.threads.some((item) => item.threadId === verified.threadId)
        ? [...threadsData.threads, verified] : threadsData.threads;
      setThreads(visibleThreads);
      setThreadsLoaded(true);
      setSelected((current) => current && visibleThreads.some((item) => item.threadId === current) ? current : null);
      setError("");
    } catch (cause) {
      setError(errorMessage(cause, "无法读取渠道会话"));
    }
  }, [role.id]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!initialSource || !threadsLoaded) return;
    const request = ++sourceRequest.current;
    let cancelled = false;
    const current = () => !cancelled && request === sourceRequest.current;
    const listedThread = threads.find((item) => item.sessionKey === initialSource.origin.sessionKey && item.channel !== "desktop");
    const listening = initialSource.origin.kind === "listening";
    setSourceError(""); setSourceHighlightedId(null); setListeningSource(null);
    const wantedId = initialSource.messageId ?? initialSource.origin.messageIds[0];
    const range = initialSource.origin.messageRange;
    const validRange = range && Number.isSafeInteger(range[0]) && Number.isSafeInteger(range[1]) && range[0] > 0 && range[1] >= range[0] && range[1] < Number.MAX_SAFE_INTEGER ? range : null;
    void (async () => {
      let before: number | null = validRange ? validRange[1] + 1 : null;
      const loaded: ConversationMessage[] = [];
      try {
        let thread = listedThread;
        if (!thread) {
          const threadId = initialSource.origin.sessionKey.startsWith("thread:") ? initialSource.origin.sessionKey.slice(7) : "";
          if (!threadId) { setSourceError("来源渠道会话当前不可访问，可能已删除或不属于此角色。"); return; }
          try {
            const proof = await api<{ thread: Thread }>(`/api/roles/${encodeURIComponent(role.id)}/channels/threads/${encodeURIComponent(threadId)}/messages?limit=1`);
            if (!current()) return;
            if (proof.thread?.sessionKey !== initialSource.origin.sessionKey || proof.thread.threadId !== threadId || proof.thread.channel === "desktop") {
              setSourceError("来源渠道会话当前不可访问，可能已删除或不属于此角色。"); return;
            }
            thread = proof.thread;
          } catch { if (current()) setSourceError("来源渠道会话当前不可访问，可能已删除或不属于此角色。"); return; }
          const verifiedThread = thread;
          setThreads((items) => items.some((item) => item.threadId === verifiedThread.threadId) ? items : [...items, verifiedThread]);
        }
        if (!current()) return;
        if (listening && (thread.channel !== "qq" || thread.chatType !== "group")) {
          setSourceError("来源旁听记录当前不可访问。"); return;
        }
        const verifiedThread = thread;
        verifiedSourceThread.current = verifiedThread;
        setSelected(verifiedThread.threadId);
        const base = `/api/roles/${encodeURIComponent(role.id)}/channels/threads/${encodeURIComponent(verifiedThread.threadId)}/${listening ? "listening/messages" : "messages"}`;
        // A strict cursor and a fixed page budget prevent stale origins
        // from causing an unbounded sequence of network requests.
        for (let page = 0; page < 20; page += 1) {
          const data = await api<{ messages: ConversationMessage[]; hasMore: boolean; nextBefore: number | null }>(`${base}?limit=100${before === null ? "" : `&before=${before}`}`);
          if (!current()) return;
          loaded.push(...data.messages);
          const found = data.messages.find((message) => wantedId ? message.id === wantedId : validRange && message.sequence >= validRange[0] && message.sequence <= validRange[1]);
          if (found) {
            if (listening) setListeningSource(found);
            else {
              setMessages((items) => [...new Map([...items.filter((item) => item.metadata.threadId === verifiedThread.threadId), ...loaded].map((item) => [item.id, item])).values()].sort((a, b) => a.sequence - b.sequence));
              setSourceHighlightedId(found.id);
            }
            return;
          }
          const next = data.nextBefore;
          if (!data.hasMore || next === null || !Number.isSafeInteger(next) || next < 1 || (before !== null && next >= before)) break;
          before = next;
        }
        setSourceError(listening ? "来源旁听记录当前不可访问，可能已清理或超出可自动定位的范围。" : "来源消息当前不可访问，可能已删除或超出可自动定位的范围。");
      } catch (cause) { if (current()) setSourceError(errorMessage(cause, "来源消息当前不可访问")); }
    })();
    return () => { cancelled = true; };
    // Ownership is checked against the role-scoped initial list. Live
    // refreshes must not restart pagination or replace the selected thread.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSource, threadsLoaded, role.id]);

  useEffect(() => {
    sourceElement.current?.scrollIntoView?.({ behavior: "smooth", block: "center" });
    if (listeningSource) listeningPanel.current?.querySelector("details")?.setAttribute("open", "");
  }, [sourceHighlightedId, listeningSource]);

  useEffect(() => () => { olderRequest.current += 1; }, []);

  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const source = new EventSource(`/api/roles/${encodeURIComponent(role.id)}/channels/events`);
    source.addEventListener("channel_update", () => { void load(); });
    source.onerror = () => { /* EventSource reconnects while the local API is available. */ };
    return () => source.close();
  }, [load, role.id]);

  useEffect(() => {
    setForm({
      ...newForm(),
      displayName: account?.displayName ?? "",
      endpoint: account?.endpoint ?? "",
      appId: account?.appId ?? account?.platformId ?? "",
      allowPrivate: account?.allowPrivate ?? true,
      allowGroup: account?.allowGroup ?? (channel === "qq" || channel === "telegram"),
      blockedSenders: account?.blockedSenders.join("\n") ?? "",
    });
    setPairingCode("");
    setLastTest(null);
  }, [account?.accountId, channel]);

  useEffect(() => {
    if (!selectedThread) {
      messageThreadId.current = null;
      messageRoleId.current = role.id;
      olderRequest.current += 1;
      setMessages([]);
      setHasOlderMessages(false);
      setOlderCursor(null);
      setLoadingOlder(false);
      return;
    }
    let cancelled = false;
    const threadId = selectedThread.threadId;
    const threadChanged = messageThreadId.current !== threadId || messageRoleId.current !== role.id;
    if (threadChanged) {
      messageThreadId.current = threadId;
      messageRoleId.current = role.id;
      olderRequest.current += 1;
      setMessages((items) => items.filter((item) => item.metadata.threadId === threadId));
      setHasOlderMessages(false);
      setOlderCursor(null);
      setLoadingOlder(false);
    }
    void api<{ messages: ConversationMessage[]; hasMore: boolean; nextBefore: number | null }>(`/api/roles/${encodeURIComponent(role.id)}/channels/threads/${encodeURIComponent(threadId)}/messages?limit=50`)
      .then((data) => {
        if (cancelled) return;
        setMessages((current) => {
          const merged = new Map(current.filter((message) => message.metadata.threadId === threadId).map((message) => [message.id, message]));
          data.messages.forEach((message) => merged.set(message.id, message));
          return [...merged.values()].sort((left, right) => left.sequence - right.sequence);
        });
        if (threadChanged) {
          setHasOlderMessages(data.hasMore);
          setOlderCursor(data.nextBefore);
        }
        setError("");
      })
      .catch((cause) => { if (!cancelled) setError(errorMessage(cause, "无法读取会话消息")); });
    return () => { cancelled = true; };
  }, [role.id, selectedThread]);

  const loadGroups = useCallback(async () => {
    if (!account || account.channel !== "qq") return;
    setLoadingGroups(true);
    setGroupError("");
    try {
      const data = await api<{ groups: Group[] }>(`/api/roles/${encodeURIComponent(role.id)}/channels/accounts/${encodeURIComponent(account.accountId)}/groups`);
      setGroups(data.groups);
    } catch (cause) {
      setGroupError(errorMessage(cause, "无法读取 NapCat 群列表"));
    } finally {
      setLoadingGroups(false);
    }
  }, [account?.accountId, account?.channel, role.id]);

  useEffect(() => {
    setGroups([]);
    setGroupError("");
    if (account?.channel === "qq") void loadGroups();
  }, [account?.accountId, account?.channel, loadGroups]);

  const loadOlderMessages = async () => {
    if (!selectedThread || olderCursor === null || loadingOlder) return;
    const threadId = selectedThread.threadId;
    const requestId = ++olderRequest.current;
    const isCurrent = () => messageThreadId.current === threadId && messageRoleId.current === role.id && olderRequest.current === requestId;
    setLoadingOlder(true);
    try {
      const data = await api<{ messages: ConversationMessage[]; hasMore: boolean; nextBefore: number | null }>(
        `/api/roles/${encodeURIComponent(role.id)}/channels/threads/${encodeURIComponent(threadId)}/messages?limit=50&before=${olderCursor}`,
      );
      if (!isCurrent()) return;
      setMessages((current) => {
        const merged = new Map(data.messages.map((message) => [message.id, message]));
        current.forEach((message) => merged.set(message.id, message));
        return [...merged.values()].sort((left, right) => left.sequence - right.sequence);
      });
      setHasOlderMessages(data.hasMore);
      setOlderCursor(data.nextBefore);
    } catch (cause) {
      if (isCurrent()) setError(errorMessage(cause, "无法读取更早的消息"));
    } finally {
      if (isCurrent()) setLoadingOlder(false);
    }
  };

  const updateForm = <K extends keyof AccountForm>(key: K, value: AccountForm[K]) => setForm((current) => ({ ...current, [key]: value }));

  const save = async () => {
    setBusy(true); setError("");
    const blockedSenders = form.blockedSenders.split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
    const body: Record<string, unknown> = {
      ...(account ? { accountId: account.accountId } : {}),
      enabled: account?.enabled ?? false,
      allowPrivate: form.allowPrivate,
      allowGroup: capabilities.group ? form.allowGroup : false,
      blockedSenders,
      displayName: form.displayName.trim(),
    };
    if (channel === "telegram") {
      if (form.token.trim()) body.token = form.token.trim();
    } else {
      if (form.endpoint.trim()) body.endpoint = form.endpoint.trim();
      if (channel === "qqbot" || channel === "feishu") body.appId = form.appId.trim();
      if (channel === "qq" && form.accessToken.trim()) body.accessToken = form.accessToken.trim();
      if (channel === "qqbot" && form.clientSecret.trim()) body.clientSecret = form.clientSecret.trim();
      if (channel === "feishu") {
        if (form.appSecret.trim()) body.appSecret = form.appSecret.trim();
        if (form.verificationToken.trim()) body.verificationToken = form.verificationToken.trim();
        if (form.encryptKey.trim()) body.encryptKey = form.encryptKey.trim();
      }
    }
    try {
      const endpoint = channel === "telegram" ? "telegram" : channel;
      const result = await api<{ account: Account }>(`/api/roles/${encodeURIComponent(role.id)}/channels/${endpoint}`, {
        method: "PUT", body: JSON.stringify(body),
      });
      setAccounts((current) => [...current.filter((item) => item.channel !== channel), result.account]);
      setForm((current) => ({ ...current, token: "", accessToken: "", clientSecret: "", appSecret: "", verificationToken: "", encryptKey: "" }));
      setLastTest(null);
      await load();
    } catch (cause) { setError(errorMessage(cause, `保存${channelName[channel]}设置失败`)); }
    finally { setBusy(false); }
  };

  const test = async () => {
    if (!account) return;
    setBusy(true); setError("");
    try {
      const path = channel === "telegram"
        ? `/api/roles/${encodeURIComponent(role.id)}/channels/telegram/${encodeURIComponent(account.accountId)}/test`
        : `/api/roles/${encodeURIComponent(role.id)}/channels/${channel}/${encodeURIComponent(account.accountId)}/test`;
      const result = await api<{ account: Account }>(path, { method: "POST" });
      setLastTest({ accountId: account.accountId, ok: true });
      setAccounts((current) => current.map((item) => item.accountId === account.accountId ? result.account : item));
      await load();
    } catch (cause) {
      setLastTest({ accountId: account.accountId, ok: false });
      setError(errorMessage(cause, "连接测试失败"));
      await load();
    } finally { setBusy(false); }
  };

  const toggle = async () => {
    if (!account) return;
    setBusy(true); setError("");
    try {
      await api(`/api/roles/${encodeURIComponent(role.id)}/channels/accounts/${encodeURIComponent(account.accountId)}/state`, {
        method: "POST", body: JSON.stringify({ enabled: !account.enabled }),
      });
      setLastTest(null);
      await load();
    } catch (cause) { setError(errorMessage(cause, "修改连接状态失败")); }
    finally { setBusy(false); }
  };

  const pair = async () => {
    setBusy(true); setError("");
    try {
      const endpoint = channel === "telegram" ? "telegram" : channel;
      const result = await api<{ code: string }>(`/api/roles/${encodeURIComponent(role.id)}/channels/${endpoint}/pairing`, { method: "POST" });
      setPairingCode(result.code);
    } catch (cause) { setError(errorMessage(cause, "生成配对码失败")); }
    finally { setBusy(false); }
  };

  const remove = async () => {
    if (!account || !window.confirm(`移除${channelName[channel]}账号？历史会话会保留。`)) return;
    setBusy(true); setError("");
    try {
      await api(`/api/roles/${encodeURIComponent(role.id)}/channels/accounts/${encodeURIComponent(account.accountId)}`, { method: "DELETE" });
      setAccounts((current) => current.filter((item) => item.accountId !== account.accountId));
      setPairingCode(""); setGroups([]); setLastTest(null); setForm(newForm());
      await load();
    } catch (cause) { setError(errorMessage(cause, "移除账号失败")); }
    finally { setBusy(false); }
  };

  const setGroupListening = async (group: Group, enabled: boolean) => {
    if (!account) return;
    setBusyGroup(group.chatId); setGroupError("");
    try {
      await api(`/api/roles/${encodeURIComponent(role.id)}/channels/accounts/${encodeURIComponent(account.accountId)}/groups/${encodeURIComponent(group.chatId)}/listening`, {
        method: "PUT", body: JSON.stringify({ enabled }),
      });
      setGroups((current) => current.map((item) => item.chatId === group.chatId ? { ...item, listening: enabled } : item));
    } catch (cause) { setGroupError(errorMessage(cause, "更新群旁听设置失败")); }
    finally { setBusyGroup(null); }
  };

  const visibleCapabilities = capabilityLabels(capabilities);
  const clearGroupListeningMessages = async (group: Group) => {
    if (!account || !window.confirm(`清理${group.title || group.chatId}的旁听记录？`)) return;
    setBusyGroup(group.chatId); setGroupError("");
    try {
      await api(`/api/roles/${encodeURIComponent(role.id)}/channels/accounts/${encodeURIComponent(account.accountId)}/groups/${encodeURIComponent(group.chatId)}/listening/messages`, { method: "DELETE" });
      if (selectedThread?.channel === "qq" && selectedThread.chatId === group.chatId) {
        setMessages((current) => current.filter((message) => message.metadata.listening !== true));
      }
      setGroupError("旁听记录已清理");
    } catch (cause) { setGroupError(errorMessage(cause, "清理旁听记录失败")); }
    finally { setBusyGroup(null); }
  };

  return <section className="channel-view" aria-labelledby="channel-view-title">
    <header className="channel-view-header">
      <IconButton icon="back" label="返回聊天" onClick={onBack} />
      <div><h1 id="channel-view-title">{role.name} · 渠道会话</h1><p>{threads.length} 个会话</p></div>
      <IconButton icon="refresh" label="刷新会话" onClick={() => void load()} />
    </header>
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    {sourceError && <Notice onDismiss={() => setSourceError("")}>{sourceError}</Notice>}
    <div className="channel-workspace">
      <aside className="channel-thread-list" aria-label="渠道会话列表">
        <h2>会话</h2>
        {threads.length === 0 && <p className="channel-empty">暂无渠道会话</p>}
        {threads.map((thread) => <button key={thread.threadId} type="button" className={`channel-thread${selected === thread.threadId ? " active" : ""}`} onClick={() => { sourceRequest.current += 1; setSourceHighlightedId(null); setListeningSource(null); setSourceError(""); setSelected(thread.threadId); }}>
          {thread.avatarUrl ? <img src={thread.avatarUrl} alt="" className="channel-contact-avatar" /> : <span className={`channel-mark ${thread.channel}`}>{thread.channel === "telegram" ? "T" : thread.channel === "desktop" ? "M" : "•"}</span>}
          <span className="channel-thread-copy"><strong>{thread.title || thread.chatId}</strong><small>{channelName[thread.channel] ?? thread.channel} · {thread.chatType === "group" ? "群聊" : "私聊"}</small>{thread.latestMessage && <small>{thread.latestMessage.role === "assistant" ? role.name : thread.title || "访客"}：{thread.latestMessage.content || "（无内容）"}</small>}{sourceLabel(thread.viaAccount) && <small>{sourceLabel(thread.viaAccount)}</small>}{thread.accountRemoved && <small>账号已移除</small>}</span>
          <time>{new Date(thread.lastActivity).toLocaleDateString()}</time>
        </button>)}
      </aside>
      <section className="channel-messages" aria-label="会话消息">
        {selectedThread ? <>
          <header><div><h2>{selectedThread.title || selectedThread.chatId}</h2><p>{channelName[selectedThread.channel] ?? selectedThread.channel} · {selectedThread.chatType === "group" ? "群聊" : "私聊"}</p>{sourceLabel(selectedThread.viaAccount) && <p>{sourceLabel(selectedThread.viaAccount)}{selectedThread.accountRemoved ? " · 账号已移除" : ""}</p>}</div></header>
          <ol>
            {hasOlderMessages && <li className="channel-older"><button type="button" disabled={loadingOlder} onClick={() => void loadOlderMessages()}>{loadingOlder ? "正在读取…" : "读取更早消息"}</button></li>}
            {messages.map((message) => {
              const attachments = messageAttachments(message.metadata.attachments);
              const quotedAttachments = messageAttachments(message.metadata.quotedAttachments);
              const replyTo = typeof message.metadata.replyTo === "string" && message.metadata.replyTo ? message.metadata.replyTo : null;
              const replyTarget = replyTo ? messages.find((item) => String(item.metadata.platformMessageId ?? "") === replyTo) : null;
              const replySender = replyTarget ? String(replyTarget.metadata.senderName ?? (replyTarget.role === "assistant" ? role.name : "访客")) : String(message.metadata.replyToSenderName || message.metadata.replyToSenderId || "");
              const quotedText = typeof message.metadata.replyToContent === "string" ? message.metadata.replyToContent : "";
              const mentionedIds = Array.isArray(message.metadata.mentionedIds) ? message.metadata.mentionedIds.map(String) : [];
              const mentionedNames = mentionedIds.map((id) => {
                const target = messages.find((item) => String(item.metadata.senderId ?? "") === id);
                return target ? String(target.metadata.senderName ?? id) : id;
              });
              const source = message.metadata.viaAccount as ViaAccount | undefined;
              const delivery = deliveryLabel(message.metadata.deliveryStatus);
              const sourceMessage = message.id === sourceHighlightedId;
              return <li key={message.id} ref={sourceMessage ? (node) => { sourceElement.current = node; } : undefined} className={`channel-message ${message.role}${sourceMessage ? " source-highlight" : ""}`} style={sourceMessage ? { boxShadow: "0 0 0 2px var(--rose)" } : undefined}>
                {sourceMessage && <span className="source-marker">记忆来源</span>}
                <span className="channel-message-sender">
                  {message.role !== "assistant" && typeof message.metadata.senderAvatarUrl === "string" && <img src={message.metadata.senderAvatarUrl} alt="" className="channel-contact-avatar" />}
                  {message.role === "assistant" ? role.name : `${message.metadata.senderIsOwner === true ? "主人 · " : ""}${String(message.metadata.senderName ?? "访客")}`}
                </span>
                {source?.avatarUrl && <img src={source.avatarUrl} alt="经由账号头像" className="channel-source-avatar" />}
                {sourceLabel(source) && <small className="channel-message-source">{sourceLabel(source)}</small>}
                {replyTo && <div className="channel-message-reply">{replyTarget || quotedText ? `回复 ${replySender}：${replyTarget?.content || quotedText || "（无内容）"}` : `回复 ${replySender || "未知发送者"} 的消息 ${replyTo}（原文未加载）`}</div>}
                {quotedAttachments.length > 0 && <div className="channel-message-reply" role="group" aria-label="引用消息附件"><ChannelAttachments roleId={role.id} attachments={quotedAttachments} /></div>}
                {mentionedNames.length > 0 && <div className="channel-message-mentions">提及：{mentionedNames.map((name) => `@${name}`).join("、")}</div>}
                {message.content && <p>{message.content}</p>}
                <ChannelAttachments roleId={role.id} attachments={attachments} />
                {!message.content && attachments.length === 0 && quotedAttachments.length === 0 && <p>（无内容）</p>}
                {delivery && <span className={`channel-delivery channel-delivery-${message.metadata.deliveryStatus}`}>{delivery}</span>}
                {Boolean(message.metadata.platformMessageId) && <details className="channel-receipt"><summary>{delivery ? "平台回执" : "平台消息"}</summary><span>消息编号：{String(message.metadata.platformMessageId)}</span></details>}
                <time>{new Date(message.createdAt).toLocaleString()}</time>
              </li>;
            })}
          </ol>
          {selectedThread.channel === "qq" && selectedThread.chatType === "group" && <div ref={(node) => {
            listeningPanel.current = node;
            if (listeningSource) node?.querySelector("details")?.setAttribute("open", "");
          }}>
            {listeningSource && <section className="channel-message source-highlight" style={{ boxShadow: "0 0 0 2px var(--rose)" }} aria-label="旁听记忆来源" ref={(node) => { sourceElement.current = node; }}>
              <span className="source-marker">记忆来源 · 当前群旁听记录</span><p>{listeningSource.content}</p>
              <p>此来源独立保存在当前群的旁听记录中，下面已展开旁听面板。</p>
            </section>}
            <GroupListeningPanel key={`${role.id}:${selectedThread.threadId}`} roleId={role.id} threadId={selectedThread.threadId} editable={!selectedThread.accountRemoved} />
          </div>}
        </> : <div className="channel-no-selection"><Icon name="chat" size={22} /><p>选择一个会话</p></div>}
      </section>
      <aside className="channel-account-panel" aria-label="渠道账号管理">
        <h2>渠道账号</h2>
        <div className="channel-platform-tabs" role="tablist" aria-label="选择渠道">
          {channels.map((item) => <button key={item.id} type="button" role="tab" aria-selected={channel === item.id} className={channel === item.id ? "active" : ""} onClick={() => setChannel(item.id)}>
            <span>{item.name}</span><small>{accounts.some((entry) => entry.channel === item.id) ? "已配置" : "未连接"}</small>
          </button>)}
        </div>
        <p className="channel-account-state">{account?.avatarUrl && <img src={account.avatarUrl} alt="渠道账号头像" className="channel-contact-avatar" />}<strong>{accountStatus(account, testResult)}</strong>{account?.displayName ? ` · ${account.displayName}` : ""}</p>
        <p className="channel-capabilities" aria-label="平台能力">{visibleCapabilities.map((item) => <span key={item}>{item}</span>)}</p>
        {account?.lastError && <p role="status" className="channel-error">连接错误：{account.lastError}</p>}

        {channel !== "telegram" && <>
          <label htmlFor="channel-endpoint">{channel === "qq" ? "NapCat WebSocket 地址" : "平台 API 地址"}</label>
          <input id="channel-endpoint" type="url" value={form.endpoint} onChange={(event) => updateForm("endpoint", event.target.value)} placeholder={account?.endpoint ?? (account ? "留空以保留当前地址" : channel === "qq" ? "ws://127.0.0.1:6700" : channel === "qqbot" ? "https://api.sgroup.qq.com" : "https://open.feishu.cn/open-apis")} />
        </>}
        {(channel === "qqbot" || channel === "feishu") && <>
          <label htmlFor="channel-app-id">{channel === "qqbot" ? "QQ Bot App ID" : "飞书 App ID"}</label>
          <input id="channel-app-id" autoComplete="off" value={form.appId} onChange={(event) => updateForm("appId", event.target.value)} />
        </>}
        {channel === "telegram" && <>
          <label htmlFor="channel-token">Telegram Bot Token</label>
          <input id="channel-token" type="password" autoComplete="new-password" value={form.token} onChange={(event) => updateForm("token", event.target.value)} placeholder={account?.tokenConfigured ? "已保存；留空则不更换" : "从 BotFather 获取"} />
        </>}
        {channel === "qq" && <>
          <label htmlFor="channel-access-token">NapCat Access Token</label>
          <input id="channel-access-token" type="password" autoComplete="new-password" value={form.accessToken} onChange={(event) => updateForm("accessToken", event.target.value)} placeholder={account?.tokenConfigured ? "已保存；留空则不更换" : "如 NapCat 配置了访问令牌"} />
        </>}
        {channel === "qqbot" && <>
          <label htmlFor="channel-client-secret">QQ Bot Client Secret</label>
          <input id="channel-client-secret" type="password" autoComplete="new-password" value={form.clientSecret} onChange={(event) => updateForm("clientSecret", event.target.value)} placeholder={account?.tokenConfigured ? "已保存；留空则不更换" : "从 QQ 开放平台获取"} />
        </>}
        {channel === "feishu" && <>
          <label htmlFor="channel-app-secret">飞书 App Secret</label>
          <input id="channel-app-secret" type="password" autoComplete="new-password" value={form.appSecret} onChange={(event) => updateForm("appSecret", event.target.value)} placeholder={account?.tokenConfigured ? "已保存；留空则不更换" : "从飞书开放平台获取"} />
          <label htmlFor="channel-verification-token">Verification Token（可选）</label>
          <input id="channel-verification-token" type="password" autoComplete="new-password" value={form.verificationToken} onChange={(event) => updateForm("verificationToken", event.target.value)} placeholder="Webhook 验证令牌" />
          <label htmlFor="channel-encrypt-key">Encrypt Key（可选）</label>
          <input id="channel-encrypt-key" type="password" autoComplete="new-password" value={form.encryptKey} onChange={(event) => updateForm("encryptKey", event.target.value)} placeholder="事件加密密钥" />
        </>}

        <label htmlFor="channel-display-name">显示名称</label>
        <input id="channel-display-name" value={form.displayName} onChange={(event) => updateForm("displayName", event.target.value)} placeholder="可选" />
        <div className="channel-account-options">
          <label><input type="checkbox" checked={form.allowPrivate} onChange={(event) => updateForm("allowPrivate", event.target.checked)} disabled={!capabilities.private} />允许私聊</label>
          <label><input type="checkbox" checked={form.allowGroup} onChange={(event) => updateForm("allowGroup", event.target.checked)} disabled={!capabilities.group} />允许群聊</label>
        </div>
        <label htmlFor="channel-blocked-senders">屏蔽发送者（ID 或平台别名）</label>
        <textarea id="channel-blocked-senders" rows={2} value={form.blockedSenders} onChange={(event) => updateForm("blockedSenders", event.target.value)} placeholder="每行一个 ID 或别名" />
        <div className="channel-actions">
          <button type="button" className="primary" disabled={busy || (channel === "telegram" && !form.token && !account?.tokenConfigured) || ((channel === "qqbot" || channel === "feishu") && !form.appId.trim())} onClick={() => void save()}>{account ? "保存设置" : "添加账号"}</button>
          {account && <button type="button" disabled={busy} onClick={() => void test()}>测试连接</button>}
          {account && <button type="button" disabled={busy} onClick={() => void toggle()}>{account.enabled ? "停用" : "启用"}</button>}
          {account && <IconButton icon="trash" label={`移除${channelName[channel]}账号`} disabled={busy} onClick={() => void remove()} />}
        </div>

        {account && channel === "qq" && capabilities.groupListening && <section className="channel-group-listening" aria-labelledby="channel-group-listening-title">
          <div className="channel-group-heading"><h3 id="channel-group-listening-title">群旁听</h3><button type="button" disabled={loadingGroups || !account.enabled} onClick={() => void loadGroups()}>{loadingGroups ? "读取中…" : "刷新群"}</button></div>
          <p>旁听只记录此群消息，不触发角色回复；仅显示已观察到的群。</p>
          {groupError && <p role="status" className="channel-error">{groupError}</p>}
          {groups.length === 0 && !loadingGroups && <p className="channel-empty">暂无已观察到的群</p>}
          {groups.map((group) => <div className="channel-group-option" key={group.chatId}>
            <span><strong>{group.title || group.chatId}</strong><small>{group.chatId}</small></span>
            <span className="channel-group-actions"><label className="channel-group-toggle"><input type="checkbox" aria-label={`旁听群 ${group.title || group.chatId}`} checked={group.listening} disabled={busyGroup === group.chatId || !account.enabled} onChange={(event) => void setGroupListening(group, event.target.checked)} /></label><button type="button" disabled={busyGroup === group.chatId} onClick={() => void clearGroupListeningMessages(group)}>清理旁听记录</button></span>
          </div>)}
        </section>}

        <div className="channel-pairing">
          <h3>主人配对</h3>
          <p>配对码有效 10 分钟，可在任一已连接角色账号的私聊中使用一次；生成新码或重启后旧码失效。</p>
          <button type="button" disabled={busy || !account?.enabled} onClick={() => void pair()}>生成配对码</button>
          {pairingCode && <output>{pairingCode}</output>}
          <IdentityBindings channel={channel} accountId={account?.accountId} />
          <PushTargets roleId={role.id} accountId={account?.accountId} />
        </div>
      </aside>
    </div>
  </section>;
}
