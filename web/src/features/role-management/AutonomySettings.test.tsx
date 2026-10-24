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

it("默认收起进阶配置，展开编辑再收起仍保留草稿并正常保存", async () => {
  const { calls } = fakeBackend({
    "GET /api/roles/r1/autonomy": () => ({ body: state }),
    "GET /api/model/configurations": () => ({ body: { configurations: models } }),
    "GET /api/roles/r1/autonomy/target-accounts": () => ({ body: { accounts: [] } }),
    "PUT /api/roles/r1/autonomy": init => ({ body: { ...state, config: { ...JSON.parse(String(init?.body)).config, revision: 1 } } }),
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  expect(await screen.findByRole("checkbox", { name: "开启主动决策" })).toBeVisible();
  const sources = screen.getByRole("textbox", { name: "来源许可草稿", hidden: true });
  expect(sources).not.toBeVisible();
  const summary = screen.getByText("表情库维护", { selector: "summary > span" });
  await user.click(summary);
  const categories = screen.getByRole("textbox", { name: "允许维护的类别（每行一个）" });
  await user.type(categories, "开心");
  await user.click(summary);
  expect(categories).not.toBeVisible();
  await user.click(summary);
  expect(categories).toHaveValue("开心");
  await user.click(screen.getByRole("button", { name: "保存自主设置" }));
  await waitFor(() => expect(calls.filter(call => call.method === "PUT")).toHaveLength(1));
  expect(calls.find(call => call.method === "PUT")?.body).toMatchObject({
    config: { activeDecisionEnabled: false, driftEnabled: false, driftMeme: { enabled: false, allowedCategories: ["开心"] } },
  });
});

it("显式读取主人目标目录后选择外部默认目标，保存仅携带本次观察许可", async () => {
  let saves = 0;
  const { calls } = fakeBackend({
    "GET /api/roles/r1/autonomy": () => ({ body: state }),
    "GET /api/model/configurations": () => ({ body: { configurations: models } }),
    "GET /api/roles/r1/autonomy/target-accounts": () => ({ body: { accounts: [
      { accountId: "a1", channel: "telegram", displayName: "主人机器人", enabled: true, platformEnabled: true,
        effectiveEnabled: true, connection: "connected", online: null, observedAt: "2026-10-16T15:00:00Z",
        reason: "平台未提供在线事实", supportedKinds: ["known", "member"], capabilities: {} },
    ] } }),
    "POST /api/roles/r1/autonomy/target-directory": (init) => {
      expect(JSON.parse(String(init?.body))).toEqual({ accountId: "a1", kind: "known", limit: 20 });
      return { body: { targets: [{ id: "private:42", kind: "private", displayName: "主人私聊", accountId: "a1",
        chatId: "42", capabilities: ["private"], canSend: true, targetRef: "opaque-owner", ownerEligible: true,
        reason: "", selectionObservationId: "observation-1" }], nextCursor: null, coverage: "known", complete: true,
        observedAt: "2026-10-16T15:01:00Z", total: 1, directoryRevision: { account: 3, platform: 5 } } };
    },
    "PUT /api/roles/r1/autonomy": (init) => {
      const body = JSON.parse(String(init?.body));
      if (++saves === 1) expect(body.targetSelections).toEqual([{ accountId: "a1", targetId: "private:42", selectionObservationId: "observation-1",
        expectedDirectoryRevision: { account: 3, platform: 5 } }]);
      else expect(body).not.toHaveProperty("targetSelections");
      expect(body.config.allowedTargetRefs).toEqual(["desktop", "opaque-owner"]);
      expect(body.config.defaultTargetRef).toBe("opaque-owner");
      return { body: { ...state, config: { ...body.config, revision: saves }, targetGrants: [{ ref: "opaque-owner", channel: "telegram",
        accountId: "a1", targetId: "private:42", displayName: "主人私聊", current: true, denial: null }] } };
    },
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await user.click(await screen.findByText("添加渠道目标", { selector: "summary > span" }));
  await screen.findByRole("combobox", { name: "自主发送账号" });
  await screen.findByRole("option", { name: "主人机器人 · telegram" });
  expect(calls.some((call) => call.method === "POST")).toBe(false);
  await user.selectOptions(screen.getByRole("combobox", { name: "自主发送账号" }), "a1");
  expect(screen.getByText("在线状态未知")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "读取目标目录" }));
  await user.click(await screen.findByRole("checkbox", { name: "许可目标 主人私聊" }));
  await user.selectOptions(screen.getByRole("combobox", { name: "自主默认目标" }), "opaque-owner");
  await user.click(screen.getByRole("button", { name: "保存自主设置" }));
  await waitFor(() => expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1));
  expect(await screen.findByText(/已保存目标：主人私聊/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "保存自主设置" }));
  await waitFor(() => expect(calls.filter((call) => call.method === "PUT")).toHaveLength(2));
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
});

it("首次读取失败后能在当前角色直接重试且不提交配置", async () => {
  let reads = 0;
  const { calls } = fakeBackend({
    "GET /api/roles/r1/autonomy": () => ++reads === 1
      ? { status: 503, body: { detail: "暂不可读" } } : { body: state },
    "GET /api/roles/r1/autonomy/target-accounts": () => ({ body: { accounts: [] } }),
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

it("未知投递只记录主人报告，503重试保留同一请求身份且不重发消息", async () => {
  const delivery = { intentId: "intent-one", status: "uncertain", messageId: null,
    target: { ref: "opaque-owner", channel: "telegram", accountId: "a1", targetId: "private:42", displayName: "主人私聊" },
    attemptId: "attempt-one", platformMessageId: null, error: "回执未知", projectionPending: false, resolutionRevision: 0, resolution: null };
  let attempts = 0;
  const { calls } = fakeBackend({
    "GET /api/roles/r1/autonomy": () => ({ body: { ...state, deliveries: [delivery] } }),
    "GET /api/model/configurations": () => ({ body: { configurations: models } }),
    "GET /api/roles/r1/autonomy/target-accounts": () => ({ body: { accounts: [] } }),
    "POST /api/roles/r1/autonomy/deliveries/intent-one/reconciliation": (init) => {
      const body = JSON.parse(String(init?.body));
      expect(body).toMatchObject({ expectedResolutionRevision: 0, expectedStatus: "uncertain", decision: "reported_not_sent", allowFuture: true, note: "主人核对未收到" });
      expect(body.requestId).toMatch(/^[a-f0-9-]{36}$/);
      if (++attempts === 1) return { status: 503, body: { detail: "本地提交结果未知" } };
      return { body: { ...state, deliveries: [{ ...delivery, resolutionRevision: 1,
        resolution: { requestId: body.requestId, decision: body.decision, allowFuture: true, note: body.note, reportedAt: 1792162800, revision: 1 } }] } };
    },
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  const panel = await screen.findByRole("group", { name: "投递处置 intent-one" });
  expect(within(panel).getByText(/主人报告不等于平台送达确认/)).toBeInTheDocument();
  await user.selectOptions(within(panel).getByRole("combobox", { name: "主人处置决定" }), "reported_not_sent");
  await user.type(within(panel).getByRole("textbox", { name: "处置说明" }), "主人核对未收到");
  await user.click(within(panel).getByRole("checkbox", { name: "允许后续新的自主投递" }));
  await user.click(within(panel).getByRole("button", { name: "记录主人处置" }));
  expect(await within(panel).findByText(/本地提交结果未知/)).toBeInTheDocument();
  expect(within(panel).getByRole("textbox", { name: "处置说明" })).toBeDisabled();
  await user.click(within(panel).getByRole("button", { name: "重试原处置请求" }));
  expect(await within(panel).findByText(/主人报告：未收到/)).toBeInTheDocument();
  const posts = calls.filter((call) => call.method === "POST");
  expect(posts).toHaveLength(2);
  expect(posts[1].body).toEqual(posts[0].body);
  expect(within(panel).getByText(/结果不确定，不会再次发送/)).toBeInTheDocument();
});

it("默认关闭，明确模型和有限操作许可后保存，能查看真实ACK并停止", async () => {
  let saved = state;
  const { calls } = fakeBackend({
    "GET /api/roles/r1/autonomy": () => ({ body: saved }),
    "GET /api/roles/r1/autonomy/target-accounts": () => ({ body: { accounts: [] } }),
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
    "GET /api/roles/r[12]/autonomy/target-accounts": () => ({ body: { accounts: [] } }),
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

it("最近运行显示冻结目标、原attempt与真实平台消息ID，旧桌面状态仍可读", async () => {
  fakeBackend({
    "GET /api/roles/r1/autonomy": () => ({ body: { ...state, runs: [
      { id: "external-tick", kind: "autonomy_tick", mode: "drift", status: "finished_sent", runtimeAcknowledged: true, error: "",
        delivery: { intentId: "intent-external", status: "sent", messageId: "local-message", target: { ref: "opaque", channel: "telegram",
          accountId: "account-one", targetId: "target-one", displayName: "原冻结主人私聊" }, attemptId: "original-attempt", platformMessageId: "original-platform" } },
      { id: "legacy-tick", kind: "autonomy_tick", status: "sent", runtimeAcknowledged: true, error: "",
        delivery: { status: "sent", messageId: "legacy-message" } },
    ] } }),
    "GET /api/model/configurations": () => ({ body: { configurations: [] } }),
    "GET /api/roles/r1/autonomy/target-accounts": () => ({ body: { accounts: [] } }),
  });
  render(<AutonomySettings roleId="r1" disabled={false} />);
  const run = await screen.findByRole('listitem', { name: '自主运行 external-tick' });
  expect(within(run).getByText(/冻结目标：原冻结主人私聊 · telegram · 账号 account-one/)).toBeInTheDocument();
  expect(within(run).getByText(/原 attempt：original-attempt · 平台消息 ID：original-platform/)).toBeInTheDocument();
  expect(within(screen.getByRole('listitem', { name: '自主运行 legacy-tick' })).getByText(/legacy-message/)).toBeInTheDocument();
});

it("显式信任本地产物后才能提交来源契约验证，验证结果须再次保存才启用", async () => {
  const source = { sourceId: "news", revision: 3, enabled: true, ackContract: "absolute-idempotent" };
  let current: unknown = { ...state, config: { ...config, revision: 2, sourceGrants: [source] } };
  let verificationRequests = 0;
  fakeBackend({
    "GET /api/roles/r1/autonomy": () => ({ body: current }),
    "GET /api/roles/r1/autonomy/target-accounts": () => ({ body: { accounts: [] } }),
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
  await user.click(screen.getByText("来源与验证", { selector: "summary > span" }));
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
