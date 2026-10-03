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
  const [avatarFile, setAvatarFile] = useState<File | null>(null);
  const [avatarRemoved, setAvatarRemoved] = useState(false);
  const [avatarPreviewUrl, setAvatarPreviewUrl] = useState<string | null>(null);
  const [avatarSource, setAvatarSource] = useState<File | null>(null);
  const [avatarOriginalFile, setAvatarOriginalFile] = useState<File | null>(null);
  const [cardImageFile, setCardImageFile] = useState<File | null>(null);
  const [cardImageSource, setCardImageSource] = useState<File | null>(null);
  const [cardImagePreviewUrl, setCardImagePreviewUrl] = useState<string | null>(null);
  const [cardImageRemoved, setCardImageRemoved] = useState(false);
  const requestId = useRef(0);

  useEffect(() => {
    if (!avatarFile) { setAvatarPreviewUrl(null); return; }
    const previewUrl = URL.createObjectURL(avatarFile);
    setAvatarPreviewUrl(previewUrl);
    return () => URL.revokeObjectURL(previewUrl);
  }, [avatarFile]);

  useEffect(() => {
    if (!cardImageFile) { setCardImagePreviewUrl(null); return; }
    const url = URL.createObjectURL(cardImageFile);
    setCardImagePreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [cardImageFile]);

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
    setAvatarFile(null);
    setAvatarSource(null);
    setCardImageFile(null);
    setCardImageSource(null);
    setCardImageRemoved(false);
    setAvatarRemoved(false);
  }, []);

  const startCreate = () => {
    setError("");
    setSelected(null);
    // Re-entering create mode keeps whatever the user already typed.
    if (!creating) setDraft(emptyDraft);
    setAvatarFile(null);
    setAvatarSource(null);
    setCardImageFile(null);
    setCardImageSource(null);
    setCardImageRemoved(false);
    setAvatarRemoved(false);
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
      setAvatarFile(null);
      setAvatarSource(null);
      setCardImageFile(null);
      setCardImageSource(null);
      setCardImageRemoved(false);
      setAvatarRemoved(false);
      return;
    }
    if (selected) setDraft(draftFromRole(selected));
    setAvatarFile(null);
    setAvatarSource(null);
    setCardImageFile(null);
    setCardImageSource(null);
    setCardImageRemoved(false);
    setAvatarRemoved(false);
    setEditing(false);
  };

  const chooseAvatar = (file: File | null) => {
    if (!file) return;
    setError("");
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      setError("仅支持 PNG、JPEG 或 WebP 图片");
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      setError("头像图片不能超过 10 MB");
      return;
    }
    setAvatarSource(file);
    setAvatarRemoved(false);
  };

  const acceptAvatarCrop = (file: File, original: File) => {
    setAvatarFile(file);
    setAvatarOriginalFile(original);
    setAvatarSource(null);
    setAvatarRemoved(false);
  };

  const cancelAvatarCrop = () => setAvatarSource(null);

  const chooseCardImage = (file: File | null) => {
    if (!file) return;
    setError("");
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) { setError("仅支持 PNG、JPEG 或 WebP 图片"); return; }
    if (file.size > 10 * 1024 * 1024) { setError("卡片图片不能超过 10 MB"); return; }
    setCardImageSource(file);
    setCardImageRemoved(false);
  };

  const acceptCardImageCrop = (file: File) => { setCardImageFile(file); setCardImageSource(null); setCardImageRemoved(false); };
  const cancelCardImageCrop = () => setCardImageSource(null);
  const clearCardImage = () => { setCardImageFile(null); setCardImageSource(null); setCardImageRemoved(Boolean(selected?.cardImageUrl)); };

  const clearAvatar = () => {
    setAvatarFile(null);
    setAvatarOriginalFile(null);
    setAvatarSource(null);
    setAvatarRemoved(Boolean(selected?.avatarUrl));
  };

  /** Saves the draft. Resolves the saved role, or null when saving failed. */
  const save = async (): Promise<Role | null> => {
    setError("");
    setSaving(true);
    const creatingSnapshot = creating;
    const selectedSnapshot = selected;
    const avatarSnapshot = avatarFile;
    const originalSnapshot = avatarOriginalFile;
    const cardImageSnapshot = cardImageFile;
    const removeAvatarSnapshot = avatarRemoved && Boolean(selectedSnapshot?.avatarUrl);
    const removeCardImageSnapshot = cardImageRemoved && Boolean(selectedSnapshot?.cardImageUrl);
    const payload = JSON.stringify(payloadFromDraft(draft));
    try {
      const data = creatingSnapshot
        ? await api<{ role: Role }>("/api/roles", { method: "POST", body: payload })
        : await api<{ role: Role }>(`/api/roles/${encodeURIComponent(selectedSnapshot!.id)}`, { method: "PUT", body: payload });
      const retainRole = (role: Role, keepEditing: boolean) => {
        requestId.current += 1;
        setLoading(false);
        setRoles((current) => creatingSnapshot ? [...current.filter((item) => item.id !== role.id), role] : current.map((item) => item.id === role.id ? role : item));
        setSelected(role);
        setDraft(draftFromRole(role));
        clearCreateDraft();
        setCreating(false);
        setEditing(keepEditing);
      };
      let savedRole = data.role;
      retainRole(savedRole, Boolean(avatarSnapshot || removeAvatarSnapshot || cardImageSnapshot || removeCardImageSnapshot));

      if (avatarSnapshot) {
        const uploaded = await api<{ role: Role }>(`/api/roles/${encodeURIComponent(savedRole.id)}/avatar`, {
          method: "POST",
          headers: { "content-type": avatarSnapshot.type },
          body: avatarSnapshot,
        });
        savedRole = uploaded.role;
        if (originalSnapshot) {
          const original = await api<{ role: Role }>(`/api/roles/${encodeURIComponent(savedRole.id)}/avatar-original`, { method: "POST", headers: { "content-type": originalSnapshot.type }, body: originalSnapshot });
          savedRole = original.role;
        }
      } else if (removeAvatarSnapshot) {
        await api<void>(`/api/roles/${encodeURIComponent(savedRole.id)}/avatar`, { method: "DELETE" });
        savedRole = { ...savedRole, avatarUrl: null, avatarMediaType: null };
      }

      if (cardImageSnapshot) {
        const uploaded = await api<{ role: Role }>(`/api/roles/${encodeURIComponent(savedRole.id)}/card-image`, { method: "POST", headers: { "content-type": cardImageSnapshot.type }, body: cardImageSnapshot });
        savedRole = uploaded.role;
      } else if (removeCardImageSnapshot) {
        await api<void>(`/api/roles/${encodeURIComponent(savedRole.id)}/card-image`, { method: "DELETE" });
        savedRole = { ...savedRole, cardImageUrl: null, cardImageMediaType: null };
      }

      retainRole(savedRole, false);
      setAvatarFile(null);
      setAvatarOriginalFile(null);
      setAvatarSource(null);
      setCardImageFile(null);
      setCardImageSource(null);
      setCardImageRemoved(false);
      setAvatarRemoved(false);
      return savedRole;
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

  return { roles, loading, loadFailed, selected, draft, setDraft, creating, editing, saving, deleting, busy: saving || deleting, error, setError, avatarFile, avatarSource, avatarOriginalFile, avatarRemoved, avatarPreviewUrl, chooseAvatar, acceptAvatarCrop, cancelAvatarCrop, clearAvatar, cardImageFile, cardImageSource, cardImagePreviewUrl, cardImageRemoved, chooseCardImage, acceptCardImageCrop, cancelCardImageCrop, clearCardImage, select, startCreate, startEdit, cancel, save, remove };
}

export type RolesState = ReturnType<typeof useRoles>;
