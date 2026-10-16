import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeBackend } from "../../test/fakeBackend";
import { AutonomySettings } from "./AutonomySettings";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const config = { revision: 0, activeDecisionEnabled: false, driftEnabled: false,
  decisionModelConfigurationId: null, driftModelConfigurationId: null,
  defaultTargetRef: "desktop", allowedTargetRefs: ["desktop"], sourceGrants: [],
  driftToolGrants: [], shellRuleIds: [], consentRevision: 0 };
const state = { config, runs: [], acknowledgements: [], cleanupPending: false, humanBusy: false };
const models = [{ id: "explicit", provider: "local", model: "autonomy", api: "openai-completions" }];

it("首次读取失败后能在当前角色直接重试且不提交配置", async () => {
  let reads = 0;
  const { calls } = fakeBackend({
    "GET /api/roles/r1/autonomy": () => ++reads === 1
      ? { status: 503, body: { detail: "暂不可读" } } : { body: state },
    "GET /api/model/configurations": () => ({ body: { configurations: models } }),
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await screen.findByText("暂不可读");
  await user.click(within(screen.getByRole("alert")).getByRole("button", { name: "关闭提示" }));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重新读取自主设置" }));
  expect(await screen.findByRole("checkbox", { name: "开启主动决策" })).not.toBeChecked();
  expect(calls.every((call) => call.method === "GET")).toBe(true);
});

it("默认关闭，明确模型和有限操作许可后保存，能查看真实ACK并停止", async () => {
  let saved = state;
  const { calls } = fakeBackend({
    "GET /api/roles/r1/autonomy": () => ({ body: saved }),
    "GET /api/model/configurations": () => ({ body: { configurations: models, activeId: "explicit" } }),
    "PUT /api/roles/r1/autonomy": (init) => {
      const input = JSON.parse(String(init?.body));
      expect(input.expectedRevision).toBe(0);
      saved = { ...state, config: { ...input.config, revision: 1 } };
      return { body: saved };
    },
    "POST /api/roles/r1/autonomy/stop": () => ({ body: { ...saved, cleanupPending: true } }),
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  const enable = await screen.findByRole("checkbox", { name: "开启主动决策" });
  expect(enable).not.toBeChecked();
  expect(screen.getByRole("checkbox", { name: "开启 Drift" })).not.toBeChecked();
  expect(screen.getByRole("combobox", { name: "主动决策模型" })).toHaveValue("");
  await user.click(enable);
  expect(screen.getByRole("button", { name: "保存自主设置" })).toBeDisabled();
  await user.selectOptions(screen.getByRole("combobox", { name: "主动决策模型" }), "explicit");
  await user.click(screen.getByRole("checkbox", { name: /同意有限自主读取/ }));
  await user.click(screen.getByRole("button", { name: "保存自主设置" }));
  await waitFor(() => expect(saved.config.activeDecisionEnabled).toBe(true));
  expect(saved.config.decisionModelConfigurationId).toBe("explicit");
  expect(calls.filter((call) => call.method !== "GET")).toHaveLength(1);
  await user.click(screen.getByRole("button", { name: "停止当前自主运行" }));
  expect(await screen.findByText(/正在回收自有资源/)).toBeInTheDocument();
});

it("切换角色后迟到的保存结果不能覆盖新角色的配置", async () => {
  let finish: ((value: { body: unknown }) => void) | undefined;
  fakeBackend({
    "GET /api/roles/r1/autonomy": () => ({ body: state }),
    "GET /api/roles/r2/autonomy": () => ({ body: { ...state, config: { ...config, decisionModelConfigurationId: "explicit" } } }),
    "GET /api/model/configurations": () => ({ body: { configurations: models } }),
    "PUT /api/roles/r1/autonomy": () => new Promise((resolve) => { finish = resolve; }),
  });
  const user = userEvent.setup();
  const rendered = render(<AutonomySettings roleId="r1" disabled={false} />);
  await screen.findByRole("checkbox", { name: "开启主动决策" });
  await user.click(screen.getByRole("button", { name: "保存自主设置" }));
  await waitFor(() => expect(finish).toBeDefined());
  rendered.rerender(<AutonomySettings roleId="r2" disabled={false} />);
  await waitFor(() => expect(screen.getByRole("combobox", { name: "主动决策模型" })).toHaveValue("explicit"));
  finish!({ body: { ...state, config: { ...config, activeDecisionEnabled: true } } });
  await waitFor(() => expect(screen.getByRole("checkbox", { name: "开启主动决策" })).not.toBeChecked());
  expect(screen.getByRole("combobox", { name: "主动决策模型" })).toHaveValue("explicit");
});

it("显式信任本地产物后才能提交来源契约验证，验证结果须再次保存才启用", async () => {
  const source = { sourceId: "news", revision: 3, enabled: true, ackContract: "absolute-idempotent" };
  let current: unknown = { ...state, config: { ...config, revision: 2, sourceGrants: [source] } };
  let verificationRequests = 0;
  fakeBackend({
    "GET /api/roles/r1/autonomy": () => ({ body: current }),
    "GET /api/model/configurations": () => ({ body: { configurations: models } }),
    "POST /api/roles/r1/autonomy/source-verifications": (init) => {
      verificationRequests += 1;
      const body = JSON.parse(String(init?.body));
      expect(body).toMatchObject({ expectedRevision: 2, sourceId: "news", manifestPath: "D:/trusted/manifest.json",
        manifestSha256: "a".repeat(64), trustLocalCode: true });
      expect(body.requestNonce).toMatch(/^[a-f0-9-]{36}$/);
      const proof = { proofId: "b".repeat(32), sourceId: "news", sourceRevision: 3 };
      current = { ...state, config: { ...config, revision: 2, sourceGrants: [source] }, sourceVerifications: [
        { id: "attempt", sourceId: "news", sourceRevision: 3, status: "succeeded", proof, runtimeAcknowledged: true, cleanupPending: false, error: "" },
      ] };
      return { body: { id: "attempt", status: "preparing" } };
    },
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await screen.findByRole("checkbox", { name: "开启主动决策" });
  await user.selectOptions(screen.getByRole("combobox", { name: "待验证来源" }), "news");
  await user.type(screen.getByRole("textbox", { name: "来源验证清单路径" }), "D:/trusted/manifest.json");
  await user.type(screen.getByRole("textbox", { name: "来源验证清单 SHA256" }), "a".repeat(64));
  expect(screen.getByRole("button", { name: "验证来源 ACK 契约" })).toBeDisabled();
  expect(verificationRequests).toBe(0);
  await user.click(screen.getByRole("checkbox", { name: /信任并运行该本地来源产物/ }));
  await user.click(screen.getByRole("button", { name: "验证来源 ACK 契约" }));
  const useProof = await screen.findByRole("button", { name: "使用 news 的验证结果" });
  expect(verificationRequests).toBe(1);
  expect(screen.getByRole("textbox", { name: "来源许可草稿" })).not.toHaveValue(expect.stringContaining("ackProofId"));
  await user.click(useProof);
  expect(JSON.parse(String((screen.getByRole("textbox", { name: "来源许可草稿" }) as HTMLTextAreaElement).value))[0].ackProofId).toBe("b".repeat(32));
  expect(screen.getByText(/验证引用已填入草稿，请保存自主设置/)).toBeInTheDocument();
});
