import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { Role } from "../../api/types";
import { api, errorMessage } from "../../api/client";
import { Icon, IconButton } from "../../ui/Icon";
import { Notice } from "../../ui/Status";
import { GroupListeningPanel } from "./GroupListeningPanel";
import { RoleAccountsPanel } from "./RoleAccountsPanel";
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
type ChannelEvent = {
  type?: string;
  roleId?: string;
  threadId?: string;
  thread?: Partial<Thread>;
  message?: Partial<ConversationMessage> & { metadata?: Record<string, unknown>; status?: string };
};

function eventMessageThreadId(message: ConversationMessage | ChannelEvent["message"]): string {
  return String(message?.metadata?.threadId ?? "");
}

function messageTime(value: string | undefined): number {
  const parsed = value ? Date.parse(value) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function orderThreads(items: Thread[]): Thread[] {
  return [...items].sort((left, right) => {
    const activity = messageTime(right.lastActivity) - messageTime(left.lastActivity);
    return Number.isFinite(activity) && activity !== 0 ? activity : left.threadId.localeCompare(right.threadId);
  });
}

function mergeMessages(current: ConversationMessage[], incoming: ConversationMessage[], threadId: string): ConversationMessage[] {
  const merged = new Map(current.filter((message) => eventMessageThreadId(message) === threadId).map((message) => [message.id, message]));
  incoming.forEach((message) => merged.set(message.id, message));
  return [...merged.values()].sort((left, right) => left.sequence - right.sequence || messageTime(left.createdAt) - messageTime(right.createdAt));
}
function mergeMessageSnapshot(current: ConversationMessage[], incoming: ConversationMessage[], threadId: string, revisions: Map<string, number>, startedAt: number): ConversationMessage[] {
  const liveIds = new Set(current.filter((message) => (revisions.get(message.id) ?? 0) > startedAt).map((message) => message.id));
  return mergeMessages(current, incoming.filter((message) => !liveIds.has(message.id)), threadId);
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

function ChannelAttachments({ roleId, attachments, onMediaLoad }: { roleId: string; attachments: ChannelAttachment[]; onMediaLoad?: () => void }) {
  return <>{attachments.map((attachment) => {
    const href = `/api/roles/${encodeURIComponent(roleId)}/attachments/${encodeURIComponent(attachment.id)}`;
    return attachment.kind === "image"
      ? <a href={href} target="_blank" rel="noreferrer" key={attachment.id}><img className="channel-attachment-image" src={href} alt={attachment.name} onLoad={onMediaLoad} /></a>
      : <a className="channel-attachment-file" href={href} target="_blank" rel="noreferrer" key={attachment.id}>{attachment.name}</a>;
  })}</>;
}

export function ChannelView({ role, onBack, onAccounts, initialSource, sourceOnly = false, phoneMode = false }: { role: Role; onBack: () => void; onAccounts?: () => void; initialSource?: ChannelMemorySource | null; sourceOnly?: boolean; phoneMode?: boolean }) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadsLoaded, setThreadsLoaded] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [listeningMessages, setListeningMessages] = useState<ConversationMessage[]>([]);
  const [hasOlderMessages, setHasOlderMessages] = useState(false);
  const [olderCursor, setOlderCursor] = useState<number | null>(null);
  const [hasOlderListening, setHasOlderListening] = useState(false);
  const [listeningCursor, setListeningCursor] = useState<number | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [messagesReady, setMessagesReady] = useState(false);
  const [listeningReady, setListeningReady] = useState(false);
  const messageThreadId = useRef<string | null>(null);
  const messageRoleId = useRef(role.id);
  const messageRequest = useRef(0);
  const olderRequest = useRef(0);
  const [error, setError] = useState("");
  const [sourceError, setSourceError] = useState("");
  const [sourceHighlightedId, setSourceHighlightedId] = useState<string | null>(null);
  const [phoneSettingsOpen, setPhoneSettingsOpen] = useState(false);
  const [listeningSource, setListeningSource] = useState<ConversationMessage | null>(null);
  const loadRequest = useRef(0);
  const listeningRequest = useRef(0);
  const sourceRequest = useRef(0);
  const verifiedSourceThread = useRef<Thread | null>(null);
  const sourceElement = useRef<HTMLElement | null>(null);
  const listeningPanel = useRef<HTMLDivElement | null>(null);
  const messageList = useRef<HTMLOListElement | null>(null);
  const atBottom = useRef(true);
  const olderAnchor = useRef<{ height: number; top: number } | null>(null);
  const liveRevision = useRef(0);
  const liveThreads = useRef(new Map<string, number>());
  const liveMessages = useRef(new Map<string, number>());
  const liveListening = useRef(new Map<string, number>());
  const liveListeningThreads = useRef(new Map<string, Set<string>>());
  const selectedThread = useMemo(() => threads.find((thread) => thread.threadId === selected) ?? null, [threads, selected]);
  const showPhoneSetup = phoneMode && !sourceOnly && threadsLoaded && !error && accounts.length === 0 && threads.length === 0;
  const openAccounts = () => { if (phoneMode) setPhoneSettingsOpen(true); else onAccounts?.(); };
  const threadsRef = useRef<Thread[]>(threads);
  threadsRef.current = threads;
  const messagesRef = useRef<ConversationMessage[]>(messages);
  messagesRef.current = messages;
  const listeningMessagesRef = useRef<ConversationMessage[]>(listeningMessages);
  listeningMessagesRef.current = listeningMessages;
  const selectedRef = useRef<string | null>(selected);
  selectedRef.current = selected;
  const account = accounts.find((item) => item.accountId === selectedThread?.accountId) ?? null;
  const load = useCallback(async () => {
    const requestId = ++loadRequest.current;
    const startedAt = liveRevision.current;
    if (sourceOnly) { setThreadsLoaded(true); return; }
    const refreshThreadId = selectedRef.current;
    const refreshMessageRequestId = refreshThreadId ? ++messageRequest.current : null;
    const refreshListeningRequestId = refreshThreadId ? ++listeningRequest.current : null;
    try {
      const [accountsData, threadsData] = await Promise.all([
        api<{ accounts: Account[] }>(`/api/roles/${encodeURIComponent(role.id)}/channels/accounts`),
        api<{ threads: Thread[] }>(`/api/roles/${encodeURIComponent(role.id)}/channels/threads?networkOnly=true`),
      ]);
      if (requestId !== loadRequest.current) return;
      setAccounts(accountsData.accounts);
      const verified = verifiedSourceThread.current;
      // The server applies Shiori's network-only projection. Keep this small
      // client guard for older deployments that do not understand the query.
      const externalThreads = threadsData.threads.filter((thread) => thread.channel !== "desktop" && thread.latestMessage !== null);
      const visibleThreads = verified && !externalThreads.some((item) => item.threadId === verified.threadId)
        ? [...externalThreads, verified] : externalThreads;
      setThreads((current) => orderThreads([
        ...visibleThreads.filter((thread) => (liveThreads.current.get(thread.threadId) ?? 0) <= startedAt),
        ...current.filter((thread) => (liveThreads.current.get(thread.threadId) ?? 0) > startedAt),
      ]));
      setThreadsLoaded(true);
      const isVisible = (threadId: string) => visibleThreads.some((item) => item.threadId === threadId) || (liveThreads.current.get(threadId) ?? 0) > startedAt;
      setSelected((current) => current && isVisible(current) ? current : null);
      const activeThreadId = selectedRef.current;
      const activeThread = activeThreadId ? threadsRef.current.find((item) => item.threadId === activeThreadId) : null;
      if (activeThreadId && activeThread && activeThreadId === refreshThreadId
        && refreshMessageRequestId === messageRequest.current
        && isVisible(activeThreadId)) {
        const listening = activeThread.channel === "qq" && activeThread.chatType === "group";
        const messagePage = api<{ messages: ConversationMessage[]; hasMore: boolean; nextBefore: number | null }>(
          `/api/roles/${encodeURIComponent(role.id)}/channels/threads/${encodeURIComponent(activeThreadId)}/messages?limit=50&networkOnly=true`,
        );
        const listeningPage = listening && refreshListeningRequestId === listeningRequest.current
          ? api<{ messages: ConversationMessage[]; hasMore: boolean; nextBefore: number | null }>(
            `/api/roles/${encodeURIComponent(role.id)}/channels/threads/${encodeURIComponent(activeThreadId)}/listening/messages?limit=50`,
          )
          : Promise.resolve(null);
        void messagePage
          .then((data) => {
            if (requestId !== loadRequest.current || selectedRef.current !== activeThreadId
              || refreshMessageRequestId !== messageRequest.current) return;
            setMessages((current) => mergeMessageSnapshot(current, data.messages, activeThreadId, liveMessages.current, startedAt));
            setMessagesReady(true);
            const currentMessages = messagesRef.current.filter((message) => eventMessageThreadId(message) === activeThreadId);
            const currentMessageStart = currentMessages[0]?.sequence;
            const currentMessageEnd = currentMessages.at(-1)?.sequence;
            const refreshedMessageStart = data.messages[0]?.sequence;
            const messageGap = currentMessageEnd !== undefined && refreshedMessageStart !== undefined
              && refreshedMessageStart > currentMessageEnd + 1;
            if (messageGap || currentMessageStart === undefined || refreshedMessageStart === undefined
              || currentMessageStart > refreshedMessageStart) {
              setHasOlderMessages(data.hasMore);
              setOlderCursor(data.nextBefore);
            }
          })
          .catch(() => { /* the existing page remains readable after a refresh failure */ });
        void listeningPage
          .then((heardData) => {
            if (!heardData || requestId !== loadRequest.current || selectedRef.current !== activeThreadId
              || refreshListeningRequestId !== listeningRequest.current) return;
            const heard = heardData.messages.map((message) => ({ ...message, metadata: { ...message.metadata, threadId: activeThreadId, listening: true } }));
            setListeningMessages((current) => mergeMessageSnapshot(current, heard, activeThreadId, liveListening.current, startedAt));
            setListeningReady(true);
            const currentListening = listeningMessagesRef.current.filter((message) => eventMessageThreadId(message) === activeThreadId);
            const currentListeningStart = currentListening[0]?.sequence;
            const currentListeningEnd = currentListening.at(-1)?.sequence;
            const refreshedListeningStart = heardData.messages[0]?.sequence;
            const listeningGap = currentListeningEnd !== undefined && refreshedListeningStart !== undefined
              && refreshedListeningStart > currentListeningEnd + 1;
            if (listeningGap || currentListeningStart === undefined || refreshedListeningStart === undefined
              || currentListeningStart > refreshedListeningStart) {
              setHasOlderListening(heardData.hasMore);
              setListeningCursor(heardData.nextBefore);
            }
          })
          .catch(() => { /* the existing page remains readable after a refresh failure */ });
      }
      setError("");
    } catch (cause) {
      if (requestId === loadRequest.current) setError(errorMessage(cause, "无法读取渠道会话"));
    }
  }, [role.id, sourceOnly]);

  useEffect(() => {
    loadRequest.current += 1;
    setAccounts([]);
    setThreads([]);
    setThreadsLoaded(false);
    setSelected(null);
    setMessages([]);
    setListeningMessages([]);
    setHasOlderMessages(false);
    setOlderCursor(null);
    setHasOlderListening(false);
    setListeningCursor(null);
    setLoadingOlder(false);
    setMessagesReady(false);
    setListeningReady(false);
    atBottom.current = true;
    olderAnchor.current = null;
    liveRevision.current = 0;
    liveThreads.current.clear();
    liveMessages.current.clear();
    liveListening.current.clear();
    liveListeningThreads.current.clear();
    setError("");
    verifiedSourceThread.current = null;
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
            const proof = await api<{ thread: Thread }>(`/api/roles/${encodeURIComponent(role.id)}/channels/threads/${encodeURIComponent(threadId)}/messages?limit=1&networkOnly=true`);
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
          const data = await api<{ messages: ConversationMessage[]; hasMore: boolean; nextBefore: number | null }>(`${base}?limit=100${listening ? "" : "&networkOnly=true"}${before === null ? "" : `&before=${before}`}`);
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
    let active = true;
    const source = new EventSource(`/api/roles/${encodeURIComponent(role.id)}/channels/events`);
    source.addEventListener("channel_update", (event) => {
      if (!active) return;
      let payload: ChannelEvent;
      try { payload = JSON.parse((event as MessageEvent).data ?? "{}"); } catch { void load(); return; }
      if (payload.roleId && payload.roleId !== role.id) return;
      const threadId = payload.threadId ?? payload.thread?.threadId ?? eventMessageThreadId(payload.message);
      if (payload.type === "message" && threadId && payload.message?.id && payload.message.createdAt) {
        const revision = ++liveRevision.current;
        liveThreads.current.set(threadId, revision);
        liveMessages.current.set(payload.message.id, revision);
        const incoming: ConversationMessage = {
          id: payload.message.id,
          sequence: typeof payload.message.sequence === "number" ? payload.message.sequence : Number.MAX_SAFE_INTEGER,
          role: String(payload.message.role ?? "user"),
          content: String(payload.message.content ?? ""),
          createdAt: payload.message.createdAt,
          metadata: { ...(payload.message.metadata ?? {}), threadId },
        };
        const incomingThread = payload.thread;
        setThreads((items) => {
          const current = items.find((item) => item.threadId === threadId);
          if (incomingThread?.channel === "desktop" || current?.channel === "desktop") return items;
          if (!current && !incomingThread?.channel) return items;
          if (current && incomingThread?.lastActivity && messageTime(incomingThread.lastActivity) < messageTime(current.lastActivity)) return items;
          const next: Thread = {
            ...(current ?? {
              threadId,
              sessionKey: `thread:${threadId}`,
              channel: String(incomingThread?.channel ?? ""),
              chatId: String(incomingThread?.chatId ?? threadId),
              chatType: String(incomingThread?.chatType ?? "private"),
              title: String(incomingThread?.title ?? threadId),
              lastActivity: incoming.createdAt,
            }),
            ...incomingThread,
            threadId,
            lastActivity: messageTime(incoming.createdAt) > messageTime(incomingThread?.lastActivity ?? current?.lastActivity) ? incoming.createdAt : String(incomingThread?.lastActivity ?? current?.lastActivity ?? incoming.createdAt),
            latestMessage: incomingThread?.latestMessage ?? {
              content: incoming.content,
              role: incoming.role,
              status: String(payload.message?.status ?? "completed"),
              createdAt: incoming.createdAt,
            },
          };
          return orderThreads([...items.filter((item) => item.threadId !== threadId), next]);
        });
        if (selectedRef.current === threadId) setMessages((current) => mergeMessages(current, [incoming], threadId));
        return;
      }
      if (payload.type === "listening_heard" && threadId && payload.message?.id && payload.message.createdAt) {
        const incomingThread = payload.thread;
        const hasVisibleThread = threadsRef.current.some((item) => item.threadId === threadId);
        const liveThread = Boolean(incomingThread?.channel && incomingThread.channel !== "desktop"
          && incomingThread.latestMessage !== null && incomingThread.latestMessage !== undefined);
        const revision = liveThread || hasVisibleThread ? ++liveRevision.current : 0;
        if (revision > 0) {
          liveThreads.current.set(threadId, revision);
          liveListening.current.set(payload.message.id, revision);
          const ids = liveListeningThreads.current.get(threadId) ?? new Set<string>();
          ids.add(payload.message.id);
          liveListeningThreads.current.set(threadId, ids);
        }
        const incoming: ConversationMessage = {
          id: payload.message.id,
          sequence: typeof payload.message.sequence === "number" ? payload.message.sequence : Number.MAX_SAFE_INTEGER,
          role: String(payload.message.role ?? "user"),
          content: String(payload.message.content ?? ""),
          createdAt: payload.message.createdAt,
          metadata: { ...(payload.message.metadata ?? {}), threadId, listening: true },
        };
        setThreads((items) => {
          const current = items.find((item) => item.threadId === threadId);
          if (incomingThread?.channel === "desktop" || current?.channel === "desktop") return items;
          if (!current && !liveThread) return items;
          const next: Thread = {
            ...(current ?? {
              threadId,
              sessionKey: "thread:" + threadId,
              channel: String(incomingThread?.channel ?? ""),
              chatId: String(incomingThread?.chatId ?? threadId),
              chatType: String(incomingThread?.chatType ?? "group"),
              title: String(incomingThread?.title ?? threadId),
              lastActivity: incoming.createdAt,
            }),
            ...incomingThread,
            threadId,
            lastActivity: messageTime(incoming.createdAt) > messageTime(incomingThread?.lastActivity ?? current?.lastActivity) ? incoming.createdAt : String(incomingThread?.lastActivity ?? current?.lastActivity ?? incoming.createdAt),
          };
          return orderThreads([...items.filter((item) => item.threadId !== threadId), next]);
        });
        if (selectedRef.current === threadId) setListeningMessages((current) => mergeMessages(current, [incoming], threadId));
        return;
      }
      if (payload.type === "listening_cleared" && threadId) {
        const ids = liveListeningThreads.current.get(threadId);
        ids?.forEach((messageId) => liveListening.current.delete(messageId));
        liveListeningThreads.current.delete(threadId);
        setListeningMessages((items) => items.filter((item) => eventMessageThreadId(item) !== threadId));
        if (selectedRef.current !== threadId) return;
        listeningRequest.current += 1;
        olderRequest.current += 1;
        setLoadingOlder(false);
        setListeningReady(true);
        setListeningMessages([]); setHasOlderListening(false); setListeningCursor(null);
        return;
      }
      // Account, avatar and delivery events do not carry a complete row. A
      // bounded authoritative refresh repairs those projections without
      // disturbing the currently loaded message pages.
      if (payload.type === "account" || payload.type === "avatars_updated" || payload.type === "delivery" || payload.type === "identities_updated") void load();
    });
    source.onerror = () => { /* EventSource reconnects while the local API is available. */ };
    return () => { active = false; source.close(); };
  }, [load, role.id, sourceOnly]);

  useEffect(() => {
    if (sourceOnly) return;
    if (!selectedThread) {
      messageThreadId.current = null;
      messageRoleId.current = role.id;
      messageRequest.current += 1;
      olderRequest.current += 1;
      listeningRequest.current += 1;
      setMessages([]);
      setListeningMessages([]);
      setHasOlderMessages(false);
      setOlderCursor(null);
      setHasOlderListening(false);
      setListeningCursor(null);
      setLoadingOlder(false);
      setMessagesReady(false);
      setListeningReady(false);
      olderAnchor.current = null;
      return;
    }
    let cancelled = false;
    const threadId = selectedThread.threadId;
    const startedAt = liveRevision.current;
    const threadChanged = messageThreadId.current !== threadId || messageRoleId.current !== role.id;
    if (threadChanged) {
      messageThreadId.current = threadId;
      messageRoleId.current = role.id;
      messageRequest.current += 1;
      olderRequest.current += 1;
      listeningRequest.current += 1;
      setMessages((items) => items.filter((item) => eventMessageThreadId(item) === threadId));
      setListeningMessages((items) => items.filter((item) => eventMessageThreadId(item) === threadId));
      setHasOlderMessages(false);
      setOlderCursor(null);
      setHasOlderListening(false);
      setListeningCursor(null);
      setLoadingOlder(false);
      setMessagesReady(false);
      setListeningReady(false);
      atBottom.current = true;
      olderAnchor.current = null;
    }
    const messageRequestId = messageRequest.current;
    void api<{ messages: ConversationMessage[]; hasMore: boolean; nextBefore: number | null }>(`/api/roles/${encodeURIComponent(role.id)}/channels/threads/${encodeURIComponent(threadId)}/messages?limit=50&networkOnly=true`)
      .then((data) => {
        if (cancelled || messageRequestId !== messageRequest.current) return;
        setMessages((current) => mergeMessageSnapshot(current, data.messages, threadId, liveMessages.current, startedAt));
        setMessagesReady(true);
        if (threadChanged) {
          setHasOlderMessages(data.hasMore);
          setOlderCursor(data.nextBefore);
        }
        setError("");
      })
      .catch((cause) => { if (!cancelled && messageRequestId === messageRequest.current) setError(errorMessage(cause, "无法读取会话消息")); });
    const listening = selectedThread.channel === "qq" && selectedThread.chatType === "group";
    const listeningRequestId = ++listeningRequest.current;
    if (listening) {
      void api<{ messages: ConversationMessage[]; hasMore: boolean; nextBefore: number | null }>(`/api/roles/${encodeURIComponent(role.id)}/channels/threads/${encodeURIComponent(threadId)}/listening/messages?limit=50`)
        .then((data) => {
          if (cancelled || messageRequestId !== messageRequest.current || listeningRequestId !== listeningRequest.current) return;
          const heard = data.messages.map((message) => ({ ...message, metadata: { ...message.metadata, threadId, listening: true } }));
          setListeningMessages((current) => mergeMessageSnapshot(current, heard, threadId, liveListening.current, startedAt));
          setListeningReady(true);
          if (threadChanged) {
            setHasOlderListening(data.hasMore);
            setListeningCursor(data.nextBefore);
          }
        })
        .catch((cause) => { if (!cancelled && messageRequestId === messageRequest.current && listeningRequestId === listeningRequest.current) setError(errorMessage(cause, "无法读取旁听记录")); });
    } else {
      setListeningReady(true);
      setListeningMessages([]); setHasOlderListening(false); setListeningCursor(null);
    }
    return () => { cancelled = true; };
  }, [role.id, selected, selectedThread?.threadId, selectedThread?.channel, selectedThread?.chatType, sourceOnly]);

  const loadOlderMessages = async () => {
    if (!selectedThread || loadingOlder) return;
    const threadId = selectedThread.threadId;
    const requestId = ++olderRequest.current;
    const isCurrent = () => messageThreadId.current === threadId && messageRoleId.current === role.id && olderRequest.current === requestId;
    const oldest = (items: ConversationMessage[]) => messageTime(items[0]?.createdAt);
    const readListening = hasOlderListening && listeningCursor !== null && (!hasOlderMessages || olderCursor === null || oldest(listeningMessages) > oldest(messages));
    const cursor = readListening ? listeningCursor : olderCursor;
    if (cursor === null) return;
    const viewport = messageList.current;
    olderAnchor.current = viewport ? { height: viewport.scrollHeight, top: viewport.scrollTop } : null;
    setLoadingOlder(true);
    try {
      const endpoint = readListening ? "listening/messages" : "messages";
      const data = await api<{ messages: ConversationMessage[]; hasMore: boolean; nextBefore: number | null }>(`/api/roles/${encodeURIComponent(role.id)}/channels/threads/${encodeURIComponent(threadId)}/${endpoint}?limit=50${readListening ? "" : "&networkOnly=true"}&before=${cursor}`);
      if (!isCurrent()) { olderAnchor.current = null; return; }
      if (readListening) {
        const heard = data.messages.map((message) => ({ ...message, metadata: { ...message.metadata, threadId, listening: true } }));
        setListeningMessages((current) => mergeMessages(heard, current, threadId));
        setHasOlderListening(data.hasMore); setListeningCursor(data.nextBefore);
      } else {
        setMessages((current) => mergeMessages(data.messages, current, threadId));
        setHasOlderMessages(data.hasMore); setOlderCursor(data.nextBefore);
      }
    } catch (cause) {
      olderAnchor.current = null;
      if (isCurrent()) setError(errorMessage(cause, "无法读取更早的消息"));
    } finally {
      if (isCurrent()) setLoadingOlder(false);
    }
  };

  const timelineMessages = useMemo(() => {
    if (!sourceOnly && (!messagesReady || !listeningReady)) return [];
    // Each stream has its own sequence. Only expose the time interval that
    // both streams have loaded, so an older page never fills a visible gap.
    const since = sourceOnly ? Number.NEGATIVE_INFINITY : Math.max(
      hasOlderMessages ? messageTime(messages[0]?.createdAt) : Number.NEGATIVE_INFINITY,
      hasOlderListening ? messageTime(listeningMessages[0]?.createdAt) : Number.NEGATIVE_INFINITY,
    );
    return [...messages, ...listeningMessages]
      .filter((message) => messageTime(message.createdAt) >= since)
      .sort((left, right) => messageTime(left.createdAt) - messageTime(right.createdAt)
        || Number(left.metadata.listening === true) - Number(right.metadata.listening === true)
        || left.sequence - right.sequence);
  }, [messages, listeningMessages, hasOlderMessages, hasOlderListening, messagesReady, listeningReady, sourceOnly]);
  const canLoadOlder = hasOlderMessages || hasOlderListening;
  const keepAtBottomAfterMediaLoad = () => {
    const list = messageList.current;
    if (list && atBottom.current) list.scrollTop = list.scrollHeight;
  };

  useLayoutEffect(() => {
    const list = messageList.current;
    if (!list) return;
    const anchor = olderAnchor.current;
    if (anchor && !loadingOlder) {
      list.scrollTop = anchor.top + list.scrollHeight - anchor.height;
      olderAnchor.current = null;
    } else if (!anchor && atBottom.current) {
      list.scrollTop = list.scrollHeight;
    }
    if (!sourceOnly && !loadingOlder && canLoadOlder && messagesReady && listeningReady && !error
      && list.clientHeight > 0 && list.scrollHeight <= list.clientHeight) {
      void loadOlderMessages();
    }
  }, [timelineMessages, canLoadOlder, loadingOlder, messagesReady, listeningReady, error, sourceOnly]);

  return <section className={`channel-view${sourceOnly ? " channel-source-view" : ""}`} aria-label={sourceOnly ? "渠道记忆来源" : undefined} aria-labelledby={sourceOnly ? undefined : "channel-view-title"}>
    {!sourceOnly && !phoneSettingsOpen && <header className="channel-view-header">
      <IconButton icon="back" label="返回聊天" onClick={onBack} />
      <div><h1 id="channel-view-title">{phoneMode ? role.name : `${role.name} · 手机会话`}</h1><p>{threads.length} 个会话</p></div>
      <div className="channel-view-actions">{onAccounts && <button type="button" className="channel-accounts-button" aria-label="角色账号" title="角色账号" onClick={openAccounts}><Icon name="settings" size={16} /><span>角色账号</span></button>}{!phoneMode && <IconButton icon="refresh" label="刷新会话" onClick={() => void load()} />}</div>
    </header>}
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    {sourceError && <Notice onDismiss={() => setSourceError("")}>{sourceError}</Notice>}
    {phoneSettingsOpen ? <div className="channel-phone-settings-app" aria-label="设置">
      <header className="channel-phone-settings-header"><IconButton icon="back" label="返回多端会话" onClick={() => setPhoneSettingsOpen(false)} /><h2>设置</h2></header>
      <RoleAccountsPanel roleId={role.id} />
    </div> : showPhoneSetup ? <div className="channel-phone-setup" aria-label="渠道设置">
      {onAccounts && <IconButton icon="settings" label="设置渠道" className="channel-phone-setup-button" onClick={openAccounts} />}
    </div> : <div className="channel-workspace">
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
          <ol ref={messageList} onScroll={() => {
            const list = messageList.current;
            if (!list) return;
            atBottom.current = list.scrollHeight - list.scrollTop - list.clientHeight < 24;
          }}>
            {canLoadOlder && <li className="channel-older"><button type="button" disabled={loadingOlder} onClick={() => void loadOlderMessages()}>{loadingOlder ? "正在读取…" : "读取更早消息"}</button></li>}
            {timelineMessages.map((message) => {
              const attachments = messageAttachments(message.metadata.attachments);
              const quotedAttachments = messageAttachments(message.metadata.quotedAttachments);
              const replyTo = typeof message.metadata.replyTo === "string" && message.metadata.replyTo ? message.metadata.replyTo : null;
              const replyTarget = replyTo ? timelineMessages.find((item) => String(item.metadata.platformMessageId ?? "") === replyTo) : null;
              const replySender = replyTarget ? String(replyTarget.metadata.senderName ?? (replyTarget.role === "assistant" ? role.name : "访客")) : String(message.metadata.replyToSenderName || message.metadata.replyToSenderId || "");
              const quotedText = typeof message.metadata.replyToContent === "string" ? message.metadata.replyToContent : "";
              const mentionedIds = Array.isArray(message.metadata.mentionedIds) ? message.metadata.mentionedIds.map(String) : [];
              const mentionedNames = mentionedIds.map((id) => {
                const target = timelineMessages.find((item) => String(item.metadata.senderId ?? "") === id);
                return target ? String(target.metadata.senderName ?? id) : id;
              });
              const source = message.metadata.viaAccount as ViaAccount | undefined;
              const delivery = deliveryLabel(message.metadata.deliveryStatus);
              const sourceMessage = message.id === sourceHighlightedId;
              return <li key={message.id} ref={sourceMessage ? (node) => { sourceElement.current = node; } : undefined} className={`channel-message ${message.role}${message.metadata.listening === true ? " listened" : ""}${sourceMessage ? " source-highlight" : ""}`} style={sourceMessage ? { boxShadow: "0 0 0 2px var(--rose)" } : undefined}>
                {sourceMessage && <span className="source-marker">记忆来源</span>}
                {message.metadata.listening === true && <span className="source-marker">旁听记录 · 不触发角色</span>}
                <span className="channel-message-sender">
                  {message.role !== "assistant" && typeof message.metadata.senderAvatarUrl === "string" && <img src={message.metadata.senderAvatarUrl} alt="" className="channel-contact-avatar" />}
                  {message.role === "assistant" ? role.name : `${message.metadata.senderIsOwner === true ? "主人 · " : ""}${String(message.metadata.senderName ?? "访客")}`}
                </span>
                {source?.avatarUrl && <img src={source.avatarUrl} alt="经由账号头像" className="channel-source-avatar" />}
                {sourceLabel(source) && <small className="channel-message-source">{sourceLabel(source)}</small>}
                {replyTo && <div className="channel-message-reply">{replyTarget || quotedText ? `回复 ${replySender}：${replyTarget?.content || quotedText || "（无内容）"}` : `回复 ${replySender || "未知发送者"} 的消息 ${replyTo}（原文未加载）`}</div>}
                {quotedAttachments.length > 0 && <div className="channel-message-reply" role="group" aria-label="引用消息附件"><ChannelAttachments roleId={role.id} attachments={quotedAttachments} onMediaLoad={keepAtBottomAfterMediaLoad} /></div>}
                {mentionedNames.length > 0 && <div className="channel-message-mentions">提及：{mentionedNames.map((name) => `@${name}`).join("、")}</div>}
                {message.content && <p>{message.content}</p>}
                <ChannelAttachments roleId={role.id} attachments={attachments} onMediaLoad={keepAtBottomAfterMediaLoad} />
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

    </div>}
  </section>;
}
