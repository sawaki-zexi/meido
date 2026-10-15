import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemorySettings } from "./MemorySettings";
import { fakeBackend } from "../../test/fakeBackend";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("MemorySettings", () => {
  it("shows test progress and the result beside the embedding actions after saving a key", async () => {
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
    const key = await screen.findByLabelText("向量 API Key");
    fireEvent.change(key, { target: { value: "secret-key" } });
    fireEvent.click(screen.getByRole("button", { name: "保存向量配置" }));
    await screen.findByText("向量模型配置已保存并生效");
    expect(key).toHaveValue("");

    const embedding = within(screen.getByRole("region", { name: "向量模型" }));
    fireEvent.click(embedding.getByRole("button", { name: "测试连接" }));

    expect(embedding.getByRole("button", { name: "测试中…" })).toBeDisabled();
    expect(embedding.getByRole("status")).toHaveTextContent("正在测试向量模型连接");
    expect(embedding.getByRole("button", { name: "保存向量配置" })).toBeDisabled();
    expect(calls.find((call) => call.path === "/api/embedding/configuration/test")?.body).toMatchObject({ apiKey: "", model: "text-embedding-v3", dimensions: 1024 });

    completeTest();
    await waitFor(() => expect(embedding.getByRole("status")).toHaveTextContent("连接成功 · 42 ms"));
    expect(embedding.getByRole("button", { name: "测试连接" })).toBeEnabled();
    expect(calls.filter((call) => call.method === "PUT")).toHaveLength(1);
  });

  it.each([
    { description: "provider failure", reply: { status: 200, body: { ok: false, message: "API Key 无效" } }, expected: "失败：API Key 无效" },
    { description: "request failure", reply: { status: 502, body: { detail: "向量服务暂时不可用" } }, expected: "向量服务暂时不可用" },
    { description: "network failure", reply: null, expected: "无法连接本地后端，请确认服务正在运行" },
  ])("shows $description beside the embedding actions and allows retry", async ({ reply, expected }) => {
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
    const embedding = within(await screen.findByRole("region", { name: "向量模型" }));
    fireEvent.click(embedding.getByRole("button", { name: "测试连接" }));

    expect(await embedding.findByRole("alert")).toHaveTextContent(expected);
    expect(embedding.getByRole("button", { name: "测试连接" })).toBeEnabled();
    expect(embedding.getByLabelText("向量模型 ID")).toHaveValue("text-embedding-v3");

    fireEvent.click(embedding.getByRole("button", { name: "测试连接" }));
    await waitFor(() => expect(embedding.getByRole("status")).toHaveTextContent("连接成功"));
    expect(embedding.queryByRole("alert")).not.toBeInTheDocument();
    expect(embedding.getByRole("button", { name: "测试连接" })).toBeEnabled();
    expect(calls.filter((call) => call.method === "POST")).toHaveLength(2);
    expect(calls.some((call) => call.method === "PUT")).toBe(false);
  });

  it("toggles memory and embedding independently and saves embedding configuration", async () => {
    let state = { enabled: true, embeddingEnabled: true };
    const { calls } = fakeBackend({
      "GET /api/memory/settings": () => ({ body: {
        ...state,
        embedding: { configuration: { provider: "dashscope", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "text-embedding-v3", dimensions: 1024, apiKeyConfigured: false }, status: { configured: false, ready: false, enabled: state.embeddingEnabled } },
      } }),
      "PUT /api/memory/settings": (init) => {
        state = JSON.parse(String(init?.body)) as typeof state;
        return { body: {
          ...state,
          embedding: { configuration: { provider: "dashscope", baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "text-embedding-v3", dimensions: 1024, apiKeyConfigured: false }, status: { configured: false, ready: false, enabled: state.embeddingEnabled } },
        } };
      },
      "PUT /api/embedding/configuration": (init) => ({ body: { configuration: { ...JSON.parse(String(init?.body)), apiKeyConfigured: true }, status: { configured: true, ready: true, enabled: state.embeddingEnabled } } }),
    });

    render(<MemorySettings />);
    const memoryToggle = await screen.findByRole("checkbox", { name: "启用记忆功能" });
    const embeddingToggle = screen.getByRole("checkbox", { name: "启用向量模型" });
    fireEvent.click(memoryToggle);
    await waitFor(() => expect(calls.some((call) => call.method === "PUT" && call.path === "/api/memory/settings" && (call.body as { enabled: boolean }).enabled === false)).toBe(true));
    fireEvent.click(embeddingToggle);
    await waitFor(() => expect(calls.some((call) => call.method === "PUT" && call.path === "/api/memory/settings" && (call.body as { embeddingEnabled: boolean }).embeddingEnabled === false)).toBe(true));
    fireEvent.change(screen.getByLabelText("向量 API Key"), { target: { value: "secret-key" } });
    fireEvent.click(screen.getByRole("button", { name: "保存向量配置" }));
    await waitFor(() => expect(calls.some((call) => call.method === "PUT" && call.path === "/api/embedding/configuration")).toBe(true));
    expect(calls.find((call) => call.path === "/api/embedding/configuration")?.body).toMatchObject({ apiKey: "secret-key" });
  });
});
