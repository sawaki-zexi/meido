import type { Message } from "./types";
import { request } from "./client";

export type StreamEvent =
  | { type: "user_message_accepted"; message: Message }
  | { type: "assistant_generation_started"; messageId: string }
  | { type: "assistant_delta"; messageId: string; delta: string }
  | { type: "assistant_completed"; message: Message }
  | { type: "assistant_failed"; message: Message; error?: string };

/** Parses one SSE block (`event:` + `data:` lines) into a typed event. */
export function parseStreamEvent(raw: string): StreamEvent | null {
  const lines = raw.split("\n");
  const type = lines.find((line) => line.startsWith("event: "))?.slice(7);
  const dataLine = lines.find((line) => line.startsWith("data: "))?.slice(6);
  if (!type || !dataLine) return null;
  const data = JSON.parse(dataLine) as { messageId?: string; delta?: string; message?: Message; error?: string };
  switch (type) {
    case "user_message_accepted": return data.message ? { type, message: data.message } : null;
    case "assistant_generation_started": return data.messageId ? { type, messageId: data.messageId } : null;
    case "assistant_delta": return data.messageId ? { type, messageId: data.messageId, delta: data.delta ?? "" } : null;
    case "assistant_completed": return data.message ? { type, message: data.message } : null;
    case "assistant_failed": return data.message ? { type, message: data.message, error: data.error } : null;
    default: return null;
  }
}

/** Sends a message to a role's session and reports each stream event in order. */
export async function streamRoleMessage(roleId: string, content: string, onEvent: (event: StreamEvent) => void): Promise<void> {
  const response = await request(`/api/roles/${encodeURIComponent(roleId)}/messages`, { method: "POST", body: JSON.stringify({ content }) });
  if (!response.body) throw new Error("服务端没有返回流式响应");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() ?? "";
    for (const block of blocks) {
      const event = parseStreamEvent(block);
      if (event) onEvent(event);
    }
    if (done) break;
  }
}
