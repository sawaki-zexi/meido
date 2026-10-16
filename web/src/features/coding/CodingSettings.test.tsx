import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { App } from "../../App";
import { fakeBackend, role } from "../../test/fakeBackend";
import { CodingSettings } from "./CodingSettings";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear(); });

it("preserves coding grants when another role field changes and enables discovery with the owner switch", async () => {
  let saved = role({ id: "r1", name: "编程角色", agentConfig: { shell: { enabled: true, allowedCommands: ["git"], timeoutSeconds: 20, maxOutputChars: 3000 }, coding: { enabled: false, repoGrantIds: ["repo-one"], profileIds: ["cli-one"], defaultHerdrProfileId: "cli-one", defaultCodexProfileId: null, maxAttemptsPerDay: 4 } } });
  fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [saved] } }),
    "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [] } }),
    "PUT /api/roles/r1": init => { saved = { ...saved, ...JSON.parse(String(init?.body)) }; return { body: { role: saved } }; },
  });
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "编程角色", level: 1 });
  await user.click(screen.getByRole("button", { name: "角色资料" }));
  const form = within(await screen.findByRole("dialog", { name: "角色资料" }));
  expect(form.getByRole("checkbox", { name: "允许编程委托" })).not.toBeChecked();
  await user.click(form.getByRole("checkbox", { name: "允许编程委托" }));
  await user.click(form.getByRole("button", { name: "保存" }));
  await waitFor(() => expect(saved.agentConfig?.coding?.enabled).toBe(true));
  expect(saved.agentConfig?.coding?.repoGrantIds).toEqual(["repo-one"]);
  expect(saved.agentConfig?.coding?.profileIds).toEqual(["cli-one"]);
  expect(saved.agentConfig?.coding?.maxAttemptsPerDay).toBe(4);
  expect(saved.agentConfig?.shell?.allowedCommands).toEqual(["git"]);
  expect(saved.agentConfig?.enabledTools).toContain("tool_search");
  await user.click(form.getByRole("button", { name: "关闭" }));
  await user.click(screen.getByRole("button", { name: "角色资料" }));
  expect(within(await screen.findByRole("dialog", { name: "角色资料" })).getByRole("checkbox", { name: "允许编程委托" })).toBeChecked();
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
