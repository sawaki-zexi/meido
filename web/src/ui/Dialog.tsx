import { useEffect, useRef, type ReactNode } from "react";
import { IconButton } from "./Icon";

/** Modal panel: `drawer` slides in from the right, `modal` is centred, `compact` is a small centred card. Esc and the backdrop close it. */
export function Dialog({ title, variant = "modal", onClose, children }: { title: string; variant?: "modal" | "drawer" | "compact" | "page"; onClose: () => void; children: ReactNode }) {
  const panelRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onCloseRef.current(); };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); previous?.focus?.(); };
  }, []);

  return <div className={`dialog-layer dialog-${variant}`} onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div ref={panelRef} className="dialog-panel" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}>
      <header className="dialog-header">
        <h2>{title}</h2>
        <IconButton icon="close" label="关闭" onClick={onClose} />
      </header>
      <div className="dialog-body">{children}</div>
    </div>
  </div>;
}
