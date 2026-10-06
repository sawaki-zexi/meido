import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RoleModelSelector } from "./RoleModelSelector";
import { fakeBackend } from "../../test/fakeBackend";

const first = { id: "m1", providerId: "openai", provider: "OpenAI", baseUrl: "https://example.test/v1", model: "gpt-test", apiKeyConfigured: true };
const second = { ...first, id: "m2", model: "claude-test" };

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("角色聊天模型选择", () => {
  it("加载角色实际使用模型，并持久化切换结果", async () => {
    let selected: string | null = "m1";
    const { calls } = fakeBackend({
      "GET /api/model/configurations": () => ({ body: { configurations: [first, second], activeId: "m1" } }),
      "GET /api/roles/r1/model-configuration": () => ({ body: { configurationId: selected, effectiveConfigurationId: selected ?? "m1" } }),
      "PUT /api/roles/r1/model-configuration": (init) => {
        selected = (JSON.parse(String(init?.body)) as { configurationId: string | null }).configurationId;
        return { body: { configurationId: selected, effectiveConfigurationId: selected ?? "m1" } };
      },
    });
    const user = userEvent.setup();
    const view = render(<RoleModelSelector roleId="r1" />);

    const trigger = await screen.findByRole("button", { name: "选择聊天模型" });
    await waitFor(() => expect(trigger).toHaveTextContent("gpt-test"));
    await user.click(trigger);
    const menu = screen.getByRole("menu", { name: "聊天模型" });
    const option = await within(menu).findByRole("menuitemradio", { name: /claude-test/ });
    expect(within(menu).getByRole("menuitemradio", { name: /gpt-test/ })).toHaveAttribute("aria-checked", "true");
    await user.click(option);

    await waitFor(() => expect(trigger).toHaveTextContent("claude-test"));
    expect(selected).toBe("m2");
    expect(calls.find((call) => call.method === "PUT")?.body).toEqual({ configurationId: "m2" });
    expect(screen.queryByRole("menu")).toBeNull();

    view.unmount();
    render(<RoleModelSelector roleId="r1" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "选择聊天模型" })).toHaveTextContent("claude-test"));
  });

  it("切换失败时保留当前模型并显示错误", async () => {
    fakeBackend({
      "GET /api/model/configurations": () => ({ body: { configurations: [first, second], activeId: "m1" } }),
      "GET /api/roles/r1/model-configuration": () => ({ body: { configurationId: "m1", effectiveConfigurationId: "m1" } }),
      "PUT /api/roles/r1/model-configuration": () => ({ status: 404, body: { detail: "模型连接已不存在" } }),
    });
    const user = userEvent.setup();
    render(<RoleModelSelector roleId="r1" />);
    const trigger = await screen.findByRole("button", { name: "选择聊天模型" });
    await waitFor(() => expect(trigger).toHaveTextContent("gpt-test"));
    await user.click(trigger);
    await user.click(await within(screen.getByRole("menu")).findByRole("menuitemradio", { name: /claude-test/ }));

    expect(await screen.findByRole("status")).toHaveTextContent("模型连接已不存在");
    expect(trigger).toHaveTextContent("gpt-test");
  });

  it("绑定模型已删除时显示明确错误", async () => {
    fakeBackend({
      "GET /api/model/configurations": () => ({ body: { configurations: [first], activeId: "m1" } }),
      "GET /api/roles/r1/model-configuration": () => ({ body: { configurationId: "missing", effectiveConfigurationId: "missing" } }),
    });
    render(<RoleModelSelector roleId="r1" />);
    expect(await screen.findByRole("button", { name: "选择聊天模型" })).toHaveTextContent("模型已不存在");
    expect(await screen.findByRole("status")).toHaveTextContent("当前角色绑定的模型已不存在");
  });

  it("生成期间禁用选择器", async () => {
    fakeBackend({
      "GET /api/model/configurations": () => ({ body: { configurations: [first], activeId: "m1" } }),
      "GET /api/roles/r1/model-configuration": () => ({ body: { configurationId: "m1", effectiveConfigurationId: "m1" } }),
    });
    render(<RoleModelSelector roleId="r1" disabled />);
    expect(await screen.findByRole("button", { name: "选择聊天模型" })).toBeDisabled();
  });

  it("菜单支持方向键和 Escape", async () => {
    fakeBackend({
      "GET /api/model/configurations": () => ({ body: { configurations: [first, second], activeId: "m1" } }),
      "GET /api/roles/r1/model-configuration": () => ({ body: { configurationId: "m1", effectiveConfigurationId: "m1" } }),
    });
    const user = userEvent.setup();
    render(<RoleModelSelector roleId="r1" />);
    const trigger = await screen.findByRole("button", { name: "选择聊天模型" });
    await waitFor(() => expect(trigger).toHaveTextContent("gpt-test"));
    await user.click(trigger);
    const selected = await within(screen.getByRole("menu")).findByRole("menuitemradio", { name: /gpt-test/ });
    selected.focus();
    await user.keyboard("{ArrowDown}");
    expect(within(screen.getByRole("menu")).getByRole("menuitemradio", { name: /claude-test/ })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole("menu")).toBeNull();
  });
});
