import { useEffect, useRef, useState } from "react";

type Role = { id: string; name: string; description: string; profile: { profile: string; personality: string; behaviorRules: string; responseConstraints: string; nickname: string }; createdAt: string; updatedAt: string };
type Message = { id: string; sessionKey: string; sequence: number; role: "user" | "assistant"; content: string; status: "streaming" | "completed" | "failed"; createdAt: string };
type Session = { sessionKey: string; roleId: string; createdAt: string; updatedAt: string };
type View = "roles" | "chat";
const empty = { name: "", description: "", profile: "", personality: "", behaviorRules: "", responseConstraints: "", nickname: "" };

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { headers: { "content-type": "application/json" }, ...init });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail ?? data.error ?? "请求失败");
  return data as T;
}

export function App() {
  const [roles, setRoles] = useState<Role[]>([]);
  const [selected, setSelected] = useState<Role | null>(null);
  const [draft, setDraft] = useState(empty);
  const [creating, setCreating] = useState(false);
  const [view, setView] = useState<View>("roles");
  const [session, setSession] = useState<Session | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [messageDraft, setMessageDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  const refresh = async () => {
    const data = await api<{ roles: Role[] }>("/api/roles");
    setRoles(data.roles);
    if (selected) setSelected(data.roles.find((role) => role.id === selected.id) ?? null);
  };

  useEffect(() => { void refresh().catch((cause) => setError(cause.message)); }, []);
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages]);

  const edit = (role: Role) => {
    setSelected(role);
    setCreating(false);
    setView("roles");
    setDraft({ name: role.name, description: role.description, ...role.profile });
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    try {
      const payload = { name: draft.name, description: draft.description, profile: { profile: draft.profile, personality: draft.personality, behaviorRules: draft.behaviorRules, responseConstraints: draft.responseConstraints, nickname: draft.nickname } };
      const data = creating
        ? await api<{ role: Role }>("/api/roles", { method: "POST", body: JSON.stringify(payload) })
        : await api<{ role: Role }>(`/api/roles/${encodeURIComponent(selected!.id)}`, { method: "PUT", body: JSON.stringify(payload) });
      await refresh();
      edit(data.role);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "保存失败"); }
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
    <header><h1>{view === "chat" ? selected?.name : "角色"}</h1><div className="header-actions">{view === "chat" && <button onClick={() => setView("roles")}>返回角色</button>}<button onClick={() => { setCreating(true); setSelected(null); setDraft(empty); setView("roles"); }}>创建角色</button></div></header>
    {error && <p className="error global-error">{error}</p>}
    {view === "chat" && selected ? <section className="chat-panel">
      <div className="chat-meta">唯一会话 · {session?.sessionKey}</div>
      <div className="message-list">{messages.map((message) => <article className={`message ${message.role}`} key={message.id}><div className="message-heading"><strong>{message.role === "user" ? "我" : selected.name}</strong><time>{new Date(message.createdAt).toLocaleString()}</time>{message.status !== "completed" && <span className={`message-status ${message.status}`}>{message.status === "streaming" ? "生成中" : "未完成"}</span>}</div><p>{message.content || (message.status === "streaming" ? "正在回复…" : "（无内容）")}</p></article>)}{!messages.length && <p className="empty-chat">发送第一条消息开始对话。</p>}<div ref={bottomRef}/></div>
      <form className="composer" onSubmit={sendMessage}><textarea aria-label="消息内容" placeholder="写消息…" value={messageDraft} onChange={(event) => setMessageDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} disabled={sending} /><button type="submit" disabled={sending || !messageDraft.trim()}>{sending ? "发送中" : "发送"}</button></form>
    </section> : <div className="layout"><aside>{roles.map((role) => <div className={`role-item ${selected?.id === role.id ? "active" : ""}`} key={role.id}><button className="role-select" onClick={() => edit(role)}><strong>{role.name}</strong><span>{role.description || "暂无简介"}</span><small>{role.id}</small></button><button className="role-open" onClick={() => void openChat(role)}>进入会话</button></div>)}{!roles.length && <p>还没有角色。</p>}</aside><section>{(creating || selected) ? <form onSubmit={save}><h2>{creating ? "创建角色" : "角色详情"}</h2><label>名称<input required value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })}/></label><label>简介<textarea value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })}/></label><label>角色设定<textarea required value={draft.profile} onChange={(e) => setDraft({ ...draft, profile: e.target.value })}/></label><label>性格<textarea value={draft.personality} onChange={(e) => setDraft({ ...draft, personality: e.target.value })}/></label><label>行为规则<textarea value={draft.behaviorRules} onChange={(e) => setDraft({ ...draft, behaviorRules: e.target.value })}/></label><label>回复约束<textarea value={draft.responseConstraints} onChange={(e) => setDraft({ ...draft, responseConstraints: e.target.value })}/></label><label>昵称<input value={draft.nickname} onChange={(e) => setDraft({ ...draft, nickname: e.target.value })}/></label><div className="form-actions"><button type="submit">保存</button>{selected && <button type="button" onClick={() => void openChat(selected)}>进入会话</button>}</div></form> : <p>选择角色或创建新角色。</p>}</section></div>}
  </main>;
}
