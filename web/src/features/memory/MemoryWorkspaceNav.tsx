import { Icon, IconButton } from '../../ui/Icon';

export const memoryWorkspaceTabs = [
  ['entries', '时间线'], ['SELF.md', '自我'], ['MEMORY.md', '长期'],
  ['HISTORY.md', '经历'], ['RECENT_CONTEXT.md', '近期'], ['PENDING.md', '待整理'],
] as const;

export function MemoryWorkspaceNav({ id, selected, onSelect, onRefresh, refreshing, onAdd }: {
  id: string; selected: string; onSelect: (id: string) => void; onRefresh: () => void; refreshing: boolean; onAdd: () => void;
}) {
  const active = memoryWorkspaceTabs.some(([key]) => key === selected) ? selected : 'PENDING.md';
  return <div className="memory-workspace-nav">
    <div className="memory-workspace-tabs" role="tablist" aria-label="记忆视图">
      {memoryWorkspaceTabs.map(([key, label], index) => <button type="button" role="tab" key={key}
        id={`${id}-${key}-tab`} aria-selected={active === key} tabIndex={active === key ? 0 : -1}
        aria-controls={`${id}-${key === 'entries' ? 'entries' : 'documents'}`} onClick={() => onSelect(key)}
        onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? memoryWorkspaceTabs.length - 1
            : (index + (event.key === 'ArrowRight' ? 1 : -1) + memoryWorkspaceTabs.length) % memoryWorkspaceTabs.length;
          const target = memoryWorkspaceTabs[next][0];
          onSelect(target); document.getElementById(`${id}-${target}-tab`)?.focus();
        }}>{label}</button>)}
    </div>
    <div className="memory-workspace-actions">
      <button type="button" className="ghost memory-add" onClick={onAdd}><Icon name="plus" size={17} />记住一件事</button>
      <IconButton icon="refresh" label="刷新记忆" disabled={refreshing} onClick={onRefresh} />
    </div>
  </div>;
}
