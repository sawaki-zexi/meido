import { useEffect, useRef, useState, type DragEvent, type FormEvent, type KeyboardEvent } from "react";
import type { ChatAttachment, Message, Role, ModelConfiguration } from "../../api/types";
import type { ChatState } from "./useChat";
import { Avatar, roleStyle } from "../../ui/Avatar";
import { Icon, IconButton } from "../../ui/Icon";
import { Notice, Placeholder } from "../../ui/Status";
import { RoleModelSelector } from "../model/RoleModelSelector";
import { ChatActions } from './ChatActions';
import { useChatAttachments } from "./useChatAttachments";
import { GenerationDrawer } from "../novelai/GenerationDrawer";
import { CodingTasks } from "../coding/CodingTasks";
import { Dialog } from "../../ui/Dialog";
import { GeneratedImageAction } from "../novelai/GeneratedImageAction";
import { roleMood } from '../role-management/roleMood';
import { roleAssets } from '../../api/roleAssets';
import '../role-management/roleIllustrations.css';
import type { GenerationSelection } from "../../api/novelai";
import { useChatScroll } from "./useChatScroll";
import { AttachmentImage } from "../../ui/AttachmentImage";
import { MessageMarkdown } from "../../ui/MessageMarkdown";
import { MessageProcess, readProcess } from './MessageProcess';
import { ComposerTools, readContextUsage, type ContextUsage } from './ComposerTools';
import type { ToolProgressState } from './toolProgress';
import "./tool-progress.css";
import "./chat-presentation.css";

function MessageItem({ message, role, highlighted, onRegenerate, progress, stopping }: { message: Message; role: Role; highlighted: boolean; stopping?: boolean; progress?: ToolProgressState | null; onRegenerate: (selection: GenerationSelection) => void }) {
  const mine = message.role === "user";
  const streaming = message.status === "streaming";
  const failed = message.status === "failed";
  const stopped = failed && message.metadata?.runtimeReason === "cancelled";
  const ref = useRef<HTMLLIElement>(null);
  // A memory source opens the chat scrolled to the message it came from.
  useEffect(() => { if (highlighted) ref.current?.scrollIntoView?.({ behavior: "smooth", block: "center" }); }, [highlighted]);
  const attachments = Array.isArray(message.metadata?.attachments)
    ? message.metadata.attachments.filter((item): item is ChatAttachment => Boolean(item && typeof item === "object" && "id" in item && "name" in item))
    : [];
  const generationAttempts = Array.isArray(message.metadata?.generationAttemptIds)
    ? message.metadata.generationAttemptIds.filter((item): item is string => typeof item === 'string' && Boolean(item)) : [];
  const hasProcess = !mine && (readProcess(message.metadata?.process).length > 0 || Boolean(message.metadata?.streamThinking) || Boolean(progress?.calls.length));
  return <li ref={ref} className={`message ${message.role}${failed ? " failed" : ""}${highlighted ? " source-highlight" : ""}`} aria-busy={streaming || undefined}>
    {!mine && <Avatar id={role.id} name={role.name} size="sm" avatarUrl={role.avatarUrl} />}
    <div className="message-body">
      <div className="message-heading">
        <strong>{mine ? "我" : role.name}</strong>
        <time dateTime={message.createdAt}>{new Date(message.createdAt).toLocaleString()}</time>
        {streaming && <span className="message-status streaming">生成中</span>}
        {failed && <span className={`message-status ${stopped ? "stopped" : "failed"}`}>{stopped ? "已停止" : "未完成"}</span>}
        {highlighted && <span className="source-marker">记忆来源</span>}
      </div>
      {(message.content || hasProcess || attachments.length === 0) && <div className="bubble">
        {!mine && <MessageProcess message={message} progress={progress} stopping={stopping} />}
        <div className="reply-content">
          {message.content && <MessageMarkdown content={message.content} />}
          {!message.content && attachments.length === 0 && <p>{streaming ? <span className="typing" aria-label="正在回复">正在回复…</span> : stopped ? "已停止回复" : "（无内容）"}</p>}
        </div>
      </div>}
      {attachments.length > 0 && <div className="message-attachments">
        {attachments.map((attachment) => {
          const href = `/api/roles/${encodeURIComponent(role.id)}/attachments/${encodeURIComponent(attachment.id)}`;
          return attachment.kind === "image"
            ? <div key={attachment.id}><AttachmentImage src={href} href={href} alt={attachment.name} />
              {!mine && message.status === 'completed' && generationAttempts.length > 0 && <GeneratedImageAction roleId={role.id} messageId={message.id} image={attachment} attemptIds={generationAttempts} onSelect={onRegenerate} />}</div>
            : <a className="message-file" href={href} target="_blank" rel="noreferrer" key={attachment.id}>{attachment.name}</a>;
        })}
      </div>}

    </div>
  </li>;
}

/** Message box shared by the conversation and the welcome screen, so home always looks like a chat. */
function Composer({ roleId, usage = null, placeholder, disabled, sending, stopping, onSend, onStop }: { roleId?: string; usage?: ContextUsage | null; placeholder: string; disabled?: boolean; sending?: boolean; stopping?: boolean; onSend: (text: string, attachmentIds: string[]) => Promise<boolean>; onStop?: () => void }) {
  const [configuration, setConfiguration] = useState<ModelConfiguration | null>(null);
  const [draft, setDraft] = useState("");
  const [unsentText, setUnsentText] = useState("");
  const { attachments, uploading, dragging, setDragging, error, setError, upload, remove, clear, restore } = useChatAttachments(roleId, Boolean(disabled), Boolean(sending));
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const draftRevision = useRef(0);
  const submitting = useRef(false);
  useEffect(() => { if (!sending && !disabled) inputRef.current?.focus(); }, [sending, disabled]);

  const insertEmoji = (emoji: string) => {
    const input = inputRef.current;
    const start = input?.selectionStart ?? draft.length;
    const end = input?.selectionEnd ?? start;
    draftRevision.current += 1;
    setDraft(draft.slice(0, start) + emoji + draft.slice(end));
    requestAnimationFrame(() => { input?.focus(); input?.setSelectionRange(start + emoji.length, start + emoji.length); });
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const text = draft;
    if ((!text.trim() && attachments.length === 0) || sending || disabled || uploading || submitting.current) return;
    submitting.current = true;
    const revision = ++draftRevision.current;
    const sentAttachments = attachments;
    setDraft("");
    clear();
    try {
      const sent = await onSend(text, sentAttachments.map((item) => item.id));
      if (!sent) {
        if (draftRevision.current === revision) setDraft(text);
        else if (text) setUnsentText(text);
        restore(sentAttachments);
      }
    } finally {
      submitting.current = false;
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  const onDrop = (event: DragEvent<HTMLFormElement>) => {
    event.preventDefault();
    setDragging(false);
    void upload([...event.dataTransfer.files]);
  };
  return <form aria-label="聊天输入" className={`composer${dragging ? " is-dragging" : ""}`} onSubmit={submit} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={onDrop}>
    {attachments.length > 0 && <div className="composer-attachments" aria-label="待发送附件">
      {attachments.map((attachment) => attachment.kind === "image"
        ? <div className="attachment-preview chat-attachment-preview" key={attachment.id}><AttachmentImage src={`/api/roles/${encodeURIComponent(roleId ?? "")}/attachments/${encodeURIComponent(attachment.id)}`} alt={attachment.name} /><button type="button" aria-label={`移除 ${attachment.name}`} onClick={() => remove(attachment.id)}>×</button></div>
        : <div className="attachment-file" key={attachment.id}><span>{attachment.name}</span><button type="button" aria-label={`移除 ${attachment.name}`} onClick={() => remove(attachment.id)}>×</button></div>)}
    </div>}
    {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
    {unsentText && <div className="composer-unsent" role="note"><p>未发送的消息：{unsentText}</p><IconButton icon="close" label="忽略未发送消息" onClick={() => setUnsentText("")} /></div>}
    <div className="composer-row">
      <input ref={fileRef} type="file" aria-label="附件文件" multiple accept="image/png,image/jpeg,image/webp,image/gif,.txt,.md,.csv,.json,.pdf,.docx" hidden onChange={(event) => { const input = event.currentTarget; const files = [...input.files ?? []]; void upload(files).finally(() => { input.value = ""; }); }} />
      <button type="button" className="composer-attach" aria-label="添加附件" title="添加附件" disabled={disabled || sending || uploading} onClick={() => fileRef.current?.click()}><Icon name="attachment" size={18} /></button>
      <textarea ref={inputRef} aria-label="消息内容" placeholder={placeholder} rows={1} value={draft} disabled={disabled} onChange={(event) => { draftRevision.current += 1; setDraft(event.target.value); }} onKeyDown={onKeyDown} />
      {roleId && <RoleModelSelector roleId={roleId} disabled={disabled || sending || uploading} onChange={setConfiguration} />}
      <ComposerTools disabled={disabled} onEmoji={insertEmoji} usage={usage && (!configuration || usage.configurationId === configuration.id && (!usage.model || usage.model === configuration.model)) ? usage : null} />
      {sending
        ? <button type="button" className="send stop" aria-label="停止回复" title={stopping ? "正在停止…" : "停止回复"} disabled={stopping} onClick={onStop}>{stopping ? <span className="spinner" aria-hidden="true" /> : <Icon name="stop" size={18} />}</button>
        : <button type="submit" className="send" aria-label="发送" title="发送（Enter）" disabled={disabled || uploading || (!draft.trim() && attachments.length === 0)}><Icon name="send" size={18} /></button>}
    </div>
  </form>;
}

type ChatProps = { role: Role; chat: ChatState; highlightedId?: string | null; onBack: () => void; onShowProfile: () => void; onShowMemories: () => void; onShowChannels: () => void };

export function ChatView({ role, chat, highlightedId = null, onBack, onShowProfile, onShowMemories, onShowChannels }: ChatProps) {
  const { scrollRef, listRef, onScroll } = useChatScroll({ roleId: role.id, messages: chat.messages, sending: chat.sending, highlightedId });
  const [generationRoleId, setGenerationRoleId] = useState<string | null>(null);
  const [selectedGenerationImage, setSelectedGenerationImage] = useState<GenerationSelection | undefined>();
  useEffect(() => { setGenerationRoleId(null); setSelectedGenerationImage(undefined); }, [role.id]);
  const [presentationRole, setPresentationRole] = useState(role);
  const activePresentation = presentationRole.id === role.id ? presentationRole : role;
  const background = activePresentation.chatBackgroundUrl;
  const presentation = roleMood(activePresentation, chat.messages);
  useEffect(() => {
    let active = true;
    setPresentationRole(role);
    void roleAssets.list(role.id).then(({ role: fresh }) => { if (active) setPresentationRole(fresh); }).catch(() => {});
    return () => { active = false; };
  }, [role, chat.sending, generationRoleId]);
  const [codingOpen, setCodingOpen] = useState(false);
  useEffect(() => { setCodingOpen(false); }, [role.id]);
  useEffect(() => {
    if (!chat.session || chat.sending) return;
    let active = true;
    const refresh = () => {
      void chat.refresh();
      void roleAssets.list(role.id).then(({ role: fresh }) => { if (active) setPresentationRole(fresh); }).catch(() => {});
    };
    const timer = window.setInterval(refresh, 10000);
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") refresh();
    };
    window.addEventListener("focus", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      active = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [role.id, chat.session?.sessionKey, chat.sending, chat.refresh]);

  return <section className="chat" style={roleStyle(role.id)} aria-label={`与${role.name}的会话`}>
    <header className="chat-header">
      <IconButton icon="back" label="对话列表" className="only-narrow" onClick={onBack} />
      <button type="button" className="chat-profile" aria-label="角色资料" title="查看角色资料" onClick={onShowProfile}>
        <Avatar id={role.id} name={role.name} size="sm" avatarUrl={role.avatarUrl} />
        <span className="chat-title">
        <h1>{role.name}</h1>
        </span>
      </button>
      <IconButton icon="phone" label="多端会话" onClick={onShowChannels} />
      <IconButton icon="memory" label="角色记忆" onClick={onShowMemories} />
      <ChatActions key={role.id} codingEnabled={Boolean(role.agentConfig?.coding?.enabled)}
        onGenerate={() => { setSelectedGenerationImage(undefined); setGenerationRoleId(role.id); }} onCoding={() => setCodingOpen(true)} />
    </header>
    <div className={`chat-scroll${background ? " has-role-background" : ""}`} style={background ? { backgroundImage: `linear-gradient(rgba(255,255,255,.2),rgba(255,255,255,.2)), url(${JSON.stringify(background)})` } : undefined} ref={scrollRef} onScroll={onScroll}>
      {presentation.illustration && <figure className="role-mood-portrait" aria-label={`当前心情：${presentation.mood}`}><img src={presentation.illustration.url} alt={`${role.name} · ${presentation.mood}`} /></figure>}
      {chat.messages.length
        ? <ol ref={listRef} className="message-list" aria-live="polite">{chat.messages.map((message) => <MessageItem key={message.id} message={message} role={role} highlighted={message.id === highlightedId} stopping={chat.stopping} progress={message.status === "streaming" && chat.toolProgress?.roleId === role.id && chat.toolProgress.sessionKey === chat.session?.sessionKey ? chat.toolProgress : null}
          onRegenerate={(selection) => { setSelectedGenerationImage(selection); setGenerationRoleId(role.id); }} />)}</ol>
        : <Placeholder>发送第一条消息开始对话。</Placeholder>}
    </div>
    <div className="composer-dock">
      {chat.error && <Notice onDismiss={() => chat.setError("")}>{chat.error}</Notice>}
      <Composer key={role.id} roleId={role.id} usage={[...chat.messages].reverse().map(message => readContextUsage(message.metadata?.contextUsage)).find(Boolean) ?? null} placeholder={`对${role.name}说点什么…`} sending={chat.sending} stopping={chat.stopping} disabled={chat.opening} onSend={chat.send} onStop={chat.stop} />
    </div>
    {generationRoleId === role.id && <GenerationDrawer key={role.id} role={role} onClose={() => setGenerationRoleId(null)} onGenerated={chat.refresh} selectedImage={selectedGenerationImage}
      attachments={chat.messages.flatMap((message) => Array.isArray(message.metadata?.attachments)
        ? message.metadata.attachments.filter((item): item is ChatAttachment => Boolean(item && typeof item === 'object' && 'id' in item && 'name' in item)) : [])} />}
    {codingOpen && <Dialog title="编程任务" onClose={() => setCodingOpen(false)}><CodingTasks roleId={role.id} /></Dialog>}
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
      <Composer placeholder={hasRoles ? "选择角色后开始对话" : "创建角色后开始对话"} disabled onSend={async () => false} />
    </div>
  </section>;
}
