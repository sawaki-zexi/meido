import { useEffect, useRef, useState } from "react";
import { ModelSettings } from "./ModelSettings";
import { MemoryOrigin, MemoryPanel } from "./MemoryPanel";

type Role = { id: string; name: string; description: string; profile: { profile: string; personality: string; behaviorRules: string; responseConstraints: string; nickname: string }; createdAt: string; updatedAt: string };
type Message = { id: string; sessionKey: string; sequence: number; role: "user" | "assistant"; content: string; status: "streaming" | "completed" | "failed"; createdAt: string };
type Session = { sessionKey: string; roleId: string; createdAt: string; updatedAt: string };
type View = "roles" | "chat" | "model" | "memories";
const empty = { name: "", description: "", profile: "", personality: "", behaviorRules: "", responseConstraints: "", nickname: "" };
type Draft = typeof empty;
const createDraftStorageKey = "meido:create-role-draft";
const draftFields: (keyof Draft)[] = ["name", "description", "profile", "personality", "behaviorRules", "responseConstraints", "nickname"];

function readCreateDraft(): Draft | null {
  try {
    const value = sessionStorage.getItem(createDraftStorageKey);
    if (!value) return null;
    const parsed = JSON.parse(value) as Record<string, unknown>;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    return draftFields.reduce((draft, field) => {
      const candidate = parsed[field];
      draft[field] = typeof candidate === "string" ? candidate : "";
      return draft;
    }, { ...empty });
  } catch {
    return null;
  }
}

function clearCreateDraft(): void {
  try { sessionStorage.removeItem(createDraftStorageKey); } catch { /* storage may be unavailable */ }
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { headers: { "content-type": "application/json" }, ...init });
  } catch {
    throw new Error("无法连接本地后端，请确认服务正在运行");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((item: { msg?: string }) => item.msg).filter(Boolean).join("；")
      : data.detail;
    throw new Error(detail ?? data.error ?? `请求失败 (${response.status})`);
  }
  return data as T;
}

export function App() {
  const [roles, setRoles] = useState<Role[]>([]);
  const [selected, setSelected] = useState<Role | null>(null);
  const [draft, setDraft] = useState<Draft>(() => readCreateDraft() ?? empty);
  const [creating, setCreating] = useState(() => readCreateDraft() !== null);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [view, setView] = useState<View>("roles");
  const [session, setSession] = useState<Session | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [messageDraft, setMessageDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [highlightedMessageId, setHighlightedMessageId] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const messageElements = useRef(new Map<string, HTMLElement>());
  const rolesRequestId = useRef(0);

  const refresh = async () => {
    const requestId = ++rolesRequestId.current;
    let data: { roles: Role[] };
    try {
      data = await api<{ roles: Role[] }>("/api/roles");
    } catch (cause) {
      if (requestId === rolesRequestId.current) throw cause;
      return;
    }
    if (requestId !== rolesRequestId.current) return;
    setRoles(data.roles);
    if (selected) setSelected(data.roles.find((role) => role.id === selected.id) ?? null);
  };

  useEffect(() => { void refresh().catch((cause) => setError(cause.message)); }, []);
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);
  useEffect(() => {
    if (view !== "chat" || !highlightedMessageId) return;
    messageElements.current.get(highlightedMessageId)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [view, highlightedMessageId, messages]);
  useEffect(() => {
    if (!creating) return;
    try { sessionStorage.setItem(createDraftStorageKey, JSON.stringify(draft)); } catch { /* storage may be unavailable */ }
  }, [creating, draft]);

  const edit = (role: Role) => {
    clearCreateDraft();
    setSelected(role);
    setCreating(false);
    setEditing(false);
    setView("roles");
    setDraft({ name: role.name, description: role.description, ...role.profile });
  };

  const cancelEditing = () => {
    setError("");
    if (creating) {
      clearCreateDraft();
      setCreating(false);
      setSelected(null);
      setDraft(empty);
      return;
    }
    if (selected) setDraft({ name: selected.name, description: selected.description, ...selected.profile });
    setEditing(false);
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    setSaving(true);
    const creatingSnapshot = creating;
    const selectedSnapshot = selected;
    const draftSnapshot = { ...draft };
    try {
      const payload = { name: draftSnapshot.name, description: draftSnapshot.description, profile: { profile: draftSnapshot.profile, personality: draftSnapshot.personality, behaviorRules: draftSnapshot.behaviorRules, responseConstraints: draftSnapshot.responseConstraints, nickname: draftSnapshot.nickname } };
      const data = creatingSnapshot
        ? await api<{ role: Role }>("/api/roles", { method: "POST", body: JSON.stringify(payload) })
        : await api<{ role: Role }>(`/api/roles/${encodeURIComponent(selectedSnapshot!.id)}`, { method: "PUT", body: JSON.stringify(payload) });
      // A list request started before this save must not overwrite the saved role.
      rolesRequestId.current += 1;
      setRoles((current) => creatingSnapshot
        ? [...current, data.role]
        : current.map((role) => role.id === data.role.id ? data.role : role));
      setSelected(data.role);
      setDraft({ name: data.role.name, description: data.role.description, ...data.role.profile });
      clearCreateDraft();
      setCreating(false);
      setEditing(false);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "保存失败"); }
    finally { setSaving(false); }
  };

  const removeRole = async () => {
    if (!selected || creating || deleting) return;
    const confirmed = window.confirm(`确定删除角色“${selected.name}”吗？\n\n角色、全部聊天记录和记忆将永久删除，且无法恢复。`);
    if (!confirmed) return;
    setError("");
    setDeleting(true);
    try {
      await api<void>(`/api/roles/${encodeURIComponent(selected.id)}`, { method: "DELETE" });
      // A list request started before this delete must not resurrect the role.
      rolesRequestId.current += 1;
      setRoles((current) => current.filter((role) => role.id !== selected.id));
      setSelected(null);
      setDraft(empty);
      setSession(null);
      setMessages([]);
      setView("roles");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "删除失败");
    } finally { setDeleting(false); }
  };

  const openChat = async (role: Role) => {
    setError("");
    setSelected(role);
    try {
      const data = await api<{ session: Session; messages: Message[] }>(`/api/roles/${encodeURIComponent(role.id)}/session`);
      setSession(data.session);
      setMessages(data.messages);
      setView("chat");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "无法打开会话"); }
  };

  const openMemoryView = (role: Role) => {
    setError("");
    setSelected(role);
    setView("memories");
  };

  const openMemorySource = async (role: Role, origin: MemoryOrigin, messageId?: string) => {
    setError("");
    if (origin.sessionKey !== `role:${role.id}`) {
      setError("来源会话与当前角色不匹配");
      return;
    }
    try {
      const data = await api<{ session: Session; messages: Message[] }>(`/api/roles/${encodeURIComponent(role.id)}/session`);
      const sourceMessage = messageId
        ? data.messages.find((message) => message.id === messageId)
        : origin.messageRange
          ? data.messages.find((message) => message.sequence >= origin.messageRange![0] && message.sequence <= origin.messageRange![1])
          : undefined;
      if (!sourceMessage) {
        setError("来源消息当前不可访问");
        return;
      }
      setSelected(role);
      setSession(data.session);
      setMessages(data.messages);
      setHighlightedMessageId(sourceMessage.id);
      setView("chat");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "无法打开来源消息"); }
  };

  const sendMessage = async (event: React.FormEvent) => {
    event.preventDefault();
    const content = messageDraft.trim();
    if (!content || !selected || sending) return;
    setMessageDraft("");
    setError("");
    setSending(true);
    try {
      const response = await fetch(`/api/roles/${encodeURIComponent(selected.id)}/messages`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ content }),
      });
      if (!response.ok) {
        const body = await response.json();
        throw new Error(body.detail ?? "发送失败");
      }
      if (!response.body) throw new Error("服务端没有返回流式响应");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      const handleEvent = (raw: string) => {
        const lines = raw.split("\n");
        const eventName = lines.find((line) => line.startsWith("event: "))?.slice(7);
        const dataLine = lines.find((line) => line.startsWith("data: "))?.slice(6);
        if (!eventName || !dataLine) return;
        const data = JSON.parse(dataLine) as { messageId?: string; delta?: string; message?: Message; error?: string };
        if (eventName === "user_message_accepted") {
          if (data.message) setMessages((current) => [...current, data.message!]);
        } else if (eventName === "assistant_generation_started" && data.messageId) {
          setMessages((current) => [...current, { id: data.messageId!, sessionKey: session?.sessionKey ?? `role:${selected.id}`, sequence: current.length + 1, role: "assistant", content: "", status: "streaming", createdAt: new Date().toISOString() }]);
        } else if (eventName === "assistant_delta" && data.messageId) {
          setMessages((current) => current.map((message) => message.id === data.messageId ? { ...message, content: message.content + (data.delta ?? "") } : message));
        } else if ((eventName === "assistant_completed" || eventName === "assistant_failed") && data.message) {
          setMessages((current) => current.map((message) => message.id === data.message!.id ? data.message! : message));
          if (eventName === "assistant_failed") setError(data.error ?? "回复未能完成");
        }
      };
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
        const events = buffer.split("\n\n");
        buffer = events.pop() ?? "";
        events.forEach(handleEvent);
        if (done) break;
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "发送失败");
    } finally { setSending(false); }
  };

  return <main>
    <header><h1>{view === "chat" ? selected?.name : view === "memories" ? "角色记忆" : view === "model" ? "模型设置" : "角色"}</h1><div className="header-actions">{view === "chat" && <><button disabled={saving || deleting} onClick={() => setView("roles")}>返回角色</button>{selected && <button disabled={saving || deleting} onClick={() => openMemoryView(selected)}>查看记忆</button>}</>}{view === "model" && <button disabled={saving || deleting} onClick={() => setView("roles")}>返回角色</button>}{view !== "model" && view !== "memories" && <button disabled={saving || deleting} onClick={() => setView("model")}>模型设置</button>}{view === "roles" && <button disabled={saving || deleting} onClick={() => { setError(""); setCreating(true); setSelected(null); setDraft((current) => creating ? current : empty); setView("roles"); }}>创建角色</button>}</div></header>
    {error && <p className="error global-error">{error}</p>}
    {view === "model" ? <ModelSettings onBack={() => setView("roles")} /> : view === "memories" && selected ? <MemoryPanel roleId={selected.id} roleName={selected.name} onBack={() => setView("roles")} onOpenSource={(origin, messageId) => void openMemorySource(selected, origin, messageId)} /> : view === "chat" && selected ? <section className="chat-panel">
      <div className="chat-meta">唯一会话 · {session?.sessionKey}</div>
      <div className="chat-meta"><button type="button" onClick={() => openMemoryView(selected)}>查看角色记忆</button></div>
      <div className="message-list">{messages.map((message) => <article ref={(element) => { if (element) messageElements.current.set(message.id, element); else messageElements.current.delete(message.id); }} className={`message ${message.role} ${message.id === highlightedMessageId ? "source-highlight" : ""}`} key={message.id}><div className="message-heading"><strong>{message.role === "user" ? "我" : selected.name}</strong><time>{new Date(message.createdAt).toLocaleString()}</time>{message.status !== "completed" && <span className={`message-status ${message.status}`}>{message.status === "streaming" ? "生成中" : "未完成"}</span>}{message.id === highlightedMessageId && <span className="source-marker">记忆来源</span>}</div><p>{message.content || (message.status === "streaming" ? "正在回复…" : "（无内容）")}</p></article>)}{!messages.length && <p className="empty-chat">发送第一条消息开始对话。</p>}<div ref={bottomRef}/></div>
      <form className="composer" onSubmit={sendMessage}><textarea aria-label="消息内容" placeholder="写消息…" value={messageDraft} onChange={(event) => setMessageDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} disabled={sending} /><button type="submit" disabled={sending || !messageDraft.trim()}>{sending ? "发送中" : "发送"}</button></form>
    </section> : <div className="layout"><aside>{roles.map((role) => <div className={`role-item ${selected?.id === role.id ? "active" : ""}`} key={role.id}><button className="role-select" onClick={() => edit(role)}><strong>{role.name}</strong><span>{role.description || "暂无简介"}</span><small>{role.id}</small></button><button className="role-open" onClick={() => void openChat(role)}>进入会话</button></div>)}{!roles.length && <p>还没有角色。</p>}</aside><section>{(creating || selected) ? <form onSubmit={save}><h2>{creating ? "创建角色" : "角色详情"}</h2><label>名称<input required disabled={!creating && !editing || saving || deleting} value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })}/></label><label>简介<textarea disabled={!creating && !editing || saving || deleting} value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })}/></label><label>角色设定<textarea required disabled={!creating && !editing || saving || deleting} value={draft.profile} onChange={(e) => setDraft({ ...draft, profile: e.target.value })}/></label><label>性格<textarea disabled={!creating && !editing || saving || deleting} value={draft.personality} onChange={(e) => setDraft({ ...draft, personality: e.target.value })}/></label><label>行为规则<textarea disabled={!creating && !editing || saving || deleting} value={draft.behaviorRules} onChange={(e) => setDraft({ ...draft, behaviorRules: e.target.value })}/></label><label>回复约束<textarea disabled={!creating && !editing || saving || deleting} value={draft.responseConstraints} onChange={(e) => setDraft({ ...draft, responseConstraints: e.target.value })}/></label><label>昵称<input disabled={!creating && !editing || saving || deleting} value={draft.nickname} onChange={(e) => setDraft({ ...draft, nickname: e.target.value })}/></label><div className="form-actions">{creating || editing ? <><button type="submit" disabled={saving || deleting}>{saving ? "保存中…" : "保存"}</button><button type="button" disabled={saving || deleting} onClick={cancelEditing}>取消</button></> : <><button type="button" onClick={() => setEditing(true)}>编辑</button><button type="button" disabled={deleting} onClick={() => void removeRole()}>{deleting ? "删除中…" : "删除角色"}</button>{selected && <button type="button" onClick={() => openMemoryView(selected)}>查看记忆</button>}</>}{selected && <button type="button" disabled={saving || deleting} onClick={() => void openChat(selected)}>进入会话</button>}</div></form> : <p>选择角色或创建新角色。</p>}</section></div>}
  </main>;
}
