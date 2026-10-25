import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fakeBackend } from "../../test/fakeBackend";
import type { Account } from "./channelAccounts";
import { RoleAccountsPanel } from "./RoleAccountsPanel";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const account = (overrides: Partial<Account> = {}): Account => ({
  accountId: "tg-1", channel: "telegram", enabled: false, allowPrivate: true, allowGroup: true,
  blockedSenders: [], displayName: "Alice", platformId: "42", lastError: null, tokenConfigured: true,
  ...overrides,
});
const settings = (enabled = true) => ({ channels: ["telegram", "qq", "qqbot", "feishu"].map((channel) => ({
  channel, name: channel, enabled, configuredAccounts: 0, connectedAccounts: 0,
})) });

describe("角色账号", () => {
  it("工作区按参考顺序显示未添加账号，暂停的平台仍能进入添加入口", async () => {
    fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings(false) }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
    });
    const user = userEvent.setup();
    render(<RoleAccountsPanel roleId="r1" workspace />);
    const rows = await screen.findAllByRole('button', { name: /未添加/ });
    expect(rows.map(row => row.getAttribute('aria-label'))).toEqual([
      '飞书 / Lark 未添加', 'QQ 未添加', 'QQ 官方机器人 未添加', 'Telegram 未添加',
    ]);
    await user.click(rows[3]);
    const detail = await screen.findByRole('dialog', { name: 'Telegram 账号' });
    expect(within(detail).getByLabelText('Telegram Bot Token')).toBeVisible();
    expect(within(detail).getByRole('status')).toHaveTextContent('此平台已在全局设置中暂停');
  });

  it("无需先打开账号详情就能前往平台总开关", async () => {
    const openSettings = vi.fn();
    fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings() }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
    });
    render(<RoleAccountsPanel roleId="r1" onOpenSettings={openSettings} />);
    await userEvent.click(screen.getByRole("button", { name: "平台总开关" }));
    expect(openSettings).toHaveBeenCalledOnce();
  });

  it("列表只展示平台和状态，打开详情后创建并连接账号，再显示响应规则", async () => {
    let saved: Account | null = null;
    const { calls } = fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings() }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: saved ? [saved] : [] } }),
      "PUT /api/roles/r1/channels/telegram": () => {
        saved = account();
        return { body: { account: saved } };
      },
      "POST /api/roles/r1/channels/accounts/tg-1/state": () => {
        saved = account({ enabled: true, connectionStatus: "connected" });
        return { body: { account: saved } };
      },
    });
    const user = userEvent.setup();
    render(<RoleAccountsPanel roleId="r1" />);
    await screen.findByRole("button", { name: /Telegram.*未连接/ });
    expect(screen.queryByLabelText("Telegram Bot Token")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Telegram.*未连接/ }));
    const detail = screen.getByRole("dialog", { name: "Telegram 账号" });
    expect(within(detail).queryByLabelText("响应私聊")).not.toBeInTheDocument();
    await user.type(within(detail).getByLabelText("Telegram Bot Token"), "bot-token");
    await user.click(within(detail).getByRole("button", { name: "保存并连接" }));
    expect(await within(detail).findByLabelText("响应私聊")).toBeChecked();
    expect(within(detail).getByLabelText("响应群聊")).toBeChecked();
    expect(calls.find((call) => call.method === "PUT")?.body).toMatchObject({ token: "bot-token", enabled: false });
    expect(calls.find((call) => call.path.endsWith("/state"))?.body).toEqual({ enabled: true });
    expect(within(detail).queryByLabelText("Telegram Bot Token")).not.toBeVisible();
    await user.click(within(detail).getByRole("button", { name: "关闭" }));
    expect(screen.getByRole("button", { name: /Telegram.*已连接/ })).toBeInTheDocument();
  });

  it("QQ 新账号默认扫码连接，准备后展示二维码、刷新及取消，再确认连接", async () => {
    let saved: Account | null = null;
    const session = { ref: "login-1", available: true, preparation: { stage: "ready", percent: 100, version: "1" }, login: { phase: "login_required", qrcode: "data:image/png;base64,qr" } };
    const { calls } = fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings() }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: saved ? [saved] : [] } }),
      "POST /api/roles/r1/channels/qq/managed/begin": () => ({ body: session }),
      "POST /api/roles/r1/channels/qq/managed/login-1/refresh": () => ({ body: { ...session, login: { phase: "online", uin: "123456", nickname: "扫码角色" } } }),
      "POST /api/roles/r1/channels/qq/managed/login-1/commit": () => {
        saved = account({ channel: "qq", accountId: "qq-1", mode: "managed", enabled: true, connectionStatus: "connected", displayName: "扫码角色" });
        return { body: { account: saved } };
      },
    });
    const user = userEvent.setup();
    render(<RoleAccountsPanel roleId="r1" />);
    await user.click(await screen.findByRole("button", { name: /^QQ.*未连接/ }));
    const detail = screen.getByRole("dialog", { name: "QQ 账号" });
    expect(within(detail).queryByLabelText("NapCat WebSocket 地址")).not.toBeInTheDocument();
    await user.click(within(detail).getByRole("button", { name: "准备并扫码登录" }));
    expect(await within(detail).findByRole("img", { name: "QQ 登录二维码" })).toHaveAttribute("src", "data:image/png;base64,qr");
    expect(within(detail).getByRole("button", { name: "取消登录" })).toBeEnabled();
    await user.click(within(detail).getByRole("button", { name: "刷新二维码" }));
    expect(await within(detail).findByText("已登录：扫码角色（123456）")).toBeInTheDocument();
    await user.click(within(detail).getByRole("button", { name: "确认连接" }));
    expect(await within(detail).findByLabelText("响应私聊")).toBeChecked();
    expect(within(detail).queryByLabelText("NapCat WebSocket 地址")).not.toBeInTheDocument();
    expect(within(detail).getByLabelText("显示名称")).toHaveValue("扫码角色");
    expect(calls.some((call) => call.path.endsWith("/commit"))).toBe(true);
  });

  it.each([
    { channel: "qq", label: "QQ", field: "NapCat WebSocket 地址", value: "ws://localhost:6700", secret: "NapCat Access Token", body: { endpoint: "ws://localhost:6700", accessToken: "secret" } },
    { channel: "qqbot", label: "官方 QQBot", field: "QQ Bot App ID", value: "app-1", secret: "QQ Bot Client Secret", body: { appId: "app-1", clientSecret: "secret" } },
    { channel: "feishu", label: "飞书", field: "飞书 App ID", value: "app-1", secret: "飞书 App Secret", body: { appId: "app-1", appSecret: "secret" } },
  ])("$label 支持平台凭据配置并保留平台的群聊能力限制", async ({ channel, label, field, value, secret, body }) => {
    let saved: Account | null = null;
    const { calls } = fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings() }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: saved ? [saved] : [] } }),
      [`PUT /api/roles/r1/channels/${channel}`]: () => {
        saved = account({ channel: channel as Account["channel"], allowGroup: channel === "qq", connectionStatus: "disconnected" });
        return { body: { account: saved } };
      },
      "POST /api/roles/r1/channels/accounts/tg-1/state": () => {
        saved = { ...saved!, enabled: true, connectionStatus: "connected" };
        return { body: { account: saved } };
      },
    });
    const user = userEvent.setup();
    render(<RoleAccountsPanel roleId="r1" />);
    await user.click(await screen.findByRole("button", { name: `${label} 未连接` }));
    const detail = screen.getByRole("dialog", { name: `${label} 账号` });
    if (channel === "qq") await user.click(within(detail).getByText("高级：手动连接已有 NapCat"));
    await user.type(within(detail).getByLabelText(field), value);
    await user.type(within(detail).getByLabelText(secret), "secret");
    await user.click(within(detail).getByRole("button", { name: "保存并连接" }));
    expect(await within(detail).findByLabelText("响应私聊")).toBeEnabled();
    if (channel === "qq") expect(within(detail).getByLabelText("响应群聊")).toBeEnabled();
    else expect(within(detail).queryByLabelText(/响应群聊/)).not.toBeInTheDocument();
    expect(calls.find((call) => call.method === "PUT")?.body).toMatchObject(body);
  });

  it("已有连接凭据留空时保留，能保存响应规则、测试、断开及移除账号", async () => {
    let saved = account({ enabled: true, connectionStatus: "connected" });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const { calls } = fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings() }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [saved] } }),
      "PUT /api/roles/r1/channels/telegram": (init) => {
        saved = { ...saved, ...JSON.parse(String(init?.body)) };
        return { body: { account: saved } };
      },
      "POST /api/roles/r1/channels/telegram/tg-1/test": () => ({ body: { account: saved } }),
      "POST /api/roles/r1/channels/accounts/tg-1/state": () => {
        saved = { ...saved, enabled: false, connectionStatus: "disconnected" };
        return { body: { account: saved } };
      },
      "DELETE /api/roles/r1/channels/accounts/tg-1": () => ({ status: 204 }),
    });
    const user = userEvent.setup();
    render(<RoleAccountsPanel roleId="r1" />);
    await user.click(await screen.findByRole("button", { name: /Telegram.*已连接/ }));
    const detail = screen.getByRole("dialog", { name: "Telegram 账号" });
    await user.click(within(detail).getByText("更改连接配置"));
    expect(within(detail).getByLabelText("Telegram Bot Token")).toHaveValue("");
    await user.click(within(detail).getByRole("button", { name: "保存连接配置" }));
    expect(await within(detail).findByText("连接配置已保存")).toBeInTheDocument();
    await user.click(within(detail).getByLabelText("响应私聊"));
    await user.type(within(detail).getByLabelText("屏蔽发送者（ID 或平台别名）"), "123\n@spam");
    await user.click(within(detail).getByRole("button", { name: "保存响应规则" }));
    expect(await within(detail).findByText("响应规则已保存")).toBeInTheDocument();
    expect(calls.filter((call) => call.method === "PUT").at(-1)?.body).toMatchObject({ allowPrivate: false, blockedSenders: ["123", "@spam"] });
    expect(calls.filter((call) => call.method === "PUT").every((call) => !("token" in (call.body as Record<string, unknown>)))).toBe(true);
    await user.click(within(detail).getByRole("button", { name: "测试连接" }));
    expect(await within(detail).findByText("连接测试通过")).toBeInTheDocument();
    await user.click(within(detail).getByRole("button", { name: "断开连接" }));
    expect(await within(detail).findByRole("button", { name: "连接" })).toBeEnabled();
    expect(within(detail).queryByLabelText("响应私聊")).not.toBeInTheDocument();
    await user.click(within(detail).getByRole("button", { name: "移除账号" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Telegram 未连接" })).toBeInTheDocument();
    confirm.mockRestore();
  });

  it("全局暂停后账号仍可查看和移除，并引导到设置，不能尝试连接", async () => {
    const openSettings = vi.fn();
    const { calls } = fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings(false) }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [account({ platformEnabled: false })] } }),
    });
    const user = userEvent.setup();
    render(<RoleAccountsPanel roleId="r1" onOpenSettings={openSettings} />);
    await user.click(await screen.findByRole("button", { name: /Telegram.*平台已暂停/ }));
    const detail = screen.getByRole("dialog", { name: "Telegram 账号" });
    expect(within(detail).getByRole("button", { name: "连接" })).toBeDisabled();
    expect(within(detail).getByRole("button", { name: "测试连接" })).toBeDisabled();
    expect(within(detail).getByRole("button", { name: "移除账号" })).toBeEnabled();
    await user.click(within(detail).getByRole("button", { name: "打开渠道设置" }));
    expect(openSettings).toHaveBeenCalledOnce();
    expect(calls.every((call) => call.method === "GET")).toBe(true);
  });

  it("切换角色时关闭旧详情、清空凭据，并忽略旧请求的迟到结果", async () => {
    let resolveSave!: (value: { body: { account: Account } }) => void;
    const delayed = new Promise<{ body: { account: Account } }>((resolve) => { resolveSave = resolve; });
    const { calls } = fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings() }),
      "GET /api/roles/(r1|r2)/channels/accounts": () => ({ body: { accounts: [] } }),
      "PUT /api/roles/r1/channels/telegram": () => delayed,
    });
    const user = userEvent.setup();
    const view = render(<RoleAccountsPanel roleId="r1" />);
    await user.click(await screen.findByRole("button", { name: "Telegram 未连接" }));
    await user.type(screen.getByLabelText("Telegram Bot Token"), "old-secret");
    await user.click(screen.getByRole("button", { name: "保存并连接" }));
    view.rerender(<RoleAccountsPanel roleId="r2" />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await act(async () => resolveSave({ body: { account: account({ displayName: "旧角色账号" }) } }));
    await user.click(await screen.findByRole("button", { name: "Telegram 未连接" }));
    expect(screen.getByLabelText("Telegram Bot Token")).toHaveValue("");
    expect(screen.queryByText("旧角色账号")).not.toBeInTheDocument();
    expect(calls.some((call) => call.path.endsWith("/state"))).toBe(false);
  });

  it("连接失败保留账号和错误，可重试恢复在线", async () => {
    let saved: Account | null = null;
    let attempts = 0;
    fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings() }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: saved ? [saved] : [] } }),
      "PUT /api/roles/r1/channels/telegram": () => { saved = account(); return { body: { account: saved } }; },
      "POST /api/roles/r1/channels/accounts/tg-1/state": () => {
        attempts += 1;
        if (attempts === 1) return { status: 502, body: { detail: "网络连接失败，请重试" } };
        saved = { ...saved!, enabled: true, connectionStatus: "connected" };
        return { body: { account: saved } };
      },
    });
    const user = userEvent.setup();
    render(<RoleAccountsPanel roleId="r1" />);
    await user.click(await screen.findByRole("button", { name: "Telegram 未连接" }));
    await user.type(screen.getByLabelText("Telegram Bot Token"), "token");
    await user.click(screen.getByRole("button", { name: "保存并连接" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("网络连接失败，请重试");
    expect(screen.getByRole("button", { name: "移除账号" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "连接" }));
    expect(await screen.findByLabelText("响应私聊")).toBeChecked();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("QQ 准备失败可重新准备，并可取消本次登录", async () => {
    const session = { ref: "temp", available: true, preparation: { stage: "error", percent: 0, error: "组件下载失败" }, login: { phase: "stopped" } };
    let attempts = 0;
    const { calls } = fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings() }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "POST /api/roles/r1/channels/qq/managed/begin": () => ({ body: ++attempts === 1 ? session : { ...session, ref: "retry", preparation: { stage: "ready", percent: 100 }, login: { phase: "login_required", qrcode: "data:image/png;base64,retry" } } }),
      "DELETE /api/roles/r1/channels/qq/managed/temp": () => ({ status: 204 }),
      "DELETE /api/roles/r1/channels/qq/managed/retry": () => ({ status: 204 }),
    });
    const user = userEvent.setup();
    render(<RoleAccountsPanel roleId="r1" />);
    await user.click(await screen.findByRole("button", { name: "QQ 未连接" }));
    await user.click(screen.getByRole("button", { name: "准备并扫码登录" }));
    expect(await screen.findByText("组件下载失败")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试登录" }));
    expect(await screen.findByRole("img", { name: "QQ 登录二维码" })).toHaveAttribute("src", "data:image/png;base64,retry");
    await user.click(screen.getByRole("button", { name: "取消登录" }));
    expect(await screen.findByRole("button", { name: "准备并扫码登录" })).toBeEnabled();
    expect(screen.queryByRole("img", { name: "QQ 登录二维码" })).not.toBeInTheDocument();
    expect(calls.filter((call) => call.method === "DELETE")).toHaveLength(2);
    expect(calls.some((call) => call.path.endsWith("/commit"))).toBe(false);
  });

  it("QQ 准备请求尚未返回时关闭详情，迟到的临时连接也会取消", async () => {
    let resolveBegin!: (value: { body: unknown }) => void;
    const begin = new Promise<{ body: unknown }>((resolve) => { resolveBegin = resolve; });
    const { calls } = fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings() }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "POST /api/roles/r1/channels/qq/managed/begin": () => begin,
      "DELETE /api/roles/r1/channels/qq/managed/late": () => ({ status: 204 }),
    });
    const user = userEvent.setup();
    render(<RoleAccountsPanel roleId="r1" />);
    await user.click(await screen.findByRole("button", { name: "QQ 未连接" }));
    await user.click(screen.getByRole("button", { name: "准备并扫码登录" }));
    await user.click(screen.getByRole("button", { name: "关闭" }));
    await act(async () => resolveBegin({ body: { ref: "late", available: true, preparation: { stage: "ready", percent: 100 }, login: { phase: "login_required" } } }));
    await waitFor(() => expect(calls.some((call) => call.method === "DELETE" && call.path.endsWith("/late"))).toBe(true));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("既有托管 QQ 重新扫码时读取自己的登录状态，关闭详情不删除已保存连接", async () => {
    const saved = account({ channel: "qq", mode: "managed", managedRef: "saved", enabled: true, connectionStatus: "error", lastError: "登录已过期" });
    const { calls } = fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings() }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [saved] } }),
      "GET /api/roles/r1/channels/qq/managed/saved": () => ({ body: { ref: "saved", available: true, preparation: { stage: "ready", percent: 100 }, login: { phase: "login_required", qrcode: "data:image/png;base64,saved" } } }),
      "POST /api/roles/r1/channels/qq/managed/saved/refresh": () => ({ body: { ref: "saved", available: true, preparation: { stage: "ready", percent: 100 }, login: { phase: "online", uin: "123456", nickname: "Alice" } } }),
      "POST /api/roles/r1/channels/accounts/tg-1/state": () => ({ body: { account: { ...saved, connectionStatus: "connected", lastError: null } } }),
    });
    const user = userEvent.setup();
    render(<RoleAccountsPanel roleId="r1" />);
    await user.click(await screen.findByRole("button", { name: /QQ.*连接异常/ }));
    expect(await screen.findByRole("img", { name: "QQ 登录二维码" })).toHaveAttribute("src", "data:image/png;base64,saved");
    expect(screen.queryByLabelText("NapCat WebSocket 地址")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "关闭" }));
    await user.click(screen.getByRole("button", { name: /QQ.*连接异常/ }));
    await user.click(await screen.findByRole("button", { name: "刷新二维码" }));
    await user.click(await screen.findByRole("button", { name: "确认连接" }));
    expect(await screen.findByLabelText("响应私聊")).toBeInTheDocument();
    expect(calls.find((call) => call.path.endsWith("/state"))?.body).toEqual({ enabled: true });
    expect(calls.some((call) => call.method === "DELETE" || call.path.endsWith("/begin"))).toBe(false);
    expect(calls.some((call) => call.path.endsWith("/commit"))).toBe(false);
  });

  it("QQ 准备期间平台被全局暂停时，迟到的临时连接会被取消", async () => {
    let source!: EventTarget;
    vi.stubGlobal("EventSource", class extends EventTarget { constructor() { super(); source = this; } close() {} });
    let enabled = true;
    let resolveBegin!: (value: { body: unknown }) => void;
    const begin = new Promise<{ body: unknown }>((resolve) => { resolveBegin = resolve; });
    const { calls } = fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings(enabled) }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "POST /api/roles/r1/channels/qq/managed/begin": () => begin,
      "DELETE /api/roles/r1/channels/qq/managed/paused": () => ({ status: 204 }),
    });
    const user = userEvent.setup();
    render(<RoleAccountsPanel roleId="r1" />);
    await user.click(await screen.findByRole("button", { name: "QQ 未连接" }));
    await user.click(screen.getByRole("button", { name: "准备并扫码登录" }));
    enabled = false;
    act(() => source.dispatchEvent(new Event("channel_update")));
    expect(await screen.findByText("此平台已在全局设置中暂停。启用后才能连接账号。")).toBeInTheDocument();
    await act(async () => resolveBegin({ body: { ref: "paused", available: true, preparation: { stage: "ready", percent: 100 }, login: { phase: "login_required", qrcode: "data:image/png;base64,paused" } } }));
    await waitFor(() => expect(calls.some((call) => call.method === "DELETE" && call.path.endsWith("/paused"))).toBe(true));
    expect(screen.queryByRole("img", { name: "QQ 登录二维码" })).not.toBeInTheDocument();
  });

  it("暂停账号修改凭据时保持暂停，保存后可另行连接", async () => {
    const saved = account();
    const { calls } = fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings() }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [saved] } }),
      "PUT /api/roles/r1/channels/telegram": () => ({ body: { account: saved } }),
    });
    const user = userEvent.setup();
    render(<RoleAccountsPanel roleId="r1" />);
    await user.click(await screen.findByRole("button", { name: /Telegram.*已暂停/ }));
    await user.click(screen.getByText("更改连接配置"));
    await user.type(screen.getByLabelText("Telegram Bot Token"), "replacement");
    await user.click(screen.getByRole("button", { name: "保存连接配置" }));
    expect(await screen.findByText("连接配置已保存")).toBeInTheDocument();
    expect(calls.some((call) => call.path.endsWith("/state"))).toBe(false);
    expect(screen.getByRole("button", { name: "连接" })).toBeEnabled();
  });

  it("QQ 组件准备过程中自动更新进度并显示登录二维码，关闭会取消临时登录", async () => {
    const { calls } = fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings() }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [] } }),
      "POST /api/roles/r1/channels/qq/managed/begin": () => ({ body: { ref: "preparing", available: true, preparation: { stage: "downloading", percent: 20 }, login: { phase: "stopped" } } }),
      "GET /api/roles/r1/channels/qq/managed/preparing": () => ({ body: { ref: "preparing", available: true, preparation: { stage: "ready", percent: 100 }, login: { phase: "login_required", qrcode: "data:image/png;base64,ready" } } }),
      "DELETE /api/roles/r1/channels/qq/managed/preparing": () => ({ status: 204 }),
    });
    const user = userEvent.setup();
    render(<RoleAccountsPanel roleId="r1" />);
    await user.click(await screen.findByRole("button", { name: "QQ 未连接" }));
    await user.click(screen.getByRole("button", { name: "准备并扫码登录" }));
    expect(await screen.findByRole("progressbar", { name: "QQ 连接组件准备进度" })).toHaveAttribute("value", "20");
    expect(await screen.findByRole("img", { name: "QQ 登录二维码" }, { timeout: 3000 })).toHaveAttribute("src", "data:image/png;base64,ready");
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "关闭" }));
    await waitFor(() => expect(calls.filter((call) => call.method === "DELETE")).toHaveLength(1));
  });

  it("已停止的托管 QQ 通过连接按钮启动，再自动显示重新登录二维码", async () => {
    const saved = account({ channel: "qq", mode: "managed", managedRef: "existing", enabled: false, connectionStatus: "disconnected" });
    let started = false;
    const { calls } = fakeBackend({
      "GET /api/channels/settings": () => ({ body: settings() }),
      "GET /api/roles/r1/channels/accounts": () => ({ body: { accounts: [saved] } }),
      "GET /api/roles/r1/channels/qq/managed/existing": () => ({ body: { ref: "existing", available: true, preparation: { stage: "ready", percent: 100 }, login: started ? { phase: "login_required", qrcode: "data:image/png;base64,relogin" } : { phase: "stopped" } } }),
      "POST /api/roles/r1/channels/accounts/tg-1/state": (init) => { started = JSON.parse(String(init?.body)).enabled; return { body: { account: { ...saved, enabled: started, connectionStatus: started ? "connecting" : "disconnected" } } }; },
    });
    const user = userEvent.setup();
    render(<RoleAccountsPanel roleId="r1" />);
    await user.click(await screen.findByRole("button", { name: /QQ.*已暂停/ }));
    expect(await screen.findByText("点击连接以启动 QQ 扫码登录。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "刷新二维码" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "连接" }));
    expect(await screen.findByRole("img", { name: "QQ 登录二维码" }, { timeout: 3000 })).toHaveAttribute("src", "data:image/png;base64,relogin");
    await user.click(screen.getByRole("button", { name: "断开连接" }));
    expect(await screen.findByText("点击连接以启动 QQ 扫码登录。")).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: "QQ 登录二维码" })).not.toBeInTheDocument();
    expect(calls.filter((call) => call.path.endsWith("/state")).at(-1)?.body).toEqual({ enabled: false });
    await user.click(screen.getByRole("button", { name: "关闭" }));
    expect(calls.some((call) => call.method === "DELETE" || call.path.endsWith("/begin"))).toBe(false);
  });
});
