export const documentOptions = [
  { id: 'MEMORY.md', label: '长期记忆', description: '整理后留下的重要信息。' },
  { id: 'SELF.md', label: '自我认识', description: '她对自己与这段关系的认识。' },
  { id: 'HISTORY.md', label: '共同经历', description: '一起经历的事情与回顾。' },
  { id: 'RECENT_CONTEXT.md', label: '近期上下文', description: '最近对话保留的上下文。' },
  { id: 'PENDING.md', label: '待整理内容', description: '等待下一次整理的内容。' },
];

export function MemoryDocuments({ documents, journals, selected, onSelect }: {
  documents: Record<string, string>; journals: { date: string; content: string }[]; selected: string; onSelect: (id: string) => void;
}) {
  const options = [...documentOptions, ...journals.map(journal => ({ id: `journal:${journal.date}`, label: `整理日志 ${journal.date}`, description: '本次记忆整理留下的记录。' }))];
  const current = options.find(option => option.id === selected) ?? documentOptions[0];
  const content = current.id.startsWith('journal:') ? journals.find(journal => `journal:${journal.date}` === current.id)?.content : documents[current.id];
  return <div className="memory-document-layout">
    <label className="memory-document-select">文档<select aria-label="选择记忆文档" value={current.id} onChange={event => onSelect(event.target.value)}>{options.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}</select></label>
    <article className="memory-document-paper" aria-label={current.label}><header><h2>{current.label}</h2><p>{current.description}</p></header>
      {content?.trim() ? <pre className="memory-document-text">{content}</pre> : <div className="memory-document-empty"><p>这里还没有内容</p><span>对应文档有记录后，会显示在这里。</span></div>}
    </article>
  </div>;
}
