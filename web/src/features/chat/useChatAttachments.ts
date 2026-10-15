import { useCallback, useState } from "react";
import { errorMessage } from "../../api/client";
import { uploadChatAttachment } from "../../api/chat";
import type { ChatAttachment } from "../../api/types";

export function useChatAttachments(roleId: string | undefined, disabled: boolean, sending: boolean) {
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");

  const upload = useCallback(async (files: File[]) => {
    if (!roleId || disabled || sending || uploading || files.length === 0) return;
    setUploading(true);
    setError("");
    try {
      const uploaded = await Promise.all(files.map((file) => uploadChatAttachment(roleId, file)));
      setAttachments((current) => [...current, ...uploaded]);
    } catch (cause) {
      setError(errorMessage(cause, "附件上传失败"));
    } finally {
      setUploading(false);
    }
  }, [disabled, roleId, sending, uploading]);

  const remove = useCallback((attachmentId: string) => {
    setAttachments((current) => current.filter((item) => item.id !== attachmentId));
  }, []);

  const clear = useCallback(() => setAttachments([]), []);
  const restore = useCallback((items: ChatAttachment[]) => setAttachments((current) => [
    ...items.filter((item) => !current.some((existing) => existing.id === item.id)), ...current,
  ]), []);

  return { attachments, uploading, dragging, setDragging, error, setError, upload, remove, clear, restore };
}
