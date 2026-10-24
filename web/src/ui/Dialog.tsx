import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from 'react-dom';
import { IconButton } from "./Icon";

/** Modal panel: `drawer` slides in from the right, `modal` is centred, `compact` is a small centred card, and `phone` uses a handset shell. Esc and the backdrop close it. */
export function Dialog({ title, variant = "modal", onClose, children, modalFocus = false, returnFocus }: { title: string; variant?: "modal" | "drawer" | "compact" | "page" | "settings" | "phone"; onClose: () => void; children: ReactNode; modalFocus?: boolean; returnFocus?: () => HTMLElement | null }) {
  const panelRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const returnFocusRef = useRef(returnFocus);
  returnFocusRef.current = returnFocus;

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
    <div ref={panelRef} className="dialog-panel" role="dialog" aria-modal={variant !== 'settings'} aria-label={title} tabIndex={-1}>
      <header className="dialog-header">
        <h2>{title}</h2>
        <IconButton icon="close" label="关闭" onClick={onClose} />
      </header>
      <div className="dialog-body">{children}</div>
    </div>
  </div>;
  return modalFocus ? createPortal(layer, document.body) : layer;
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
