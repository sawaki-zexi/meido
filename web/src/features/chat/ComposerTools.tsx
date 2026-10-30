import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { Icon } from '../../ui/Icon';

const emojis = ['😊', '🥰', '😍', '😘', '😆', '😂', '🥺', '😭', '😮', '😴', '🤔', '😤', '😳', '🙃', '😎', '🤗', '👋', '👍', '🙏', '💪', '❤️', '💕', '💖', '✨', '🎉', '🌸', '☀️', '🌙', '☕', '🍰', '🐱', '🐾'];
export type ContextUsage = { inputTokens: number; capacity: number | null; source: 'actual' | 'anchor_delta' | 'local'; configurationId: string | null; model?: string | null };
export function readContextUsage(value: unknown): ContextUsage | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as ContextUsage;
  return Number.isFinite(item.inputTokens) && item.inputTokens >= 0 && (item.source === 'actual' || item.source === 'anchor_delta' || item.source === 'local')
    ? { ...item, capacity: typeof item.capacity === 'number' && Number.isFinite(item.capacity) && item.capacity > 0 ? item.capacity : null } : null;
}

export function ComposerTools({ disabled, onEmoji, usage }: { disabled?: boolean; onEmoji: (emoji: string) => void; usage: ContextUsage | null }) {
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
  const percent = usage?.capacity ? Math.round(usage.inputTokens / usage.capacity * 100) : null;
  return <div className="composer-tools" ref={ref}>
    <button type="button" ref={emojiRef} className="composer-tool" aria-label="选择表情" title="选择表情" aria-expanded={panel === 'emoji'} disabled={disabled} onClick={() => setPanel(panel === 'emoji' ? null : 'emoji')}><Icon name="smile" size={18} /></button>
    <button type="button" ref={contextRef} className="composer-tool" aria-label={`上下文用量${percent === null ? '：未知' : `：${percent}%`}`} title="上下文用量" aria-expanded={panel === 'context'} onClick={() => setPanel(panel === 'context' ? null : 'context')}>
      <span className={`context-ring${percent !== null && percent >= 90 ? ' is-full' : ''}`} style={{ '--context-used': `${Math.min(100, percent ?? 0)}%` } as CSSProperties}>{percent === null ? '?' : ''}</span>
    </button>
    {panel === 'emoji' && <div className="composer-popover emoji-grid" role="dialog" aria-label="表情" onKeyDown={event => {
      const buttons = [...event.currentTarget.querySelectorAll('button')];
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const move = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 8, ArrowUp: -8 }[event.key];
      if (move !== undefined) { event.preventDefault(); buttons[(index + move + buttons.length) % buttons.length]?.focus(); }
    }}>{emojis.map(emoji => <button type="button" key={emoji} aria-label={emoji} onClick={() => { setPanel(null); onEmoji(emoji); }}>{emoji}</button>)}</div>}
    {panel === 'context' && <div className="composer-popover context-details" role="dialog" aria-label="上下文详情">
      <strong>最近一次请求的输入上下文</strong>
      <p>{usage ? `${usage.inputTokens.toLocaleString()} / ${usage.capacity?.toLocaleString() ?? '未知'} tokens${percent === null ? '' : ` · ${percent}%`}` : '暂无用量统计'}</p>
      <small>{usage ? { actual: '供应商实测输入量', anchor_delta: '锚点估算输入量', local: '本地估算输入量' }[usage.source] : '发送消息后更新'}；不含待发送草稿，非累计消耗。{usage && !usage.capacity && '模型尚未配置上下文容量。'}</small>
    </div>}
  </div>;
}
