import { FormEvent, useEffect, useState } from "react";

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
  roleId: string;
  roleName: string;
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

function formatDate(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "未知";
}

export function MemoryPanel({ roleId, roleName, onBack, onOpenSource }: Props) {
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [memories, setMemories] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let current = true;
    const params = new URLSearchParams();
    if (query.trim()) params.set("q", query.trim());
    setLoading(true);
    setError("");
    fetch(`/api/roles/${encodeURIComponent(roleId)}/memories${params.size ? `?${params}` : ""}`)
      .then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail ?? `读取记忆失败 (${response.status})`);
        return data as { memories: Memory[] };
      })
      .then((data) => { if (current) setMemories(data.memories); })
      .catch((cause: unknown) => {
        if (current) setError(cause instanceof Error ? cause.message : "读取记忆失败");
      })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [roleId, query]);

  const search = (event: FormEvent) => {
    event.preventDefault();
    setQuery(queryDraft.trim());
  };

  return <section className="memory-panel">
    <div className="memory-heading">
      <div><h2>{roleName}的记忆</h2><p>查看长期记忆及其对话来源。</p></div>
      <button type="button" onClick={onBack}>返回</button>
    </div>
    <form className="memory-search" onSubmit={search}>
      <label htmlFor="memory-search-input">搜索记忆</label>
      <div><input id="memory-search-input" value={queryDraft} onChange={(event) => setQueryDraft(event.target.value)} placeholder="输入关键词"/><button type="submit" disabled={loading}>搜索</button>{query && <button type="button" onClick={() => { setQuery(""); setQueryDraft(""); }}>清除</button>}</div>
    </form>
    {error && <p className="error" role="alert">{error}</p>}
    {loading ? <p className="memory-empty">正在读取记忆…</p> : memories.length ? <div className="memory-list">
      {memories.map((memory) => <article className="memory-card" key={memory.id}>
        <div className="memory-card-heading"><span className="memory-type">{typeLabels[memory.memoryType] ?? memory.memoryType}</span><span className="memory-status">{memory.status === "active" ? "有效" : memory.status}</span></div>
        <p className="memory-summary">{memory.summary}</p>
        <dl className="memory-dates"><div><dt>发生时间</dt><dd>{formatDate(memory.happenedAt)}</dd></div><div><dt>记录时间</dt><dd>{formatDate(memory.createdAt)}</dd></div></dl>
        <div className="memory-sources"><strong>来源</strong>{(memory.sourceRef.sources?.length ? memory.sourceRef.sources : [memory.sourceRef]).map((origin) => <div className="memory-source" key={origin.stableSourceKey}>
          <span>{sourceLabels[origin.kind] ?? origin.kind} · {origin.messageRange ? `消息 ${origin.messageRange[0]}–${origin.messageRange[1]}` : origin.messageIds.length ? `${origin.messageIds.length} 条消息` : origin.stableSourceKey}</span>
          {origin.messageIds.map((messageId) => <button type="button" className="source-link" key={messageId} onClick={() => onOpenSource(origin, messageId)}>定位到消息</button>)}
          {!origin.messageIds.length && origin.messageRange && <button type="button" className="source-link" onClick={() => onOpenSource(origin)}>定位到回合</button>}
        </div>)}</div>
      </article>)}
    </div> : <p className="memory-empty">{query ? "没有找到匹配的记忆。" : "这个角色还没有保存记忆。"}</p>}
  </section>;
}
