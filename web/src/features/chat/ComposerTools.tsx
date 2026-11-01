import { useEffect, useRef, useState } from 'react';
import { Icon } from '../../ui/Icon';
import type { ContextInspection } from './useChatContextStatus';
import { ChatContextRing } from './ChatContextRing';
import type { ContextUsage } from './chatContextState';
export { readContextUsage, type ContextUsage } from './chatContextState';

const emojis = ['😊', '🥰', '😍', '😘', '😆', '😂', '🥺', '😭', '😮', '😴', '🤔', '😤', '😳', '🙃', '😎', '🤗', '👋', '👍', '🙏', '💪', '❤️', '💕', '💖', '✨', '🎉', '🌸', '☀️', '🌙', '☕', '🍰', '🐱', '🐾'];
export function ComposerTools({ disabled, onEmoji, usage, inspection }: { disabled?: boolean; onEmoji: (emoji: string) => void; usage?: ContextUsage | null; inspection?: ContextInspection }) {
  const [panel, setPanel] = useState<'emoji' | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const emojiRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!panel) return;
    const outside = (event: PointerEvent) => { if (!ref.current?.contains(event.target as Node)) setPanel(null); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { setPanel(null); emojiRef.current?.focus(); } };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    if (panel === 'emoji') ref.current?.querySelector<HTMLButtonElement>('.emoji-grid button')?.focus();
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape); };
  }, [panel]);
  return <div className="composer-tools" ref={ref}>
    <button type="button" ref={emojiRef} className="composer-tool" aria-label="选择表情" title="选择表情" aria-expanded={panel === 'emoji'} disabled={disabled} onClick={() => setPanel(panel === 'emoji' ? null : 'emoji')}><Icon name="smile" size={18} /></button>
    <ChatContextRing inspection={inspection} usage={usage} />
    {panel === 'emoji' && <div className="composer-popover emoji-grid" role="dialog" aria-label="表情" onKeyDown={event => {
      const buttons = [...event.currentTarget.querySelectorAll('button')];
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
      const move = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 8, ArrowUp: -8 }[event.key];
      if (move !== undefined) { event.preventDefault(); buttons[(index + move + buttons.length) % buttons.length]?.focus(); }
    }}>{emojis.map(emoji => <button type="button" key={emoji} aria-label={emoji} onClick={() => { setPanel(null); onEmoji(emoji); }}>{emoji}</button>)}</div>}
  </div>;
}
