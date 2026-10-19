import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { novelai } from '../../api/novelai';
import { Icon } from '../../ui/Icon';

export function ChatActions({ codingEnabled, onGenerate, onCoding }: {
  codingEnabled: boolean; onGenerate: () => void; onCoding: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [generation, setGeneration] = useState<'loading' | 'enabled' | 'disabled' | 'error'>('loading');
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const id = useId();
  const close = (restoreFocus = false) => { setOpen(false); if (restoreFocus) trigger.current?.focus(); };
  useEffect(() => {
    if (!open) return;
    let active = true;
    menu.current?.focus();
    void novelai.configuration().then(value => { if (active) setGeneration(value.enabled ? 'enabled' : 'disabled'); })
      .catch(() => { if (active) setGeneration('error'); });
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('pointerdown', outside);
    return () => { active = false; document.removeEventListener('pointerdown', outside); };
  }, [open]);
  useEffect(() => {
    if (open && document.activeElement === menu.current) menu.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
  }, [open, generation]);
  const keyDown = (event: KeyboardEvent) => {
    if (!open && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) { event.preventDefault(); setGeneration('loading'); setOpen(true); return; }
    if (!open) return;
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); return; }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const items = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []);
    if (!items.length) return;
    const index = items.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
      : event.key === 'ArrowDown' ? (index + 1) % items.length : index < 0 ? items.length - 1 : (index - 1 + items.length) % items.length;
    items[next]?.focus();
  };
  return <div className="chat-actions" ref={root} onKeyDown={keyDown}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) close(); }}>
    <button type="button" ref={trigger} className="icon-button" aria-label="更多" title="更多" aria-haspopup="menu"
      aria-expanded={open} aria-controls={open ? id : undefined} onClick={() => { setGeneration('loading'); setOpen(value => !value); }}><Icon name="more" /></button>
    {open && <div className="chat-actions-menu" role="menu" aria-label="会话操作" id={id} ref={menu} tabIndex={-1}>
      {generation === 'enabled' && <button type="button" role="menuitem" tabIndex={-1} onClick={() => { close(true); onGenerate(); }}><Icon name="image" />生图与历史</button>}
      {codingEnabled && <button type="button" role="menuitem" tabIndex={-1} onClick={() => { close(true); onCoding(); }}><Icon name="code" />编程任务</button>}
      {generation === 'loading' && <p role="status">正在读取…</p>}
      {generation === 'error' && <p role="status">无法读取生图状态，请重新打开重试。</p>}
      {generation === 'disabled' && !codingEnabled && <p>暂无可用操作</p>}
    </div>}
  </div>;
}
