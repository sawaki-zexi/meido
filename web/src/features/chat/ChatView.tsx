import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import type { Message, Role } from "../../api/types";
import type { ChatState } from "./useChat";
import { Avatar, roleStyle } from "../../ui/Avatar";
import { Icon, IconButton } from "../../ui/Icon";
import { Notice, Placeholder } from "../../ui/Status";
import { RoleModelSelector } from "../model/RoleModelSelector";

function MessageItem({ message, role, highlighted }: { message: Message; role: Role; highlighted: boolean }) {
  const mine = message.role === "user";
  const streaming = message.status === "streaming";
  const failed = message.status === "failed";
  const ref = useRef<HTMLLIElement>(null);
  // A memory source opens the chat scrolled to the message it came from.
  useEffect(() => { if (highlighted) ref.current?.scrollIntoView?.({ behavior: "smooth", block: "center" }); }, [highlighted]);
  return <li ref={ref} className={`message ${message.role}${failed ? " failed" : ""}${highlighted ? " source-highlight" : ""}`} aria-busy={streaming || undefined}>
    {!mine && <Avatar id={role.id} name={role.name} size="sm" avatarUrl={role.avatarUrl} />}
    <div className="message-body">
      <div className="message-heading">
        <strong>{mine ? "我" : role.name}</strong>
        <time dateTime={message.createdAt}>{new Date(message.createdAt).toLocaleString()}</time>
        {streaming && <span className="message-status streaming">生成中</span>}
        {failed && <span className="message-status failed">未完成</span>}
        {highlighted && <span className="source-marker">记忆来源</span>}
      </div>
      <p className="bubble">{message.content || (streaming ? <span className="typing" aria-label="正在回复">正在回复…</span> : "（无内容）")}</p>
    </div>
  </li>;
}

/** Message box shared by the conversation and the welcome screen, so home always looks like a chat. */
function Composer({ placeholder, disabled, sending, onSend }: { placeholder: string; disabled?: boolean; sending?: boolean; onSend: (text: string) => Promise<unknown> }) {
  const [draft, setDraft] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (!sending && !disabled) inputRef.current?.focus(); }, [sending, disabled]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const text = draft;
    if (!text.trim() || sending || disabled) return;
    setDraft("");
    await onSend(text);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  return <form className="composer" onSubmit={submit}>
    <textarea ref={inputRef} aria-label="消息内容" placeholder={placeholder} rows={1} value={draft} disabled={disabled || sending} onChange={(event) => setDraft(event.target.value)} onKeyDown={onKeyDown} />
    <button type="submit" className="send" aria-label="发送" title="发送（Enter）" disabled={disabled || sending || !draft.trim()}>
      {sending ? <span className="spinner" aria-hidden="true" /> : <Icon name="send" size={18} />}
    </button>
  </form>;
}

type ChatProps = { role: Role; chat: ChatState; highlightedId?: string | null; onBack: () => void; onShowProfile: () => void; onShowMemories: () => void };

export function ChatView({ role, chat, highlightedId = null, onBack, onShowProfile, onShowMemories }: ChatProps) {
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => { if (!highlightedId) bottomRef.current?.scrollIntoView?.({ behavior: "smooth", block: "end" }); }, [chat.messages, highlightedId]);

  return <section className="chat" style={roleStyle(role.id)} aria-label={`与${role.name}的会话`}>
    <header className="chat-header">
      <IconButton icon="back" label="对话列表" className="only-narrow" onClick={onBack} />
      <Avatar id={role.id} name={role.name} size="sm" avatarUrl={role.avatarUrl} />
      <div className="chat-title">
        <h1>{role.name}</h1>
        {role.description && <small>{role.description}</small>}
      </div>
      <RoleModelSelector roleId={role.id} disabled={chat.sending || chat.opening} />
      <IconButton icon="memory" label="角色记忆" onClick={onShowMemories} />
      <IconButton icon="profile" label="角色资料" onClick={onShowProfile} />
    </header>
    <div className="chat-scroll">
      {chat.messages.length
        ? <ol className="message-list" aria-live="polite">{chat.messages.map((message) => <MessageItem key={message.id} message={message} role={role} highlighted={message.id === highlightedId} />)}</ol>
        : <Placeholder>发送第一条消息开始对话。</Placeholder>}
      <div ref={bottomRef} />
    </div>
    <div className="composer-dock">
      {chat.error && <Notice onDismiss={() => chat.setError("")}>{chat.error}</Notice>}
      <Composer placeholder={`对${role.name}说点什么…`} sending={chat.sending} onSend={chat.send} />
    </div>
  </section>;
}

type WelcomeProps = { loading: boolean; hasRoles: boolean; error: string; onDismissError: () => void; onCreate: () => void; onBack: () => void };

/** Home before any conversation is open: the same chat frame, with a note from Meido herself. */
export function WelcomeView({ loading, hasRoles, error, onDismissError, onCreate, onBack }: WelcomeProps) {
  return <section className="chat" aria-label="Meido">
    <header className="chat-header">
      <IconButton icon="back" label="对话列表" className="only-narrow" onClick={onBack} />
      <span className="avatar avatar-sm avatar-meido" aria-hidden="true">M</span>
      <div className="chat-title"><h1>Meido</h1></div>
    </header>
    <div className="chat-scroll">
      {loading ? <Placeholder loading>正在加载角色…</Placeholder>
        : hasRoles ? <Placeholder>从左侧选一位角色，继续你们的对话。</Placeholder>
        : <div className="empty-create">
          <button type="button" className="empty-create-button" aria-label="创建角色" onClick={onCreate}><Icon name="plus" size={28} /></button>
          <p>还没有角色。先创建一位，她会在这里等你。</p>
        </div>}
    </div>
    <div className="composer-dock">
      {error && <Notice onDismiss={onDismissError}>{error}</Notice>}
      <Composer placeholder={hasRoles ? "选择角色后开始对话" : "创建角色后开始对话"} disabled onSend={async () => undefined} />
    </div>
  </section>;
}
