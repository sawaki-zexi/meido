import { describe, expect, it } from "vitest";
import { applyToolProgress, createToolProgress, closeToolProgress } from "./toolProgress";
import type { ToolStreamEvent } from "../../api/stream";

const source = { roleId: "r1", sessionKey: "role:r1", runId: "run-1" };
const start = (sequence = 1, toolCallId = "call-1"): ToolStreamEvent => ({ type: "tool_execution_start", ...source, sequence, toolCallId, toolName: "same_tool" });
const update = (sequence = 2, result = "latest", toolCallId = "call-1"): ToolStreamEvent => ({ ...start(sequence, toolCallId), type: "tool_execution_update", status: "running", result, isError: false, droppedUpdates: 0 });
const end = (sequence = 3, toolCallId = "call-1"): ToolStreamEvent => ({ ...start(sequence, toolCallId), type: "tool_execution_end", status: "unknown", result: "已派发，结果不确定", isError: true, droppedUpdates: 4 });

describe("latest call snapshots (#233)", () => {
  it("replaces rather than appends progress and pairs consecutive same-name calls by call ID", () => {
    let state = applyToolProgress(createToolProgress(source), start());
    for (let sequence = 2; sequence <= 10000; sequence++) state = applyToolProgress(state, update(sequence, String(sequence)));
    expect(state.calls).toHaveLength(1);
    expect(state.calls[0].result).toBe("10000");
    state = applyToolProgress(state, end(10001));
    state = applyToolProgress(state, start(10002, "call-2"));
    expect(state.calls.map(call => [call.toolCallId, call.status])).toEqual([["call-1", "unknown"], ["call-2", "running"]]);
  });

  it("ignores duplicate/out-of-order events, other sources/runs and updates after a call ends", () => {
    let state = applyToolProgress(createToolProgress(source), start());
    state = applyToolProgress(state, update());
    for (const event of [start(3), update(2, "duplicate"), update(1, "stale"), { ...update(20), roleId: "r2" }, { ...update(20), sessionKey: "other" }, { ...update(20), runId: "run-old" }, update(3, "unstarted", "missing")]) {
      expect(applyToolProgress(state, event)).toBe(state);
    }
    state = applyToolProgress(state, end());
    state = applyToolProgress(state, start(4, "call-2"));
    expect(applyToolProgress(state, update(5, "late"))).toBe(state);
    expect(applyToolProgress(state, end(6))).toBe(state);
  });

  it("closes progress on stop but accepts the real end without claiming rollback", () => {
    const state = closeToolProgress(applyToolProgress(createToolProgress(source), start()));
    expect(state.closing).toBe(true);
    expect(state.calls[0].status).toBe("running");
    expect(applyToolProgress(state, update())).toBe(state);
    expect(applyToolProgress(state, start(2, "call-2"))).toBe(state);
    expect(applyToolProgress(state, end()).calls[0]).toMatchObject({ status: "unknown", result: "已派发，结果不确定", droppedUpdates: 4 });
  });
});
