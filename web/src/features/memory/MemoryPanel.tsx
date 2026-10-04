import { useEffect, useMemo, useState, type FormEvent } from "react";
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
  extra: Record<string, unknown>;
  status: string;
  happenedAt: string | null;
  createdAt: string;
  updatedAt: string;
  reinforcement?: number;
  emotionalWeight?: number;
  hasEmbedding?: boolean;
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
const statusLabels: Record<string, string> = { active: "有效", rejected: "已拒绝", forgotten: "已忘记", superseded: "已替代" };
type EditorDraft = { summary: string; memoryType: string; status: string; extraJson: string; sourceRefJson: string; happenedAt: string; emotionalWeight: string };
const emptyDraft: EditorDraft = { summary: "", memoryType: "fact", status: "active", extraJson: "{}", sourceRefJson: "", happenedAt: "", emotionalWeight: "0" };

const formatDate = (value: string | null) => value ? new Date(value).toLocaleString() : "未知";

/** A role's long-term memories: search, remember, edit, forget, and jump back to where she learned them. */
export function MemoryPanel({ role, onBack, onOpenSource }: Props) {
  const base = `/api/roles/${encodeURIComponent(role.id)}`;
  const [queryDraft, setQueryDraft] = useState("");
  const [filters, setFilters] = useState({ query: "", status: "active", memoryType: "", domain: "", embedding: "" });
  const [memories, setMemories] = useState<Memory[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const pageSize = 20;
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
  const [selected, setSelected] = useState<string[]>([]);
  const [similar, setSimilar] = useState<{ source: Memory; items: Memory[] } | null>(null);

  const listQuery = useMemo(() => {
    const params = new URLSearchParams();
    if (filters.query) params.set("q", filters.query);
    if (filters.status !== "active") params.set("status", filters.status);
    if (filters.memoryType) params.set("memoryType", filters.memoryType);
    if (filters.domain) params.set("memoryDomain", filters.domain);
    if (filters.embedding) params.set("hasEmbedding", filters.embedding);
    if (page > 1) params.set("page", String(page));
    // Keep the no-filter request path unchanged for older clients and mocks.
    if (page > 1 || params.size) params.set("pageSize", String(pageSize));
    const query = params.toString();
    return query ? `?${query}` : "";
  }, [filters, page]);

  useEffect(() => {
    let current = true;
    setLoading(true);
    api<{ memories: Memory[]; total?: number }>(`${base}/memories${listQuery}`)
      .then((data) => { if (current) { setMemories(data.memories || []); setTotal(data.total ?? data.memories?.length ?? 0); setSelected([]); } })
      .catch((cause) => { if (current) setError(errorMessage(cause, "读取记忆失败")); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [base, listQuery, version]);

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

  const search = (event: FormEvent) => { event.preventDefault(); setPage(1); setFilters((current) => ({ ...current, query: queryDraft.trim() })); };

  const startEdit = (target: Memory | "new") => {
    setError("");
    setEditing(target);
    setDraft(target === "new" ? { ...emptyDraft } : {
      summary: target.summary, memoryType: target.memoryType, status: target.status,
      extraJson: JSON.stringify(target.extra || {}, null, 2), sourceRefJson: JSON.stringify(target.sourceRef, null, 2),
      happenedAt: target.happenedAt ? target.happenedAt.slice(0, 16) : "", emotionalWeight: String(target.emotionalWeight ?? 0),
    });
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!editing) return;
    setSaving(true);
    setError("");
    try {
      if (editing === "new") {
        await api(`${base}/memories`, { method: "POST", body: JSON.stringify({ summary: draft.summary, memoryType: draft.memoryType, happenedAt: draft.happenedAt || null }) });
      } else {
        let extra: Record<string, unknown>;
        let sourceRef: unknown;
        try { extra = JSON.parse(draft.extraJson || "{}"); } catch { throw new Error("扩展字段必须是有效 JSON"); }
        try { sourceRef = draft.sourceRefJson ? JSON.parse(draft.sourceRefJson) : undefined; } catch { throw new Error("来源必须是有效 JSON"); }
        const body: Record<string, unknown> = {
          status: draft.status, extraJson: extra,
          happenedAt: draft.happenedAt ? new Date(draft.happenedAt).toISOString() : null,
          emotionalWeight: Number(draft.emotionalWeight || 0),
        };
        if (sourceRef !== undefined) body.sourceRef = sourceRef;
        await api(`${base}/memory-admin/items/${encodeURIComponent(editing.id)}`, { method: "PATCH", body: JSON.stringify(body) });
      }
      setEditing(null);
      setDraft(emptyDraft);
      setVersion((value) => value + 1);
    } catch (cause) { setError(errorMessage(cause, "保存记忆失败")); } finally { setSaving(false); }
  };

  const mutate = async (memory: Memory, action: "forget" | "delete" | "reject") => {
    if (action === "delete" && !window.confirm("确定删除这条记忆吗？")) return;
    setError("");
    try {
      if (action === "delete") {
        await api(`${base}/memories/${encodeURIComponent(memory.id)}`, { method: "DELETE" });
      } else {
        await api(`${base}/memory-admin/items/${encodeURIComponent(memory.id)}`, { method: "PATCH", body: JSON.stringify({ status: action === "forget" ? "forgotten" : "rejected" }) });
      }
      setVersion((value) => value + 1);
    } catch (cause) { setError(errorMessage(cause, "操作记忆失败")); }
  };

  const batchDelete = async () => {
    if (!selected.length || !window.confirm(`确定删除选中的 ${selected.length} 条记忆吗？`)) return;
    try {
      await api(`${base}/memory-admin/items/batch-delete`, { method: "POST", body: JSON.stringify({ ids: selected }) });
      setSelected([]); setVersion((value) => value + 1);
    } catch (cause) { setError(errorMessage(cause, "批量删除失败")); }
  };

  const invalidateAll = async () => {
    if (!window.confirm("确定将该角色的全部记忆标记为失效吗？")) return;
    try {
      await api(`${base}/memory-admin/invalidate`, { method: "POST" });
      setVersion((value) => value + 1);
    } catch (cause) { setError(errorMessage(cause, "记忆失效操作失败")); }
  };

  const showSimilar = async (memory: Memory) => {
    try {
      const data = await api<{ items: Memory[] }>(`${base}/memory-admin/items/${encodeURIComponent(memory.id)}/similar`);
      setSimilar({ source: memory, items: data.items || [] });
    } catch (cause) { setError(errorMessage(cause, "读取相似记忆失败")); }
  };

  const documentContent = selectedDocument.startsWith("journal:")
    ? journals.find((journal) => `journal:${journal.date}` === selectedDocument)?.content || "暂无整理日志。"
    : documents[selectedDocument] || "暂无内容。";
  const allSelected = memories.length > 0 && memories.every((memory) => selected.includes(memory.id));
  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  return <section className="memory-panel" style={roleStyle(role.id)} aria-label={`${role.name}的记忆`}>
    <header className="chat-header">
      <IconButton icon="back" label="返回对话" onClick={onBack} />
      <div className="chat-title"><h1>{role.name}的记忆</h1><small>长期记忆及其对话来源</small></div>
      <IconButton icon="plus" label="记住一件事" onClick={() => startEdit("new")} />
    </header>
    <div className="memory-scroll">
      <form className="memory-search" role="search" onSubmit={search}>
        <input aria-label="搜索记忆" value={queryDraft} onChange={(event) => setQueryDraft(event.target.value)} placeholder="搜索记忆…" />
        {filters.query && <button type="button" className="ghost" onClick={() => { setFilters((current) => ({ ...current, query: "" })); setQueryDraft(""); setPage(1); }}>清除</button>}
        <IconButton icon="search" label="搜索" disabled={loading} onClick={() => { setPage(1); setFilters((current) => ({ ...current, query: queryDraft.trim() })); }} />
      </form>
      <div className="memory-filters" aria-label="记忆筛选">
        <select aria-label="状态筛选" value={filters.status} onChange={(event) => { setPage(1); setFilters((current) => ({ ...current, status: event.target.value })); }}><option value="active">有效</option><option value="">全部状态</option><option value="rejected">已拒绝</option><option value="forgotten">已忘记</option><option value="superseded">已替代</option></select>
        <select aria-label="类型筛选" value={filters.memoryType} onChange={(event) => { setPage(1); setFilters((current) => ({ ...current, memoryType: event.target.value })); }}><option value="">全部类型</option>{Object.entries(typeLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select>
        <input aria-label="领域筛选" placeholder="领域" value={filters.domain} onChange={(event) => setFilters((current) => ({ ...current, domain: event.target.value }))} onBlur={() => setPage(1)} />
        <select aria-label="向量筛选" value={filters.embedding} onChange={(event) => { setPage(1); setFilters((current) => ({ ...current, embedding: event.target.value })); }}><option value="">全部向量</option><option value="true">有向量</option><option value="false">无向量</option></select>
        <button type="button" className="ghost" onClick={invalidateAll}>全部失效</button>
        {selected.length > 0 && <button type="button" className="ghost danger" onClick={() => void batchDelete()}>删除选中 ({selected.length})</button>}
      </div>
      {error && <Notice onDismiss={() => setError("")}>{error}</Notice>}
      {editing && <form className="memory-editor" aria-label={editing === "new" ? "记住一件事" : "编辑记忆"} onSubmit={save}>
        <label>内容<textarea required disabled={editing !== "new"} value={draft.summary} onChange={(event) => setDraft({ ...draft, summary: event.target.value })} /></label>
        <label>类型<select disabled={editing !== "new"} value={draft.memoryType} onChange={(event) => setDraft({ ...draft, memoryType: event.target.value })}>
          <option value="fact">事实</option><option value="preference">偏好</option><option value="procedure">习惯与要求</option><option value="event">共同经历</option>
        </select></label>
        {editing !== "new" && <>
          <label>状态<select value={draft.status} onChange={(event) => setDraft({ ...draft, status: event.target.value })}>{Object.entries(statusLabels).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
          <label>发生时间<input type="datetime-local" value={draft.happenedAt} onChange={(event) => setDraft({ ...draft, happenedAt: event.target.value })} /></label>
          <label>情绪权重<input type="number" min="0" max="10" value={draft.emotionalWeight} onChange={(event) => setDraft({ ...draft, emotionalWeight: event.target.value })} /></label>
          <label>扩展字段（JSON）<textarea value={draft.extraJson} onChange={(event) => setDraft({ ...draft, extraJson: event.target.value })} /></label>
          <label>来源（JSON）<textarea value={draft.sourceRefJson} onChange={(event) => setDraft({ ...draft, sourceRefJson: event.target.value })} /></label>
        </>}
        <div className="form-actions">
          <button type="button" className="ghost" onClick={() => setEditing(null)}>取消</button>
          <button type="submit" className="primary" disabled={saving}>{saving ? "保存中…" : "保存"}</button>
        </div>
      </form>}
      {similar && <div className="memory-similar" role="dialog" aria-label="相似记忆"><div className="memory-similar-heading"><strong>与“{similar.source.summary}”相似的记忆</strong><button type="button" className="ghost" onClick={() => setSimilar(null)}>关闭</button></div>{similar.items.length ? <ul>{similar.items.map((item) => <li key={item.id}>{item.summary} <span className="memory-status">{statusLabels[item.status] ?? item.status}</span></li>)}</ul> : <p>没有找到相似记忆。</p>}</div>}
      {loading ? <Placeholder loading>正在读取记忆…</Placeholder>
        : memories.length ? <>
          <div className="memory-select-all"><label><input type="checkbox" checked={allSelected} onChange={(event) => setSelected(event.target.checked ? memories.map((memory) => memory.id) : [])} /> 全选本页</label><span>共 {total} 条</span></div>
          <ul className="memory-list" aria-label="记忆列表">
          {memories.map((memory) => <li className="memory-card" key={memory.id}>
            <div className="memory-card-heading">
              <input aria-label={`选择记忆 ${memory.summary}`} type="checkbox" checked={selected.includes(memory.id)} onChange={(event) => setSelected((current) => event.target.checked ? [...current, memory.id] : current.filter((id) => id !== memory.id))} />
              <span className="memory-type">{typeLabels[memory.memoryType] ?? memory.memoryType}</span>
              <span className="memory-status">{statusLabels[memory.status] ?? memory.status}</span>
              {memory.hasEmbedding !== undefined && <span className="memory-status">{memory.hasEmbedding ? "有向量" : "无向量"}</span>}
            </div>
            <p className="memory-summary">{memory.summary}</p>
            <dl className="memory-dates">
              <div><dt>发生时间</dt><dd>{formatDate(memory.happenedAt)}</dd></div>
              <div><dt>记录时间</dt><dd>{formatDate(memory.createdAt)}</dd></div>
              <div><dt>情绪权重</dt><dd>{memory.emotionalWeight ?? 0}</dd></div>
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
              <button type="button" className="ghost" onClick={() => void showSimilar(memory)}>相似记忆</button>
              {memory.status === "active" && <><button type="button" className="ghost" onClick={() => void mutate(memory, "forget")}>忘记</button><button type="button" className="ghost" onClick={() => void mutate(memory, "reject")}>拒绝</button></>}
              <button type="button" className="ghost danger" onClick={() => void mutate(memory, "delete")}>删除</button>
            </div>
          </li>)}
          </ul>
          {pageCount > 1 && <div className="memory-pagination"><button type="button" className="ghost" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>上一页</button><span>第 {page} / {pageCount} 页</span><button type="button" className="ghost" disabled={page >= pageCount} onClick={() => setPage((value) => value + 1)}>下一页</button></div>}
        </>
        : <Placeholder>{filters.query ? "没有找到匹配的记忆。" : "她还没有保存记忆。"}</Placeholder>}
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
