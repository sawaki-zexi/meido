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

const commonRoutes = (getStatus: () => unknown, getStats: () => unknown = () => ({ total: 0, byStatus: {}, drafts: 0, unconfirmedAppends: 0, followups: {} })) => ({
  "GET /api/mail/preferences": () => ({ body: { writingStyle: "", roleAccess: false } }),
  "GET /api/mail/status": () => ({ body: getStatus() }),
  "GET /api/mail/items\\?limit=50&offset=0": () => ({ body: { items: [] } }),
  "GET /api/roles": () => ({ body: { roles: [] } }),
  "GET /api/mail/reminder-role": () => ({ body: { roleId: null } }),
  "GET /api/mail/reminder-batches\\?limit=8": () => ({ body: { batches: [] } }),
  "GET /api/mail/stats(?:\\?.*)?": () => ({ body: getStats() }),
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

  it("saves thread follow-up reminder settings", async () => {
    const status = { ...emptyStatus(), followup: { enabled: false, delayHours: 24, autoDraft: false } };
    const { calls } = fakeBackend({
      ...commonRoutes(() => status),
      "PUT /api/mail/followup": () => ({ body: calls.at(-1)?.body }),
    });
    render(<MailSettings />);
    fireEvent.click(await screen.findByLabelText("启用线程跟进提醒"));
    fireEvent.click(screen.getByLabelText("到期自动生成催复草稿"));
    fireEvent.change(screen.getByLabelText("提醒间隔（小时）"), { target: { value: "48" } });
    fireEvent.click(screen.getByRole("button", { name: "保存跟进设置" }));
    await screen.findByText("线程跟进设置已保存");
    expect(calls.find((call) => call.method === "PUT" && call.path === "/api/mail/followup")?.body).toEqual({ enabled: true, delayHours: 48, autoDraft: true });
  });

  it("filters the mail list by a sent association state with a localized label", async () => {
    const status = { ...emptyStatus(), username: "owner@qq.com", verified: true, autoDraftReady: true };
    const { calls } = fakeBackend({
      ...commonRoutes(() => status),
      "GET /api/mail/items\\?limit=50&offset=0": () => ({ body: { items: [], count: 0 } }),
      "GET /api/mail/items\\?limit=50&offset=0&status=sent_pending": () => ({ body: { items: [], count: 0 } }),
    });
    render(<MailSettings />);
    const filter = await screen.findByLabelText("筛选状态");
    expect(screen.getByRole("option", { name: "发送待确认" })).toBeInTheDocument();
    fireEvent.change(filter, { target: { value: "sent_pending" } });
    await screen.findByText("0 封");
    expect(calls.some((call) => call.path === "/api/mail/items?limit=50&offset=0&status=sent_pending")).toBe(true);
  });

  it("submits bounded mail search text and keeps the result count query scoped", async () => {
    const status = { ...emptyStatus(), username: "owner@qq.com", verified: true, autoDraftReady: true };
    const { calls } = fakeBackend({
      ...commonRoutes(() => status),
      "GET /api/mail/items\\?limit=50&offset=0&query=%E9%A1%B9%E7%9B%AE%20%E6%8E%92%E6%9C%9F": () => ({ body: { items: [], count: 0 } }),
    });
    render(<MailSettings />);
    fireEvent.change(await screen.findByRole("searchbox", { name: "搜索邮件" }), { target: { value: "项目 排期" } });
    fireEvent.click(screen.getByRole("button", { name: "搜索邮件" }));
    await screen.findByText("0 封");
    expect(calls.some((call) => call.path === "/api/mail/items?limit=50&offset=0&query=%E9%A1%B9%E7%9B%AE%20%E6%8E%92%E6%9C%9F")).toBe(true);
  });

  it("shows account-scoped thread actions and sends state and action filters", async () => {
    const status = { ...emptyStatus(), username: "owner@qq.com", verified: true, autoDraftReady: true };
    const action = { threadId: "thread-action", latestMailId: "mail-action", state: "to_reply", reason: "需要处理", nextAction: "review_or_reply", nextDueAt: "2026-11-05T23:59:59+00:00", latestEventAt: "2026-10-24T09:00:00Z", nextReminderAt: null, reminderEnabled: false, subject: "项目确认", sender: "teacher@example.com", recipients: "owner@qq.com", latestDirection: "inbound", latestStatus: "new", itemCount: 1, draftCount: 0, unconfirmedCount: 0, openQuestionCount: 1, commitmentCount: 0, needsInput: true, overdue: true, needsVerification: true };
    const { calls } = fakeBackend({
      ...commonRoutes(() => status),
      "GET /api/mail/threads\\?limit=50&offset=0": () => ({ body: { threads: [action], count: 1 } }),
      "GET /api/mail/threads\\?limit=50&offset=0&state=to_reply": () => ({ body: { threads: [action], count: 1 } }),
      "GET /api/mail/threads\\?limit=50&offset=0&state=to_reply&action=review_or_reply": () => ({ body: { threads: [action], count: 1 } }),
      "GET /api/mail/threads\\?limit=50&offset=0&state=to_reply&action=needs_input": () => ({ body: { threads: [action], count: 1 } }),
      "GET /api/mail/threads\\?limit=50&offset=0&state=to_reply&action=overdue": () => ({ body: { threads: [action], count: 1 } }),
      "GET /api/mail/threads\\?limit=50&offset=0&state=to_reply&action=needs_verification": () => ({ body: { threads: [action], count: 1 } }),
      "GET /api/mail/items/mail-action": () => ({ body: { item: { id: "mail-action", threadId: "thread-action", sender: "teacher@example.com", replyTo: "teacher@example.com", subject: "项目确认", receivedAt: "2026-10-24T09:00:00Z", body: "请确认", status: "new", lastError: null }, thread: [{ id: "mail-action", threadId: "thread-action", sender: "teacher@example.com", subject: "项目确认", receivedAt: "2026-10-24T09:00:00Z", body: "请确认", status: "new", direction: "inbound" }], drafts: [] } }),
    });
    render(<MailSettings />);
    fireEvent.click(await screen.findByRole("tab", { name: "线程行动" }));
    expect(await screen.findByText("项目确认")).toBeInTheDocument();
    expect(screen.getByText(/下一步：查看并决定是否回复/)).toBeInTheDocument();
    expect(screen.getByText(/截止/)).toBeInTheDocument();
    expect(screen.getByLabelText("线程行动列表")).toHaveTextContent("待补充");
    expect(screen.getByLabelText("线程行动列表")).toHaveTextContent("已到期");
    expect(screen.getByLabelText("线程行动列表")).toHaveTextContent("需核实");
    fireEvent.change(screen.getByLabelText("线程状态"), { target: { value: "to_reply" } });
    fireEvent.change(screen.getByLabelText("筛选下一步"), { target: { value: "review_or_reply" } });
    await screen.findByText("1 个线程");
    expect(calls.some((call) => call.path === "/api/mail/threads?limit=50&offset=0&state=to_reply&action=review_or_reply")).toBe(true);
    fireEvent.change(screen.getByLabelText("筛选下一步"), { target: { value: "needs_input" } });
    await screen.findByText("1 个线程");
    fireEvent.change(screen.getByLabelText("筛选下一步"), { target: { value: "overdue" } });
    await screen.findByText("1 个线程");
    fireEvent.change(screen.getByLabelText("筛选下一步"), { target: { value: "needs_verification" } });
    await screen.findByText("1 个线程");
    expect(calls.some((call) => call.path === "/api/mail/threads?limit=50&offset=0&state=to_reply&action=needs_input")).toBe(true);
    expect(calls.some((call) => call.path === "/api/mail/threads?limit=50&offset=0&state=to_reply&action=overdue")).toBe(true);
    expect(calls.some((call) => call.path === "/api/mail/threads?limit=50&offset=0&state=to_reply&action=needs_verification")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: /项目确认/ }));
    expect(await screen.findByText("请确认")).toBeInTheDocument();
  });

  it("shows account-scoped inbox summary counts", async () => {
    const status = { ...emptyStatus(), username: "owner@qq.com", verified: true, autoDraftReady: true };
    fakeBackend({
      ...commonRoutes(() => status, () => ({ total: 4, byStatus: { new: 2, needs_input: 1 }, drafts: 3, unconfirmedAppends: 1, followups: { to_reply: 1, awaiting_reply: 2 } })),
    });
    render(<MailSettings />);
    expect(await screen.findByLabelText("邮件统计")).toHaveTextContent("待处理 2");
    expect(screen.getByLabelText("邮件统计")).toHaveTextContent("待补充 1");
    expect(screen.getByLabelText("邮件统计")).toHaveTextContent("未核实写入 1");
    expect(screen.getByLabelText("邮件统计")).toHaveTextContent("跟进 3");
  });

  it("shows an in-flight reminder batch while delivery is claimed", async () => {
    const status = { ...emptyStatus(), username: "owner@qq.com", verified: true, autoDraftReady: true };
    fakeBackend({
      ...commonRoutes(() => status),
      "GET /api/mail/reminder-batches\\?limit=8": () => ({ body: { batches: [{ batchId: "batch-sending", status: "sending", failureCount: 0, failureSummaries: [], lastError: null, createdAt: "2026-10-24T09:00:00Z", deliveredAt: null }] } }),
    });
    render(<MailSettings />);
    expect(await screen.findByLabelText("邮件提醒批次")).toHaveTextContent("投递中");
  });

  it("shows the persisted question and sources, then submits the owner's answer", async () => {
    const status = { ...emptyStatus(), username: "owner@qq.com", verified: true, autoDraftReady: true, items: 1 };
    const item = { id: "mail-1", threadId: "thread-1", sender: "teacher@example.com", subject: "面试邀请", receivedAt: "2026-10-20T09:00:00Z", status: "needs_input", needsReply: true, draftId: null, lastError: null };
    const preparation = { id: "prep-1", mailId: "mail-1", decision: "needs_input", reason: "还需要主人决定", questions: ["是否接受周五面试？"], createdAt: "2026-10-20T09:01:00Z", model: "test", modelUsage: { inputTokens: 100, outputTokens: 20, cost: { total: 0.01 } }, stale: false,
      sources: [{ kind: "owner_knowledge", id: "note-1", title: "面试计划", location: "第二段", url: "https://example.com/notes" }], warnings: ["相关已发送邮件读取失败"] };
    let answered = false;
    const { calls } = fakeBackend({
      ...commonRoutes(() => status),
      "GET /api/mail/items\\?limit=50&offset=0": () => ({ body: { items: [item] } }),
      "GET /api/mail/items/mail-1": () => ({ body: { item: { ...item, body: "周五能参加吗？", replyTo: "teacher@example.com", status: answered ? "drafted" : "needs_input" }, thread: [{ ...item, body: "周五能参加吗？", direction: "inbound" }], drafts: [], followup: { threadId: "thread-1", state: "to_reply", reason: "最新来信仍需要主人处理或确认", reminderEnabled: false, analysis: { classifier: "signals-v2", nextAction: "review_or_reply", openQuestions: [{ eventId: "mail-1", kind: "inbound_question", owner: "owner", summary: "周五能参加吗？" }], commitments: [], evidenceEventIds: ["mail-1"] } }, threadDossier: { mailIds: ["mail-1"], latestMailId: "mail-1", drafts: [], preparations: [preparation], unconfirmedAppends: [] }, preparation: answered ? { ...preparation, id: "prep-2", decision: "draft", questions: [] } : preparation } }),
      "POST /api/mail/items/mail-1/answer": () => { answered = true; return { body: { job: { jobId: "answer-job" } } }; },
      "GET /api/mail/jobs/answer-job": () => ({ body: { job: { jobId: "answer-job", status: "completed", result: { status: "drafted" } } } }),
    });
    render(<MailSettings />);
    fireEvent.click(await screen.findByRole("button", { name: /面试邀请/ }));
    await screen.findByText("是否接受周五面试？");
    expect(screen.getByLabelText("线程处理记录")).toHaveTextContent("共 1 封邮件 · 0 个草稿版本 · 1 次判断");
    expect(screen.getByText("下一步：查看并决定是否回复")).toBeInTheDocument();
    expect(screen.getByText("未解决问题")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看依据邮件：面试邀请" }));
    await screen.findByText("周五能参加吗？");
    expect(calls.filter((call) => call.method === "GET" && call.path === "/api/mail/items/mail-1").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("相关已发送邮件读取失败")).toBeTruthy();
    expect(screen.getByText(/输入 100 tokens · 输出 20 tokens · 费用 0.01/)).toBeInTheDocument();
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

  it("shows stable revision numbers for appended draft versions", async () => {
    const status = { ...emptyStatus(), username: "owner@qq.com", verified: true, autoDraftReady: true, items: 1 };
    const item = { id: "mail-versioned", threadId: "thread-versioned", sender: "teacher@example.com", subject: "面试邀请", receivedAt: "2026-10-20T09:00:00Z", status: "drafted", needsReply: true, draftId: "draft-v2", lastError: null };
    fakeBackend({
      ...commonRoutes(() => status),
      "GET /api/mail/items\\?limit=50&offset=0": () => ({ body: { items: [item] } }),
      "GET /api/mail/items/mail-versioned": () => ({ body: {
        item: { ...item, body: "周五能参加吗？", replyTo: "teacher@example.com" }, thread: [], preparation: null,
        drafts: [{ id: "draft-v2", revision: 2, status: "created", stale: false, remoteMissing: false, body: "我可以参加。", subject: "Re: 面试邀请", recipients: "teacher@example.com", updatedAt: "2026-10-20T09:02:00Z" }],
      } }),
    });
    render(<MailSettings />);
    fireEvent.click(await screen.findByRole("button", { name: /面试邀请/ }));
    expect(await screen.findByText("版本 2 · QQ 草稿")).toBeInTheDocument();
  });

  it("distinguishes uncertain writes and requires confirmation before creating another version", async () => {
    const status = { ...emptyStatus(), username: "owner@qq.com", verified: true, autoDraftReady: true, items: 1 };
    const item = { id: "mail-unknown", threadId: "thread-unknown", sender: "teacher@example.com", subject: "面试邀请", receivedAt: "2026-10-20T09:00:00Z", status: "uncertain", needsReply: true, draftId: null, lastError: "草稿写入结果待核实" };
    let regenerateBody: unknown;
    const { calls } = fakeBackend({
      ...commonRoutes(() => status),
      "GET /api/mail/items\\?limit=50&offset=0": () => ({ body: { items: [item] } }),
      "GET /api/mail/items/mail-unknown": () => ({ body: { item, thread: [], drafts: [], preparation: null, unconfirmedAppend: true } }),
      "POST /api/mail/items/mail-unknown/retry": () => ({ body: { job: { jobId: "reconcile-job" } } }),
      "GET /api/mail/jobs/reconcile-job": () => ({ body: { job: { jobId: "reconcile-job", status: "completed", result: { status: "uncertain" } } } }),
      "POST /api/mail/items/mail-unknown/regenerate": () => {
        regenerateBody = calls.at(-1)?.body;
        return { body: { job: { jobId: "version-job" } } };
      },
      "GET /api/mail/jobs/version-job": () => ({ body: { job: { jobId: "version-job", status: "completed", result: { status: "drafted" } } } }),
    });

    render(<MailSettings />);
    fireEvent.click(await screen.findByRole("button", { name: /面试邀请/ }));
    fireEvent.click(await screen.findByRole("button", { name: "核对 QQ 草稿标记" }));
    expect(await screen.findByText("草稿结果仍待核实；系统没有重复追加，请核对 QQ 草稿箱。", {}, { timeout: 3000 })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "我已检查 QQ 草稿箱，仍创建新版本" }));
    fireEvent.click(screen.getByRole("button", { name: "确认创建独立草稿版本" }));
    expect(await screen.findByText("已创建新的 QQ 草稿，原草稿会保留", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(regenerateBody).toEqual({ requestId: expect.any(String), confirmUncertainNewVersion: true });
    expect(calls.some((call) => call.method === "POST" && call.path === "/api/mail/items/mail-unknown/regenerate")).toBe(true);
  });

  it("clears the selected detail when the active mailbox account changes", async () => {
    let account = "owner@qq.com";
    let status = { ...emptyStatus(), username: account, verified: true, autoDraftReady: true, items: 1 };
    const item = { id: "mail-account-switch", threadId: "thread-account-switch", sender: "teacher@example.com", subject: "旧账号邮件", receivedAt: "2026-10-20T09:00:00Z", status: "needs_input", needsReply: true, draftId: null, lastError: null };
    fakeBackend({
      ...commonRoutes(() => status),
      "GET /api/mail/items\\?limit=50&offset=0": () => ({ body: { items: account === "owner@qq.com" ? [item] : [], count: account === "owner@qq.com" ? 1 : 0 } }),
      "GET /api/mail/items/mail-account-switch": () => ({ body: { item: { ...item, body: "旧账号正文", replyTo: "teacher@example.com" }, thread: [{ ...item, body: "旧账号正文", direction: "inbound" }], drafts: [], preparation: null } }),
      "POST /api/mail/sync": () => {
        account = "other@qq.com";
        status = { ...status, username: account, items: 0 };
        return { body: { job: { jobId: "account-switch-job" } } };
      },
      "GET /api/mail/jobs/account-switch-job": () => ({ body: { job: { jobId: "account-switch-job", status: "completed", error: null, result: { status: "completed" } } } }),
    });

    render(<MailSettings />);
    fireEvent.click(await screen.findByRole("button", { name: /旧账号邮件/ }));
    expect(await screen.findByText("旧账号正文")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "立即同步" }));
    await screen.findByText("同步完成：发现 0 封，处理 0 封，失败 0 封", {}, { timeout: 3000 });
    expect(screen.queryByText("旧账号正文")).not.toBeInTheDocument();
  });

  it("collapses older thread messages and expands them on demand", async () => {
    const status = { ...emptyStatus(), username: "owner@qq.com", verified: true, autoDraftReady: true, items: 1 };
    const item = { id: "mail-long-8", threadId: "thread-long", sender: "teacher@example.com", subject: "长线程", receivedAt: "2026-10-24T09:00:00Z", status: "new", needsReply: true, draftId: null, lastError: null };
    const thread = Array.from({ length: 8 }, (_, index) => ({
      id: `mail-long-${index + 1}`,
      threadId: "thread-long",
      sender: index % 2 ? "owner@qq.com" : "teacher@example.com",
      subject: "长线程",
      receivedAt: `2026-10-${String(index + 1).padStart(2, "0")}T09:00:00Z`,
      body: `正文-${index + 1}`,
      status: index === 7 ? "new" : "sent",
      direction: index % 2 ? "outbound" : "inbound",
    }));
    fakeBackend({
      ...commonRoutes(() => status),
      "GET /api/mail/items\\?limit=50&offset=0": () => ({ body: { items: [item], count: 1 } }),
      "GET /api/mail/items/mail-long-8": () => ({ body: { item: { ...item, body: "正文-8", replyTo: "teacher@example.com" }, thread, drafts: [] } }),
    });
    render(<MailSettings />);
    fireEvent.click(await screen.findByText("长线程"));
    expect(await screen.findByRole("button", { name: "展开其余 2 封" })).toBeInTheDocument();
    expect(screen.queryByText("正文-1")).not.toBeInTheDocument();
    expect(screen.getByText("正文-8")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开其余 2 封" }));
    expect(screen.getByText("正文-1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "收起较早邮件" }));
    expect(screen.queryByText("正文-1")).not.toBeInTheDocument();
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

  it("lets the owner re-evaluate a no-reply decision", async () => {
    const status = { ...emptyStatus(), username: "owner@qq.com", verified: true, autoDraftReady: true, items: 1 };
    let regenerated = false;
    const item = { id: "mail-no-reply", threadId: "thread-no-reply", sender: "teacher@example.com", subject: "已处理", receivedAt: "2026-10-20T09:00:00Z", status: "no_reply", needsReply: false, draftId: null, lastError: null };
    const { calls } = fakeBackend({
      ...commonRoutes(() => status),
      "GET /api/mail/items\\?limit=50&offset=0": () => ({ body: { items: [item], count: 1 } }),
      "GET /api/mail/items/mail-no-reply": () => ({ body: { item: { ...item, status: regenerated ? "drafted" : "no_reply" }, thread: [], drafts: [], preparation: { id: "prep-no-reply", decision: regenerated ? "draft" : "no_reply", reason: "原判断", questions: [], createdAt: "2026-10-20T09:01:00Z", model: "test", stale: false, sources: [], warnings: [] } } }),
      "POST /api/mail/items/mail-no-reply/regenerate": () => { regenerated = true; return { body: { job: { jobId: "recheck-job" } } }; },
      "GET /api/mail/jobs/recheck-job": () => ({ body: { job: { jobId: "recheck-job", status: "completed", error: null, result: { status: "drafted" } } } }),
    });
    render(<MailSettings />);
    fireEvent.click(await screen.findByRole("button", { name: /已处理/ }));
    fireEvent.click(await screen.findByRole("button", { name: "重新判断邮件" }));
    await screen.findByText("已创建新的 QQ 草稿，原草稿会保留", {}, { timeout: 3000 });
    expect(calls.some((call) => call.method === "POST" && call.path === "/api/mail/items/mail-no-reply/regenerate")).toBe(true);
  });
});
