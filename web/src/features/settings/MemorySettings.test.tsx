import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemorySettings } from "./MemorySettings";
import { fakeBackend } from "../../test/fakeBackend";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function expectFeedbackBelowActions(saveButton: HTMLElement, feedback: HTMLElement) {
  expect(saveButton.compareDocumentPosition(feedback) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
}

describe("MemorySettings", () => {
  it("only reveals the saved embedding key on demand and keeps it out of the test draft", async () => {
    const configuration = { provider: "dashscope", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "text-embedding-v3", dimensions: 1024, apiKeyConfigured: true };
    const { calls } = fakeBackend({
      "GET /api/memory/settings": () => ({ body: {
        enabled: true, embeddingEnabled: true,
        embedding: { configuration, status: { configured: true, ready: true, enabled: true } },
      } }),
      "POST /api/embedding/configuration/reveal-key": () => ({ body: { apiKey: "saved-embedding-secret" } }),
      "POST /api/embedding/configuration/test": () => ({ body: { ok: true, message: "连接成功", latencyMs: 12 } }),
      "PUT /api/embedding/configuration": () => ({ body: { configuration, status: { configured: true, ready: true, enabled: true } } }),
    });

    render(<MemorySettings />);
    fireEvent.click(await screen.findByRole("button", { name: "向量模型" }));
    const key = await screen.findByLabelText("向量 API Key");
    expect(key).toHaveValue("");
    expect(key).toHaveAttribute("placeholder", "••••••");
    expect(calls.some((call) => call.path.endsWith("/reveal-key"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "显示向量 API Key" }));
    await waitFor(() => expect(key).toHaveValue("saved-embedding-secret"));
    expect(key).toHaveAttribute("type", "text");

    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    await screen.findByText("连接成功 · 12 ms");
    expect(calls.find((call) => call.path === "/api/embedding/configuration/test")?.body).toMatchObject({ apiKey: "" });
    expect(key).toHaveValue("");
    expect(key).toHaveAttribute("type", "password");
    fireEvent.click(screen.getByRole("button", { name: "显示向量 API Key" }));
    await waitFor(() => expect(key).toHaveValue("saved-embedding-secret"));
    expect(calls.filter((call) => call.path.endsWith("/reveal-key"))).toHaveLength(2);

    fireEvent.change(key, { target: { value: "replacement-embedding-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "保存向量配置" }));
    await screen.findByText("向量模型配置已保存并生效");
    expect(calls.find((call) => call.method === "PUT")?.body).toMatchObject({ apiKey: "replacement-embedding-secret" });
    expect(key).toHaveValue("");
    expect(key).toHaveAttribute("placeholder", "••••••");
  });

  it("shows test progress and the result below the embedding actions after saving a key", async () => {
    const configuration = { provider: "dashscope", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "text-embedding-v3", dimensions: 1024, apiKeyConfigured: false };
    let completeTest!: () => void;
    const { calls } = fakeBackend({
      "GET /api/memory/settings": () => ({ body: {
        enabled: true, embeddingEnabled: true,
        embedding: { configuration, status: { configured: false, ready: false, enabled: true } },
      } }),
      "PUT /api/embedding/configuration": () => ({ body: {
        configuration: { ...configuration, apiKeyConfigured: true },
        status: { configured: true, ready: true, enabled: true },
      } }),
      "POST /api/embedding/configuration/test": () => new Promise((resolve) => {
        completeTest = () => resolve({ body: { ok: true, message: "连接成功", latencyMs: 42 } });
      }),
    });

    render(<MemorySettings />);
    fireEvent.click(await screen.findByRole("button", { name: "向量模型" }));
    const key = await screen.findByLabelText("向量 API Key");
    fireEvent.change(key, { target: { value: "secret-key" } });
    fireEvent.click(screen.getByRole("button", { name: "保存向量配置" }));
    await screen.findByText("向量模型配置已保存并生效");
    expect(key).toHaveValue("");

    const embedding = within(screen.getByRole("region", { name: "向量模型" }));
    expectFeedbackBelowActions(embedding.getByRole("button", { name: "保存向量配置" }), embedding.getByRole("status"));
    fireEvent.click(embedding.getByRole("button", { name: "测试连接" }));

    expect(embedding.getByRole("button", { name: "测试中…" })).toBeDisabled();
    expect(embedding.getByRole("status")).toHaveTextContent("正在测试向量模型连接");
    expectFeedbackBelowActions(embedding.getByRole("button", { name: "保存向量配置" }), embedding.getByRole("status"));
    expect(embedding.getByRole("button", { name: "保存向量配置" })).toBeDisabled();
    expect(calls.find((call) => call.path === "/api/embedding/configuration/test")?.body).toMatchObject({ apiKey: "", model: "text-embedding-v3", dimensions: 1024 });

    completeTest();
    await waitFor(() => expect(embedding.getByRole("status")).toHaveTextContent("连接成功 · 42 ms"));
    expectFeedbackBelowActions(embedding.getByRole("button", { name: "保存向量配置" }), embedding.getByRole("status"));
    expect(embedding.getByRole("button", { name: "测试连接" })).toBeEnabled();
    expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1);
  });

  it.each([
    { description: "provider failure", reply: { status: 200, body: { ok: false, message: "API Key 无效" } }, expected: "失败：API Key 无效" },
    { description: "request failure", reply: { status: 502, body: { detail: "向量服务暂时不可用" } }, expected: "向量服务暂时不可用" },
    { description: "network failure", reply: null, expected: "无法连接本地后端，请确认服务正在运行" },
  ])("shows $description below the embedding actions and allows retry", async ({ reply, expected }) => {
    let attempts = 0;
    const { calls } = fakeBackend({
      "GET /api/memory/settings": () => ({ body: {
        enabled: true, embeddingEnabled: true,
        embedding: {
          configuration: { provider: "dashscope", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "text-embedding-v3", dimensions: 1024, apiKeyConfigured: true },
          status: { configured: true, ready: true, enabled: true },
        },
      } }),
      "POST /api/embedding/configuration/test": () => {
        if (++attempts > 1) return { body: { ok: true, message: "连接成功" } };
        if (!reply) throw new TypeError("Failed to fetch");
        return reply;
      },
    });

    render(<MemorySettings />);
    fireEvent.click(await screen.findByRole("button", { name: "向量模型" }));
    const embedding = within(await screen.findByRole("region", { name: "向量模型" }));
    fireEvent.click(embedding.getByRole("button", { name: "测试连接" }));

    expect(await embedding.findByRole("alert")).toHaveTextContent(expected);
    expectFeedbackBelowActions(embedding.getByRole("button", { name: "保存向量配置" }), embedding.getByRole("alert"));
    expect(embedding.getByRole("button", { name: "测试连接" })).toBeEnabled();
    expect(embedding.getByLabelText("向量模型 ID")).toHaveValue("text-embedding-v3");

    fireEvent.click(embedding.getByRole("button", { name: "测试连接" }));
    await waitFor(() => expect(embedding.getByRole("status")).toHaveTextContent("连接成功"));
    expectFeedbackBelowActions(embedding.getByRole("button", { name: "保存向量配置" }), embedding.getByRole("status"));
    expect(embedding.queryByRole("alert")).not.toBeInTheDocument();
    expect(embedding.getByRole("button", { name: "测试连接" })).toBeEnabled();
    expect(calls.filter((call) => call.method === "POST")).toHaveLength(2);
    expect(calls.some((call) => call.method === "PUT")).toBe(false);
  });

  it("shows the saved embedding status error below the embedding actions", async () => {
    fakeBackend({
      "GET /api/memory/settings": () => ({ body: {
        enabled: true, embeddingEnabled: true,
        embedding: {
          configuration: { provider: "dashscope", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "text-embedding-v3", dimensions: 1024, apiKeyConfigured: true },
          status: { configured: true, ready: false, enabled: true, lastError: "向量服务暂时不可用" },
        },
      } }),
    });

    render(<MemorySettings />);
    fireEvent.click(await screen.findByRole("button", { name: "向量模型" }));
    const embedding = within(await screen.findByRole("region", { name: "向量模型" }));

    expect(embedding.getByRole("alert")).toHaveTextContent("向量服务暂时不可用");
    expectFeedbackBelowActions(embedding.getByRole("button", { name: "保存向量配置" }), embedding.getByRole("alert"));
  });

  it("toggles memory and embedding independently and saves embedding configuration", async () => {
    let state = { enabled: true, embeddingEnabled: true };
    const { calls } = fakeBackend({
      "GET /api/memory/settings": () => ({ body: {
        ...state,
        embedding: { configuration: { provider: "dashscope", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "text-embedding-v3", dimensions: 1024, apiKeyConfigured: false }, status: { configured: false, ready: false, enabled: state.embeddingEnabled } },
      } }),
      "PUT /api/memory/settings": (init) => {
        state = { ...state, ...JSON.parse(String(init?.body)) };
        return { body: {
          ...state,
          embedding: { configuration: { provider: "dashscope", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "text-embedding-v3", dimensions: 1024, apiKeyConfigured: false }, status: { configured: false, ready: false, enabled: state.embeddingEnabled } },
        } };
      },
      "PUT /api/embedding/configuration": (init) => ({ body: { configuration: { ...JSON.parse(String(init?.body)), apiKeyConfigured: true }, status: { configured: true, ready: true, enabled: state.embeddingEnabled } } }),
    });

    render(<MemorySettings />);
    const memoryToggle = await screen.findByRole("switch", { name: "启用记忆功能" });
    fireEvent.click(memoryToggle);
    await waitFor(() => expect(calls.some((call) => call.method === "PUT" && call.path === "/api/memory/settings" && (call.body as { enabled: boolean }).enabled === false)).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "向量模型" }));
    fireEvent.click(screen.getByRole("switch", { name: "启用向量模型" }));
    await waitFor(() => expect(calls.some((call) => call.method === "PUT" && call.path === "/api/memory/settings" && (call.body as { embeddingEnabled: boolean }).embeddingEnabled === false)).toBe(true));
    fireEvent.change(screen.getByLabelText("向量 API Key"), { target: { value: "secret-key" } });
    fireEvent.click(screen.getByRole("button", { name: "保存向量配置" }));
    await waitFor(() => expect(calls.some((call) => call.method === "PUT" && call.path === "/api/embedding/configuration")).toBe(true));
    expect(calls.find((call) => call.path === "/api/embedding/configuration")?.body).toMatchObject({ apiKey: "secret-key" });
  });
});


it('基础页不堆叠向量表单，子页往返保留未保存内容', async () => {
  const { calls } = fakeBackend({ 'GET /api/memory/settings': () => ({ body: { enabled: true, embeddingEnabled: false,
    embedding: { configuration: { provider: 'local', baseUrl: 'http://localhost/v1', model: 'embedding', dimensions: 1024, apiKeyConfigured: false }, status: { configured: false, ready: false, enabled: false } } } }) });
  render(<MemorySettings />);
  await screen.findByRole('switch', { name: '启用记忆功能' });
  expect(screen.queryByLabelText('向量模型 ID')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '向量模型' }));
  fireEvent.change(screen.getByLabelText('向量模型 ID'), { target: { value: 'unsaved-embedding' } });
  fireEvent.click(screen.getByRole('button', { name: '基础' }));
  expect(screen.queryByLabelText('向量模型 ID')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '向量模型' }));
  expect(screen.getByLabelText('向量模型 ID')).toHaveValue('unsaved-embedding');
  expect(calls.some(call => call.method !== 'GET')).toBe(false);
});

it('重回缓存页面刷新开关事实并保留向量草稿，切换只提交当前字段', async () => {
  let enabled = true;
  const configuration = { provider: 'local', baseUrl: 'http://localhost/v1', model: 'saved-embedding', dimensions: 32, apiKeyConfigured: false };
  const state = () => ({ enabled, embeddingEnabled: false, embedding: { configuration, status: { configured: false, ready: false, enabled: false } } });
  const { calls } = fakeBackend({
    'GET /api/memory/settings': () => ({ body: state() }),
    'PUT /api/memory/settings': () => ({ body: state() }),
  });
  const view = render(<MemorySettings />);
  await screen.findByRole('switch', { name: '启用记忆功能' });
  fireEvent.click(screen.getByRole('button', { name: '向量模型' }));
  fireEvent.change(screen.getByLabelText('向量模型 ID'), { target: { value: 'unsaved-embedding' } });
  view.rerender(<MemorySettings active={false} />);
  enabled = false;
  view.rerender(<MemorySettings />);
  await waitFor(() => expect(calls.filter(call => call.method === 'GET')).toHaveLength(2));
  expect(screen.getByLabelText('向量模型 ID')).toHaveValue('unsaved-embedding');
  fireEvent.click(screen.getByRole('button', { name: '基础' }));
  await waitFor(() => expect(screen.getByRole('switch', { name: '启用记忆功能' })).not.toBeChecked());
  fireEvent.click(screen.getByRole('button', { name: '向量模型' }));
  fireEvent.click(screen.getByRole('switch', { name: '启用向量模型' }));
  await waitFor(() => expect(calls.find(call => call.method === 'PUT')?.body).toEqual({ embeddingEnabled: true }));
});
