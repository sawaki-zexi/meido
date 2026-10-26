import { afterEach, describe, expect, it, vi } from "vitest";
import { parseStreamEvent, streamRoleMessage } from "./stream";
import { fakeBackend, message, sse } from "../test/fakeBackend";

afterEach(() => vi.unstubAllGlobals());

const call = { roleId: "r1", sessionKey: "role:r1", runId: "run-1", sequence: 3, toolCallId: "call-1", toolName: "mcp_connect" };

describe("tool SSE parsing (#233)", () => {
  it("keeps typed call identity and only the safe text snapshot, not arguments, images or details", () => {
    expect(parseStreamEvent(sse("tool_execution_start", { ...call, arguments: { token: "secret" } }))).toEqual({ type: "tool_execution_start", ...call });
    expect(parseStreamEvent(sse("tool_execution_update", { ...call, status: "running", result: "正在连接", isError: false, droppedUpdates: 12, details: { token: "secret" }, images: ["base64"] }))).toEqual({ type: "tool_execution_update", ...call, status: "running", result: "正在连接", isError: false, droppedUpdates: 12 });
    expect(parseStreamEvent(sse("tool_execution_end", { ...call, status: "unknown", result: "已派发，结果不确定", isError: true, droppedUpdates: 12 }))).toMatchObject({ type: "tool_execution_end", status: "unknown", result: "已派发，结果不确定" });
  });

  it("supports old tool envelopes and all five ordinary chat events", () => {
    expect(parseStreamEvent(sse("tool_execution_end", { ...call, result: "完成", isError: false }))).toMatchObject({ status: "succeeded", droppedUpdates: 0 });
    const saved = message({ id: "m1", role: "assistant", content: "回复" });
    for (const [type, payload] of [
      ["user_message_accepted", { message: saved }], ["assistant_generation_started", { messageId: "m1" }],
      ["assistant_delta", { messageId: "m1", delta: "回复" }], ["assistant_completed", { message: saved }], ["assistant_failed", { message: saved, error: "失败" }],
    ] as const) expect(parseStreamEvent(sse(type, payload))).toMatchObject({ type, ...payload });
  });

  it.each([{ runId: "" }, { toolCallId: "" }, { toolName: 1 }, { sequence: -1 }, { sequence: 1.5 }, { sequence: "3" }, { result: { content: [{ type: "image", data: "raw" }] } }])("ignores malformed tool payload %j", (invalid) => {
    expect(parseStreamEvent(sse("tool_execution_update", { ...call, result: "safe", ...invalid }))).toBeNull();
  });

  it("ignores malformed JSON and does not turn an unfamiliar terminal status into success", () => {
    expect(parseStreamEvent("event: tool_execution_update\ndata: {broken")).toBeNull();
    expect(parseStreamEvent(sse("tool_execution_end", { ...call, result: "", status: "future_status", isError: false }))).toMatchObject({ status: "unknown" });
  });

  it("bounds retained multibyte result text to 16 KiB without retaining raw fields", () => {
    const parsed = parseStreamEvent(sse("tool_execution_update", { ...call, result: "中文🙂".repeat(10000), isError: false }));
    expect(parsed?.type).toBe("tool_execution_update");
    if (parsed?.type !== "tool_execution_update") throw new Error("missing update");
    expect(new TextEncoder().encode(parsed.result).byteLength).toBeLessThanOrEqual(16 * 1024);
    expect(parsed.result).not.toContain("�");
  });

  it("does not let a rejected stale terminal event end the current stream", async () => {
    fakeBackend({ "POST /api/roles/r1/messages": () => ({ stream: [
      sse("assistant_completed", { runId: "old", message: message({ id: "old", role: "assistant", content: "old" }) }),
      sse("tool_execution_start", call),
      sse("assistant_completed", { runId: "run-1", message: message({ id: "new", role: "assistant", content: "new" }) }),
    ] }) });
    const seen: string[] = [];
    await streamRoleMessage("r1", "go", [], (event) => { if (event.runId === "old") return false; seen.push(event.type); });
    expect(seen).toEqual(["tool_execution_start", "assistant_completed"]);
  });
});
