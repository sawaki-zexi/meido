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
  "GET /api/mail/status": () => ({ body: getStatus() }),
  "GET /api/mail/items\\?limit=50&offset=0": () => ({ body: { items: [] } }),
  "GET /api/roles": () => ({ body: { roles: [] } }),
  "GET /api/mail/reminder-role": () => ({ body: { roleId: null } }),
  "GET /api/mail/reminder-batches\\?limit=8": () => ({ body: { batches: [] } }),
});

describe("MailSettings", () => {
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
