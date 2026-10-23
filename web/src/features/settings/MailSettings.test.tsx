import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fakeBackend } from "../../test/fakeBackend";
import { MailSettings } from "./MailSettings";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

const emptyStatus = () => ({
  host: "imap.qq.com", port: 993, username: "", useSsl: true, enabled: false, paused: false,
  verified: false, autoDraftReady: false, folders: { inbox: "", sent: "", drafts: "" },
  lastError: null, lastSyncAt: null, items: 0, drafts: 0,
  candidate: null as { host: string; port: number; username: string; connection_tested: boolean; test_draft_present: boolean; test_sent: boolean } | null,
  testDraft: null, lastErrors: [],
});

const commonRoutes = (getStatus: () => unknown) => ({
  "GET /api/mail/preferences": () => ({ body: { writingStyle: "", roleAccess: false } }),
  "GET /api/mail/status": () => ({ body: getStatus() }),
  "GET /api/mail/items\\?limit=50&offset=0": () => ({ body: { items: [] } }),
  "GET /api/roles": () => ({ body: { roles: [] } }),
  "GET /api/mail/reminder-role": () => ({ body: { roleId: null } }),
  "GET /api/mail/reminder-batches\\?limit=8": () => ({ body: { batches: [] } }),
});

describe("MailSettings", () => {
  it("saves the owner's writing preference and explicit role mail permission", async () => {
    const { calls } = fakeBackend({
      ...commonRoutes(emptyStatus),
      "PUT /api/mail/preferences": () => ({ body: calls.at(-1)?.body }),
    });
    render(<MailSettings />);
    fireEvent.change(await screen.findByLabelText("邮件写作偏好"), { target: { value: "简短，不额外承诺" } });
    fireEvent.click(screen.getByLabelText("允许角色管理邮件"));
    fireEvent.click(screen.getByRole("button", { name: "保存邮件偏好" }));
    await screen.findByText("邮件偏好已保存");
    expect(calls.find((call) => call.method === "PUT" && call.path === "/api/mail/preferences")?.body).toEqual({ writingStyle: "简短，不额外承诺", roleAccess: true });
  });

  it("shows the persisted question and sources, then submits the owner's answer", async () => {
    const status = { ...emptyStatus(), username: "owner@qq.com", verified: true, autoDraftReady: true, items: 1 };
    const item = { id: "mail-1", threadId: "thread-1", sender: "teacher@example.com", subject: "面试邀请", receivedAt: "2026-10-20T09:00:00Z", status: "needs_input", needsReply: true, draftId: null, lastError: null };
    const preparation = { id: "prep-1", decision: "needs_input", reason: "还需要主人决定", questions: ["是否接受周五面试？"], createdAt: "2026-10-20T09:01:00Z", model: "test", stale: false,
      sources: [{ kind: "owner_knowledge", id: "note-1", title: "面试计划", location: "第二段", url: "https://example.com/notes" }], warnings: ["相关已发送邮件读取失败"] };
    let answered = false;
    const { calls } = fakeBackend({
      ...commonRoutes(() => status),
      "GET /api/mail/items\\?limit=50&offset=0": () => ({ body: { items: [item] } }),
      "GET /api/mail/items/mail-1": () => ({ body: { item: { ...item, body: "周五能参加吗？", replyTo: "teacher@example.com", status: answered ? "drafted" : "needs_input" }, thread: [], drafts: [], preparation: answered ? { ...preparation, id: "prep-2", decision: "draft", questions: [] } : preparation } }),
      "POST /api/mail/items/mail-1/answer": () => { answered = true; return { body: { job: { jobId: "answer-job" } } }; },
      "GET /api/mail/jobs/answer-job": () => ({ body: { job: { jobId: "answer-job", status: "completed", result: { status: "drafted" } } } }),
    });
    render(<MailSettings />);
    fireEvent.click(await screen.findByRole("button", { name: /面试邀请/ }));
    await screen.findByText("是否接受周五面试？");
    expect(screen.getByText("相关已发送邮件读取失败")).toBeTruthy();
    expect(screen.getByRole("link", { name: "面试计划" })).toHaveAttribute("href", "https://example.com/notes");
    fireEvent.change(screen.getByLabelText("补充答案"), { target: { value: "可以参加" } });
    fireEvent.click(screen.getByRole("button", { name: "补充并继续起草" }));
    await screen.findByText("补充已处理：草稿已创建", {}, { timeout: 3000 });
    expect(calls.find((call) => call.method === "POST" && call.path === "/api/mail/items/mail-1/answer")?.body).toMatchObject({ questionId: "prep-1", answer: "可以参加" });
  });

  it("opens sent source details without offering inbound processing actions", async () => {
    const status = { ...emptyStatus(), username: "owner@qq.com", verified: true, autoDraftReady: true, items: 1 };
    const inbound = { id: "inbound-1", threadId: "thread-1", sender: "teacher@example.com", subject: "面试邀请", receivedAt: "2026-10-20T09:00:00Z", status: "needs_input", needsReply: true, draftId: null, lastError: null };
    const sent = { id: "sent-1", threadId: "thread-sent", sender: "owner@qq.com", replyTo: "teacher@example.com", subject: "历史往来", receivedAt: "2026-10-19T09:00:00Z", body: "此前的回复", status: "sent", lastError: null, direction: "outbound" };
    const preparation = { id: "prep-1", decision: "needs_input", reason: "还需要主人决定", questions: ["是否接受周五面试？"], createdAt: "2026-10-20T09:01:00Z", model: "test", stale: false, warnings: [],
      sources: [{ kind: "sent_sample", id: "sent-1", title: "历史往来", sender: "owner@qq.com" }] };
    fakeBackend({
      ...commonRoutes(() => status),
      "GET /api/mail/items\\?limit=50&offset=0": () => ({ body: { items: [inbound] } }),
      "GET /api/mail/items/inbound-1": () => ({ body: { item: { ...inbound, body: "周五能参加吗？", replyTo: "teacher@example.com" }, thread: [], drafts: [], preparation } }),
      "GET /api/mail/items/sent-1": () => ({ body: { item: sent, thread: [sent], drafts: [] } }),
    });
    render(<MailSettings />);
    await screen.findByText("QQ 邮件");
    fireEvent.click(await screen.findByRole("button", { name: /面试邀请/ }));
    fireEvent.click(await screen.findByRole("button", { name: "历史往来" }));
    expect(await screen.findByText("此前的回复")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "处理邮件" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "处理整个线程" })).not.toBeInTheDocument();
  });
  it("requires the QQ web draft round-trip before marking the account ready", async () => {
    let status = emptyStatus();
    const { calls } = fakeBackend({
      ...commonRoutes(() => status),
      "PUT /api/mail/connection": () => {
        status = { ...status, candidate: { host: "imap.qq.com", port: 993, username: "owner@qq.com", connection_tested: false, test_draft_present: false, test_sent: false } };
        return { body: status };
      },
      "POST /api/mail/connection/test": () => {
        const request = calls.at(-1)?.body as { writeTest: boolean };
        status = { ...status, candidate: { ...status.candidate!, connection_tested: true, test_draft_present: request.writeTest, test_sent: false } };
        return { body: status };
      },
      "POST /api/mail/connection/confirm-test-send": () => {
        status = { ...status, username: "owner@qq.com", verified: true, autoDraftReady: true, candidate: { ...status.candidate!, test_sent: true } };
        return { body: status };
      },
    });

    render(<MailSettings />);
    fireEvent.change(await screen.findByLabelText("QQ 邮箱"), { target: { value: "owner@qq.com" } });
    fireEvent.change(screen.getByLabelText("邮箱授权码"), { target: { value: "qq-auth-code" } });
    fireEvent.click(screen.getByRole("button", { name: "保存待测连接" }));
    expect(await screen.findByText("连接信息已保存在系统凭据库，请继续测试")).toBeTruthy();
    expect(screen.getByLabelText("邮箱授权码")).toHaveValue("");

    fireEvent.click(screen.getByRole("button", { name: "测试文件夹访问" }));
    await screen.findByText("收件箱、已发送和草稿箱均可访问");
    fireEvent.click(screen.getByRole("button", { name: "写入测试草稿" }));
    await screen.findByText("测试草稿已写入 QQ 草稿箱");
    expect(await screen.findByRole("button", { name: "我已在 QQ 网页端修改并发送测试草稿" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "我已在 QQ 网页端修改并发送测试草稿" }));

    await screen.findByText("已验证 QQ 网页端可编辑和手动发送草稿。Meido 不会发送、删除、归档或移动邮件。");
    expect(calls.some((call) => call.method === "POST" && call.path === "/api/mail/connection/test" && (call.body as { writeTest: boolean }).writeTest)).toBe(true);
    expect(status.autoDraftReady).toBe(true);
  });

  it("waits for a persisted sync job and reports its final result", async () => {
    const status = { ...emptyStatus(), username: "owner@qq.com", verified: true };
    const { calls } = fakeBackend({
      ...commonRoutes(() => status),
      "POST /api/mail/sync": () => ({ body: { job: { jobId: "mail-job-1" } } }),
      "GET /api/mail/jobs/mail-job-1": () => ({ body: { job: { jobId: "mail-job-1", status: "completed", error: null, result: { discovered: 2, processed: 1, errors: [] } } } }),
    });

    render(<MailSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "立即同步" }));

    expect(await screen.findByText("同步完成：发现 2 封，处理 1 封，失败 0 封", {}, { timeout: 3000 })).toBeTruthy();
    expect(calls.some((call) => call.method === "GET" && call.path === "/api/mail/jobs/mail-job-1")).toBe(true);
  });
});
