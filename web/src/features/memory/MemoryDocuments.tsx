import { MessageMarkdown } from '../../ui/MessageMarkdown';
import { remarkMemoryDocument } from './memoryDocumentPresentation';

export const documentOptions = [
  { id: 'MEMORY.md', label: '长期记忆', description: '整理后留下的重要信息。' },
  { id: 'SELF.md', label: '自我认识', description: '她对自己与这段关系的认识。' },
  { id: 'HISTORY.md', label: '共同经历', description: '一起经历的事情与回顾。' },
  { id: 'RECENT_CONTEXT.md', label: '近期上下文', description: '最近对话保留的上下文。' },
  { id: 'PENDING.md', label: '待整理候选', description: '等待归并为长期记忆的信息。' },
];

function journalLabel(date: string) {
  const valid = /^\d{4}-\d{2}-\d{2}$/.test(date) && !Number.isNaN(Date.parse(date)) && new Date(date).toISOString().slice(0, 10) === date;
  return valid ? `整理日志 ${date}` : '整理日志（日期未记录）';
}

export function MemoryDocuments({ documents, journals, selected, onSelect, compact = false }: {
  documents: Record<string, string>; journals: { date: string; content: string }[]; selected: string; onSelect: (id: string) => void; compact?: boolean;
}) {
  const journalOptions = journals.map(journal => ({ id: `journal:${journal.date}`, label: journalLabel(journal.date), description: '本次记忆整理留下的记录。' }));
  const options = [...documentOptions, ...journalOptions];
  const current = options.find(option => option.id === selected) ?? documentOptions[0];
  const content = current.id.startsWith('journal:') ? journals.find(journal => `journal:${journal.date}` === current.id)?.content : documents[current.id];
  const presentationPlugins: [typeof remarkMemoryDocument, { recent: boolean }][] = [[remarkMemoryDocument, { recent: current.id === 'RECENT_CONTEXT.md' }]];
  return <div className={`memory-document-layout${compact ? ' is-compact' : ''}`}>
    {!compact && <nav className="memory-document-nav" aria-label="文档与整理日志">
      <span className="memory-eyebrow">记忆文档</span>
      {documentOptions.map(option => <button type="button" key={option.id} aria-current={current.id === option.id ? 'page' : undefined} onClick={() => onSelect(option.id)}><span>{option.label}</span><small>{option.description}</small></button>)}
      {journalOptions.length > 0 && <><span className="memory-eyebrow">整理日志</span>{journalOptions.map(option => <button type="button" key={option.id} aria-current={current.id === option.id ? 'page' : undefined} onClick={() => onSelect(option.id)}><span>{option.label}</span></button>)}</>}
    </nav>}
    {compact && journals.length > 0 && <label className="memory-document-select">文档与整理日志<select aria-label="选择记忆文档" value={current.id} onChange={event => onSelect(event.target.value)}>{options.map(option => <option key={option.id} value={option.id}>{option.label}</option>)}</select></label>}
    <article className="memory-document-paper" aria-label={current.label}><header><h2>{current.label}</h2><p>{current.description}</p></header>
      {content?.trim() ? <div className="memory-document-text"><MessageMarkdown content={content} presentationPlugins={presentationPlugins} /></div> : <div className="memory-document-empty"><p>这里还没有内容</p><span>对应文档有记录后，会显示在这里。</span></div>}
      <footer className="memory-document-provenance">这份文档未提供可直接定位的原文入口。</footer>
    </article>
  </div>;
}
