import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import type { TodoItem, TodoReminder } from "../../api/types";
import { fakeBackend, message, role } from "../../test/fakeBackend";

const todo = (id: string, title: string, patch: Partial<TodoItem> = {}): TodoItem => ({
  id, title, description: "", status: "inbox", dueDate: null, dueAt: null, reminderDate: null, reminderAt: null,
  timezone: "Asia/Shanghai", sourceRoleId: null, sourceSessionKey: null, sourceMessageId: null, sourceAvailable: true,
  createdAt: "2026-10-24T01:00:00Z", updatedAt: "2026-10-24T01:00:00Z", completedAt: null, cancelledAt: null, ...patch,
});

function serveTodos(items: TodoItem[], routes: Parameters<typeof fakeBackend>[0] = {}) {
  return fakeBackend({
    "GET /api/roles": () => ({ body: { roles: [] } }),
    "GET /api/todos/settings": () => ({ body: { enabled: true, remindersEnabled: true, timezone: "Asia/Shanghai" } }),
    "GET /api/todos": () => ({ body: { todos: items } }),
    "GET /api/todos/reminders": () => ({ body: { reminders: [], unreadCount: 0 } }),
    "GET /api/todos/diagnostics": () => ({ body: { items: [] } }),
    ...routes,
  });
}

beforeEach(() => {
  localStorage.clear(); sessionStorage.clear();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-24T16:30:00Z"));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("Today 风格待办面板", () => {
  it("加载失败显示重试入口，恢复连接后显示原有事项", async () => {
    let failed = true;
    serveTodos([], { "GET /api/todos": () => failed ? { status: 503, body: { detail: "连接暂时中断" } } : { body: { todos: [todo("saved", "原有的安排")] } } });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "待办" }));
    const panel = await screen.findByRole("dialog", { name: "待办" });
    expect(await within(panel).findByRole("alert")).toHaveTextContent("连接暂时中断");
    expect(within(panel).queryByText("眼前的事，都告一段落")).not.toBeInTheDocument();
    failed = false;
    await user.click(within(panel).getByRole("button", { name: "重新加载" }));
    expect(await within(panel).findByText("原有的安排")).toBeInTheDocument();
    expect(within(panel).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("键盘可切换待办与提醒页签，Esc 关闭后焦点回到入口", async () => {
    serveTodos([]);
    const user = userEvent.setup();
    render(<App />);
    const entry = await screen.findByRole("button", { name: "待办" });
    await user.click(entry);
    const panel = await screen.findByRole("dialog", { name: "待办" });
    const list = await within(panel).findByRole("tab", { name: /^待办/ });
    list.focus();
    await user.keyboard("{ArrowRight}");
    const reminders = within(panel).getByRole("tab", { name: "提醒" });
    expect(reminders).toHaveAttribute("aria-selected", "true");
    expect(reminders).toHaveFocus();
    await user.keyboard("{Home}");
    expect(list).toHaveFocus();
    expect(list).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "待办" })).not.toBeInTheDocument();
    expect(entry).toHaveFocus();
  });

  it("提醒可重试、标已读并反复定位已完成事项，还能回到来源对话", async () => {
    const items = [todo("source", "确认出行安排", { status: "completed", sourceRoleId: "r1", sourceSessionKey: "role:r1", sourceMessageId: "m1" })];
    const reminder: TodoReminder = { id: "reminder", todoId: "source", title: "确认出行安排", todoStatus: "scheduled", dueKey: "source:date:2026-10-25", dueAt: "2026-10-24T16:00:00Z", status: "failed", attempts: 1, lastError: "网络暂时不可用", readAt: null, createdAt: "2026-10-24T16:00:00Z", updatedAt: "2026-10-24T16:00:00Z", deliveredAt: null };
    serveTodos(items, {
      "GET /api/roles": () => ({ body: { roles: [role({ id: "r1", name: "爱丽丝" })] } }),
      "GET /api/roles/r1/session": () => ({ body: { session: { sessionKey: "role:r1", roleId: "r1" }, messages: [message({ id: "m1", role: "user", content: "帮我记得确认出行安排" })] } }),
      "GET /api/todos/reminders": () => ({ body: { reminders: [reminder], unreadCount: reminder.readAt ? 0 : 1 } }),
      "POST /api/todos/reminders/reminder/read": () => { reminder.readAt = "2026-10-24T16:30:00Z"; return { status: 204 }; },
      "POST /api/todos/reminders/reminder/retry": () => { reminder.status = "pending"; reminder.lastError = null; return { body: { ok: true } }; },
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "爱丽丝", level: 1 });
    await user.click(await screen.findByRole("button", { name: /^待办提醒/ }));
    const panel = await screen.findByRole("dialog", { name: "待办" });
    await user.click(await within(panel).findByRole("tab", { name: /^提醒/ }));
    expect(await within(panel).findByText("网络暂时不可用")).toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: "重试提醒" }));
    expect(await within(panel).findByText("待投递")).toBeInTheDocument();
    for (let i = 0; i < 2; i++) {
      const reminderButton = await within(panel).findByRole("button", { name: /确认出行安排/ });
      await waitFor(() => expect(reminderButton).toBeEnabled());
      await user.click(reminderButton);
      const item = await within(panel).findByRole("button", { name: "查看待办：确认出行安排" });
      await waitFor(() => expect(item).toHaveFocus());
      if (i === 0) {
        await user.click(within(panel).getByRole("tab", { name: "提醒" }));
        expect(within(panel).queryByText("未读")).not.toBeInTheDocument();
      }
    }
    await user.click(within(panel).getByRole("button", { name: "查看待办：确认出行安排" }));
    await user.click(within(panel).getByRole("button", { name: "查看来源" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "待办" })).not.toBeInTheDocument());
    expect(await screen.findByText("帮我记得确认出行安排")).toBeInTheDocument();
  });

  it("保存失败保留草稿，重试期间禁止重复编辑，创建后可以完成、取消和恢复", async () => {
    const items: TodoItem[] = [];
    let fail = true;
    let release!: () => void;
    const pending = new Promise<void>(resolve => { release = resolve; });
    serveTodos(items, {
      "POST /api/todos": async init => {
        if (fail) { fail = false; return { status: 503, body: { detail: "暂时无法保存" } }; }
        await pending;
        items.push(todo("created", "寄出明信片", JSON.parse(String(init?.body))));
        return { status: 201, body: items[0] };
      },
      "PUT /api/todos/created/status": init => {
        items[0].status = JSON.parse(String(init?.body)).status;
        return { body: items[0] };
      },
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "待办" }));
    const panel = await screen.findByRole("dialog", { name: "待办" });
    await user.click(await within(panel).findByRole("button", { name: "添加一件待办" }));
    await user.type(within(panel).getByLabelText("标题"), "寄出明信片");
    await user.click(within(panel).getByRole("button", { name: "保存" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("暂时无法保存");
    expect(within(panel).getByLabelText("标题")).toHaveValue("寄出明信片");
    await user.click(within(panel).getByRole("button", { name: "保存" }));
    expect(within(panel).getByLabelText("标题")).toBeDisabled();
    release();
    await user.click(await within(panel).findByRole("checkbox", { name: "完成寄出明信片" }));
    await user.click(within(panel).getByRole("button", { name: /^已完成/ }));
    const completed = await within(panel).findByRole("checkbox", { name: "恢复寄出明信片" });
    expect(completed).toBeChecked();
    await waitFor(() => expect(completed).toBeEnabled());
    await user.click(completed);
    await user.click(within(panel).getByRole("button", { name: /^未完成/ }));
    const row = await within(panel).findByRole("button", { name: "查看待办：寄出明信片" });
    await waitFor(() => expect(row).toBeEnabled());
    await user.click(row);
    await user.click(within(panel).getByRole("button", { name: "取消待办" }));
    await user.click(await within(panel).findByRole("button", { name: /^已取消/ }));
    await user.click(await within(panel).findByRole("button", { name: "恢复" }));
    await user.click(within(panel).getByRole("button", { name: /^未完成/ }));
    expect(await within(panel).findByRole("region", { name: "未安排" })).toHaveTextContent("寄出明信片");
  });

  it("编辑时按主人时区显示精确截止与提醒时间，只改标题保留原始时刻，清空日期可移回未安排", async () => {
    const items = [todo("precise", "跨时区会议", { status: "scheduled", dueAt: "2026-10-24T18:15:30Z", reminderAt: "2026-10-24T17:45:20Z", timezone: "UTC" })];
    const { calls } = serveTodos(items, {
      "PUT /api/todos/precise": init => {
        Object.assign(items[0], JSON.parse(String(init?.body)));
        if (!items[0].dueDate && !items[0].dueAt && !items[0].reminderDate && !items[0].reminderAt) items[0].status = "inbox";
        return { body: items[0] };
      },
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "待办" }));
    const panel = await screen.findByRole("dialog", { name: "待办" });
    await user.click(await within(panel).findByRole("button", { name: "查看待办：跨时区会议" }));
    expect(within(panel).getByLabelText("截止日期")).toHaveValue("2026-10-25");
    expect(within(panel).getByLabelText("截止时刻")).toHaveValue("02:15");
    expect(within(panel).getByLabelText("提醒日期")).toHaveValue("2026-10-25");
    expect(within(panel).getByLabelText("提醒时刻")).toHaveValue("01:45");
    await user.type(within(panel).getByLabelText("标题"), "确认");
    await user.click(within(panel).getByRole("button", { name: "保存" }));
    const row = await within(panel).findByRole("button", { name: "查看待办：跨时区会议确认" });
    expect(row).toHaveTextContent("今天 02:15 截止");
    expect(calls.find(call => call.method === "PUT")?.body).toMatchObject({ dueAt: "2026-10-24T18:15:30Z", reminderAt: "2026-10-24T17:45:20Z" });
    await user.click(row);
    fireEvent.change(within(panel).getByLabelText("截止日期"), { target: { value: "" } });
    fireEvent.change(within(panel).getByLabelText("提醒日期"), { target: { value: "" } });
    await user.click(within(panel).getByRole("button", { name: "保存" }));
    expect(await within(panel).findByRole("region", { name: "未安排" })).toHaveTextContent("跨时区会议确认");
  });

  it("按主人时区分组未完成事项，并能搜索和查看已完成事项", async () => {
    serveTodos([
      todo("past", "补交报告", { status: "scheduled", dueDate: "2026-10-24" }),
      todo("today", "准备会议", { status: "scheduled", reminderAt: "2026-10-24T17:00:00Z" }),
      todo("next", "周末采购", { status: "scheduled", dueDate: "2026-10-26" }),
      todo("inbox", "整理书架"),
      todo("done", "已经寄信", { status: "completed", completedAt: "2026-10-24T17:00:00Z" }),
    ]);
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole("button", { name: "待办" }));
    const panel = await screen.findByRole("dialog", { name: "待办" });
    expect(await within(panel).findByRole("heading", { name: "今天" })).toBeInTheDocument();
    expect(within(panel).getByRole("region", { name: "今天" })).toHaveTextContent("准备会议");
    expect(within(panel).getByRole("region", { name: "之前" })).toHaveTextContent("补交报告");
    expect(within(panel).getByRole("region", { name: "明天" })).toHaveTextContent("周末采购");
    expect(within(panel).getByRole("region", { name: "未安排" })).toHaveTextContent("整理书架");
    expect(within(panel).queryByText("已经寄信")).not.toBeInTheDocument();
    await user.type(within(panel).getByRole("searchbox", { name: "搜索待办" }), "会议");
    expect(within(panel).getByText("准备会议")).toBeInTheDocument();
    expect(within(panel).queryByText("整理书架")).not.toBeInTheDocument();
    await user.clear(within(panel).getByRole("searchbox", { name: "搜索待办" }));
    await user.click(within(panel).getByRole("button", { name: /^已完成/ }));
    expect(within(panel).getByText("已经寄信")).toBeInTheDocument();
    expect(within(panel).queryByText("准备会议")).not.toBeInTheDocument();
  });
});
