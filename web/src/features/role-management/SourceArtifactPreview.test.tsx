import { afterEach, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { fakeBackend } from "../../test/fakeBackend";
import { AutonomySettings } from "./AutonomySettings";
import { SourceArtifactPreview } from "./SourceArtifactPreview";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const config = { revision: 1, activeDecisionEnabled: false, driftEnabled: false,
  decisionModelConfigurationId: null, driftModelConfigurationId: null, defaultTargetRef: "desktop",
  allowedTargetRefs: ["desktop"], sourceGrants: [], driftToolGrants: [], shellRuleIds: [], consentRevision: 0 };
const state = { config, runs: [], acknowledgements: [], cleanupPending: false, humanBusy: false };

it("只读预览真实hash，明确导入disabled草稿和验证路径不会授权或执行", async () => {
  const { calls } = fakeBackend({
    "GET /api/roles/r1/autonomy": () => ({ body: state }),
    "GET /api/model/configurations": () => ({ body: { configurations: [] } }),
    "POST /api/roles/r1/autonomy/source-artifact-preview": (init) => {
      expect(JSON.parse(String(init?.body))).toEqual({ manifestPath: "mcp/example/manifest.json", serverId: "example" });
      return { body: { manifestPath: "D:/role/workspace/mcp/example/manifest.json", manifestSha256: "a".repeat(64),
        manifestReady: true, preparedManifest: "{\"profile\":\"meido-source-author-v2\"}\n",
        files: [{ path: "source.py", size: 300, sha256: "b".repeat(64) }],
        sourceGrants: [{ sourceId: "context", revision: 1, enabled: false, channel: "context", ackContract: "none" }],
        configurationStatus: "draft_only" } };
    },
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await screen.findByRole("textbox", { name: "来源许可草稿" });
  await user.type(screen.getByRole("textbox", { name: "产物相对清单路径" }), "mcp/example/manifest.json");
  await user.type(screen.getByRole("textbox", { name: "已保存的 MCP 服务 ID" }), "example");
  await user.click(screen.getByRole("button", { name: "预览来源产物" }));
  expect(await screen.findByText("source.py · 300 bytes · " + "b".repeat(64))).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "填入验证清单" }));
  expect(screen.getByRole("textbox", { name: "来源验证清单 SHA256" })).toHaveValue("a".repeat(64));
  expect(screen.getByRole("checkbox", { name: /信任并运行该本地来源/ })).not.toBeChecked();
  await user.click(screen.getByRole("button", { name: "填入来源许可草稿" }));
  expect(JSON.parse((screen.getByRole("textbox", { name: "来源许可草稿" }) as HTMLTextAreaElement).value)[0].enabled).toBe(false);
  expect(calls.filter((call) => call.method !== "GET").map((call) => call.path)).toEqual(["/api/roles/r1/autonomy/source-artifact-preview"]);
});

it("没有剪贴板API时仍可手动复制，不能抛异常或宣称已保存", async () => {
  const user = userEvent.setup();
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
  fakeBackend({ "POST /api/roles/r1/autonomy/source-artifact-preview": () => ({ body: {
    manifestPath: "D:/manifest.json", manifestSha256: "a".repeat(64), manifestReady: false,
    preparedManifest: "{\"files\":{}}", files: [], sourceGrants: [],
  } }) });
  render(<SourceArtifactPreview roleId="r1" importManifest={vi.fn()} importSources={() => true} />);
  await user.type(screen.getByRole("textbox", { name: "产物相对清单路径" }), "mcp/manifest.json");
  await user.click(screen.getByRole("button", { name: "预览来源产物" }));
  await user.click(await screen.findByRole("button", { name: "复制修正清单" }));
  expect(await screen.findByText("无法自动复制，请手动复制修正后的清单。")).toBeInTheDocument();
});

it("修改预览路径后旧响应不能恢复旧文件或导入权限草稿", async () => {
  let finish: ((value: { body: unknown }) => void) | undefined;
  fakeBackend({ "POST /api/roles/r1/autonomy/source-artifact-preview": () => new Promise((resolve) => { finish = resolve; }) });
  const user = userEvent.setup();
  const importManifest = vi.fn();
  render(<SourceArtifactPreview roleId="r1" importManifest={importManifest} importSources={() => true} />);
  const input = screen.getByRole("textbox", { name: "产物相对清单路径" });
  await user.type(input, "mcp/old/manifest.json");
  await user.click(screen.getByRole("button", { name: "预览来源产物" }));
  await waitFor(() => expect(finish).toBeDefined());
  await user.clear(input);
  await user.type(input, "mcp/new/manifest.json");
  await act(async () => { finish!({ body: { manifestPath: "D:/old/manifest.json", manifestSha256: "a".repeat(64),
    manifestReady: true, preparedManifest: "old-secret-marker", files: [], sourceGrants: [] } }); });
  expect(screen.queryByText("old-secret-marker")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "填入验证清单" })).not.toBeInTheDocument();
  expect(importManifest).not.toHaveBeenCalled();
});

it("纯context来源可验证读取契约，但不能导入ACK重试证明", async () => {
  const source = { sourceId: "background", revision: 1, channel: "context", enabled: true, ackContract: "none" };
  let current: unknown = { ...state, config: { ...config, sourceGrants: [source] } };
  const { calls } = fakeBackend({
    "GET /api/roles/r1/autonomy": () => ({ body: current }),
    "GET /api/model/configurations": () => ({ body: { configurations: [] } }),
    "POST /api/roles/r1/autonomy/source-verifications": () => {
      current = { ...state, config: { ...config, sourceGrants: [source] }, sourceVerifications: [{ id: "v2-context",
        sourceId: "background", sourceRevision: 1, status: "succeeded", runtimeAcknowledged: true, cleanupPending: false, error: "",
        proof: { proofId: "b".repeat(32), sourceId: "background", sourceRevision: 1, collectionVerified: true, ackVerified: false,
          verifierVersion: "meido-source-author-v2" } }] };
      return { body: { id: "v2-context" } };
    },
  });
  const user = userEvent.setup();
  render(<AutonomySettings roleId="r1" disabled={false} />);
  await user.selectOptions(await screen.findByRole("combobox", { name: "待验证来源" }), "background");
  await user.type(screen.getByRole("textbox", { name: "来源验证清单路径" }), "D:/context/manifest.json");
  await user.type(screen.getByRole("textbox", { name: "来源验证清单 SHA256" }), "a".repeat(64));
  await user.click(screen.getByRole("checkbox", { name: /信任并运行该本地来源/ }));
  await user.click(screen.getByRole("button", { name: "验证来源读取契约" }));
  expect(await screen.findByText(/仅验证读取契约；context 不需要 ACK/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "使用 background 的验证结果" })).not.toBeInTheDocument();
  expect(calls.filter((call) => call.method !== "GET")).toHaveLength(1);
});
