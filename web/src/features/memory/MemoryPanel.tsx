import { useEffect, useId, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { api, errorMessage } from '../../api/client';
import type { Role } from '../../api/types';
import { Avatar, roleStyle } from '../../ui/Avatar';
import { Icon, IconButton } from '../../ui/Icon';
import { Dialog } from '../../ui/Dialog';
import { Notice, Placeholder } from '../../ui/Status';
import { type Memory, type MemoryOrigin, typeLabels, statusLabels, formatDate } from './types';
import { MemoryInspector } from './MemoryInspector';
import { MemoryEditor, emptyDraft, type EditorDraft } from './MemoryEditor';
import { MemoryDocuments } from './MemoryDocuments';
export type { MemoryOrigin } from './types';

type Confirmation = { kind: 'delete' | 'batch' | 'invalidate' | 'forget' | 'reject'; memory?: Memory; ids?: string[] };
const initialFilters = { query: '', status: 'active', memoryType: '', domain: '', embedding: '' };
const confirmationLabels = { delete: '删除这条记忆', batch: '删除选中的记忆', invalidate: '使全部记忆失效', forget: '忘记这条记忆', reject: '拒绝这条记忆' };

export function MemoryPanel({ role, onBack, onOpenSource }: {
  role: Role; onBack: () => void; onOpenSource: (origin: MemoryOrigin, messageId?: string) => void;
}) {
  const base = `/api/roles/${encodeURIComponent(role.id)}`;
  const viewId = useId();
  const [view, setView] = useState<'entries' | 'documents'>('entries');
  const [queryDraft, setQueryDraft] = useState('');
  const [filters, setFilters] = useState(initialFilters);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [memories, setMemories] = useState<Memory[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState('');
  const [error, setError] = useState('');
  const [detail, setDetail] = useState<Memory | null>(null);
  const [editing, setEditing] = useState<Memory | 'new' | null>(null);
  const [draft, setDraft] = useState<EditorDraft>(emptyDraft);
  const [saving, setSaving] = useState(false);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [mutating, setMutating] = useState(false);
  const [managing, setManaging] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [documents, setDocuments] = useState<Record<string, string>>({});
  const [journals, setJournals] = useState<{ date: string; content: string }[]>([]);
  const [selectedDocument, setSelectedDocument] = useState('MEMORY.md');
  const [documentsLoading, setDocumentsLoading] = useState(false);
  const [documentsError, setDocumentsError] = useState('');
  const [version, setVersion] = useState(0);
  const [similar, setSimilar] = useState<{ source: Memory; items: Memory[]; loading: boolean } | null>(null);
  const similarRequest = useRef(0);
  const opener = useRef<HTMLElement | null>(null);
  const title = useRef<HTMLHeadingElement>(null);
  const rememberOpener = () => { opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; };
  const returnFocus = () => opener.current?.isConnected && !opener.current.matches(':disabled') ? opener.current : title.current;
  useEffect(() => () => { ++similarRequest.current; }, []);
  const pageSize = 20;
  const listQuery = useMemo(() => {
    const params = new URLSearchParams({ pageSize: String(pageSize) });
    if (filters.query) params.set('q', filters.query);
    if (filters.status !== 'active') params.set('status', filters.status);
    if (filters.memoryType) params.set('memoryType', filters.memoryType);
    if (filters.domain) params.set('memoryDomain', filters.domain);
    if (filters.embedding) params.set('hasEmbedding', filters.embedding);
    if (page > 1) params.set('page', String(page));
    return params.size ? `?${params}` : '';
  }, [filters, page]);
  useEffect(() => {
    let current = true;
    setLoading(true); setListError(''); setSelected([]);
    void api<{ memories: Memory[]; total?: number }>(`${base}/memories${listQuery}`)
      .then(data => { if (current) {
        const count = data.total ?? data.memories?.length ?? 0;
        setMemories(data.memories || []); setTotal(count); setSelected([]);
        setPage(currentPage => Math.min(currentPage, Math.max(1, Math.ceil(count / pageSize))));
      } })
      .catch(cause => { if (current) setListError(errorMessage(cause, '读取记忆失败')); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [base, listQuery, version]);
  useEffect(() => {
    if (view !== 'documents') return;
    let current = true;
    setDocumentsLoading(true); setDocumentsError('');
    void api<{ documents: { name: string; content: string }[]; journals: { date: string; content: string }[] }>(`${base}/memory-documents`)
      .then(data => { if (current) { setDocuments(Object.fromEntries((data.documents || []).map(item => [item.name, item.content]))); setJournals(data.journals || []); } })
      .catch(cause => { if (current) setDocumentsError(errorMessage(cause, '读取记忆文档失败')); })
      .finally(() => { if (current) setDocumentsLoading(false); });
    return () => { current = false; };
  }, [base, view, version]);
  const search = (event: FormEvent) => { event.preventDefault(); setPage(1); setFilters(current => ({ ...current, query: queryDraft.trim() })); };
  const resetFilters = () => { setQueryDraft(''); setFilters(initialFilters); setPage(1); };
  const startEdit = (target: Memory | 'new') => {
    if (!detail) rememberOpener();
    setError(''); setEditing(target);
    setDraft(target === 'new' ? { ...emptyDraft } : { summary: target.summary, memoryType: target.memoryType, status: target.status,
      extraJson: JSON.stringify(target.extra || {}, null, 2), sourceRefJson: JSON.stringify(target.sourceRef, null, 2),
      happenedAt: target.happenedAt ? localDateTime(target.happenedAt) : '', emotionalWeight: String(target.emotionalWeight ?? 0) });
  };
  const save = async (event: FormEvent) => {
    event.preventDefault(); if (!editing || saving) return;
    setSaving(true); setError('');
    try {
      if (editing === 'new') await api(`${base}/memories`, { method: 'POST', body: JSON.stringify({ summary: draft.summary, memoryType: draft.memoryType, happenedAt: draft.happenedAt ? new Date(draft.happenedAt).toISOString() : null }) });
      else {
        let extra: unknown, sourceRef: unknown;
        try { extra = JSON.parse(draft.extraJson || '{}'); } catch { throw new Error('扩展字段必须是有效 JSON'); }
        try { sourceRef = draft.sourceRefJson ? JSON.parse(draft.sourceRefJson) : undefined; } catch { throw new Error('来源必须是有效 JSON'); }
        await api(`${base}/memory-admin/items/${encodeURIComponent(editing.id)}`, { method: 'PATCH', body: JSON.stringify({ status: draft.status, extraJson: extra,
          happenedAt: draft.happenedAt === (editing.happenedAt ? localDateTime(editing.happenedAt) : '') ? editing.happenedAt
            : draft.happenedAt ? new Date(draft.happenedAt).toISOString() : null, emotionalWeight: Number(draft.emotionalWeight || 0),
          ...(sourceRef !== undefined ? { sourceRef } : {}) }) });
      }
      setEditing(null); setDetail(null); setDraft(emptyDraft); setVersion(value => value + 1);
    } catch (cause) { setError(errorMessage(cause, '保存记忆失败')); } finally { setSaving(false); }
  };
  const confirmMutation = async () => {
    if (!confirmation || mutating) return;
    setMutating(true); setError('');
    try {
      const { kind, memory, ids } = confirmation;
      if (kind === 'invalidate') await api(`${base}/memory-admin/invalidate`, { method: 'POST' });
      else if (kind === 'batch') await api(`${base}/memory-admin/items/batch-delete`, { method: 'POST', body: JSON.stringify({ ids }) });
      else if (kind === 'delete') await api(`${base}/memories/${encodeURIComponent(memory!.id)}`, { method: 'DELETE' });
      else await api(`${base}/memory-admin/items/${encodeURIComponent(memory!.id)}`, { method: 'PATCH', body: JSON.stringify({ status: kind === 'forget' ? 'forgotten' : 'rejected' }) });
      setConfirmation(null); setDetail(null); setSelected([]); setVersion(value => value + 1);
    } catch (cause) { setError(errorMessage(cause, '操作记忆失败')); } finally { setMutating(false); }
  };
  const showSimilar = async (memory: Memory) => {
    const request = ++similarRequest.current;
    setError(''); setSimilar({ source: memory, items: [], loading: true });
    try {
      const data = await api<{ items: Memory[] }>(`${base}/memory-admin/items/${encodeURIComponent(memory.id)}/similar`);
      if (request === similarRequest.current) setSimilar({ source: memory, items: data.items || [], loading: false });
    } catch (cause) { if (request === similarRequest.current) { setSimilar(null); setError(errorMessage(cause, '读取相似记忆失败')); } }
  };
  const changeTab = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault(); const next = event.key === 'Home' ? 'entries' : event.key === 'End' ? 'documents' : view === 'entries' ? 'documents' : 'entries';
    setView(next); document.getElementById(`${viewId}-${next}-tab`)?.focus();
  };
  const allSelected = memories.length > 0 && memories.every(memory => selected.includes(memory.id));
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const filterCount = Number(filters.status !== 'active') + Number(Boolean(filters.domain)) + Number(Boolean(filters.embedding));
  const filtered = Boolean(filters.query || filters.memoryType || filterCount);
  const overlay = Boolean(editing || similar || confirmation || detail);
  const errorNotice = error && <Notice onDismiss={() => setError('')}>{error}</Notice>;
  return <section className="memory-panel" style={roleStyle(role.id)} aria-label={`${role.name}的记忆`}>
    <header className="memory-topbar"><button type="button" className="ghost memory-back" onClick={onBack}><Icon name="back" size={17} />返回对话</button>
      <div className="memory-role"><Avatar id={role.id} name={role.name} avatarUrl={role.avatarUrl} size="sm" /><span>{role.name}</span></div>
      <button type="button" className="primary memory-add" onClick={() => startEdit('new')}><Icon name="plus" size={17} />记住一件事</button></header>
    <div className="memory-scroll"><div className="memory-workspace">
      <div className="memory-overview"><span className="memory-overview-icon"><Icon name="memory" size={27} /></span><div><h1 ref={title} tabIndex={-1}>{role.name}的记忆</h1><p>偏好、习惯，以及一起经历的事。</p></div></div>
      <div className="memory-view-tabs" role="tablist" aria-label="记忆视图">{([['entries', '记忆条目'], ['documents', '记忆文档']] as const).map(([id, label]) =>
        <button type="button" role="tab" key={id} id={`${viewId}-${id}-tab`} aria-selected={view === id} aria-controls={`${viewId}-${id}`} tabIndex={view === id ? 0 : -1} onKeyDown={changeTab} onClick={() => setView(id)}>{label}</button>)}</div>
      {!overlay && errorNotice}
      {view === 'entries' ? <div role="tabpanel" id={`${viewId}-entries`} aria-labelledby={`${viewId}-entries-tab`} className="memory-entries">
        <div className="memory-toolbar"><form className="memory-search" role="search" onSubmit={search}><Icon name="search" size={18} /><input aria-label="搜索记忆" value={queryDraft} onChange={event => setQueryDraft(event.target.value)} placeholder="搜索她记住的事…" />
          {(queryDraft || filters.query) && <IconButton icon="close" label="清除搜索" onClick={() => { setQueryDraft(''); setPage(1); setFilters(current => ({ ...current, query: '' })); }} />}
          <button type="submit" className="ghost" disabled={loading}>搜索</button></form>
          <button type="button" className={`ghost memory-filter-trigger${filtersOpen || filterCount ? ' is-active' : ''}`} aria-expanded={filtersOpen} aria-controls={`${viewId}-filters`} onClick={() => setFiltersOpen(value => !value)}><Icon name="sliders" size={17} />筛选{filterCount > 0 && <span>{filterCount}</span>}</button>
        </div>
        {filtersOpen && <div className="memory-filter-panel" id={`${viewId}-filters`} aria-label="记忆筛选">
          <label className="field"><span>状态</span><select aria-label="状态筛选" value={filters.status} onChange={event => { setPage(1); setFilters(current => ({ ...current, status: event.target.value })); }}><option value="active">有效记忆</option><option value="">全部状态</option>{Object.entries(statusLabels).filter(([id]) => id !== 'active').map(([id, label]) => <option value={id} key={id}>{label}</option>)}</select></label>
          <label className="field"><span>领域</span><input aria-label="领域筛选" placeholder="按领域筛选" value={filters.domain} onChange={event => { setPage(1); setFilters(current => ({ ...current, domain: event.target.value })); }} /></label>
          <label className="field"><span>语义索引</span><select aria-label="向量筛选" value={filters.embedding} onChange={event => { setPage(1); setFilters(current => ({ ...current, embedding: event.target.value })); }}><option value="">全部</option><option value="true">已建立</option><option value="false">未建立</option></select></label>
          <button type="button" className="ghost" onClick={resetFilters}>重置筛选</button>
        </div>}
        <div className="memory-type-tabs" aria-label="记忆类型">{[['', '全部类型'], ...Object.entries(typeLabels)].map(([id, label]) => <button type="button" key={id} aria-pressed={filters.memoryType === id} onClick={() => { setPage(1); setFilters(current => ({ ...current, memoryType: id })); }}>{label}</button>)}</div>
        <div className="memory-list-heading"><span>{loading ? '正在读取…' : listError ? '读取失败' : `共 ${total} 条记忆`}{!filtered && !listError && <small> · 有效记录</small>}</span><button type="button" className="ghost" aria-pressed={managing} onClick={() => { setManaging(value => !value); setSelected([]); }}>{managing ? '退出管理' : '批量管理'}</button></div>
        {managing && <div className="memory-management" role="toolbar" aria-label="批量管理"><label><input type="checkbox" checked={allSelected} disabled={loading || Boolean(listError)} onChange={event => setSelected(event.target.checked ? memories.map(memory => memory.id) : [])} />全选本页</label><span>已选 {selected.length} 条</span>
          <button type="button" className="ghost danger" disabled={!selected.length || loading || Boolean(listError)} onClick={() => { rememberOpener(); setError(''); setConfirmation({ kind: 'batch', ids: [...selected] }); }}>删除选中</button><button type="button" className="ghost" onClick={() => { rememberOpener(); setError(''); setConfirmation({ kind: 'invalidate' }); }}>全部失效</button></div>}
        {loading ? <Placeholder loading>正在读取记忆…</Placeholder> : listError ? <div className="memory-empty"><Icon name="refresh" size={28} /><h2>暂时无法读取记忆</h2><p>{listError}</p><button type="button" className="ghost" onClick={() => setVersion(value => value + 1)}>重新加载</button></div>
          : memories.length ? <ul className="memory-list" aria-label="记忆列表">{memories.map(memory => <li className={`memory-card${selected.includes(memory.id) ? ' is-selected' : ''}`} key={memory.id}>
            {managing && <input className="memory-card-check" aria-label={`选择记忆 ${memory.summary}`} type="checkbox" checked={selected.includes(memory.id)} onChange={event => setSelected(current => event.target.checked ? [...current, memory.id] : current.filter(id => id !== memory.id))} />}
            <button type="button" className="memory-card-open" aria-label={`查看记忆：${memory.summary}`} onClick={() => { rememberOpener(); setError(''); setDetail(memory); }}><span className="memory-card-tags"><span className="memory-type">{typeLabels[memory.memoryType] ?? memory.memoryType}</span>{memory.status !== 'active' && <span className="memory-status">{statusLabels[memory.status] ?? memory.status}</span>}</span>
              <span className="memory-card-text">{memory.summary}</span><span className="memory-card-footer"><time dateTime={memory.createdAt}>记录于 {formatDate(memory.createdAt, true)}</time><span>查看详情 <span aria-hidden="true">↗</span></span></span></button>
          </li>)}</ul> : <div className="memory-empty"><span className="memory-empty-icon"><Icon name={filtered ? 'search' : 'memory'} size={30} /></span><h2>{filtered ? '没有找到匹配的记忆' : '故事从这里开始'}</h2><p>{filtered ? '换个关键词，或调整筛选条件试试。' : '她记住的偏好与共同经历，会慢慢出现在这里。'}</p><button type="button" className="ghost" onClick={filtered ? resetFilters : () => startEdit('new')}>{filtered ? '清除筛选' : '记住第一件事'}</button></div>}
        {!loading && !listError && pageCount > 1 && <nav className="memory-pagination" aria-label="记忆分页"><button type="button" className="ghost" disabled={page <= 1} onClick={() => setPage(value => value - 1)}>上一页</button><span>第 {page} / {pageCount} 页</span><button type="button" className="ghost" disabled={page >= pageCount} onClick={() => setPage(value => value + 1)}>下一页</button></nav>}
      </div> : <div role="tabpanel" id={`${viewId}-documents`} aria-labelledby={`${viewId}-documents-tab`}>
        {documentsLoading ? <Placeholder loading>正在读取记忆文档…</Placeholder> : documentsError ? <div className="memory-empty"><h2>暂时无法读取文档</h2><p>{documentsError}</p><button type="button" className="ghost" onClick={() => setVersion(value => value + 1)}>重新加载</button></div>
          : <MemoryDocuments documents={documents} journals={journals} selected={selectedDocument} onSelect={setSelectedDocument} />}
      </div>}
    </div></div>
    {detail && !editing && !similar && !confirmation && <Dialog modalFocus returnFocus={returnFocus} title="记忆详情" variant="drawer" onClose={() => { setDetail(null); setError(''); }}>{errorNotice}<MemoryInspector memory={detail} onEdit={() => startEdit(detail)} onSimilar={() => void showSimilar(detail)} onOpenSource={onOpenSource} onAction={kind => { setError(''); setConfirmation({ kind, memory: detail }); }} /></Dialog>}
    {editing && <Dialog modalFocus returnFocus={returnFocus} title={editing === 'new' ? '记住一件事' : '编辑记忆'} variant="drawer" onClose={() => { if (!saving) { setEditing(null); setError(''); } }}>{errorNotice}<MemoryEditor target={editing} draft={draft} onChange={setDraft} saving={saving} onSubmit={event => void save(event)} onCancel={() => { setEditing(null); setError(''); }} /></Dialog>}
    {similar && <Dialog modalFocus returnFocus={returnFocus} title="相似记忆" variant="drawer" onClose={() => { ++similarRequest.current; setSimilar(null); }}><div className="memory-similar-intro"><span className="memory-eyebrow">正在比较的记忆</span><p>{similar.source.summary}</p></div>
      {similar.loading ? <Placeholder loading>正在查找相似记忆…</Placeholder> : similar.items.length ? <ul className="memory-similar-list">{similar.items.map(item => <li key={item.id}><span className="memory-type">{typeLabels[item.memoryType] ?? item.memoryType}</span><p>{item.summary}</p><span className="memory-status">{statusLabels[item.status] ?? item.status}</span></li>)}</ul> : <Placeholder>没有找到相似记忆。</Placeholder>}</Dialog>}
    {confirmation && <Dialog modalFocus returnFocus={returnFocus} title={confirmationLabels[confirmation.kind]} variant="compact" onClose={() => { if (!mutating) { setConfirmation(null); setError(''); } }}><div className="memory-confirm">{errorNotice}
      <p>{confirmation.kind === 'batch' ? `将删除选中的 ${confirmation.ids?.length} 条记忆，无法撤销。` : confirmation.kind === 'invalidate' ? `这会将${role.name}的全部记忆标记为失效。` : confirmation.kind === 'delete' ? '删除后无法撤销。' : '这会改变记录状态，它将不再出现在有效记忆中。'}</p>
      {confirmation.memory && <blockquote>{confirmation.memory.summary}</blockquote>}<div className="form-actions"><button type="button" className="ghost" disabled={mutating} onClick={() => { setConfirmation(null); setError(''); }}>取消</button><button type="button" className="primary danger" disabled={mutating} onClick={() => void confirmMutation()}>{mutating ? '处理中…' : '确认'}</button></div></div></Dialog>}
  </section>;
}

function localDateTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}T${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}
