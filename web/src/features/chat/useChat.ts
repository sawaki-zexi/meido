import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { streamRoleMessage, type StreamEvent } from "../../api/stream";
import type { Message, Role, Session } from "../../api/types";
import { applyToolProgress, closeToolProgress, createToolProgress, type ToolProgressState } from "./toolProgress";

type SessionData = { session: Session; messages: Message[] };
type ActiveReply = { controller: AbortController; roleId: string; sessionKey: string; runId?: string; sequence: number; closed: boolean; progressClosed: boolean; stopPromise: Promise<void> | null; stopped: boolean; accepted: boolean; content: string; previousIds: Set<string> };

/** Applies one stream event to the message list; kept pure so ordering is easy to reason about. */
export function applyStreamEvent(messages: Message[], event: StreamEvent, sessionKey: string): Message[] {
  switch (event.type) {
    case "user_message_accepted":
      return [...messages, event.message];
    case "assistant_generation_started":
      return [...messages, { id: event.messageId, sessionKey, sequence: messages.length + 1, role: "assistant", content: "", status: "streaming", createdAt: new Date().toISOString() }];
    case "assistant_delta":
      return messages.map((message) => message.id === event.messageId ? { ...message, content: message.content + event.delta } : message);
    case "assistant_completed":
    case "assistant_failed":
      return messages.map((message) => message.id === event.message.id ? event.message : message);
    default:
      return messages;
  }
}

/** The role's unique session: loading history, sending, and streaming replies. */
export function useChat() {
  const [roleId, setRoleId] = useState<string | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [opening, setOpening] = useState(false);
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState("");
  const [toolProgress, setToolProgress] = useState<ToolProgressState | null>(null);
  const activeReply = useRef<ActiveReply | null>(null);
  const sourceRevision = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      sourceRevision.current += 1;
      activeReply.current?.controller.abort();
      activeReply.current = null;
    };
  }, []);

  /** Opens a role's session. Resolves her messages, or null when it could not be opened. */
  const open = async (role: Role): Promise<Message[] | null> => {
    // Every entry point shares this guard, including memory and task sources.
    if (activeReply.current) return null;
    const revision = ++sourceRevision.current;
    setToolProgress(null);
    setError("");
    setOpening(true);
    try {
      const data = await api<{ session: Session; messages: Message[] }>(`/api/roles/${encodeURIComponent(role.id)}/session`);
      if (!mounted.current || sourceRevision.current !== revision || data.session.roleId !== role.id) return null;
      setRoleId(role.id);
      setSession(data.session);
      setMessages(data.messages);
      return data.messages;
    } catch (cause) {
      if (mounted.current && sourceRevision.current === revision) setError(errorMessage(cause, "无法打开会话"));
      return null;
    } finally {
      if (mounted.current && sourceRevision.current === revision) setOpening(false);
    }
  };

  const refresh = useCallback(async () => {
    if (!roleId || sending || activeReply.current) return;
    const revision = sourceRevision.current;
    try {
      const data = await api<{ session: Session; messages: Message[] }>(`/api/roles/${encodeURIComponent(roleId)}/session`);
      if (!mounted.current || sourceRevision.current !== revision || activeReply.current || data.session.roleId !== roleId) return;
      setSession(data.session);
      setMessages(data.messages);
    } catch {
      // Keep the conversation usable during a temporary background refresh failure.
    }
  }, [roleId, sending]);

  const reset = () => {
    if (activeReply.current) return;
    sourceRevision.current += 1;
    setRoleId(null); setSession(null); setMessages([]); setToolProgress(null); setOpening(false); setError("");
  };

  const reconcileReply = async (reply: ActiveReply, id: string) => {
    const data = await api<SessionData>(`/api/roles/${encodeURIComponent(id)}/session`);
    if (activeReply.current !== reply || data.session.roleId !== reply.roleId || data.session.sessionKey !== reply.sessionKey) return;
    const accepted = data.messages.find((item) => item.role === "user" && item.content === reply.content && !reply.previousIds.has(item.id));
    reply.accepted = Boolean(accepted);
    reply.runId = accepted?.runId ?? undefined;
    setSession(data.session);
    setMessages(data.messages);
  };

  /** Sends a message. Resolves false when nothing was sent so the caller can keep the draft. */
  const send = async (text: string, attachments: string[] = []): Promise<boolean> => {
    const content = text.trim();
    if ((!content && attachments.length === 0) || !roleId || activeReply.current) return false;
    // A read started before this run cannot replace its eventual persisted reply.
    sourceRevision.current += 1;
    const sessionKey = session?.sessionKey ?? `role:${roleId}`;
    const reply: ActiveReply = { controller: new AbortController(), roleId, sessionKey, sequence: -1, closed: false, progressClosed: false, stopPromise: null, stopped: false, accepted: false, content, previousIds: new Set(messages.map((item) => item.id)) };
    activeReply.current = reply;
    setToolProgress(null);
    setError("");
    setSending(true);
    try {
      await streamRoleMessage(roleId, content, attachments, (event) => {
        if (activeReply.current !== reply || reply.controller.signal.aborted || reply.closed) return false;
        if (event.runId && reply.runId && event.runId !== reply.runId) return false;
        if ("toolCallId" in event && (event.roleId !== undefined && event.roleId !== roleId || event.sessionKey !== undefined && event.sessionKey !== sessionKey)) return false;
        if ("message" in event && event.message.sessionKey !== sessionKey) return false;
        if (event.sequence !== undefined && event.sequence <= reply.sequence) return false;
        if (event.runId) reply.runId = event.runId;
        if (event.sequence !== undefined) reply.sequence = event.sequence;
        if ("toolCallId" in event) {
          if (reply.progressClosed && event.type !== "tool_execution_end") return false;
          setToolProgress(current => applyToolProgress(current ?? createToolProgress({ roleId, sessionKey, runId: event.runId }), event));
          return true;
        }
        if (event.type === "user_message_accepted") reply.accepted = true;
        setMessages((current) => applyStreamEvent(current, event, sessionKey));
        if (event.type === "assistant_failed") setError(event.error ?? "回复未能完成");
        if (event.type === "assistant_completed" || event.type === "assistant_failed") {
          reply.closed = true;
          setToolProgress(null);
        }
        return true;
      }, { signal: reply.controller.signal, onRun: (runId) => {
        if (activeReply.current !== reply || reply.controller.signal.aborted || reply.closed || reply.runId && reply.runId !== runId) return;
        reply.runId = runId; reply.accepted = true;
      } });
    } catch (cause) {
      if (reply.stopPromise) await reply.stopPromise.catch(() => undefined);
      if (activeReply.current === reply && !reply.controller.signal.aborted) {
        if (!reply.accepted) {
          // A lost response header does not mean the server rejected the POST.
          try { await reconcileReply(reply, roleId); } catch { /* Keep the original request error. */ }
        }
        if (activeReply.current !== reply) return reply.accepted;
        setError(errorMessage(cause, "发送失败"));
        setMessages((current) => current.map((item) => item.status === "streaming" ? { ...item, status: "failed" } : item));
      }
    } finally {
      if (reply.stopPromise) await reply.stopPromise.catch(() => undefined);
      if (activeReply.current === reply) {
        activeReply.current = null;
        setToolProgress(null);
        setSending(false);
        setStopping(false);
      }
    }
    return reply.accepted;
  };

  const stop = () => {
    const reply = activeReply.current;
    if (!reply || reply.stopPromise || !roleId) return;
    reply.progressClosed = true;
    setStopping(true);
    setToolProgress(current => current ? closeToolProgress(current) : null);
    reply.stopPromise = (async () => {
      if (!reply.runId) {
        // The request may already be persisted even when its response headers
        // have not reached the browser. Reconcile before restoring a draft.
        reply.controller.abort();
        await reconcileReply(reply, roleId);
      }
      if (activeReply.current !== reply) return;
      if (reply.runId) {
        const data = await api<SessionData>(`/api/roles/${encodeURIComponent(roleId)}/runs/${encodeURIComponent(reply.runId)}/stop`, { method: "POST" });
        if (activeReply.current !== reply || data.session.roleId !== reply.roleId || data.session.sessionKey !== reply.sessionKey) return;
        setSession(data.session);
        setMessages(data.messages);
      }
      reply.stopped = true;
      reply.controller.abort();
    })();
    void reply.stopPromise.catch((cause) => {
      if (activeReply.current === reply) setError(errorMessage(cause, "停止回复失败，请重试"));
    }).finally(() => {
      if (activeReply.current === reply && !reply.stopped) {
        reply.stopPromise = null;
        setStopping(false);
      }
    });
  };

  return { roleId, session, messages, toolProgress, opening, sending, stopping, error, setError, open, reset, send, stop, refresh };
}

export type ChatState = ReturnType<typeof useChat>;
