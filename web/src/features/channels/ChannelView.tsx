import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Role } from "../../api/types";
import { api, errorMessage } from "../../api/client";
import { Icon, IconButton } from "../../ui/Icon";
import { Notice } from "../../ui/Status";
import { GroupListeningPanel } from "./GroupListeningPanel";
import { PushTargets } from "./PushTargets";
import { channelName, type Account } from "./channelAccounts";
import type { MemoryOrigin } from "../memory/MemoryPanel";
import "./channels.css";

export type ChannelMemorySource = { origin: MemoryOrigin; messageId?: string };

type Thread = {
  threadId: string;
  sessionKey: string;
  accountId?: string;
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

export function ChannelView({ role, onBack, onAccounts, initialSource, sourceOnly = false }: { role: Role; onBack: () => void; onAccounts?: () => void; initialSource?: ChannelMemorySource | null; sourceOnly?: boolean }) {
  const [accounts, setAccounts] = useState<Account[]>([]);
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
  const [error, setError] = useState("");
  const [sourceError, setSourceError] = useState("");
  const [sourceHighlightedId, setSourceHighlightedId] = useState<string | null>(null);
  const [listeningSource, setListeningSource] = useState<ConversationMessage | null>(null);
  const sourceRequest = useRef(0);
  const verifiedSourceThread = useRef<Thread | null>(null);
  const sourceElement = useRef<HTMLElement | null>(null);
  const listeningPanel = useRef<HTMLDivElement | null>(null);
  const selectedThread = useMemo(() => threads.find((thread) => thread.threadId === selected) ?? null, [threads, selected]);
  const account = accounts.find((item) => item.accountId === selectedThread?.accountId) ?? null;
  const load = useCallback(async () => {
    if (sourceOnly) { setThreadsLoaded(true); return; }
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
  }, [role.id, sourceOnly]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!initialSource || !threadsLoaded) return;
    const request = ++sourceRequest.current;
    let cancelled = false;
    const current = () => !cancelled && request === sourceRequest.current;
    const listedThread = sourceOnly ? undefined : threads.find((item) => item.sessionKey === initialSource.origin.sessionKey && item.channel !== "desktop");
    const listening = initialSource.origin.kind === "listening";
    setSourceError(""); setSourceHighlightedId(null); setListeningSource(null);
    if (sourceOnly) {
      setMessages([]); setThreads([]); setSelected(null);
      verifiedSourceThread.current = null;
    }
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
    // The source viewer verifies its thread through the role-scoped API.
    // The legacy history view can also use its verified role-scoped list;
    // refreshing that list must not restart source pagination.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSource, threadsLoaded, role.id, sourceOnly]);

  useEffect(() => {
    sourceElement.current?.scrollIntoView?.({ behavior: "smooth", block: "center" });
    if (listeningSource) listeningPanel.current?.querySelector("details")?.setAttribute("open", "");
  }, [sourceHighlightedId, listeningSource]);

  useEffect(() => () => { olderRequest.current += 1; }, []);

  useEffect(() => {
    if (sourceOnly || typeof EventSource === "undefined") return;
    const source = new EventSource(`/api/roles/${encodeURIComponent(role.id)}/channels/events`);
    source.addEventListener("channel_update", () => { void load(); });
    source.onerror = () => { /* EventSource reconnects while the local API is available. */ };
    return () => source.close();
  }, [load, role.id, sourceOnly]);

  useEffect(() => {
    if (sourceOnly) return;
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
  }, [role.id, selectedThread, sourceOnly]);

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

  return <section className={`channel-view${sourceOnly ? " channel-source-view" : ""}`} aria-label={sourceOnly ? "渠道记忆来源" : undefined} aria-labelledby={sourceOnly ? undefined : "channel-view-title"}>
    {!sourceOnly && <header className="channel-view-header">
      <IconButton icon="back" label="返回聊天" onClick={onBack} />
      <div><h1 id="channel-view-title">{role.name} · 手机会话</h1><p>{threads.length} 个会话</p></div>
      <div className="channel-view-actions">{onAccounts && <button type="button" onClick={onAccounts}>角色账号</button>}<IconButton icon="refresh" label="刷新会话" onClick={() => void load()} /></div>
    </header>}
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    {sourceError && <Notice onDismiss={() => setSourceError("")}>{sourceError}</Notice>}
    <div className="channel-workspace">
      {!sourceOnly && <aside className="channel-thread-list" aria-label="渠道会话列表">
        <h2>会话</h2>
        {threads.length === 0 && <p className="channel-empty">暂无渠道会话</p>}
        {threads.map((thread) => <button key={thread.threadId} type="button" className={`channel-thread${selected === thread.threadId ? " active" : ""}`} onClick={() => { sourceRequest.current += 1; setSourceHighlightedId(null); setListeningSource(null); setSourceError(""); setSelected(thread.threadId); }}>
          {thread.avatarUrl ? <img src={thread.avatarUrl} alt="" className="channel-contact-avatar" /> : <span className={`channel-mark ${thread.channel}`}>{thread.channel === "telegram" ? "T" : thread.channel === "desktop" ? "M" : "•"}</span>}
          <span className="channel-thread-copy"><strong>{thread.title || thread.chatId}</strong><small>{channelName[thread.channel] ?? thread.channel} · {thread.chatType === "group" ? "群聊" : "私聊"}</small>{thread.latestMessage && <small>{thread.latestMessage.role === "assistant" ? role.name : thread.title || "访客"}：{thread.latestMessage.content || "（无内容）"}</small>}{sourceLabel(thread.viaAccount) && <small>{sourceLabel(thread.viaAccount)}</small>}{thread.accountRemoved && <small>账号已移除</small>}</span>
          <time>{new Date(thread.lastActivity).toLocaleDateString()}</time>
        </button>)}
      </aside>}
      <section className="channel-messages" aria-label="会话消息">
        {selectedThread ? <>
          <header><div><h2>{selectedThread.title || selectedThread.chatId}</h2><p>{channelName[selectedThread.channel] ?? selectedThread.channel} · {selectedThread.chatType === "group" ? "群聊" : "私聊"}</p>{sourceLabel(selectedThread.viaAccount) && <p>{sourceLabel(selectedThread.viaAccount)}{selectedThread.accountRemoved ? " · 账号已移除" : ""}</p>}</div></header>
          {!sourceOnly && selectedThread.chatType === "private" && account && <details className="channel-conversation-settings"><summary>会话设置</summary><PushTargets key={selectedThread.threadId} roleId={role.id} accountId={account.accountId} chatId={selectedThread.chatId} /></details>}
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
          {selectedThread.channel === "qq" && selectedThread.chatType === "group" && <div className="channel-group-settings" ref={(node) => {
            listeningPanel.current = node;
            if (listeningSource) node?.querySelector("details")?.setAttribute("open", "");
          }}>
            {listeningSource && <section className="channel-message source-highlight" style={{ boxShadow: "0 0 0 2px var(--rose)" }} aria-label="旁听记忆来源" ref={(node) => { sourceElement.current = node; }}>
              <span className="source-marker">记忆来源 · 当前群旁听记录</span><p>{listeningSource.content}</p>
              <p>{sourceOnly ? "此来源独立保存在当前群的旁听记录中。" : "此来源独立保存在当前群的旁听记录中，下面已展开旁听面板。"}</p>
            </section>}
            {!sourceOnly && <GroupListeningPanel key={`${role.id}:${selectedThread.threadId}`} roleId={role.id} threadId={selectedThread.threadId} accountId={account?.accountId} chatId={selectedThread.chatId} editable={Boolean(account && account.enabled && account.platformEnabled !== false && !selectedThread.accountRemoved)} />}
          </div>}
        </> : <div className="channel-no-selection"><Icon name="chat" size={22} /><p>{sourceOnly ? sourceError ? "来源当前不可访问" : "正在读取记忆来源…" : "选择一个会话"}</p></div>}
      </section>

    </div>
  </section>;
}
