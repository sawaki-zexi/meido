import { useEffect, useRef, type ReactNode } from "react";
import { IconButton } from "./Icon";

/** Modal panel: `drawer` slides in from the right, `modal` is centred, `compact` is a small centred card. Esc and the backdrop close it. */
export function Dialog({ title, variant = "modal", onClose, children }: { title: string; variant?: "modal" | "drawer" | "compact" | "page" | "settings"; onClose: () => void; children: ReactNode }) {
  const panelRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const panels = Array.from(document.querySelectorAll<HTMLElement>('.dialog-panel'));
    const covered = variant === 'settings' ? panels.filter(panel => panel !== panelRef.current).at(-1) : undefined;
    const wasInert = covered?.inert ?? false;
    const wasHidden = covered?.getAttribute('aria-hidden') ?? null;
    if (covered) { covered.inert = true; covered.setAttribute('aria-hidden', 'true'); }
    panelRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      const panels = document.querySelectorAll(".dialog-panel");
      if (panels.item(panels.length - 1) !== panelRef.current) return;
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
      previous?.focus?.();
    };
  }, [variant]);

  return <div className={`dialog-layer dialog-${variant}`} onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div ref={panelRef} className="dialog-panel" role="dialog" aria-modal={variant !== 'settings'} aria-label={title} tabIndex={-1}>
      <header className="dialog-header">
        <h2>{title}</h2>
        <IconButton icon="close" label="关闭" onClick={onClose} />
      </header>
      <div className="dialog-body">{children}</div>
    </div>
  </div>;
}
