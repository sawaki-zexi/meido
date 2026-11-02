import { useCallback, useRef, useState } from "react";
import { errorMessage } from "../../api/client";
import { uploadChatAttachment } from "../../api/chat";
import type { ChatAttachment } from "../../api/types";

export function useChatAttachments(roleId: string | undefined, disabled: boolean, sending: boolean) {
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const uploadInProgress = useRef(false);

  const upload = useCallback(async (files: File[]) => {
    if (!roleId || disabled || sending || uploadInProgress.current || files.length === 0) return;
    uploadInProgress.current = true;
    setUploading(true);
    setError("");
    try {
      const results = await Promise.allSettled(files.map((file) => uploadChatAttachment(roleId, file)));
      const uploaded: ChatAttachment[] = [];
      const failures: string[] = [];
      results.forEach((result, index) => {
        if (result.status === "fulfilled") uploaded.push(result.value);
        else failures.push(`${files[index].name}：${errorMessage(result.reason, "附件上传失败")}`);
      });
      setAttachments((current) => [...current, ...uploaded]);
      setError(failures.join("；"));
    } finally {
      uploadInProgress.current = false;
      setUploading(false);
    }
  }, [disabled, roleId, sending]);

  const remove = useCallback((attachmentId: string) => {
    setAttachments((current) => current.filter((item) => item.id !== attachmentId));
  }, []);

  const clear = useCallback(() => setAttachments([]), []);
  const restore = useCallback((items: ChatAttachment[]) => setAttachments((current) => [
    ...items.filter((item) => !current.some((existing) => existing.id === item.id)), ...current,
  ]), []);

  return { attachments, uploading, dragging, setDragging, error, setError, upload, remove, clear, restore };
}
