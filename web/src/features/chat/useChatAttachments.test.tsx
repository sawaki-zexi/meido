import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ChatAttachment } from "../../api/types";
import { useChatAttachments } from "./useChatAttachments";

function pendingResponse() {
  let resolve!: (response: Response) => void;
  const promise = new Promise<Response>((finish) => { resolve = finish; });
  return { promise, resolve };
}

const file = (name: string, content = "note") => new File([content], name, { type: "text/plain" });
const attachment = (id: string, name: string): ChatAttachment => ({ id, name, mediaType: "text/plain", size: 4, kind: "file" });
const success = (id: string, name: string) => new Response(JSON.stringify(attachment(id, name)));
const failure = (detail: string) => new Response(JSON.stringify({ detail }), { status: 422 });

beforeEach(() => { vi.stubGlobal("fetch", vi.fn()); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("keeps the batch busy after an early failure, preserving successful selection order and current attachments", async () => {
  const first = pendingResponse();
  const failed = pendingResponse();
  const last = pendingResponse();
  vi.mocked(fetch).mockReturnValueOnce(first.promise).mockReturnValueOnce(failed.promise).mockReturnValueOnce(last.promise);
  const { result } = renderHook(() => useChatAttachments("alice", false, false));
  act(() => result.current.restore([attachment("kept", "kept.txt"), attachment("removed", "removed.txt")]));
  let batch!: Promise<void>;
  act(() => { batch = result.current.upload([file("first.txt"), file("empty.txt", ""), file("last.txt")]); });
  await act(async () => { failed.resolve(failure("附件不能为空")); });
  expect(result.current.uploading).toBe(true);
  expect(fetch).toHaveBeenCalledTimes(3);
  act(() => result.current.remove("removed"));
  await act(async () => { last.resolve(success("last", "last.txt")); });
  expect(result.current.uploading).toBe(true);
  await act(async () => { first.resolve(success("first", "first.txt")); await batch; });
  expect(result.current.uploading).toBe(false);
  expect(result.current.attachments.map((item) => item.id)).toEqual(["kept", "first", "last"]);
  expect(result.current.error).toContain("empty.txt：附件不能为空");
  expect(result.current.error).not.toContain("first.txt");
  expect(fetch).toHaveBeenCalledTimes(3);
});

it("waits for a late failure and treats identically named files as separate selections", async () => {
  const first = pendingResponse();
  const failed = pendingResponse();
  const last = pendingResponse();
  vi.mocked(fetch).mockReturnValueOnce(first.promise).mockReturnValueOnce(failed.promise).mockReturnValueOnce(last.promise);
  const { result } = renderHook(() => useChatAttachments("alice", false, false));
  let batch!: Promise<void>;
  act(() => { batch = result.current.upload([file("same.txt", "one"), file("same.txt", ""), file("same.txt", "two")]); });
  await act(async () => { last.resolve(success("last", "same.txt")); first.resolve(success("first", "same.txt")); });
  expect(result.current.uploading).toBe(true);
  await act(async () => { failed.resolve(failure("附件不能为空")); await batch; });
  expect(result.current.attachments.map((item) => item.id)).toEqual(["first", "last"]);
  expect(result.current.error).toContain("same.txt：附件不能为空");
  expect(result.current.uploading).toBe(false);
});

it("reports every failed file, unlocks an all-failed batch and allows selecting repaired files", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(failure("附件不能为空")).mockResolvedValueOnce(failure("附件不能超过 10 MB"));
  const { result } = renderHook(() => useChatAttachments("alice", false, false));
  await act(async () => { await result.current.upload([file("empty.txt", ""), file("large.txt")]); });
  expect(result.current.attachments).toEqual([]);
  expect(result.current.uploading).toBe(false);
  expect(result.current.error).toContain("empty.txt：附件不能为空");
  expect(result.current.error).toContain("large.txt：附件不能超过 10 MB");
  vi.mocked(fetch).mockResolvedValueOnce(success("repaired", "empty.txt"));
  await act(async () => { await result.current.upload([file("empty.txt")]); });
  expect(result.current.attachments.map((item) => item.id)).toEqual(["repaired"]);
  expect(result.current.error).toBe("");
  expect(fetch).toHaveBeenCalledTimes(3);
});

it("preserves a successful file when the user reselects only the failed file", async () => {
  vi.mocked(fetch).mockResolvedValueOnce(success("good", "good.txt")).mockResolvedValueOnce(failure("附件不能为空"));
  const { result } = renderHook(() => useChatAttachments("alice", false, false));
  await act(async () => { await result.current.upload([file("good.txt"), file("empty.txt", "")]); });
  vi.mocked(fetch).mockResolvedValueOnce(success("repaired", "empty.txt"));
  await act(async () => { await result.current.upload([file("empty.txt")]); });
  expect(result.current.attachments.map((item) => item.id)).toEqual(["good", "repaired"]);
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(result.current.error).toBe("");
});

it("does not start another batch in the same tick before the busy state renders", async () => {
  const response = pendingResponse();
  vi.mocked(fetch).mockReturnValue(response.promise);
  const { result } = renderHook(() => useChatAttachments("alice", false, false));
  let batch!: Promise<void>;
  act(() => {
    batch = result.current.upload([file("first.txt")]);
    void result.current.upload([file("second.txt")]);
  });
  expect(fetch).toHaveBeenCalledTimes(1);
  await act(async () => { response.resolve(success("first", "first.txt")); await batch; });
  expect(result.current.attachments.map((item) => item.id)).toEqual(["first"]);
});
