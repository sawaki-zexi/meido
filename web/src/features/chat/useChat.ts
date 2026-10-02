import { useState } from "react";
import { api, errorMessage } from "../../api/client";
import { streamRoleMessage, type StreamEvent } from "../../api/stream";
import type { Message, Role, Session } from "../../api/types";

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
  const [error, setError] = useState("");

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

  const reset = () => { setRoleId(null); setSession(null); setMessages([]); setError(""); };

  /** Sends a message. Resolves false when nothing was sent so the caller can keep the draft. */
  const send = async (text: string): Promise<boolean> => {
    const content = text.trim();
    if (!content || !roleId || sending) return false;
    setError("");
    setSending(true);
    const sessionKey = session?.sessionKey ?? `role:${roleId}`;
    try {
      await streamRoleMessage(roleId, content, (event) => {
        setMessages((current) => applyStreamEvent(current, event, sessionKey));
        if (event.type === "assistant_failed") setError(event.error ?? "回复未能完成");
      });
    } catch (cause) {
      setError(errorMessage(cause, "发送失败"));
    } finally {
      setSending(false);
    }
    return true;
  };

  return { roleId, session, messages, opening, sending, error, setError, open, reset, send };
}

export type ChatState = ReturnType<typeof useChat>;
