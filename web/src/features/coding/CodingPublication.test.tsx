import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "../../App";
import { fakeBackend, role } from "../../test/fakeBackend";
import { CodingSettings } from "./CodingSettings";
import { CodingTasks } from "./CodingTasks";
import { defaultCodingRole } from "../../api/coding";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear(); });

it("shows the exact diff and HEAD before one explicit host commit approval and never resubmits on refresh", async () => {
  const head = "a".repeat(40), commit = "b".repeat(40);
  const task = { id: "coding-one", attemptId: "attempt-one", executor: "codex_exec", status: "validated_diff",
    runtimeStatus: "succeeded", cleanupState: "complete", deliveryStatus: "sent", branch: "codex/meido-one",
    baseHead: head, validatedHead: head, nativeSession: "fixture-session", ownerActions: { commitAllowed: true, publicationAllowed: false, hasEvidence: true } };
  const review = { id: "review-one", fingerprint: "review-fingerprint", taskId: task.id, attemptId: task.attemptId,
    action: "commit", head, commitSha: commit, branch: task.branch, baseHead: head, nativeSession: task.nativeSession,
    diff: "+reviewed file change", tree: "c".repeat(40), message: "已审阅的修改" };
  let operations: unknown[] = [];
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [role({ id: "r1", name: "编程审阅", agentConfig: { coding: { ...defaultCodingRole(), enabled: true } } })] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "GET /api/coding/roles/r1/tasks": () => ({ body: { tasks: [task], availability: { status: "available", reason: "fixture" } } }),
    "GET /api/coding/roles/r1/operations": () => ({ body: { operations } }),
    "POST /api/coding/roles/r1/tasks/coding-one/reviews": () => ({ body: review }),
    "POST /api/coding/roles/r1/reviews/review-one/approve": () => {
      operations = [{ id: "operation-one", taskId: task.id, attemptId: task.attemptId, action: "commit",
        status: "validated_commit", runtimeStatus: "succeeded", cleanupState: "complete", deliveryStatus: "sent", commitSha: commit }];
      return { status: 202, body: operations[0] };
    },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "编程审阅", level: 1 });
  await user.click(screen.getByRole("button", { name: "编程任务" }));
  const dialog = within(await screen.findByRole("dialog", { name: "编程任务" }));
  await user.click(await dialog.findByRole("button", { name: "审阅本地提交 coding-one" }));
  await user.type(dialog.getByRole("textbox", { name: "提交说明" }), "已审阅的修改");
  await user.type(dialog.getByRole("textbox", { name: "提交作者" }), "Owner");
  await user.type(dialog.getByRole("textbox", { name: "提交邮箱" }), "owner@example.invalid");
  await user.click(dialog.getByRole("button", { name: "准备审阅" }));
  expect(await dialog.findByText("+reviewed file change")).toBeInTheDocument();
  expect(dialog.getAllByText(head).length).toBeGreaterThan(0);
  expect(dialog.getByRole("button", { name: "批准此次本地提交" })).toBeDisabled();
  expect(calls.filter(call => call.path.endsWith("/approve"))).toHaveLength(0);
  await user.click(dialog.getByRole("checkbox", { name: /我已审阅此次确切成果/ }));
  await user.dblClick(dialog.getByRole("button", { name: "批准此次本地提交" }));
  await waitFor(() => expect(calls.filter(call => call.path.endsWith("/approve"))).toHaveLength(1));
  expect(calls.find(call => call.path.endsWith("/approve"))?.body).toMatchObject({ approved: true });
  await user.click(dialog.getByRole("button", { name: "刷新编程状态" }));
  expect(calls.filter(call => call.path.endsWith("/approve"))).toHaveLength(1);
  expect(await dialog.findByText("validated_commit")).toBeInTheDocument();
});

it("keeps local commit, push and Draft PR grants separate and stores only an explicitly entered broker credential", async () => {
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [role({ id: "r1", name: "主人角色" })] } }),
    "GET /api/coding/publication": () => ({ body: { revision: 1, configured: false, cleanupPending: false } }),
    "POST /api/coding/repositories": init => ({ body: { id: "repo-one", ...JSON.parse(String(init?.body)) } }),
    "PUT /api/coding/publication/credential": () => ({ body: { revision: 2, configured: true, cleanupPending: false } }),
  });
  const user = userEvent.setup();
  render(<CodingSettings />);
  await screen.findByRole("checkbox", { name: "主人角色" });
  expect(screen.getByRole("checkbox", { name: "允许宿主本地提交" })).not.toBeChecked();
  expect(screen.getByRole("checkbox", { name: "允许批准后推送" })).not.toBeChecked();
  expect(screen.getByRole("checkbox", { name: "允许批准后创建 Draft PR" })).not.toBeChecked();
  expect(calls.some(call => call.method === "PUT" && call.path.endsWith("/credential"))).toBe(false);
  await user.type(screen.getByRole("textbox", { name: "可信仓库路径" }), "D:/trusted/repository");
  await user.click(screen.getByRole("checkbox", { name: "允许宿主本地提交" }));
  await user.click(screen.getByRole("button", { name: "登记仓库" }));
  await waitFor(() => expect(calls.some(call => call.method === "POST" && call.path.endsWith("/repositories"))).toBe(true));
  expect(calls.find(call => call.path.endsWith("/repositories"))?.body).toMatchObject({ localCommit: true, push: false, draftPr: false });
  await user.type(screen.getByLabelText("独立发布凭据"), "fixture-broker-secret");
  await user.click(screen.getByRole("button", { name: "保存发布凭据" }));
  await waitFor(() => expect(calls.some(call => call.method === "PUT" && call.path.endsWith("/credential"))).toBe(true));
  expect(calls.find(call => call.path.endsWith("/credential") && call.method === "PUT")?.body).toEqual({ token: "fixture-broker-secret" });
  expect(screen.getByLabelText("独立发布凭据")).toHaveValue("");
});

it("does not carry a pending review, busy state or private draft across a role switch", async () => {
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  const task = (id: string) => ({ id, attemptId: `attempt-${id}`, status: "validated_diff", runtimeStatus: "succeeded", cleanupState: "complete", deliveryStatus: "sent", ownerActions: { commitAllowed: true, publicationAllowed: false, hasEvidence: true } });
  fakeBackend({
    "GET /api/coding/roles/r1/tasks": () => ({ body: { tasks: [task("one")] } }),
    "GET /api/coding/roles/r2/tasks": () => ({ body: { tasks: [task("two")] } }),
    "GET /api/coding/roles/(r1|r2)/operations": () => ({ body: { operations: [] } }),
    "POST /api/coding/roles/r1/tasks/one/reviews": async () => { await held; return { body: { id: "old-review", head: "old-head", taskId: "one", attemptId: "attempt-one" } }; },
  });
  const user = userEvent.setup();
  const view = render(<CodingTasks roleId="r1" />);
  try {
    await user.click(await screen.findByRole("button", { name: "审阅本地提交 one" }));
    await user.type(screen.getByRole("textbox", { name: "提交说明" }), "角色一的私有修改");
    await user.type(screen.getByRole("textbox", { name: "提交作者" }), "Owner");
    await user.type(screen.getByRole("textbox", { name: "提交邮箱" }), "owner@example.invalid");
    await user.click(screen.getByRole("button", { name: "准备审阅" }));
    view.rerender(<CodingTasks roleId="r2" />);
    release();
    const next = await screen.findByRole("button", { name: "审阅本地提交 two" });
    expect(next).toBeEnabled();
    await user.click(next);
    expect(screen.getByRole("textbox", { name: "提交说明" })).toHaveValue("");
    expect(screen.queryByText("old-head")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "批准此次本地提交" })).not.toBeInTheDocument();
  } finally { release(); }
});

it("shows uncertain publication honestly and only observes it after an explicit click", async () => {
  let operation = { id: "unknown-one", taskId: "task-one", attemptId: "attempt-one", action: "publish", status: "unknown", runtimeStatus: "failed", cleanupState: "complete", deliveryStatus: "sent", prUrl: "" };
  const { calls } = fakeBackend({
    "GET /api/coding/roles/r1/tasks": () => ({ body: { tasks: [] } }),
    "GET /api/coding/roles/r1/operations": () => ({ body: { operations: [operation] } }),
    "POST /api/coding/roles/r1/operations/unknown-one/observe": () => { operation = { ...operation, status: "delivered_draft", prUrl: "https://github.com/owner/repo/pull/1" }; return { body: operation }; },
  });
  const user = userEvent.setup();
  render(<CodingTasks roleId="r1" />);
  expect(await screen.findByText(/远端结果不确定/)).toBeInTheDocument();
  expect(calls.filter(call => call.method === "POST")).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "刷新编程状态" }));
  expect(calls.filter(call => call.method === "POST")).toHaveLength(0);
  await user.click(screen.getByRole("button", { name: "只读核对 unknown-one" }));
  expect(await screen.findByRole("link", { name: "查看已核验 Draft PR" })).toHaveAttribute("href", operation.prUrl);
  expect(calls.filter(call => call.method === "POST").map(call => call.path)).toEqual(["/api/coding/roles/r1/operations/unknown-one/observe"]);
});

it("requires a fresh review for only the remaining PR after a confirmed push", async () => {
  const task = { id: "task-one", attemptId: "attempt-one", status: "validated_commit", runtimeStatus: "succeeded",
    cleanupState: "complete", deliveryStatus: "sent", expectedHead: "approved-sha", ownerActions: { publicationAllowed: true } };
  const { calls } = fakeBackend({
    "GET /api/coding/roles/r1/tasks": () => ({ body: { tasks: [task] } }),
    "GET /api/coding/roles/r1/operations": () => ({ body: { operations: [{ id: "pushed-one", taskId: task.id,
      attemptId: task.attemptId, action: "publish", status: "push_confirmed", cleanupState: "complete", deliveryStatus: "sent" }] } }),
    "POST /api/coding/roles/r1/tasks/task-one/reviews": () => ({ body: { id: "remaining-review", action: "publish",
      taskId: task.id, attemptId: task.attemptId, head: "approved-sha", commitSha: "approved-sha", diff: "+reviewed",
      pushRequired: false, previousOperationId: "pushed-one", title: "New reviewed PR", body: "Remaining only" } }),
  });
  const user = userEvent.setup();
  render(<CodingTasks roleId="r1" />);
  await user.click(await screen.findByRole("button", { name: "审阅剩余 PR pushed-one" }));
  await user.type(screen.getByRole("textbox", { name: "PR 标题" }), "New reviewed PR");
  await user.type(screen.getByRole("textbox", { name: "PR 正文" }), "Remaining only");
  await user.click(screen.getByRole("button", { name: "准备审阅" }));
  expect(await screen.findByText(/此次只批准创建 Draft PR/)).toBeInTheDocument();
  expect(calls.find(call => call.path.endsWith("/reviews"))?.body).toEqual({ action: "publish", title: "New reviewed PR", body: "Remaining only", previousOperationId: "pushed-one" });
  expect(screen.getByRole("button", { name: "批准此次发布" })).toBeDisabled();
  expect(calls.filter(call => call.path.endsWith("/approve"))).toHaveLength(0);
});

it("keeps a newly saved credential revision when an older initial read returns late", async () => {
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [] } }),
    "GET /api/coding/publication": async () => { await held; return { body: { revision: 1, configured: false, cleanupPending: false } }; },
    "PUT /api/coding/publication/credential": () => ({ body: { revision: 2, configured: true, cleanupPending: false } }),
  });
  const user = userEvent.setup();
  render(<CodingSettings />);
  try {
    await user.type(screen.getByLabelText("独立发布凭据"), "fixture-broker-secret");
    await user.click(screen.getByRole("button", { name: "保存发布凭据" }));
    await screen.findByText("已配置独立发布凭据");
    await act(async () => { release(); await held; });
    expect(screen.getByText("已配置独立发布凭据")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "移除发布凭据" })).toBeEnabled();
  } finally { release(); }
});
