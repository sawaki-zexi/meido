import { Icon } from '../../ui/Icon';
import { type Memory, type MemoryOrigin, sourceLabels, formatDate, originsOf } from './types';

export function MemoryInspector({ memory, onEdit, onSimilar, onAction, onOpenSource }: {
  memory: Memory; onEdit: () => void; onSimilar: () => void; onAction: (action: 'forget' | 'reject' | 'delete') => void;
  onOpenSource: (origin: MemoryOrigin, messageId?: string) => void;
}) {
  const origins = originsOf(memory);
  return <div className="memory-inspector">
    <section className="memory-origin-section" aria-label="记忆来源">
      <h3><Icon name="chat" size={15} />来源</h3>
      {origins.some(origin => origin.messageIds?.length || origin.messageRange)
        ? origins.map((origin, index) => <div className="memory-origin-card" key={`${origin.stableSourceKey}-${index}`}>
          <span>{sourceLabels[origin.kind] ?? origin.kind}{origin.messageRange ? ` · 消息 ${origin.messageRange[0]}–${origin.messageRange[1]}` : ''}</span>
          <div>{(origin.messageIds ?? []).map((messageId, itemIndex) => <button type="button" className="memory-source-link" key={messageId} onClick={() => onOpenSource(origin, messageId)}>
            定位到消息{origin.messageIds.length > 1 ? ` ${itemIndex + 1}` : ''}<span aria-hidden="true">↗</span></button>)}
          {!origin.messageIds?.length && origin.messageRange && <button type="button" className="memory-source-link" onClick={() => onOpenSource(origin)}>定位到回合<span aria-hidden="true">↗</span></button>}</div>
        </div>) : <p className="field-hint">这条记忆没有可定位的对话来源。</p>}
    </section>
    <div className="memory-detail-actions"><button type="button" className="primary" onClick={onEdit}><Icon name="edit" size={16} />编辑记录</button><button type="button" className="ghost" onClick={onSimilar}>查找相似记忆</button></div>
    <details className="memory-advanced"><summary>更多操作与信息</summary>
      <dl className="memory-detail-meta"><div><dt>记录时间</dt><dd>{formatDate(memory.createdAt)}</dd></div>
        {memory.happenedAt && <div><dt>发生时间</dt><dd>{formatDate(memory.happenedAt)}</dd></div>}
        {memory.updatedAt !== memory.createdAt && <div><dt>最近更新</dt><dd>{formatDate(memory.updatedAt)}</dd></div>}
        <div><dt>情绪权重</dt><dd>{memory.emotionalWeight ?? 0}</dd></div>
        {memory.hasEmbedding !== undefined && <div><dt>语义索引</dt><dd>{memory.hasEmbedding ? '已建立' : '未建立'}</dd></div>}</dl>
      <p className="field-hint">忘记或拒绝会改变记录状态；删除会移除这条记忆。</p>
      <div className="memory-danger-actions">{memory.status === 'active' && <><button type="button" className="ghost" onClick={() => onAction('forget')}>忘记</button><button type="button" className="ghost" onClick={() => onAction('reject')}>拒绝</button></>}
        <button type="button" className="ghost danger" onClick={() => onAction('delete')}>删除</button></div>
    </details>
  </div>;
}
