import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fakeBackend } from "../../test/fakeBackend";
import { MemorySettings } from "../settings/MemorySettings";
import { ModelSettings } from "./ModelSettings";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

describe("MemorySettings embedding configuration", () => {
  it("loads the shared embedding defaults and saves the API key without displaying it", async () => {
    const { calls } = fakeBackend({
      "GET /api/memory/settings": () => ({ body: { enabled: true, embeddingEnabled: true, embedding: { configuration: { provider: "dashscope", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "text-embedding-v3", dimensions: 1024, apiKeyConfigured: false }, status: { configured: false, ready: false, enabled: true } } } }),
      "PUT /api/embedding/configuration": (init) => ({ body: { configuration: { ...(JSON.parse(String(init?.body)) as object), apiKeyConfigured: true }, status: { configured: true, ready: true, enabled: true } } }),
    });

    render(<MemorySettings />);
    const key = await screen.findByLabelText("向量 API Key");
    fireEvent.change(key, { target: { value: "secret-key" } });
    fireEvent.click(screen.getByRole("button", { name: "保存向量配置" }));

    await waitFor(() => expect(calls.some((call) => call.method === "PUT" && call.path === "/api/embedding/configuration")).toBe(true));
    const save = calls.find((call) => call.method === "PUT" && call.path === "/api/embedding/configuration");
    expect(save?.body).toMatchObject({ model: "text-embedding-v3", dimensions: 1024, apiKey: "secret-key" });
  });
});

describe("ModelSettings protocol selection", () => {
  it("sends the selected native protocol when testing a custom model", async () => {
    const { calls } = fakeBackend({
      "GET /api/model/providers": () => ({ body: { providers: [
        { id: "openai", label: "OpenAI", provider: "openai", baseUrl: "https://api.openai.com/v1", modelHint: "gpt-4.1-mini", api: "openai-completions" },
      ] } }),
      "GET /api/model/configurations": () => ({ body: { configurations: [], activeId: null } }),
      "POST /api/model/configuration/test": () => ({ body: { ok: true, message: "ok", latencyMs: 1 } }),
    });

    render(<ModelSettings />);
    fireEvent.change(await screen.findByLabelText("服务商"), { target: { value: "custom" } });
    fireEvent.change(await screen.findByLabelText("服务商标识"), { target: { value: "custom-proxy" } });
    fireEvent.change(screen.getByLabelText("API 协议"), { target: { value: "anthropic-messages" } });
    fireEvent.change(screen.getByLabelText("API 地址"), { target: { value: "https://provider.test/v1" } });
    fireEvent.change(screen.getByLabelText("模型 ID"), { target: { value: "claude-test" } });
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));

    await waitFor(() => expect(calls.some((call) => call.path === "/api/model/configuration/test")).toBe(true));
    expect(calls.find((call) => call.path === "/api/model/configuration/test")?.body).toMatchObject({
      providerId: "custom",
      api: "anthropic-messages",
      model: "claude-test",
    });
  });

  it("shows protocol-specific Azure options in the test payload", async () => {
    const { calls } = fakeBackend({
      "GET /api/model/providers": () => ({ body: { providers: [] } }),
      "GET /api/model/configurations": () => ({ body: { configurations: [], activeId: null } }),
      "POST /api/model/configuration/test": () => ({ body: { ok: true, message: "ok", latencyMs: 1 } }),
    });

    render(<ModelSettings />);
    fireEvent.change(await screen.findByLabelText("服务商"), { target: { value: "custom" } });
    fireEvent.change(screen.getByLabelText("服务商标识"), { target: { value: "azure" } });
    fireEvent.change(screen.getByLabelText("API 协议"), { target: { value: "azure-openai-responses" } });
    fireEvent.change(screen.getByLabelText("Azure 部署名称"), { target: { value: "chat-prod" } });
    fireEvent.change(screen.getByLabelText("API 地址"), { target: { value: "https://resource.openai.azure.com" } });
    fireEvent.change(screen.getByLabelText("模型 ID"), { target: { value: "gpt-4.1" } });
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));

    await waitFor(() => expect(calls.some((call) => call.path === "/api/model/configuration/test")).toBe(true));
    expect(calls.find((call) => call.path === "/api/model/configuration/test")?.body).toMatchObject({
      api: "azure-openai-responses",
      apiOptions: { deploymentName: "chat-prod" },
    });
  });

  it("allows Codex to infer the account ID from its OAuth token", async () => {
    const { calls } = fakeBackend({
      "GET /api/model/providers": () => ({ body: { providers: [] } }),
      "GET /api/model/configurations": () => ({ body: { configurations: [], activeId: null } }),
      "POST /api/model/configuration/test": () => ({ body: { ok: true, message: "ok", latencyMs: 1 } }),
    });

    render(<ModelSettings />);
    fireEvent.change(await screen.findByLabelText("服务商"), { target: { value: "custom" } });
    fireEvent.change(screen.getByLabelText("服务商标识"), { target: { value: "codex" } });
    fireEvent.change(screen.getByLabelText("API 协议"), { target: { value: "openai-codex-responses" } });
    expect(screen.getByLabelText("ChatGPT Account ID（可选）")).not.toBeRequired();
    fireEvent.change(screen.getByLabelText("API 地址"), { target: { value: "https://chatgpt.com/backend-api" } });
    fireEvent.change(screen.getByLabelText("模型 ID"), { target: { value: "codex-model" } });
    fireEvent.change(screen.getByLabelText("ChatGPT OAuth Token"), { target: { value: "oauth-token" } });
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));

    await waitFor(() => expect(calls.some((call) => call.path === "/api/model/configuration/test")).toBe(true));
    expect(calls.find((call) => call.path === "/api/model/configuration/test")?.body).toMatchObject({
      api: "openai-codex-responses",
      apiKey: "oauth-token",
      apiOptions: {},
    });
  });
});
