// Adapted from Shiori c14152ed ChatContextRing/useChatComposerPopover (MIT).
// Copyright (c) 2026 YinFengWindy. See third_party/shiori-agent/LICENSE.
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { contextUsageLabel, readContextUsage, type ContextUsage } from './chatContextState';
import type { ContextInspection } from './useChatContextStatus';

/** Capacity display only; opening the tooltip never starts compression. */
export function ChatContextRing({ inspection, usage: fallback = null }: { inspection?: ContextInspection; usage?: ContextUsage | null }) {
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ left: number; bottom: number } | null>(null);
  const id = useId();
  const usage = inspection ? readContextUsage(inspection.status) : fallback;
  const display = contextUsageLabel(usage);
  const busy = Boolean(inspection?.busy);
  const reason = busy
    ? `${inspection?.status?.reason || inspection?.error || '正在回复或整理上下文'}${usage ? '；显示上次用量' : ''}`
    : inspection?.loading ? `正在读取上下文${usage ? '；显示上次用量' : ''}`
    : inspection?.error || inspection?.status?.reason || '当前已保存会话，不含草稿';
  const show = () => {
    if (!open) { setOpen(true); inspection?.refresh(); }
  };

  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!trigger.current?.contains(event.target as Node) && !panel.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false); };
    window.addEventListener('pointerdown', outside, true);
    window.addEventListener('keydown', escape);
    return () => { window.removeEventListener('pointerdown', outside, true); window.removeEventListener('keydown', escape); };
  }, [open]);

  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const rect = trigger.current?.getBoundingClientRect();
      if (rect) setPosition({ left: Math.max(8, Math.min(rect.right - 280, window.innerWidth - 288)), bottom: window.innerHeight - rect.top + 4 });
    };
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => { window.removeEventListener('resize', place); window.removeEventListener('scroll', place, true); };
  }, [open]);

  return <>
    <button ref={trigger} type="button" className="composer-tool context-ring-button"
      aria-label={`上下文用量；${display.label}；${reason}`} aria-describedby={open ? id : undefined} aria-busy={busy}
      onMouseEnter={show} onMouseLeave={() => { if (document.activeElement !== trigger.current) setOpen(false); }}
      onFocus={show} onBlur={() => setOpen(false)} onClick={show}>
      <svg className="context-ring" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="9" fill="none" stroke="var(--line)" strokeWidth="2.5" />
        {display.ratio !== null && <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="2.5" pathLength="100"
          strokeDasharray={`${Math.min(100, Math.max(0, display.ratio * 100))} 100`} strokeLinecap="round" />}
        <text x="12" y="12" transform="rotate(90 12 12)" textAnchor="middle" dominantBaseline="central" fill="currentColor">{busy ? '…' : display.ratio === null ? '?' : ''}</text>
      </svg>
    </button>
    {open && position && createPortal(<div ref={panel} id={id} role="tooltip" className="context-tooltip" style={position}>
      <div>{display.label}</div>
      <div>{reason}</div>
    </div>, document.body)}
  </>;
}
