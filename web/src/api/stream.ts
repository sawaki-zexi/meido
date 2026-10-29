import type { Message } from "./types";
import { request } from "./client";

export type ToolStatus = "running" | "succeeded" | "failed" | "unknown" | "denied" | "invalid_arguments" | "timed_out" | "cancelled";
type ToolIdentity = { runId: string; sequence: number; roleId?: string; sessionKey?: string; toolCallId: string; toolName: string };
export type ToolStreamEvent = ToolIdentity & (
  | { type: "tool_execution_start" }
  | { type: "tool_execution_update"; result: string; isError: boolean; status: "running"; droppedUpdates: number }
  | { type: "tool_execution_end"; result: string; isError: boolean; status: Exclude<ToolStatus, "running">; droppedUpdates: number }
);

export type StreamEvent = ToolStreamEvent | ((
  | { type: "user_message_accepted"; message: Message }
  | { type: "assistant_generation_started"; messageId: string; messageSequence?: number }
  | { type: "assistant_delta"; messageId: string; delta: string }
  | { type: "assistant_message_committed"; message: Message }
  | { type: "assistant_completed"; message: Message }
  | { type: "assistant_failed"; message: Message; error?: string }
) & { runId?: string; sequence?: number });

/** The server owns redaction; retain only its bounded text projection, never raw tool payloads. */
function boundedSummary(text: string): string {
  const encoder = new TextEncoder();
  // Slice before encoding so even a legacy oversized result makes only a bounded copy.
  const bytes = encoder.encode(text.slice(0, 16 * 1024));
  if (text.length <= 16 * 1024 && bytes.length <= 16 * 1024) return text;
  return new TextDecoder().decode(bytes.subarray(0, 16 * 1024 - 3), { stream: true }) + "…";
}

/** Parses one SSE block (`event:` + `data:` lines) into a typed event. */
export function parseStreamEvent(raw: string): StreamEvent | null {
  const lines = raw.split("\n");
  const type = lines.find((line) => line.startsWith("event: "))?.slice(7);
  const dataLine = lines.find((line) => line.startsWith("data: "))?.slice(6);
  if (!type || !dataLine) return null;
  let data: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(dataLine);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    data = parsed as Record<string, unknown>;
  } catch { return null; }
  const runId = typeof data.runId === "string" ? data.runId : undefined;
  const sequence = typeof data.sequence === "number" && Number.isSafeInteger(data.sequence) && data.sequence >= 0 ? data.sequence : undefined;
  if (type === "tool_execution_start" || type === "tool_execution_update" || type === "tool_execution_end") {
    if (!runId || sequence === undefined || typeof data.toolCallId !== "string" || !data.toolCallId || typeof data.toolName !== "string" || !data.toolName) return null;
    if (data.roleId !== undefined && typeof data.roleId !== "string" || data.sessionKey !== undefined && typeof data.sessionKey !== "string") return null;
    const identity: ToolIdentity = { runId, sequence, toolCallId: data.toolCallId, toolName: data.toolName,
      ...(typeof data.roleId === "string" ? { roleId: data.roleId } : {}), ...(typeof data.sessionKey === "string" ? { sessionKey: data.sessionKey } : {}) };
    if (type === "tool_execution_start") return { type, ...identity };
    if (typeof data.result !== "string") return null;
    const result = boundedSummary(data.result);
    const isError = data.isError === true;
    const droppedUpdates = typeof data.droppedUpdates === "number" && Number.isSafeInteger(data.droppedUpdates) && data.droppedUpdates >= 0 ? data.droppedUpdates : 0;
    if (type === "tool_execution_update") return { type, ...identity, result, isError, status: "running", droppedUpdates };
    const status = data.status === undefined ? (isError ? "failed" : "succeeded")
      : ["succeeded", "failed", "unknown", "denied", "invalid_arguments", "timed_out", "cancelled"].includes(String(data.status)) ? data.status as Exclude<ToolStatus, "running"> : "unknown";
    return { type, ...identity, result, isError, status, droppedUpdates };
  }
  const message = data.message && typeof data.message === "object" ? data.message as Message : undefined;
  const messageId = typeof data.messageId === "string" ? data.messageId : undefined;
  switch (type) {
    case "user_message_accepted": return message ? { type, message, runId, sequence } : null;
    case "assistant_generation_started": return messageId ? { type, messageId, runId, sequence, messageSequence: typeof data.messageSequence === "number" && Number.isSafeInteger(data.messageSequence) && data.messageSequence > 0 ? data.messageSequence : undefined } : null;
    case "assistant_delta": return messageId ? { type, messageId, delta: typeof data.delta === "string" ? data.delta : "", runId, sequence } : null;
    case "assistant_message_committed":
    case "assistant_completed": return message ? { type, message, runId, sequence } : null;
    case "assistant_failed": return message ? { type, message, error: typeof data.error === "string" ? data.error : undefined, runId, sequence } : null;
    default: return null;
  }
}

/** Sends a message to a role's session and reports each stream event in order. */
export async function streamRoleMessage(roleId: string, content: string, attachments: string[], onEvent: (event: StreamEvent) => void | boolean, options: { signal?: AbortSignal; onRun?: (runId: string) => void } = {}): Promise<void> {
  const body: { content: string; attachments?: string[] } = { content };
  if (attachments.length > 0) body.attachments = attachments;
  const response = await request(`/api/roles/${encodeURIComponent(roleId)}/messages`, { method: "POST", body: JSON.stringify(body), signal: options.signal });
  const runId = response.headers.get("X-Meido-Run-Id");
  if (runId) options.onRun?.(runId);
  if (!response.body) throw new Error("服务端没有返回流式响应");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  options.signal?.addEventListener("abort", cancel, { once: true });
  try {
    if (options.signal?.aborted) { cancel(); return; }
    while (true) {
      const { value, done } = await reader.read();
      if (options.signal?.aborted) return;
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() ?? "";
      for (const block of blocks) {
        if (options.signal?.aborted) return;
        const event = parseStreamEvent(block);
        if (event && onEvent(event) !== false) {
          if (event.type === "assistant_completed" || event.type === "assistant_failed") return;
        }
      }
      if (done) break;
    }
  } finally {
    options.signal?.removeEventListener("abort", cancel);
    // Stream completion releases the chat immediately; transport cleanup may
    // continue after the final event and must not hold the next message.
    cancel();
    reader.releaseLock();
  }
}
