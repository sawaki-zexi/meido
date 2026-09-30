import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import type { Message, Role } from "../../api/types";
import type { ChatState } from "./useChat";
import { Avatar, roleStyle } from "../../ui/Avatar";
import { Notice, Placeholder } from "../../ui/Status";

function MessageItem({ message, role }: { message: Message; role: Role }) {
  const mine = message.role === "user";
  const streaming = message.status === "streaming";
  const failed = message.status === "failed";
  return <li className={`message ${message.role}${failed ? " failed" : ""}`} aria-busy={streaming || undefined}>
    {!mine && <Avatar id={role.id} name={role.name} size="sm" />}
    <div className="bubble">
      <div className="message-heading">
        <strong>{mine ? "我" : role.name}</strong>
        <time dateTime={message.createdAt}>{new Date(message.createdAt).toLocaleString()}</time>
        {streaming && <span className="message-status streaming">生成中</span>}
        {failed && <span className="message-status failed">未完成</span>}
      </div>
      <p>{message.content || (streaming ? <span className="typing" aria-label="正在回复">正在回复…</span> : "（无内容）")}</p>
    </div>
  </li>;
}

export function ChatView({ role, chat }: { role: Role; chat: ChatState }) {
  const [draft, setDraft] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { bottomRef.current?.scrollIntoView?.({ behavior: "smooth", block: "end" }); }, [chat.messages]);
  useEffect(() => { if (!chat.sending) inputRef.current?.focus(); }, [chat.sending]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const text = draft;
    if (!text.trim() || chat.sending) return;
    setDraft("");
    await chat.send(text);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  return <section className="chat" style={roleStyle(role.id)} aria-label={`与${role.name}的会话`}>
    <div className="chat-scroll">
      <p className="chat-meta">唯一会话 · {chat.session?.sessionKey}</p>
      {chat.messages.length
        ? <ol className="message-list" aria-live="polite">{chat.messages.map((message) => <MessageItem key={message.id} message={message} role={role} />)}</ol>
        : <Placeholder>发送第一条消息开始对话。</Placeholder>}
      <div ref={bottomRef} />
    </div>
    <div className="composer-dock">
      {chat.error && <Notice onDismiss={() => chat.setError("")}>{chat.error}</Notice>}
      <form className="composer" onSubmit={submit}>
        <textarea ref={inputRef} aria-label="消息内容" placeholder={`对${role.name}说点什么…`} rows={1} value={draft} disabled={chat.sending} onChange={(event) => setDraft(event.target.value)} onKeyDown={onKeyDown} />
        <button type="submit" className="primary" disabled={chat.sending || !draft.trim()}>{chat.sending ? "发送中" : "发送"}</button>
      </form>
      <small className="composer-hint muted">Enter 发送 · Shift + Enter 换行</small>
    </div>
  </section>;
}
