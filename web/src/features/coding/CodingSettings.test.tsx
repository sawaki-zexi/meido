import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "../../App";
import { fakeBackend, role } from "../../test/fakeBackend";
import { CodingSettings } from "./CodingSettings";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear(); });

it("edits an independent Codex exec profile and qualifies its own hash without Herdr", async () => {
  const profile = { id: "exec-one", name: "独立 exec", kind: "codex", executor: "codex_exec", executable: "D:/fixed/codex.exe", sha256: "a".repeat(64), version: "0.159.3", model: "fixed-model", authDirectory: "D:/dedicated/auth", allowedRoleIds: ["r1"], configurationManifest: [], revision: "exec-v1", verification: { status: "unverified", reason: "尚未核验" } };
  const configuration = { enabled: true, repositories: [], profiles: [profile], nativeBuild: null };
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [role({ id: "r1", name: "诗织" })] } }),
    "GET /api/coding/configuration": () => ({ body: configuration }),
    "POST /api/coding/profiles": init => ({ body: { ...profile, ...JSON.parse(String(init?.body)) } }),
    "POST /api/coding/profiles/exec-one/probe": () => ({ body: { id: "probe-exec", status: "accepted" } }),
  });
  const user = userEvent.setup();
  render(<CodingSettings />);
  await user.click(await screen.findByRole("button", { name: "编辑 CLI 独立 exec" }));
  expect(screen.getByRole("combobox", { name: "委托方式" })).toHaveValue("codex_exec");
  expect(screen.getByRole("combobox", { name: "CLI" })).toHaveValue("codex");
  await user.click(screen.getByRole("button", { name: "登记 CLI" }));
  await screen.findByText("CLI 已登记，尚未核验。保存不会登录或调用模型。");
  expect(calls.find(call => call.path === "/api/coding/profiles" && call.method === "POST")?.body).toMatchObject({ executor: "codex_exec", kind: "codex", id: "exec-one" });
  await user.click(screen.getByText("原生核验", { selector: "summary" }));
  await user.click(screen.getByRole("checkbox", { name: /我批准所选 CLI 执行首次和继续两个任务回合/ }));
  await user.click(screen.getByRole("button", { name: "核验 独立 exec" }));
  await waitFor(() => expect(calls.some(call => call.path.endsWith("/probe"))).toBe(true));
  expect(calls.find(call => call.path.endsWith("/probe"))?.body).toMatchObject({ buildSha256: "a".repeat(64), approveNativeTurns: 2 });
});

it("saves coding grants in settings without rewriting default-tool selections", async () => {
  let saved = role({ id: "r1", name: "编程角色", agentConfig: { shell: { enabled: true, allowedCommands: ["git"], timeoutSeconds: 20, maxOutputChars: 3000 }, coding: { enabled: false, repoGrantIds: ["repo-one"], profileIds: ["cli-one", "exec-one"], defaultHerdrProfileId: "cli-one", defaultCodexProfileId: null, maxAttemptsPerDay: 4 } } });
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1": () => ({ body: { role: saved } }),
    "GET /api/coding/configuration": () => ({ body: { enabled: true, repositories: [], nativeBuild: null, profiles: [
      { id: "exec-one", name: "独立 exec", executor: "codex_exec", allowedRoleIds: ["r1"], verification: { status: "verified" } },
    ] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "PUT /api/roles/r1": init => { const body = JSON.parse(String(init?.body)); saved = { ...saved, ...body,
      agentConfig: { ...saved.agentConfig, coding: { ...saved.agentConfig!.coding, ...body.agentConfig.coding } } }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "编程角色", level: 1 });
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "插件" }));
  await user.click(await screen.findByRole("button", { name: "编程委托 设置" }));
  await user.click(screen.getByRole('button', { name: '角色参数' }));
  const form = within(await screen.findByRole("dialog", { name: "设置" }));
  expect(form.queryByRole("checkbox", { name: "允许编程委托" })).not.toBeInTheDocument();
  await user.selectOptions(form.getByRole("combobox", { name: "默认 Codex exec CLI" }), "exec-one");
  await user.click(form.getByRole("button", { name: "保存角色参数" }));
  await waitFor(() => expect(saved.agentConfig?.coding?.defaultCodexProfileId).toBe('exec-one'));
  expect(saved.agentConfig?.coding?.enabled).toBe(false);
  expect(saved.agentConfig?.coding?.repoGrantIds).toEqual(["repo-one"]);
  expect(saved.agentConfig?.coding?.profileIds).toEqual(["cli-one", "exec-one"]);
  expect(saved.agentConfig?.coding?.defaultCodexProfileId).toBe("exec-one");
  expect(saved.agentConfig?.coding?.maxAttemptsPerDay).toBe(4);
  expect(saved.agentConfig?.shell?.allowedCommands).toEqual(["git"]);
  expect(saved.agentConfig?.enabledTools).toBeUndefined();
  await user.click(form.getByRole("button", { name: "关闭" }));
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "插件" }));
  await user.click(await screen.findByRole("button", { name: "编程委托 设置" }));
  await user.click(screen.getByRole('button', { name: '角色参数' }));
  await screen.findByRole('combobox', { name: '默认 Codex exec CLI' });
  expect(within(screen.getByRole("dialog", { name: "设置" })).getByRole("combobox", { name: "默认 Codex exec CLI" })).toHaveValue("exec-one");
});

it("requires explicit native-turn cost consent, displays blocked facts, and revokes exact persisted CLI grants", async () => {
  const profile = { id: "cli-one", name: "专用 Codex", kind: "codex", executor: "herdr", executable: "D:/fixed/codex.exe", sha256: "a".repeat(64), version: "0.159.3", model: "fixed-model", authDirectory: "D:/explicit/auth", allowedRoleIds: ["r1"], configurationManifest: [], revision: "revision-one", verification: { status: "unverified", reason: "缺少原生拒绝证据" } };
  let configuration = { enabled: false, repositories: [], profiles: [profile], nativeBuild: { executable: "D:/fixed/herdr.exe", sha256: "b".repeat(64), version: "0.9.4-meido.1" } };
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [role({ id: "r1", name: "诗织" })] } }),
    "GET /api/coding/configuration": () => ({ body: configuration }),
    "GET /api/coding/probes": () => ({ body: { probes: [] } }),
    "POST /api/coding/profiles/cli-one/probe": () => ({ body: { id: "probe-one", status: "accepted" } }),
    "POST /api/coding/profiles": init => { configuration = { ...configuration, profiles: [{ ...profile, ...JSON.parse(String(init?.body)) }] }; return { body: configuration.profiles[0] }; },
  });
  const user = userEvent.setup();
  render(<CodingSettings />);
  await user.click(screen.getByText("原生核验", { selector: "summary" }));
  expect(await screen.findByRole("button", { name: "核验 专用 Codex" })).toBeDisabled();
  expect(screen.getByText(/内部可能多次请求模型；最多运行 600 秒/)).toBeInTheDocument();
  await user.click(screen.getByRole("checkbox", { name: /我批准所选 CLI 执行首次和继续两个任务回合/ }));
  await user.click(screen.getByRole("button", { name: "核验 专用 Codex" }));
  await screen.findByText("原生核验已启动；请刷新查看确切结果。");
  expect(calls.filter(call => call.path.endsWith("/probe") && call.method === "POST")).toHaveLength(1);
  expect(calls.find(call => call.path.endsWith("/probe") && call.method === "POST")?.body).toMatchObject({ revision: "revision-one", roleId: "r1", buildSha256: "b".repeat(64), approveNativeTurns: 2 });
  expect(screen.getByRole("button", { name: "核验 专用 Codex" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "撤销 CLI 专用 Codex" }));
  await screen.findByText("已撤销 CLI 的角色许可并停止当前任务");
  expect(configuration.profiles[0].allowedRoleIds).toEqual([]);
  expect(configuration.profiles[0].id).toBe("cli-one");
});


it("编程设置默认折叠细节，以真实启停按钮保存全局状态", async () => {
  let configuration = { enabled: false, repositories: [], profiles: [], nativeBuild: null };
  const { calls } = fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [role({ id: "r1", name: "诗织" })] } }),
    "GET /api/coding/configuration": () => ({ body: configuration }),
    "PUT /api/coding/configuration": init => { configuration = { ...configuration, ...JSON.parse(String(init?.body)) }; return { body: configuration }; },
  });
  const user = userEvent.setup(); render(<CodingSettings />);
  const start = await screen.findByRole("button", { name: "启动编程委托" });
  await waitFor(() => expect(start).toBeEnabled());
  expect(screen.getByRole("textbox", { name: "可信仓库路径" })).not.toBeVisible();
  expect(calls.some(call => call.method !== "GET")).toBe(false);
  await user.click(start);
  await screen.findByRole("button", { name: "关闭编程委托" });
  expect(configuration.enabled).toBe(true);
  await user.click(screen.getByRole("button", { name: "关闭编程委托" }));
  await screen.findByRole("button", { name: "启动编程委托" });
  expect(configuration.enabled).toBe(false);
  expect(calls.filter(call => call.method === "PUT").map(call => call.body)).toEqual([{ enabled: true }, { enabled: false }]);
});
