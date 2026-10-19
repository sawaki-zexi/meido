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
    fireEvent.click(await screen.findByRole("button", { name: "向量模型" }));
    const key = await screen.findByLabelText("向量 API Key");
    fireEvent.change(key, { target: { value: "secret-key" } });
    fireEvent.click(screen.getByRole("button", { name: "保存向量配置" }));

    await waitFor(() => expect(calls.some((call) => call.method === "PUT" && call.path === "/api/embedding/configuration")).toBe(true));
    const save = calls.find((call) => call.method === "PUT" && call.path === "/api/embedding/configuration");
    expect(save?.body).toMatchObject({ model: "text-embedding-v3", dimensions: 1024, apiKey: "secret-key" });
  });
});

describe("ModelSettings protocol selection", () => {
  it('模型容量与压缩阈值读出并以数字保存，旧空值保持未配置', async () => {
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 0));
    const configuration = { id: 'capacity', providerId: 'custom', provider: 'fixture', api: 'openai-completions', baseUrl: 'http://localhost/v1', model: 'fixture', apiKeyConfigured: false,
      contextWindowTokens: 128000, modelAutoCompactTokenLimit: 96000 };
    const { calls } = fakeBackend({
      'GET /api/model/providers': () => ({ body: { providers: [] } }),
      'GET /api/model/configurations': () => ({ body: { configurations: [configuration], activeId: configuration.id } }),
      'PUT /api/model/configurations/capacity': () => ({ body: { configuration } }),
    });
    render(<ModelSettings />);
    fireEvent.click(await screen.findByRole('button', { name: '编辑' }));
    expect(screen.getByLabelText('上下文窗口（token）')).toHaveValue(128000);
    expect(screen.getByLabelText('自动压缩阈值（token，可选）')).toHaveValue(96000);
    fireEvent.change(screen.getByLabelText('上下文窗口（token）'), { target: { value: '200000' } });
    fireEvent.change(screen.getByLabelText('自动压缩阈值（token，可选）'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(calls.some(call => call.method === 'PUT')).toBe(true));
    expect(calls.find(call => call.method === 'PUT')?.body).toMatchObject({ contextWindowTokens: 200000, modelAutoCompactTokenLimit: null });
  });
  it('列表进入模型详情后仅展示当前表单，返回可继续选其他连接', async () => {
    const configuration = { id: 'chat', providerId: 'custom', provider: 'local', api: 'openai-completions', baseUrl: 'http://localhost:9/v1', model: 'chat', apiKeyConfigured: false };
    fakeBackend({
      'GET /api/model/providers': () => ({ body: { providers: [] } }),
      'GET /api/model/configurations': () => ({ body: { configurations: [configuration], activeId: 'chat' } }),
    });
    render(<ModelSettings />);
    fireEvent.click(await screen.findByRole('button', { name: '编辑' }));
    expect(screen.queryByRole('list', { name: '已保存的模型配置' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('模型 ID')).toHaveValue('chat');
    fireEvent.click(screen.getByRole('button', { name: '返回模型列表' }));
    expect(screen.getByRole('list', { name: '已保存的模型配置' })).toBeInTheDocument();
    expect(screen.queryByLabelText('模型 ID')).not.toBeInTheDocument();
  });
  it("masks a saved model key and only reads it when the eye is clicked", async () => {
    vi.stubGlobal("requestAnimationFrame", vi.fn(() => 0));
    const configuration = { id: "model-one", providerId: "openai", provider: "openai", api: "openai-completions", baseUrl: "https://api.openai.com/v1", model: "gpt-test", apiKeyConfigured: true };
    let saved = false;
    const { calls } = fakeBackend({
      "GET /api/model/providers": () => ({ body: { providers: [{ id: "openai", label: "OpenAI", provider: "openai", baseUrl: "https://api.openai.com/v1", modelHint: "gpt-test" }] } }),
      "GET /api/model/configurations": () => ({ body: { configurations: saved ? [configuration] : [], activeId: saved ? configuration.id : null } }),
      "POST /api/model/configurations": () => { saved = true; return { body: { configuration } }; },
      "PUT /api/model/configurations/model-one": () => ({ body: { configuration } }),
      "POST /api/model/configurations/model-one/reveal-key": () => ({ body: { apiKey: "saved-model-secret" } }),
    });

    const view = render(<ModelSettings />);
    let key = await screen.findByLabelText("API Key");
    fireEvent.change(screen.getByLabelText("模型 ID"), { target: { value: "gpt-test" } });
    fireEvent.change(key, { target: { value: "saved-model-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByText("配置已保存并生效");

    key = screen.getByLabelText("API Key");
    expect(key).toHaveValue("");
    expect(key).toHaveAttribute("placeholder", "••••••");
    expect(calls.some((call) => call.path.endsWith("/reveal-key"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "显示API Key" }));
    await waitFor(() => expect(key).toHaveValue("saved-model-secret"));
    expect(key).toHaveAttribute("type", "text");
    expect(calls.filter((call) => call.path === "/api/model/configurations/model-one/reveal-key")).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "隐藏API Key" }));
    expect(key).toHaveValue("");
    expect(key).toHaveAttribute("type", "password");

    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(calls.some((call) => call.method === "PUT")).toBe(true));
    expect(calls.find((call) => call.method === "PUT")?.body).toMatchObject({ apiKey: "" });
    await screen.findByText("配置已保存并生效");
    view.unmount();
    render(<ModelSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    expect(screen.getByLabelText("API Key")).toHaveValue("");
    expect(screen.getByLabelText("API Key")).toHaveAttribute("placeholder", "••••••");
    fireEvent.click(screen.getByRole("button", { name: "返回模型列表" }));
    fireEvent.click(screen.getByRole("button", { name: "新建配置" }));
    expect(screen.getByLabelText("API Key")).not.toHaveAttribute("placeholder", "••••••");
    expect(screen.getByRole("button", { name: "显示API Key" })).toBeDisabled();
  });

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


it('已有模型时先显示连接列表，新建表单可取消且不会写入配置或读取密钥', async () => {
  const configuration = { id: 'saved', providerId: 'openai', provider: 'openai', model: 'saved-model', baseUrl: 'https://model.example/v1', apiKeyConfigured: true };
  const { calls } = fakeBackend({
    'GET /api/model/providers': () => ({ body: { providers: [] } }),
    'GET /api/model/configurations': () => ({ body: { configurations: [configuration], activeId: 'saved' } }),
  });
  render(<ModelSettings />);
  await screen.findByRole('list', { name: '已保存的模型配置' });
  expect(screen.queryByLabelText('模型 ID')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '新建配置' }));
  fireEvent.change(screen.getByLabelText('模型 ID'), { target: { value: 'unsaved' } });
  fireEvent.click(screen.getByRole('button', { name: '取消' }));
  expect(screen.queryByLabelText('模型 ID')).not.toBeInTheDocument();
  expect(screen.getByRole('list', { name: '已保存的模型配置' })).toBeInTheDocument();
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
});
