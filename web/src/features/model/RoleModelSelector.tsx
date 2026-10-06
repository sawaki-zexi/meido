import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { api, errorMessage } from "../../api/client";
import type { ModelConfiguration as Configuration } from "../../api/types";

type Binding = { configurationId: string | null; effectiveConfigurationId: string | null };

type RoleModelSelectorProps = {
  roleId: string;
  disabled?: boolean;
};

/** Loads and persists one role's model binding from the chat header. */
export function RoleModelSelector({ roleId, disabled = false }: RoleModelSelectorProps) {
  const [configurations, setConfigurations] = useState<Configuration[]>([]);
  const [binding, setBinding] = useState<Binding | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [savingId, setSavingId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const requestSequence = useRef(0);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const load = async () => {
    const requestId = ++requestSequence.current;
    setLoading(true);
    setError("");
    try {
      const [catalog, roleBinding] = await Promise.all([
        api<{ configurations: Configuration[] }>("/api/model/configurations"),
        api<Binding>(`/api/roles/${encodeURIComponent(roleId)}/model-configuration`),
      ]);
      if (requestId === requestSequence.current) {
        setConfigurations(catalog.configurations);
        setBinding(roleBinding);
        setError(roleBinding.configurationId && !catalog.configurations.some((item) => item.id === roleBinding.configurationId)
          ? "当前角色绑定的模型已不存在，请重新选择"
          : "");
      }
    } catch (cause) {
      if (requestId === requestSequence.current) setError(errorMessage(cause, "无法加载模型列表"));
    } finally {
      if (requestId === requestSequence.current) setLoading(false);
    }
  };

  useEffect(() => {
    requestSequence.current += 1;
    setOpen(false);
    setBinding(null);
    setSavingId(null);
    void load();
    // roleId is the only value that starts a new load; load intentionally uses the current role.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [roleId]);

  const effectiveId = binding?.effectiveConfigurationId ?? null;
  const current = useMemo(
    () => configurations.find((configuration) => configuration.id === effectiveId) ?? null,
    [configurations, effectiveId],
  );
  const selectedId = binding?.configurationId ?? null;
  const busy = loading || savingId !== null;

  const select = async (configurationId: string | null) => {
    if (busy || disabled || !binding || configurationId === selectedId) {
      setOpen(false);
      return;
    }
    const requestId = ++requestSequence.current;
    setSavingId(configurationId ?? "__default__");
    setError("");
    try {
      const next = await api<Binding>(`/api/roles/${encodeURIComponent(roleId)}/model-configuration`, {
        method: "PUT",
        body: JSON.stringify({ configurationId }),
      });
      if (requestId === requestSequence.current) {
        setBinding(next);
        setOpen(false);
      }
    } catch (cause) {
      if (requestId === requestSequence.current) setError(errorMessage(cause, "模型切换失败"));
    } finally {
      if (requestId === requestSequence.current) setSavingId(null);
    }
  };

  useEffect(() => {
    if (!open || loading) return;
    const menu = triggerRef.current?.parentElement?.querySelector<HTMLElement>(".role-model-menu");
    const selected = menu?.querySelector<HTMLElement>('[aria-checked="true"]');
    (selected ?? menu?.querySelector<HTMLElement>("[role='menuitemradio']"))?.focus({ preventScroll: true });
  }, [open, loading]);

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>("[role='menuitemradio']:not(:disabled)")];
    const index = items.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Home" || event.key === "End") {
      event.preventDefault();
      const next = event.key === "Home" ? 0
        : event.key === "End" ? items.length - 1
        : event.key === "ArrowDown" ? (index + 1 + items.length) % items.length
        : (index - 1 + items.length) % items.length;
      items[next]?.focus();
    }
  };

  const label = current?.model ?? (loading ? "加载模型…" : binding?.configurationId ? "模型已不存在" : "未配置模型");
  const toggleMenu = () => {
    setError("");
    setOpen((value) => {
      if (!value) void load();
      return !value;
    });
  };
  return <div className="role-model-selector">
    <button
      ref={triggerRef}
      type="button"
      className="role-model-trigger"
      aria-label="选择聊天模型"
      aria-haspopup="menu"
      aria-expanded={open}
      disabled={disabled || busy || configurations.length === 0}
      onClick={toggleMenu}
    >
      <span className="role-model-label">{label}</span>
      <span aria-hidden="true" className="role-model-caret">{open ? "▴" : "▾"}</span>
    </button>
    {open && <div className="role-model-menu" role="menu" aria-label="聊天模型" onKeyDown={handleMenuKeyDown}>
      {loading
        ? <span className="role-model-empty" role="status">正在刷新模型…</span>
        : configurations.length === 0
        ? <span className="role-model-empty">暂无已保存模型</span>
        : <>
          <button type="button" role="menuitemradio" aria-checked={selectedId === null} className="role-model-option" disabled={savingId !== null} onClick={() => void select(null)}>
            <span>跟随全局模型</span>{selectedId === null && <span aria-hidden="true">✓</span>}
          </button>
          {configurations.map((configuration) => <button
            type="button"
            role="menuitemradio"
            aria-checked={selectedId === configuration.id}
            className="role-model-option"
            key={configuration.id}
            disabled={savingId !== null}
            onClick={() => void select(configuration.id)}
          >
            <span>{configuration.model}<small>{configuration.provider}</small></span>
            {selectedId === configuration.id && <span aria-hidden="true">✓</span>}
          </button>)}
        </>}
    </div>}
    {error && <span className="role-model-error" role="status">{error}</span>}
  </div>;
}
