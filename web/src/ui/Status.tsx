import type { ReactNode } from "react";

/** Quiet placeholder for loading and empty states. */
export function Placeholder({ loading, children }: { loading?: boolean; children: ReactNode }) {
  return <div className="placeholder" role={loading ? "status" : undefined} aria-busy={loading || undefined}>
    {loading && <span className="spinner" aria-hidden="true" />}
    <p>{children}</p>
  </div>;
}

/** Inline error or success notice; errors are announced to assistive tech. */
export function Notice({ tone = "error", children, onDismiss }: { tone?: "error" | "success"; children: ReactNode; onDismiss?: () => void }) {
  return <div className={`notice notice-${tone}`} role={tone === "error" ? "alert" : "status"}>
    <p>{children}</p>
    {onDismiss && <button type="button" className="ghost icon" aria-label="关闭提示" onClick={onDismiss}>×</button>}
  </div>;
}
