import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { Icon } from '../../ui/Icon';
import type { ContextInspection } from './useChatContextStatus';

const emojis = ['😊', '🥰', '😍', '😘', '😆', '😂', '🥺', '😭', '😮', '😴', '🤔', '😤', '😳', '🙃', '😎', '🤗', '👋', '👍', '🙏', '💪', '❤️', '💕', '💖', '✨', '🎉', '🌸', '☀️', '🌙', '☕', '🍰', '🐱', '🐾'];
export type ContextUsage = { inputTokens: number; capacity: number | null; source: 'actual' | 'anchor_delta' | 'local'; configurationId: string | null; model?: string | null };
export function readContextUsage(value: unknown): ContextUsage | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as ContextUsage;
  return Number.isFinite(item.inputTokens) && item.inputTokens >= 0 && (item.source === 'actual' || item.source === 'anchor_delta' || item.source === 'local')
    ? { ...item, capacity: typeof item.capacity === 'number' && Number.isFinite(item.capacity) && item.capacity > 0 ? item.capacity : null } : null;
}

export function ComposerTools({ disabled, onEmoji, usage: previousUsage = null, inspection }: { disabled?: boolean; onEmoji: (emoji: string) => void; usage?: ContextUsage | null; inspection?: ContextInspection }) {
  const [panel, setPanel] = useState<'emoji' | 'context' | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const emojiRef = useRef<HTMLButtonElement>(null);
  const contextRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!panel) return;
    const outside = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) setPanel(null); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { setPanel(null); (panel === 'emoji' ? emojiRef : contextRef).current?.focus(); } };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    if (panel === 'emoji') ref.current?.querySelector<HTMLButtonElement>('.emoji-grid button')?.focus();
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape); };
  }, [panel]);
  const status = inspection?.status;
  const usage = inspection ? status?.status === 'ready' ? readContextUsage(status) : null : previousUsage;
  const percent = usage?.capacity ? Math.round(usage.inputTokens / usage.capacity * 100) : null;
  const label = inspection?.busy ? '忙碌' : inspection?.loading ? '读取中' : percent === null ? '未知' : `${percent}%`;
  return <div className="composer-tools" ref={ref}>
    <button type="button" ref={emojiRef} className="composer-tool" aria-label="选择表情" title="选择表情" aria-expanded={panel === 'emoji'} disabled={disabled} onClick={() => setPanel(panel === 'emoji' ? null : 'emoji')}><Icon name="smile" size={18} /></button>
    <button type="button" ref={contextRef} className="composer-tool" aria-label={`上下文用量：${label}`} title="上下文用量" aria-expanded={panel === 'context'} onClick={() => { if (panel !== 'context') inspection?.refresh(); setPanel(panel === 'context' ? null : 'context'); }}>
      <span className={`context-ring${percent !== null && percent >= 90 ? ' is-full' : ''}`} style={{ '--context-used': `${Math.min(100, percent ?? 0)}%` } as CSSProperties}>{percent === null ? inspection?.loading ? '…' : '?' : ''}</span>
    </button>
    {panel === 'emoji' && <div className="composer-popover emoji-grid" role="dialog" aria-label="表情" onKeyDown={event => {
      const buttons = [...event.currentTarget.querySelectorAll('button')];
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const move = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 8, ArrowUp: -8 }[event.key];
      if (move !== undefined) { event.preventDefault(); buttons[(index + move + buttons.length) % buttons.length]?.focus(); }
    }}>{emojis.map(emoji => <button type="button" key={emoji} aria-label={emoji} onClick={() => { setPanel(null); onEmoji(emoji); }}>{emoji}</button>)}</div>}
    {panel === 'context' && <div className="composer-popover context-details" role="dialog" aria-label="上下文详情">
      <strong>当前已保存会话的上下文</strong>
      <p role="status">{inspection?.loading ? '正在读取当前上下文…' : usage ? `${usage.inputTokens.toLocaleString()} / ${usage.capacity?.toLocaleString() ?? '未知'} tokens${percent === null ? '' : ` · ${percent}%`}` : inspection?.error || status?.reason || '暂无用量统计'}</p>
      {usage && <small>{ { actual: '供应商实测输入量', anchor_delta: '锚点估算输入量', local: '本地估算输入量' }[usage.source]}{!usage.capacity && '；模型尚未配置上下文容量。'}</small>}
      {usage && status && <dl>
        <dt>聊天模型</dt><dd>{status.model}</dd>
        <dt>实际输入限额</dt><dd>{status.inputLimit?.toLocaleString() ?? '未知'} tokens</dd>
        <dt>回复预留 / 安全余量</dt><dd>{status.outputReserve?.toLocaleString()} / {status.safetyMargin?.toLocaleString()} tokens</dd>
        <dt>自动压缩阈值 / 目标</dt><dd>{status.compactTrigger?.toLocaleString() ?? '未知'} / {status.compactTarget?.toLocaleString() ?? '未知'} tokens</dd>
      </dl>}
      <small>不含待发送草稿、未发送附件及新消息检索结果，非累计消耗。</small>
      {inspection && <button type="button" className="ghost" disabled={inspection.loading} onClick={inspection.refresh}>重新检查</button>}
    </div>}
  </div>;
}
