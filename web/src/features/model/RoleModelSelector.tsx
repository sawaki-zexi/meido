import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { api, errorMessage } from "../../api/client";
import type { ModelBindingResponse as Binding, ModelConfiguration as Configuration } from "../../api/types";

type RoleModelSelectorProps = {
  roleId: string;
  revision?: string;
  disabled?: boolean;
  onChange?: (configuration: Configuration | null) => void;
};

/** Loads and persists one role's model binding from the chat header. */
export function RoleModelSelector({ roleId, revision = "", disabled = false, onChange }: RoleModelSelectorProps) {
  const [configurations, setConfigurations] = useState<Configuration[]>([]);
  const [binding, setBinding] = useState<Binding | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [savingId, setSavingId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const requestSequence = useRef(0);
  const savingRequest = useRef<number | null>(null);
  const refreshAfterSave = useRef(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef(false);

  const load = async (saveError = "") => {
    const requestId = ++requestSequence.current;
    setLoading(true);
    setError(saveError);
    try {
      const [catalog, roleBinding] = await Promise.all([
        api<{ configurations: Configuration[] }>("/api/model/configurations"),
        api<Binding>(`/api/roles/${encodeURIComponent(roleId)}/model-configuration`),
      ]);
      if (requestId === requestSequence.current) {
        setConfigurations(catalog.configurations);
        setBinding(roleBinding);
        const bindingError = roleBinding.error || (roleBinding.configurationId && !catalog.configurations.some((item) => item.id === roleBinding.configurationId)
          ? "当前角色绑定的模型已不存在，请重新选择"
          : "");
        setError([saveError, bindingError].filter(Boolean).join("；"));
      }
    } catch (cause) {
      if (requestId === requestSequence.current) setError([saveError, errorMessage(cause, "无法加载模型列表")].filter(Boolean).join("；"));
    } finally {
      if (requestId === requestSequence.current) setLoading(false);
    }
  };

  useEffect(() => {
    requestSequence.current += 1;
    restoreFocusRef.current = false;
    setOpen(false);
    setBinding(null);
    setSavingId(null);
    savingRequest.current = null;
    refreshAfterSave.current = false;
    return () => {
      requestSequence.current += 1;
      savingRequest.current = null;
      refreshAfterSave.current = false;
    };
  }, [roleId]);

  useEffect(() => {
    // A settings refresh must read after any pending binding write settles.
    if (savingRequest.current !== null) refreshAfterSave.current = true;
    else void load();
    // Only a new role/settings snapshot triggers this read; load uses that role.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [roleId, revision]);

  const reselectionRequired = binding?.reselectionRequired === true;
  const effectiveId = reselectionRequired ? null : binding?.effectiveConfigurationId ?? null;
  const current = useMemo(
    () => configurations.find((configuration) => configuration.id === effectiveId) ?? null,
    [configurations, effectiveId],
  );
  const selectedId = binding?.configurationId ?? null;
  useEffect(() => { if (!loading) onChange?.(current); }, [current, loading, onChange]);
  const busy = loading || savingId !== null;
  const closeMenu = () => {
    restoreFocusRef.current = true;
    setOpen(false);
  };
  useEffect(() => {
    if (!open && !busy && restoreFocusRef.current) {
      restoreFocusRef.current = false;
      triggerRef.current?.focus({ preventScroll: true });
    }
  }, [open, busy]);

  const select = async (configurationId: string | null) => {
    if (busy || disabled || !binding || (configurationId === selectedId && !reselectionRequired)) {
      closeMenu();
      return;
    }
    const requestId = ++requestSequence.current;
    savingRequest.current = requestId;
    setSavingId(configurationId ?? "__default__");
    setError("");
    let saveError = "";
    try {
      const next = await api<Binding>(`/api/roles/${encodeURIComponent(roleId)}/model-configuration`, {
        method: "PUT",
        body: JSON.stringify({ configurationId }),
      });
      if (requestId === requestSequence.current) {
        setBinding(next);
        setError(next.error ?? "");
        closeMenu();
      }
    } catch (cause) {
      saveError = errorMessage(cause, "模型切换失败");
      if (requestId === requestSequence.current) setError(saveError);
    } finally {
      if (requestId === requestSequence.current) {
        savingRequest.current = null;
        setSavingId(null);
        if (refreshAfterSave.current) {
          refreshAfterSave.current = false;
          void load(saveError);
        }
      }
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
      closeMenu();
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Home" || event.key === "End") {
      event.preventDefault();
      const next = event.key === "Home" ? 0
        : event.key === "End" ? items.length - 1
        : event.key === "ArrowDown" ? (index + 1 + items.length) % items.length
        : (index - 1 + items.length) % items.length;
      items[next]?.focus();
    }
  };

  const label = current?.model ?? (loading ? "加载模型…" : reselectionRequired ? "请重新选择模型" : binding?.configurationId ? "模型已不存在" : "未配置模型");
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
      {error && <span className="role-model-error" role="status">{error}</span>}
      {loading
        ? <span className="role-model-empty" role="status">正在刷新模型…</span>
        : configurations.length === 0
        ? <span className="role-model-empty">暂无已保存模型</span>
        : <>
          <button type="button" role="menuitemradio" aria-checked={!reselectionRequired && selectedId === null} className="role-model-option" disabled={savingId !== null} onClick={() => void select(null)}>
            <span>跟随全局模型</span>{!reselectionRequired && selectedId === null && <span aria-hidden="true">✓</span>}
          </button>
          {configurations.map((configuration) => <button
            type="button"
            role="menuitemradio"
            aria-checked={!reselectionRequired && selectedId === configuration.id}
            className="role-model-option"
            key={configuration.id}
            disabled={savingId !== null}
            onClick={() => void select(configuration.id)}
          >
            <span>{configuration.model}<small>{configuration.provider}</small></span>
            {!reselectionRequired && selectedId === configuration.id && <span aria-hidden="true">✓</span>}
          </button>)}
        </>}
    </div>}
    {!open && error && <span className="role-model-error" role="status">{error}</span>}
  </div>;
}
