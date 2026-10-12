import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MemorySettings } from "./MemorySettings";
import { fakeBackend } from "../../test/fakeBackend";

describe("MemorySettings", () => {
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
