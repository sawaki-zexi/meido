import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { Dialog } from "../../ui/Dialog";
import { Notice } from "../../ui/Status";

type Props = { name: string; busy: boolean; error: string; onCancel: () => void; onConfirm: () => void; editor: HTMLFormElement | null; trigger: HTMLButtonElement | null };

/** Keep the underlying editor and its draft in place while confirming deletion. */
export function RoleDeleteConfirmation({ name, busy, error, onCancel, onConfirm, editor, trigger }: Props) {
  const content = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const background = editor?.closest<HTMLElement>(".dialog-panel, .shell");
    const panel = content.current?.closest<HTMLElement>(".dialog-panel");
    background?.setAttribute("inert", "");
    background?.setAttribute("aria-hidden", "true");
    cancel.current?.focus();
    const trapFocus = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || !panel) return;
      const buttons = Array.from(panel.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"));
      const first = buttons[0];
      const last = buttons[buttons.length - 1];
      if (!first) { event.preventDefault(); panel.focus(); return; }
      if (event.shiftKey && (document.activeElement === first || !panel.contains(document.activeElement))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !panel.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    };
    document.addEventListener("keydown", trapFocus);
    return () => {
      document.removeEventListener("keydown", trapFocus);
      background?.removeAttribute("inert");
      background?.removeAttribute("aria-hidden");
      trigger?.focus();
    };
  }, [editor, trigger]);

  return createPortal(<Dialog title="删除角色" variant="compact" onClose={onCancel}>
    <div ref={content} className="role-delete-confirmation" aria-busy={busy}>
      <p>确定删除角色“{name}”吗？</p>
      <p className="muted">角色、全部聊天记录和记忆将永久删除，且无法恢复。</p>
      {error && <Notice>{error}</Notice>}
      <div className="form-actions">
        <button ref={cancel} type="button" className="ghost" disabled={busy} onClick={onCancel}>取消</button>
        <button type="button" className="danger" disabled={busy} onClick={onConfirm}>{busy ? "删除中…" : "确认删除"}</button>
      </div>
    </div>
  </Dialog>, document.body);
}
