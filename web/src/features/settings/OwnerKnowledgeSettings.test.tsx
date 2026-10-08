import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { OwnerKnowledgeSettings } from "./OwnerKnowledgeSettings";
import { fakeBackend } from "../../test/fakeBackend";

describe("OwnerKnowledgeSettings", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("shows sync counts and lets the owner pause the application plugin", async () => {
    let enabled = true;
    const { calls } = fakeBackend({
      "GET /api/owner-knowledge": () => ({ body: {
        enabled,
        connected: true,
        oauthConfigured: true,
        appId: "cli_local",
        embeddingReady: true,
        lastError: null,
        lastSync: { trigger: "manual", complete: true, created: 2, updated: 1, deleted: 0, skipped: 3, failed: 0, unchanged: 5, errors: [] },
      } }),
      "GET /api/roles": () => ({ body: { roles: [{ id: "role-a", name: "测试角色" }] } }),
      "GET /api/roles/role-a/owner-understanding": () => ({ body: { understanding: null } }),
      "GET /api/owner-knowledge/documents": () => ({ body: { documents: [{ documentId: "table-1", objectType: "sheet", title: "学习计划表", url: "https://example.test/table", status: "skipped", error: "" }] } }),
      "POST /api/owner-knowledge/pause": () => { enabled = false; return { body: { enabled } }; },
    });

    render(<OwnerKnowledgeSettings />);
    const toggle = await screen.findByRole("checkbox", { name: "启用主人资料插件" });
    expect(toggle).toBeChecked();
    expect(screen.getByText("新增")).toBeInTheDocument();
    expect(screen.getByText("更新")).toBeInTheDocument();
    expect(screen.getByText("跳过")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "学习计划表" })).toHaveAttribute("href", "https://example.test/table");

    fireEvent.click(toggle);
    await waitFor(() => expect(calls.some((call) => call.method === "POST" && call.path === "/api/owner-knowledge/pause")).toBe(true));
    await waitFor(() => expect(toggle).not.toBeChecked());
  });
});
