import { useEffect, useState, type FormEvent } from "react";
import { api, errorMessage } from "../../api/client";
import type { Role } from "../../api/types";
import { roleStyle } from "../../ui/Avatar";
import { IconButton } from "../../ui/Icon";
import { Notice, Placeholder } from "../../ui/Status";

export type MemoryOrigin = {
  kind: string;
  sessionKey: string;
  messageIds: string[];
  messageRange: [number, number] | null;
  stableSourceKey: string;
};

type Memory = {
  id: string;
  roleId: string;
  memoryType: string;
  summary: string;
  status: string;
  happenedAt: string | null;
  createdAt: string;
  updatedAt: string;
  sourceRef: MemoryOrigin & { sourceKeys: string[]; sources: MemoryOrigin[] };
};

type Props = {
  role: Role;
  onBack: () => void;
  onOpenSource: (origin: MemoryOrigin, messageId?: string) => void;
};

const typeLabels: Record<string, string> = {
  fact: "事实",
  profile: "资料",
  preference: "偏好",
  procedure: "习惯与要求",
  event: "共同经历",
};

const sourceLabels: Record<string, string> = {
  message: "消息来源",
  turn: "对话回合",
  consolidation: "整理窗口",
  manual: "手动记录",
};

const documentNames = ["SELF.md", "MEMORY.md", "HISTORY.md", "PENDING.md", "RECENT_CONTEXT.md"];
const emptyDraft = { summary: "", memoryType: "fact" };

const formatDate = (value: string | null) => value ? new Date(value).toLocaleString() : "未知";

/** A role's long-term memories: search, remember, edit, forget, and jump back to where she learned them. */
export function MemoryPanel({ role, onBack, onOpenSource }: Props) {
  const base = `/api/roles/${encodeURIComponent(role.id)}`;
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [memories, setMemories] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  // null: no form; "new": remembering something; otherwise the memory being edited.
  const [editing, setEditing] = useState<Memory | "new" | null>(null);
  const [draft, setDraft] = useState(emptyDraft);
  const [saving, setSaving] = useState(false);
  const [documents, setDocuments] = useState<Record<string, string>>({});
  const [journals, setJournals] = useState<{ date: string; content: string }[]>([]);
  const [selectedDocument, setSelectedDocument] = useState("MEMORY.md");
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let current = true;
    const params = query ? `?${new URLSearchParams({ q: query })}` : "";
    setLoading(true);
    api<{ memories: Memory[] }>(`${base}/memories${params}`)
      .then((data) => { if (current) setMemories(data.memories); })
      .catch((cause) => { if (current) setError(errorMessage(cause, "读取记忆失败")); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [base, query, version]);

  useEffect(() => {
    let current = true;
    api<{ documents: { name: string; content: string }[]; journals: { date: string; content: string }[] }>(`${base}/memory-documents`)
      .then((data) => {
        if (!current) return;
        setDocuments(Object.fromEntries(data.documents.map((item) => [item.name, item.content])));
        setJournals(data.journals);
      })
      .catch((cause) => { if (current) setError(errorMessage(cause, "读取记忆文档失败")); });
    return () => { current = false; };
  }, [base, version]);

  const search = (event: FormEvent) => { event.preventDefault(); setQuery(queryDraft.trim()); };

  const startEdit = (target: Memory | "new") => {
    setError("");
    setEditing(target);
    setDraft(target === "new" ? emptyDraft : { summary: target.summary, memoryType: target.memoryType });
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!editing) return;
    setSaving(true);
    setError("");
    try {
      const path = editing === "new" ? `${base}/memories` : `${base}/memories/${encodeURIComponent(editing.id)}`;
      await api(path, { method: editing === "new" ? "POST" : "PUT", body: JSON.stringify(draft) });
      setEditing(null);
      setDraft(emptyDraft);
      setVersion((value) => value + 1);
    } catch (cause) { setError(errorMessage(cause, "保存记忆失败")); } finally { setSaving(false); }
  };

  const mutate = async (memory: Memory, action: "forget" | "delete" | "reject") => {
    if (action === "delete" && !window.confirm("确定删除这条记忆吗？")) return;
    setError("");
    try {
      const suffix = action === "delete" ? "" : `/${action}`;
      await api(`${base}/memories/${encodeURIComponent(memory.id)}${suffix}`, { method: action === "delete" ? "DELETE" : "POST" });
      setMemories((current) => current.filter((item) => item.id !== memory.id));
      setVersion((value) => value + 1);
    } catch (cause) { setError(errorMessage(cause, "操作记忆失败")); }
  };

  const documentContent = selectedDocument.startsWith("journal:")
    ? journals.find((journal) => `journal:${journal.date}` === selectedDocument)?.content || "暂无整理日志。"
    : documents[selectedDocument] || "暂无内容。";

  return <section className="memory-panel" style={roleStyle(role.id)} aria-label={`${role.name}的记忆`}>
    <header className="chat-header">
      <IconButton icon="back" label="返回对话" onClick={onBack} />
      <div className="chat-title"><h1>{role.name}的记忆</h1><small>长期记忆及其对话来源</small></div>
      <IconButton icon="plus" label="记住一件事" onClick={() => startEdit("new")} />
    </header>
    <div className="memory-scroll">
      <form className="memory-search" role="search" onSubmit={search}>
        <input aria-label="搜索记忆" value={queryDraft} onChange={(event) => setQueryDraft(event.target.value)} placeholder="搜索记忆…" />
        {query && <button type="button" className="ghost" onClick={() => { setQuery(""); setQueryDraft(""); }}>清除</button>}
        <IconButton icon="search" label="搜索" disabled={loading} onClick={() => setQuery(queryDraft.trim())} />
      </form>
      {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
      {editing && <form className="memory-editor" aria-label={editing === "new" ? "记住一件事" : "编辑记忆"} onSubmit={save}>
        <label>内容<textarea required value={draft.summary} onChange={(event) => setDraft({ ...draft, summary: event.target.value })} /></label>
        <label>类型<select value={draft.memoryType} onChange={(event) => setDraft({ ...draft, memoryType: event.target.value })}>
          <option value="fact">事实</option><option value="preference">偏好</option><option value="procedure">习惯与要求</option><option value="event">共同经历</option>
        </select></label>
        <div className="form-actions">
          <button type="button" className="ghost" onClick={() => setEditing(null)}>取消</button>
          <button type="submit" className="primary" disabled={saving}>{saving ? "保存中…" : "保存"}</button>
        </div>
      </form>}
      {loading ? <Placeholder loading>正在读取记忆…</Placeholder>
        : memories.length ? <ul className="memory-list" aria-label="记忆列表">
          {memories.map((memory) => <li className="memory-card" key={memory.id}>
            <div className="memory-card-heading">
              <span className="memory-type">{typeLabels[memory.memoryType] ?? memory.memoryType}</span>
              <span className="memory-status">{memory.status === "active" ? "有效" : memory.status}</span>
            </div>
            <p className="memory-summary">{memory.summary}</p>
            <dl className="memory-dates">
              <div><dt>发生时间</dt><dd>{formatDate(memory.happenedAt)}</dd></div>
              <div><dt>记录时间</dt><dd>{formatDate(memory.createdAt)}</dd></div>
            </dl>
            <div className="memory-sources">
              {(memory.sourceRef.sources?.length ? memory.sourceRef.sources : [memory.sourceRef]).map((origin) => <div className="memory-source" key={origin.stableSourceKey}>
                <span>{sourceLabels[origin.kind] ?? origin.kind}{origin.messageRange ? ` · 消息 ${origin.messageRange[0]}–${origin.messageRange[1]}` : origin.messageIds.length ? ` · ${origin.messageIds.length} 条消息` : ""}</span>
                {origin.messageIds.map((messageId) => <button type="button" className="link" key={messageId} onClick={() => onOpenSource(origin, messageId)}>定位到消息</button>)}
                {!origin.messageIds.length && origin.messageRange && <button type="button" className="link" onClick={() => onOpenSource(origin)}>定位到回合</button>}
              </div>)}
            </div>
            <div className="memory-actions">
              <button type="button" className="ghost" onClick={() => startEdit(memory)}>编辑</button>
              <button type="button" className="ghost" onClick={() => void mutate(memory, "forget")}>忘记</button>
              <button type="button" className="ghost" onClick={() => void mutate(memory, "reject")}>拒绝</button>
              <button type="button" className="ghost danger" onClick={() => void mutate(memory, "delete")}>删除</button>
            </div>
          </li>)}
        </ul>
        : <Placeholder>{query ? "没有找到匹配的记忆。" : "她还没有保存记忆。"}</Placeholder>}
      <details className="memory-documents">
        <summary>记忆文档</summary>
        <div className="memory-document-tabs">
          {documentNames.map((name) => <button type="button" key={name} aria-pressed={selectedDocument === name} onClick={() => setSelectedDocument(name)}>{name}</button>)}
          {journals.map((journal) => <button type="button" key={journal.date} aria-pressed={selectedDocument === `journal:${journal.date}`} onClick={() => setSelectedDocument(`journal:${journal.date}`)}>整理日志 {journal.date}</button>)}
        </div>
        <pre className="memory-document-content">{documentContent}</pre>
      </details>
    </div>
  </section>;
}
