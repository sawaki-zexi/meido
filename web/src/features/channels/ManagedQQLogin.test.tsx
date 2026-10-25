import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fakeBackend } from "../../test/fakeBackend";
import { ManagedQQLogin } from "./ManagedQQLogin";

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

const waiting = {
  ref: "phone-confirmation", available: true,
  preparation: { stage: "ready", percent: 100 },
  login: { phase: "login_required", loginPhase: "waiting_qrcode", qrcode: "data:image/png;base64,current", error: "", uin: "" },
};

describe("QQ 手机确认进度", () => {
  it("扫码后保留二维码并等待手机确认，初始化后继续检查到真实登录成功", async () => {
    vi.useFakeTimers();
    let session = waiting;
    const { calls } = fakeBackend({
      "POST /api/roles/r1/channels/qq/managed/begin": () => ({ body: session }),
      "GET /api/roles/r1/channels/qq/managed/phone-confirmation": () => ({ body: session }),
      "DELETE /api/roles/r1/channels/qq/managed/phone-confirmation": () => ({ status: 204 }),
    });
    render(<ManagedQQLogin roleId="r1" canConnect getRules={() => ({})} onConnected={() => undefined} />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "准备并扫码登录" })); });
    expect(screen.getByRole("img", { name: "QQ 登录二维码" })).toHaveAttribute("src", waiting.login.qrcode);
    session = { ...waiting, login: { ...waiting.login, loginPhase: "qrcode_scanned" } };
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(screen.getByText("二维码已扫描，请在手机 QQ 上确认登录…")).toBeVisible();
    expect(screen.getByRole("img", { name: "QQ 登录二维码" })).toHaveAttribute("src", waiting.login.qrcode);
    expect(screen.getByRole("button", { name: "刷新二维码" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "确认连接" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    session = { ...waiting, login: { ...waiting.login, loginPhase: "initializing" } };
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(screen.getByText("正在完成 QQ 登录，请稍候…")).toBeVisible();
    session = { ...waiting, login: { ...waiting.login, phase: "online", loginPhase: "ready", uin: "10001", qrcode: "" } };
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(screen.getByRole("button", { name: "确认连接" })).toBeEnabled();
    expect(calls.filter(call => call.method === "GET")).toHaveLength(3);
    expect(calls.some(call => call.path.endsWith("/refresh") || call.path.endsWith("/commit"))).toBe(false);
  });

  it("真正过期时显示原因、隐藏旧二维码并允许刷新，刷新后恢复扫码", async () => {
    const session = { ...waiting, login: { ...waiting.login, qrcode: "", loginPhase: "qrcode_scanned", error: "QQ 登录二维码已过期，请刷新后重试" } };
    const { calls } = fakeBackend({
      "POST /api/roles/r1/channels/qq/managed/begin": () => ({ body: session }),
      "POST /api/roles/r1/channels/qq/managed/phone-confirmation/refresh": () => ({ body: { ...waiting, login: { ...waiting.login, qrcode: "data:image/png;base64,new" } } }),
      "DELETE /api/roles/r1/channels/qq/managed/phone-confirmation": () => ({ status: 204 }),
    });
    const user = userEvent.setup();
    render(<ManagedQQLogin roleId="r1" canConnect getRules={() => ({})} onConnected={() => undefined} />);
    await user.click(screen.getByRole("button", { name: "准备并扫码登录" }));
    expect(screen.getByRole("alert")).toHaveTextContent(session.login.error);
    expect(screen.queryByRole("img", { name: "QQ 登录二维码" })).not.toBeInTheDocument();
    expect(screen.queryByText("二维码已扫描，请在手机 QQ 上确认登录…")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "刷新二维码" }));
    expect(screen.getByRole("img", { name: "QQ 登录二维码" })).toHaveAttribute("src", "data:image/png;base64,new");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(calls.filter(call => call.path.endsWith("/refresh"))).toHaveLength(1);
  });

  it.each(["qrcode_scanned", "initializing"])("%s 期间仍可取消本次登录", async (loginPhase) => {
    const { calls } = fakeBackend({
      "POST /api/roles/r1/channels/qq/managed/begin": () => ({ body: { ...waiting, login: { ...waiting.login, loginPhase } } }),
      "DELETE /api/roles/r1/channels/qq/managed/phone-confirmation": () => ({ status: 204 }),
    });
    const user = userEvent.setup();
    render(<ManagedQQLogin roleId="r1" canConnect getRules={() => ({})} onConnected={() => undefined} />);
    await user.click(screen.getByRole("button", { name: "准备并扫码登录" }));
    expect(screen.getByRole("button", { name: "刷新二维码" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "取消登录" }));
    expect(screen.getByRole("button", { name: "准备并扫码登录" })).toBeEnabled();
    expect(screen.queryByRole("img", { name: "QQ 登录二维码" })).not.toBeInTheDocument();
    expect(calls.filter(call => call.method === "DELETE")).toHaveLength(1);
  });

  it("已扫码阶段的真实失败仍显示错误并允许重试，保留可能有效的二维码", async () => {
    fakeBackend({
      "POST /api/roles/r1/channels/qq/managed/begin": () => ({ body: { ...waiting, login: { ...waiting.login, loginPhase: "qrcode_scanned", error: "QQ 登录遇到问题（错误码 45）" } } }),
      "DELETE /api/roles/r1/channels/qq/managed/phone-confirmation": () => ({ status: 204 }),
    });
    const user = userEvent.setup();
    render(<ManagedQQLogin roleId="r1" canConnect getRules={() => ({})} onConnected={() => undefined} />);
    await user.click(screen.getByRole("button", { name: "准备并扫码登录" }));
    expect(screen.getByRole("alert")).toHaveTextContent("错误码 45");
    expect(screen.getByRole("button", { name: "刷新二维码" })).toBeEnabled();
    expect(screen.getByRole("img", { name: "QQ 登录二维码" })).toHaveAttribute("src", waiting.login.qrcode);
    expect(screen.queryByText("二维码已扫描，请在手机 QQ 上确认登录…")).not.toBeInTheDocument();
  });

  it("等待确认时读取状态失败退出等待提示，保留刷新和读取重试并可恢复", async () => {
    vi.useFakeTimers();
    let reads = 0;
    const { calls } = fakeBackend({
      "POST /api/roles/r1/channels/qq/managed/begin": () => ({ body: { ...waiting, login: { ...waiting.login, loginPhase: "qrcode_scanned" } } }),
      "GET /api/roles/r1/channels/qq/managed/phone-confirmation": () => ++reads === 1
        ? { status: 503, body: { detail: "状态服务暂不可用" } }
        : { body: { ...waiting, login: { phase: "online", uin: "10001", qrcode: "", error: "" } } },
      "DELETE /api/roles/r1/channels/qq/managed/phone-confirmation": () => ({ status: 204 }),
    });
    render(<ManagedQQLogin roleId="r1" canConnect getRules={() => ({})} onConnected={() => undefined} />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "准备并扫码登录" })); });
    expect(screen.getByText("二维码已扫描，请在手机 QQ 上确认登录…")).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(screen.getByRole("alert")).toHaveTextContent("状态服务暂不可用");
    expect(screen.queryByText("二维码已扫描，请在手机 QQ 上确认登录…")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "刷新二维码" })).toBeEnabled();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "重试读取状态" })); });
    expect(screen.getByRole("button", { name: "确认连接" })).toBeEnabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(calls.some(call => call.path.endsWith("/refresh"))).toBe(false);
  });
});
