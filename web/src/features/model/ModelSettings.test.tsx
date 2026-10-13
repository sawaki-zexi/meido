import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemorySettings } from "../settings/MemorySettings";
import { fakeBackend } from "../../test/fakeBackend";

describe("MemorySettings embedding configuration", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => window.history.replaceState({}, "", "/"));

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
