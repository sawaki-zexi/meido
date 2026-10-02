import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "../../api/client";
import type { Role } from "../../api/types";
import { clearCreateDraft, draftFromRole, emptyDraft, payloadFromDraft, readCreateDraft, writeCreateDraft, type RoleDraft } from "./draft";

/** Role list plus the create / view / edit / delete state of the selected role. */
export function useRoles() {
  const [roles, setRoles] = useState<Role[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [selected, setSelected] = useState<Role | null>(null);
  const [draft, setDraft] = useState<RoleDraft>(() => readCreateDraft() ?? emptyDraft);
  const [creating, setCreating] = useState(() => readCreateDraft() !== null);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");
  const requestId = useRef(0);

  useEffect(() => {
    const id = ++requestId.current;
    api<{ roles: Role[] }>("/api/roles")
      .then((data) => { if (id === requestId.current) setRoles(data.roles); })
      .catch((cause) => { if (id === requestId.current) { setLoadFailed(true); setError(errorMessage(cause, "无法加载角色")); } })
      .finally(() => { if (id === requestId.current) setLoading(false); });
  }, []);

  useEffect(() => { if (creating) writeCreateDraft(draft); }, [creating, draft]);

  const select = useCallback((role: Role) => {
    clearCreateDraft();
    setSelected(role);
    setCreating(false);
    setEditing(false);
    setDraft(draftFromRole(role));
  }, []);

  const startCreate = () => {
    setError("");
    setSelected(null);
    // Re-entering create mode keeps whatever the user already typed.
    if (!creating) setDraft(emptyDraft);
    setCreating(true);
  };

  const startEdit = () => setEditing(true);

  const cancel = () => {
    setError("");
    if (creating) {
      clearCreateDraft();
      setCreating(false);
      setSelected(null);
      setDraft(emptyDraft);
      return;
    }
    if (selected) setDraft(draftFromRole(selected));
    setEditing(false);
  };

  /** Saves the draft. Resolves the saved role, or null when saving failed. */
  const save = async (): Promise<Role | null> => {
    setError("");
    setSaving(true);
    const creatingSnapshot = creating;
    const selectedSnapshot = selected;
    const payload = JSON.stringify(payloadFromDraft(draft));
    try {
      const data = creatingSnapshot
        ? await api<{ role: Role }>("/api/roles", { method: "POST", body: payload })
        : await api<{ role: Role }>(`/api/roles/${encodeURIComponent(selectedSnapshot!.id)}`, { method: "PUT", body: payload });
      // A list request started before this save must not overwrite the saved role.
      requestId.current += 1;
      setLoading(false);
      setRoles((current) => creatingSnapshot ? [...current, data.role] : current.map((role) => role.id === data.role.id ? data.role : role));
      setSelected(data.role);
      setDraft(draftFromRole(data.role));
      clearCreateDraft();
      setCreating(false);
      setEditing(false);
      return data.role;
    } catch (cause) {
      setError(errorMessage(cause, "保存失败"));
      return null;
    } finally {
      setSaving(false);
    }
  };

  /** Deletes the selected role after confirmation. Resolves true once deleted. */
  const remove = async (): Promise<boolean> => {
    if (!selected || creating || deleting) return false;
    if (!window.confirm(`确定删除角色“${selected.name}”吗？\n\n角色、全部聊天记录和记忆将永久删除，且无法恢复。`)) return false;
    setError("");
    setDeleting(true);
    try {
      await api<void>(`/api/roles/${encodeURIComponent(selected.id)}`, { method: "DELETE" });
      // A list request started before this delete must not resurrect the role.
      requestId.current += 1;
      setRoles((current) => current.filter((role) => role.id !== selected.id));
      setSelected(null);
      setDraft(emptyDraft);
      return true;
    } catch (cause) {
      setError(errorMessage(cause, "删除失败"));
      return false;
    } finally {
      setDeleting(false);
    }
  };

  return { roles, loading, loadFailed, selected, draft, setDraft, creating, editing, saving, deleting, busy: saving || deleting, error, setError, select, startCreate, startEdit, cancel, save, remove };
}

export type RolesState = ReturnType<typeof useRoles>;
