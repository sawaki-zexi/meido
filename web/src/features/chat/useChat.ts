import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import { streamRoleMessage, type StreamEvent } from "../../api/stream";
import type { Message, Role, Session } from "../../api/types";

type SessionData = { session: Session; messages: Message[] };
type ActiveReply = { controller: AbortController; runId?: string; stopPromise: Promise<void> | null; stopped: boolean; accepted: boolean; content: string; previousIds: Set<string> };

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
  const activeReply = useRef<ActiveReply | null>(null);
  useEffect(() => () => { activeReply.current?.controller.abort(); }, []);

  /** Loads the role's session. Resolves true when the chat can be shown. */
  /** Opens a role's session. Resolves her messages, or null when it could not be opened. */
  const open = async (role: Role): Promise<Message[] | null> => {
    setError("");
    setOpening(true);
    try {
      const data = await api<{ session: Session; messages: Message[] }>(`/api/roles/${encodeURIComponent(role.id)}/session`);
      setRoleId(role.id);
      setSession(data.session);
      setMessages(data.messages);
      return data.messages;
    } catch (cause) {
      setError(errorMessage(cause, "无法打开会话"));
      return null;
    } finally {
      setOpening(false);
    }
  };

  const refresh = useCallback(async () => {
    if (!roleId || sending) return;
    try {
      const data = await api<{ session: Session; messages: Message[] }>(`/api/roles/${encodeURIComponent(roleId)}/session`);
      if (data.session.roleId !== roleId) return;
      setSession(data.session);
      setMessages(data.messages);
    } catch {
      // Keep the conversation usable during a temporary background refresh failure.
    }
  }, [roleId, sending]);

  const reset = () => { setRoleId(null); setSession(null); setMessages([]); setError(""); };

  /** Sends a message. Resolves false when nothing was sent so the caller can keep the draft. */
  const send = async (text: string, attachments: string[] = []): Promise<boolean> => {
    const content = text.trim();
    if ((!content && attachments.length === 0) || !roleId || activeReply.current) return false;
    const reply: ActiveReply = { controller: new AbortController(), stopPromise: null, stopped: false, accepted: false, content, previousIds: new Set(messages.map((item) => item.id)) };
    activeReply.current = reply;
    setError("");
    setSending(true);
    const sessionKey = session?.sessionKey ?? `role:${roleId}`;
    try {
      await streamRoleMessage(roleId, content, attachments, (event) => {
        if (reply.controller.signal.aborted) return;
        if (event.runId) reply.runId = event.runId;
        if (event.type === "user_message_accepted") reply.accepted = true;
        setMessages((current) => applyStreamEvent(current, event, sessionKey));
        if (event.type === "assistant_failed") setError(event.error ?? "回复未能完成");
      }, { signal: reply.controller.signal, onRun: (runId) => { reply.runId = runId; reply.accepted = true; } });
    } catch (cause) {
      if (reply.stopPromise) await reply.stopPromise.catch(() => undefined);
      if (!reply.controller.signal.aborted) {
        setError(errorMessage(cause, "发送失败"));
        setMessages((current) => current.map((item) => item.status === "streaming" ? { ...item, status: "failed" } : item));
      }
    } finally {
      if (reply.stopPromise) await reply.stopPromise.catch(() => undefined);
      if (activeReply.current === reply) {
        activeReply.current = null;
        setSending(false);
        setStopping(false);
      }
    }
    return reply.accepted;
  };

  const stop = () => {
    const reply = activeReply.current;
    if (!reply || reply.stopPromise || !roleId) return;
    setStopping(true);
    reply.stopPromise = (async () => {
      if (!reply.runId) {
        // The request may already be persisted even when its response headers
        // have not reached the browser. Reconcile before restoring a draft.
        reply.controller.abort();
        const data = await api<SessionData>(`/api/roles/${encodeURIComponent(roleId)}/session`);
        const accepted = data.messages.find((item) => item.role === "user" && item.content === reply.content && !reply.previousIds.has(item.id));
        reply.accepted = Boolean(accepted);
        reply.runId = accepted?.runId ?? undefined;
        setSession(data.session);
        setMessages(data.messages);
      }
      if (reply.runId) {
        const data = await api<SessionData>(`/api/roles/${encodeURIComponent(roleId)}/runs/${encodeURIComponent(reply.runId)}/stop`, { method: "POST" });
        setSession(data.session);
        setMessages(data.messages);
      }
      reply.stopped = true;
      reply.controller.abort();
    })();
    void reply.stopPromise.catch((cause) => {
      setError(errorMessage(cause, "停止回复失败，请重试"));
    }).finally(() => {
      if (activeReply.current === reply && !reply.stopped) {
        reply.stopPromise = null;
        setStopping(false);
      }
    });
  };

  return { roleId, session, messages, opening, sending, stopping, error, setError, open, reset, send, stop, refresh };
}

export type ChatState = ReturnType<typeof useChat>;
