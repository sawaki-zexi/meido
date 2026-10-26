import type { ToolStatus, ToolStreamEvent } from "../../api/stream";

type Source = { roleId: string; sessionKey: string; runId: string };
export type ToolCallProgress = { toolCallId: string; toolName: string; status: ToolStatus; result: string; droppedUpdates: number };
export type ToolProgressState = Source & { sequence: number; closing: boolean; calls: ToolCallProgress[] };

export function createToolProgress(source: Source): ToolProgressState {
  return { ...source, sequence: -1, closing: false, calls: [] };
}

export function closeToolProgress(state: ToolProgressState): ToolProgressState {
  return { ...state, closing: true };
}

/** One latest snapshot per call, scoped to the active reply; never a progress log or persisted history. */
export function applyToolProgress(state: ToolProgressState, event: ToolStreamEvent): ToolProgressState {
  if (event.runId !== state.runId || event.roleId !== undefined && event.roleId !== state.roleId
    || event.sessionKey !== undefined && event.sessionKey !== state.sessionKey || event.sequence <= state.sequence) return state;
  if (state.closing && event.type !== "tool_execution_end") return state;
  const existing = state.calls.find(call => call.toolCallId === event.toolCallId);
  if (event.type === "tool_execution_start") {
    if (existing) return state;
    return { ...state, sequence: event.sequence, calls: [...state.calls, { toolCallId: event.toolCallId, toolName: event.toolName, status: "running", result: "", droppedUpdates: 0 }] };
  }
  if (!existing || existing.status !== "running" || existing.toolName !== event.toolName) return state;
  return { ...state, sequence: event.sequence, calls: state.calls.map(call => call === existing
    ? { ...call, status: event.status, result: event.result, droppedUpdates: Math.max(call.droppedUpdates, event.droppedUpdates) } : call) };
}
