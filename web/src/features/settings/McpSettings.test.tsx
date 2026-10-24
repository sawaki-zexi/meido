import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fakeBackend, role } from "../../test/fakeBackend";
import { SettingsDialog } from "./SettingsDialog";
import { McpSettings } from "./McpSettings";
import type { McpServer } from "../../api/mcp";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const configuredServer = (): McpServer => ({ serverId: "notes", name: "本地笔记", command: ["D:\\Python\\python.exe", "D:\\MCP\\server.py"], cwd: "D:\\MCP", envKeys: ["ACCESS_TOKEN", "OLD_TOKEN"], roleIds: ["r1"], enabled: true, risk: "external", timeoutSeconds: 30, maxOutputChars: 20000, revision: "v1", connectionCount: 2, activeProcesses: 1, errors: [] });

describe("MCP 服务设置", () => {
  it("lets the owner save fixed launch parameters, credentials and an explicit role grant without connecting", async () => {
    let servers: Record<string, unknown>[] = [];
    const { calls } = fakeBackend({
      "GET /api/mcp/servers": () => ({ body: { servers } }),
      "GET /api/roles": () => ({ body: { roles: [role({ id: "r1", name: "诗织", agentConfig: { enabledTools: ["time_now"], shell: { enabled: true, allowedCommands: ["git"], timeoutSeconds: 20, maxOutputChars: 3000 } } })] } }),
      "POST /api/mcp/servers": (init) => {
        const data = JSON.parse(String(init?.body));
        const { env, enableDiscovery: _discovery, ...configuration } = data;
        servers = [{ ...configuration, envKeys: Object.keys(env), revision: "v1", connectionCount: 0, activeProcesses: 0, errors: [] }];
        return { status: 201, body: { server: servers[0] } };
      },
    });
    const user = userEvent.setup();
    render(<SettingsDialog onClose={() => {}} initialSection="mcp" />);
    await user.click(await screen.findByRole("button", { name: "添加 MCP 服务" }));
    await user.type(screen.getByLabelText("服务 ID"), "notes");
    await user.type(screen.getByLabelText("服务名称"), "本地笔记");
    await user.type(screen.getByLabelText("可执行文件（绝对路径）"), "D:\\Python\\python.exe");
    await user.type(screen.getByLabelText("参数（每行一个）"), "D:\\MCP 目录\\server.py{enter}--stdio");
    await user.type(screen.getByLabelText("工作目录（绝对路径）"), "D:\\MCP 目录");
    await user.click(screen.getByRole("button", { name: "添加环境变量" }));
    await user.type(screen.getByLabelText("环境变量名称 1"), "ACCESS_TOKEN");
    await user.type(screen.getByLabelText("环境变量值 1"), "owner-secret");
    await user.click(screen.getByRole("checkbox", { name: "授权 诗织" }));
    await user.selectOptions(screen.getByLabelText("远端工具风险"), "read_only");
    expect(screen.getByRole("checkbox", { name: /同时为授权角色开启 tool_search 和 mcp_add、mcp_list、mcp_remove/ })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "保存 MCP 配置" }));
    expect(await screen.findByText("MCP 配置已保存；尚未启动服务。" )).toBeInTheDocument();
    const card = within(screen.getByRole("region", { name: "服务 本地笔记" }));
    expect(card.getByText("运行中连接：0")).toBeInTheDocument();
    expect(card.getByText("已保存连接意愿：0")).toBeInTheDocument();
    expect(calls.find((call) => call.method === "POST")?.body).toEqual({
      serverId: "notes", name: "本地笔记", command: ["D:\\Python\\python.exe", "D:\\MCP 目录\\server.py", "--stdio"], cwd: "D:\\MCP 目录",
      env: { ACCESS_TOKEN: "owner-secret" }, roleIds: ["r1"], enabled: true, risk: "read_only", timeoutSeconds: 30, maxOutputChars: 20000, enableDiscovery: true,
    });
    expect(calls.filter((call) => call.method !== "GET")).toHaveLength(1);
    expect(screen.queryByDisplayValue("owner-secret")).not.toBeInTheDocument();
  });

  it("edits persisted configuration without revealing credentials and replaces the authorized roles", async () => {
    let server = configuredServer();
    const { calls } = fakeBackend({
      "GET /api/mcp/servers": () => ({ body: { servers: [server] } }),
      "GET /api/roles": () => ({ body: { roles: [role({ id: "r1", name: "诗织" }), role({ id: "r2", name: "澪" })] } }),
      "PUT /api/mcp/servers/notes": (init) => {
        const { env, enableDiscovery: _discovery, ...configuration } = JSON.parse(String(init?.body));
        server = { ...configuration, envKeys: Object.keys(env), revision: "v2", connectionCount: 0, activeProcesses: 0, errors: [] };
        return { body: { server } };
      },
    });
    const user = userEvent.setup();
    render(<McpSettings />);
    await user.click(await screen.findByRole("button", { name: "编辑 本地笔记" }));
    expect(screen.getByLabelText("服务 ID")).toBeDisabled();
    expect(screen.getByLabelText("环境变量值 1")).toHaveValue("");
    expect(screen.getByLabelText("环境变量值 1")).toHaveAttribute("placeholder", "已配置；留空保留原值");
    await user.clear(screen.getByLabelText("服务名称"));
    await user.type(screen.getByLabelText("服务名称"), "新笔记");
    await user.click(screen.getByRole("button", { name: "移除环境变量 2" }));
    await user.click(screen.getByRole("button", { name: "添加环境变量" }));
    await user.type(screen.getByLabelText("环境变量名称 2"), "NEW_TOKEN");
    await user.type(screen.getByLabelText("环境变量值 2"), "replacement-secret");
    await user.click(screen.getByRole("checkbox", { name: "授权 诗织" }));
    await user.click(screen.getByRole("checkbox", { name: "授权 澪" }));
    await user.selectOptions(screen.getByLabelText("远端工具风险"), "mutating");
    await user.clear(screen.getByLabelText("超时（秒）"));
    await user.type(screen.getByLabelText("超时（秒）"), "45");
    await user.clear(screen.getByLabelText("输出字符上限"));
    await user.type(screen.getByLabelText("输出字符上限"), "4096");
    expect(screen.getByText("修改后断开现有连接，需重新连接。" )).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "保存 MCP 配置" }));
    expect(await screen.findByRole("region", { name: "服务 新笔记" })).toHaveTextContent("授权角色：澪");
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({
      serverId: "notes", name: "新笔记", command: ["D:\\Python\\python.exe", "D:\\MCP\\server.py"], cwd: "D:\\MCP", env: { ACCESS_TOKEN: "", NEW_TOKEN: "replacement-secret" }, roleIds: ["r2"], enabled: true, risk: "mutating", timeoutSeconds: 45, maxOutputChars: 4096, enableDiscovery: true,
    });
    await user.click(screen.getByRole("button", { name: "编辑 新笔记" }));
    expect(screen.getByLabelText("环境变量名称 2")).toHaveValue("NEW_TOKEN");
    expect(screen.getByLabelText("环境变量值 2")).toHaveValue("");
    expect(calls.some((call) => call.path.includes("reveal"))).toBe(false);
  });

  it("explains why a role cannot connect when discovery enabling is declined without changing existing role settings", async () => {
    const server = configuredServer();
    const { calls } = fakeBackend({
      "GET /api/mcp/servers": () => ({ body: { servers: [server] } }),
      "GET /api/roles": () => ({ body: { roles: [
        role({ id: "r1", name: "诗织", agentConfig: { enabledTools: ["time_now"] } }),
        role({ id: "r2", name: "澪", agentConfig: { enabledTools: ["tool_search", "mcp_add", "mcp_list", "mcp_remove"] } }),
      ] } }),
      "PUT /api/mcp/servers/notes": () => ({ body: { server } }),
    });
    const user = userEvent.setup();
    render(<McpSettings />);
    await user.click(await screen.findByRole("button", { name: "编辑 本地笔记" }));
    await user.click(screen.getByRole("checkbox", { name: "授权 澪" }));
    await user.click(screen.getByRole("checkbox", { name: /同时为授权角色开启 tool_search/ }));
    expect(screen.getByText("诗织 尚未开启 tool_search、mcp_add、mcp_list、mcp_remove；需在角色设置中开启后才能使用对应的发现或连接管理操作。" )).toBeInTheDocument();
    expect(screen.queryByText(/澪 尚未开启/)).not.toBeInTheDocument();
    expect(screen.getByText("不会撤销已有工具权限。" )).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "保存 MCP 配置" }));
    await screen.findByText("MCP 配置已保存；连接已撤销，请在对话中重新连接。");
    expect(calls.find((call) => call.method === "PUT")?.body).toMatchObject({ roleIds: ["r1", "r2"], enableDiscovery: false });
    expect(calls.some((call) => call.method !== "GET" && call.path.startsWith("/api/roles"))).toBe(false);
  });

  it("revokes all role grants and deletes a service only after the owner confirms deletion", async () => {
    let servers = [configuredServer()];
    const { calls } = fakeBackend({
      "GET /api/mcp/servers": () => ({ body: { servers } }),
      "GET /api/roles": () => ({ body: { roles: [role({ id: "r1", name: "诗织" })] } }),
      "PUT /api/mcp/servers/notes": () => {
        servers = [{ ...servers[0], roleIds: [], revision: "v2", connectionCount: 0, activeProcesses: 0 }];
        return { body: { server: servers[0] } };
      },
      "DELETE /api/mcp/servers/notes": () => { servers = []; return { status: 204 }; },
    });
    const user = userEvent.setup();
    render(<McpSettings />);
    await user.click(await screen.findByRole("button", { name: "撤销 本地笔记 的全部角色授权" }));
    expect(await screen.findByText("已撤销该服务的全部角色授权并断开连接。" )).toBeInTheDocument();
    const card = within(screen.getByRole("region", { name: "服务 本地笔记" }));
    expect(card.getByText("授权角色：无")).toBeInTheDocument();
    expect(card.getByText("运行中连接：0")).toBeInTheDocument();
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({
      serverId: "notes", name: "本地笔记", command: ["D:\\Python\\python.exe", "D:\\MCP\\server.py"], cwd: "D:\\MCP", env: { ACCESS_TOKEN: "", OLD_TOKEN: "" }, roleIds: [], enabled: true, risk: "external", timeoutSeconds: 30, maxOutputChars: 20000, enableDiscovery: false,
    });
    await user.click(card.getByRole("button", { name: "删除 本地笔记" }));
    expect(calls.some((call) => call.method === "DELETE")).toBe(false);
    await user.click(card.getByRole("button", { name: "取消删除" }));
    expect(card.queryByRole("button", { name: "确认删除 本地笔记" })).not.toBeInTheDocument();
    await user.click(card.getByRole("button", { name: "删除 本地笔记" }));
    await user.click(card.getByRole("button", { name: "确认删除 本地笔记" }));
    expect(await screen.findByText("MCP 服务及凭据已删除。" )).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "服务 本地笔记" })).not.toBeInTheDocument();
  });

  it("refreshes real connection counts and errors and marks old results when status cannot be read", async () => {
    let server = configuredServer();
    let unavailable = false;
    const { calls } = fakeBackend({
      "GET /api/mcp/servers": () => unavailable ? { status: 503, body: { detail: "MCP 状态读取暂不可用" } } : { body: { servers: [server] } },
      "GET /api/roles": () => ({ body: { roles: [role({ id: "r1", name: "诗织" })] } }),
    });
    const user = userEvent.setup();
    render(<McpSettings />);
    expect(await screen.findByText("运行中连接：1")).toBeInTheDocument();
    server = { ...server, activeProcesses: 0, errors: ["MCP handshake timed out"] };
    await user.click(screen.getByRole("button", { name: "刷新状态" }));
    expect(await screen.findByText("运行中连接：0")).toBeInTheDocument();
    expect(screen.getByText("已保存连接意愿：2")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("MCP handshake timed out");
    unavailable = true;
    await user.click(screen.getByRole("button", { name: "刷新状态" }));
    expect(await screen.findByText("MCP 状态读取暂不可用")).toBeInTheDocument();
    expect(screen.getByText("状态读取失败；下方为上次读取结果。" )).toBeInTheDocument();
    unavailable = false;
    server = { ...server, activeProcesses: 2, connectionCount: 3, errors: [] };
    await user.click(screen.getByRole("button", { name: "刷新状态" }));
    expect(await screen.findByText("运行中连接：2")).toBeInTheDocument();
    expect(screen.getByText("已保存连接意愿：3")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText("状态读取失败；下方为上次读取结果。" )).not.toBeInTheDocument();
    expect(calls.every((call) => call.method === "GET")).toBe(true);
  });

  it("prevents duplicate environment names from silently replacing a credential", async () => {
    const { calls } = fakeBackend({
      "GET /api/mcp/servers": () => ({ body: { servers: [configuredServer()] } }),
      "GET /api/roles": () => ({ body: { roles: [] } }),
      "PUT /api/mcp/servers/notes": () => ({ body: { server: configuredServer() } }),
    });
    const user = userEvent.setup();
    render(<McpSettings />);
    await user.click(await screen.findByRole("button", { name: "编辑 本地笔记" }));
    await user.clear(screen.getByLabelText("环境变量名称 2"));
    await user.type(screen.getByLabelText("环境变量名称 2"), "ACCESS_TOKEN");
    await user.click(screen.getByRole("button", { name: "保存 MCP 配置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("环境变量名称不能重复");
    expect(calls.some((call) => call.method === "PUT")).toBe(false);
    expect(screen.getByRole("form", { name: "MCP 配置" })).toBeInTheDocument();
  });

  it("keeps a rejected edit available for retry and clears secret input after a successful save", async () => {
    let attempts = 0;
    const server = configuredServer();
    const { calls } = fakeBackend({
      "GET /api/mcp/servers": () => ({ body: { servers: [server] } }),
      "GET /api/roles": () => ({ body: { roles: [role({ id: "r1", name: "诗织" })] } }),
      "PUT /api/mcp/servers/notes": () => ++attempts === 1
        ? { status: 503, body: { detail: "MCP 配置或系统凭据库暂不可写" } }
        : { body: { server } },
    });
    const user = userEvent.setup();
    render(<McpSettings />);
    await user.click(await screen.findByRole("button", { name: "编辑 本地笔记" }));
    await user.type(screen.getByLabelText("环境变量值 1"), "new-secret");
    await user.click(screen.getByRole("checkbox", { name: "启用此服务授权" }));
    await user.click(screen.getByRole("button", { name: "保存 MCP 配置" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("MCP 配置或系统凭据库暂不可写");
    expect(screen.getByLabelText("环境变量值 1")).toHaveValue("new-secret");
    expect(screen.getByRole("button", { name: "保存 MCP 配置" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "保存 MCP 配置" }));
    await screen.findByText("MCP 配置已保存；连接已撤销，请在对话中重新连接。");
    expect(screen.queryByDisplayValue("new-secret")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(calls.filter((call) => call.method === "PUT")).toHaveLength(2);
    expect(calls.find((call) => call.method === "PUT")?.body).toMatchObject({ enabled: false, env: { ACCESS_TOKEN: "new-secret", OLD_TOKEN: "" } });
  });

  it("can retry an initial configuration read failure without inventing an empty service list", async () => {
    let attempts = 0;
    fakeBackend({
      "GET /api/mcp/servers": () => ++attempts === 1 ? { status: 503, body: { detail: "MCP 目录暂不可用" } } : { body: { servers: [configuredServer()] } },
      "GET /api/roles": () => ({ body: { roles: [] } }),
    });
    const user = userEvent.setup();
    render(<McpSettings />);
    expect(await screen.findByRole("alert")).toHaveTextContent("MCP 目录暂不可用");
    expect(screen.queryByText("尚未配置 MCP 服务。")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "添加 MCP 服务" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "刷新状态" }));
    expect(await screen.findByRole("region", { name: "服务 本地笔记" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lets the owner remove a deleted role grant while editing a persisted service", async () => {
    let server = { ...configuredServer(), roleIds: ["r1", "deleted-role"] };
    const { calls } = fakeBackend({
      "GET /api/mcp/servers": () => ({ body: { servers: [server] } }),
      "GET /api/roles": () => ({ body: { roles: [role({ id: "r1", name: "诗织" })] } }),
      "PUT /api/mcp/servers/notes": () => { server = { ...server, roleIds: ["r1"] }; return { body: { server } }; },
    });
    const user = userEvent.setup();
    render(<McpSettings />);
    await user.click(await screen.findByRole("button", { name: "编辑 本地笔记" }));
    await user.click(screen.getByRole("checkbox", { name: "移除已删除角色 deleted-role 的授权" }));
    await user.click(screen.getByRole("button", { name: "保存 MCP 配置" }));
    await screen.findByText("MCP 配置已保存；连接已撤销，请在对话中重新连接。");
    expect(calls.find((call) => call.method === "PUT")?.body).toMatchObject({ roleIds: ["r1"] });
  });
});
