import { request } from "./client";
import type { ChatAttachment } from "./types";

export async function uploadChatAttachment(roleId: string, file: File): Promise<ChatAttachment> {
  const response = await request(`/api/roles/${encodeURIComponent(roleId)}/attachments`, {
    method: "POST",
    headers: {
      "content-type": file.type || "application/octet-stream",
      "x-attachment-filename": encodeURIComponent(file.name),
    },
    body: file,
  });
  return await response.json() as ChatAttachment;
}
