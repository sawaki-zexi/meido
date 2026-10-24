import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { createPortal } from 'react-dom';
import { IconButton } from "./Icon";

/** Modal panel: `drawer` slides in from the right, `modal` is centred, `compact` is a small centred card, and `phone` uses a handset shell. Esc and the backdrop close it. */
export function Dialog({ title, variant = "modal", onClose, children, modalFocus = false, returnFocus }: { title: string; variant?: "modal" | "drawer" | "compact" | "page" | "settings" | "phone"; onClose: () => void; children: ReactNode; modalFocus?: boolean; returnFocus?: () => HTMLElement | null }) {
  const panelRef = useRef<HTMLDivElement>(null);
  const [phoneOffset, setPhoneOffset] = useState({ x: 0, y: 0 });
  const [phoneDragging, setPhoneDragging] = useState(false);
  const phoneDragRef = useRef<PhoneDragState | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const returnFocusRef = useRef(returnFocus);
  returnFocusRef.current = returnFocus;

  const startPhoneDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    if (variant !== "phone" || (event.pointerType === "mouse" && event.button !== 0)) return;
    const panel = panelRef.current;
    if (!panel) return;
    const frame = panel.getBoundingClientRect();
    const frameLeft = finiteOr(frame.left);
    const frameTop = finiteOr(frame.top);
    const frameWidth = finiteOr(frame.width);
    const frameHeight = finiteOr(frame.height);
    phoneDragRef.current = {
      pointerId: event.pointerId,
      startX: finiteOr(event.clientX),
      startY: finiteOr(event.clientY),
      originX: phoneOffset.x,
      originY: phoneOffset.y,
      baseLeft: frameLeft - phoneOffset.x,
      baseTop: frameTop - phoneOffset.y,
      width: frameWidth,
      height: frameHeight,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
    setPhoneDragging(true);
    event.preventDefault();
  };

  const movePhoneDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = phoneDragRef.current;
    if (variant !== "phone" || !drag || !samePointer(drag.pointerId, event.pointerId)) return;
    const minX = 8 - drag.baseLeft;
    const maxX = finiteOr(window.innerWidth, 1024) - 8 - drag.baseLeft - drag.width;
    const minY = 8 - drag.baseTop;
    const maxY = finiteOr(window.innerHeight, 768) - 8 - drag.baseTop - drag.height;
    const clientX = finiteOr(event.clientX, drag.startX);
    const clientY = finiteOr(event.clientY, drag.startY);
    setPhoneOffset({
      x: clamp(drag.originX + clientX - drag.startX, minX, maxX),
      y: clamp(drag.originY + clientY - drag.startY, minY, maxY),
    });
  };

  const endPhoneDrag = (event: ReactPointerEvent<HTMLButtonElement>) => {
    const drag = phoneDragRef.current;
    if (!drag || !samePointer(drag.pointerId, event.pointerId)) return;
    phoneDragRef.current = null;
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    setPhoneDragging(false);
  };

  useEffect(() => {
    if (variant !== "phone") return;
    const keepPhoneVisible = () => {
      const frame = panelRef.current?.getBoundingClientRect();
      if (!frame) return;
      phoneDragRef.current = null;
      setPhoneDragging(false);
      const x = clamp(frame.left, 8, Math.max(8, window.innerWidth - 8 - frame.width)) - frame.left;
      const y = clamp(frame.top, 8, Math.max(8, window.innerHeight - 8 - frame.height)) - frame.top;
      if (x || y) setPhoneOffset(current => ({ x: current.x + x, y: current.y + y }));
    };
    window.addEventListener("resize", keepPhoneVisible);
    return () => window.removeEventListener("resize", keepPhoneVisible);
  }, [variant]);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const panels = Array.from(document.querySelectorAll<HTMLElement>('.dialog-panel'));
    const covered = variant === 'settings' ? panels.filter(panel => panel !== panelRef.current).at(-1) : undefined;
    const wasInert = covered?.inert ?? false;
    const wasHidden = covered?.getAttribute('aria-hidden') ?? null;
    if (covered) { covered.inert = true; covered.setAttribute('aria-hidden', 'true'); }
    const background = modalFocus ? Array.from(document.body.children).filter((element): element is HTMLElement => element instanceof HTMLElement && element !== panelRef.current?.parentElement) : [];
    for (const element of background) pauseBackground(element);
    panelRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" && !(modalFocus && event.key === 'Tab')) return;
      const panels = document.querySelectorAll(".dialog-panel");
      if (panels.item(panels.length - 1) !== panelRef.current) return;
      if (event.key === 'Tab') {
        const items = Array.from(panelRef.current?.querySelectorAll<HTMLElement>('a[href],button,input,select,textarea,summary,[tabindex]') ?? [])
          .filter(element => element.tabIndex >= 0 && !element.matches(':disabled') && isVisible(element));
        const first = items[0], last = items.at(-1);
        if (!items.length || !panelRef.current?.contains(document.activeElement) || document.activeElement === panelRef.current
          || (event.shiftKey ? document.activeElement === first : document.activeElement === last)) {
          event.preventDefault(); (event.shiftKey ? last : first)?.focus();
          if (!items.length) panelRef.current?.focus();
        }
        return;
      }
      event.stopImmediatePropagation();
      onCloseRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (covered) {
        covered.inert = wasInert;
        if (wasHidden === null) covered.removeAttribute('aria-hidden');
        else covered.setAttribute('aria-hidden', wasHidden);
      }
      for (const element of background) resumeBackground(element);
      const target = returnFocusRef.current?.() ?? previous;
      if (target?.isConnected && !target.closest('[inert]') && !target.matches(':disabled')) target.focus();
    };
  }, [variant, modalFocus]);

  const layer = <div className={`dialog-layer dialog-${variant}`} onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div ref={panelRef} className={`dialog-panel${phoneDragging ? " is-dragging" : ""}`} style={variant === "phone" ? { transform: `translate3d(${phoneOffset.x}px, ${phoneOffset.y}px, 0)` } : undefined} role="dialog" aria-modal={variant !== 'settings'} aria-label={title} tabIndex={-1}>
      {variant === "phone" ? <button type="button" className="dialog-phone-drag-handle" aria-label="拖动手机会话" onPointerDown={startPhoneDrag} onPointerMove={movePhoneDrag} onPointerUp={endPhoneDrag} onPointerCancel={endPhoneDrag}><span aria-hidden="true" /></button>
        : <header className="dialog-header">
          <h2>{title}</h2>
          <IconButton icon="close" label="关闭" onClick={onClose} />
        </header>}
      <div className="dialog-body">{children}</div>
    </div>
  </div>;
  return modalFocus ? createPortal(layer, document.body) : layer;
}

type PhoneDragState = {
  pointerId: number;
  startX: number;
  startY: number;
  originX: number;
  originY: number;
  baseLeft: number;
  baseTop: number;
  width: number;
  height: number;
};

function clamp(value: number, minimum: number, maximum: number) {
  const lower = Math.min(minimum, maximum);
  const upper = Math.max(minimum, maximum);
  return Math.min(Math.max(value, lower), upper);
}

function finiteOr(value: number, fallback = 0) {
  return Number.isFinite(value) ? value : fallback;
}

function samePointer(expected: number, actual: number) {
  return actual === 0 || actual === undefined || expected === actual;
}

function isVisible(element: HTMLElement) {
  for (let current: HTMLElement | null = element; current; current = current.parentElement) {
    if (current.hidden || current.inert || getComputedStyle(current).display === 'none' || getComputedStyle(current).visibility === 'hidden') return false;
    if (current.tagName === 'DETAILS' && !current.hasAttribute('open') && !current.querySelector('summary')?.contains(element)) return false;
  }
  return true;
}

// Nested modal owners may finish out of order (for example when the underlying role disappears).
const backgroundOwners = new WeakMap<HTMLElement, { count: number; inert: boolean; hidden: string | null }>();
function pauseBackground(element: HTMLElement) {
  const previous = backgroundOwners.get(element);
  if (previous) previous.count++;
  else backgroundOwners.set(element, { count: 1, inert: element.inert, hidden: element.getAttribute('aria-hidden') });
  element.inert = true; element.setAttribute('aria-hidden', 'true');
}
function resumeBackground(element: HTMLElement) {
  const previous = backgroundOwners.get(element);
  if (!previous || --previous.count) return;
  backgroundOwners.delete(element); element.inert = previous.inert;
  if (previous.hidden === null) element.removeAttribute('aria-hidden'); else element.setAttribute('aria-hidden', previous.hidden);
}
