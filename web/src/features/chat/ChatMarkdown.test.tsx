import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { fakeBackend, message, role, sse } from "../../test/fakeBackend";

const hiyori = role({ id: "r1", name: "hiyori" });
const session = { sessionKey: "role:r1", roleId: "r1", createdAt: hiyori.createdAt, updatedAt: hiyori.updatedAt };

beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("聊天 Markdown", () => {
  it.each(["user", "assistant"] as const)("历史 %s 消息把中文加粗和行内代码显示为格式化正文", async (sender) => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [hiyori] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [message({
        id: "reply", role: sender, content: "主人没提日期 → 自动按**今天**填写。\n\n用 `recall_memory` 检查。",
      })] } }),
    });
    render(<App />);
    await screen.findByText(/主人没提日期/);
    const conversation = within(screen.getByRole("region", { name: "与hiyori的会话" }));
    expect(conversation.getByText("今天", { selector: "strong" })).toBeVisible();
    expect(conversation.getByText("recall_memory", { selector: "code" })).toBeVisible();
  });

  it("长回复保留中文标点加粗、换行、层级列表、表格和代码块", async () => {
    const content = [
      "## 今日安排", "", "她说**“你好”**然后继续。", "下一行保留换行。", "",
      "1. *查看待办*", "2. 更新记录", "   - ~~旧规则~~", "", "- [x] 已完成", "",
      "> 引用说明", "", "| 日期 | 状态 |", "| --- | --- |", "| 今天 | 待办 |", "",
      "```ts", "const today = '<今天>';", "```",
    ].join("\n");
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [hiyori] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [message({ id: "rich", role: "assistant", content })] } }),
    });
    render(<App />);
    const heading = await screen.findByRole("heading", { name: "今日安排", level: 2 });
    const body = heading.parentElement!;
    const reply = within(body);
    expect(reply.getByText("“你好”", { selector: "strong" })).toBeVisible();
    expect(body.querySelector("p > br")).toBeInTheDocument();
    expect(reply.getByText("查看待办", { selector: "em" }).closest("ol")).toBeInTheDocument();
    expect(reply.getByText("旧规则", { selector: "del" }).closest("ol ul")).toBeInTheDocument();
    expect(reply.getByRole("checkbox")).toBeChecked();
    expect(reply.getByRole("checkbox")).toBeDisabled();
    expect(reply.getByText("引用说明").closest("blockquote")).toBeInTheDocument();
    expect(within(reply.getByRole("table")).getByRole("cell", { name: "今天" })).toBeVisible();
    expect(reply.getByText("const today = '<今天>';", { selector: "pre code" })).toBeVisible();
  });

  it("显示 HTML 原文并限制链接，Markdown 图片不绕过已有附件展示", async () => {
    const content = [
      "[文档](https://example.test/docs) [邮箱](mailto:hello@example.test)", "",
      "[运行](javascript:alert%281%29) [数据](data:text/html,hello) [本地文件](file:///C:/secret.txt) [相对路径](/api/roles)", "",
      "![远程图片说明](https://example.test/tracking.png)", "",
      "<script>alert(1)</script>", '<img src="https://example.test/unsafe.png" onerror="alert(2)">', "",
      "<role-reply>", "回复正文", "</role-reply>", "", "末尾未完成标签 <span",
    ].join("\n");
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [hiyori] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [message({
        id: "safe", role: "assistant", content,
        metadata: { attachments: [{ id: "attachment", name: "会话附件", kind: "image" }] },
      })] } }),
    });
    render(<App />);
    const docs = await screen.findByRole("link", { name: "文档" });
    expect(docs).toHaveAttribute("href", "https://example.test/docs");
    expect(docs).toHaveAttribute("target", "_blank");
    expect(docs).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByRole("link", { name: "邮箱" })).toHaveAttribute("href", "mailto:hello@example.test");
    const region = screen.getByRole("region", { name: "与hiyori的会话" });
    const conversation = within(region);
    for (const label of ["运行", "数据", "本地文件", "相对路径"]) {
      expect(region).toHaveTextContent(label);
      expect(conversation.queryByRole("link", { name: label })).not.toBeInTheDocument();
    }
    expect(region.textContent).toContain("<script>alert(1)</script>");
    expect(region.textContent).toContain("<role-reply>\n回复正文\n</role-reply>");
    expect(region.textContent).toContain("末尾未完成标签 <span");
    expect(region.querySelector("script, role-reply, [onerror]")).toBeNull();
    expect(conversation.getByText("远程图片说明")).toBeVisible();
    expect(conversation.getAllByRole("img")).toHaveLength(1);
    expect(conversation.getByRole("img", { name: "会话附件" })).toHaveAttribute("src", "/api/roles/r1/attachments/attachment");
  });

  it("流式回复可读地显示未闭合片段，补齐后立即格式化且发送保留原文", async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; } });
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [hiyori] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
      "POST /api/roles/r1/messages": () => ({ stream }),
    });
    const emit = (event: string, data: unknown) => act(() => controller.enqueue(new TextEncoder().encode(sse(event, data))));
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "hiyori" });
    await user.type(screen.getByRole("textbox", { name: "消息内容" }), "请按**今天**整理{Enter}");
    await screen.findByRole("button", { name: "停止回复" });
    emit("user_message_accepted", { message: message({ id: "user", role: "user", content: "请按**今天**整理" }) });
    emit("assistant_generation_started", { messageId: "stream" });
    emit("assistant_delta", { messageId: "stream", delta: "**规则" });
    expect(await screen.findByText("**规则")).toBeVisible();
    emit("assistant_delta", { messageId: "stream", delta: "：**默认今天。\n\n```ts\nconst day = 26;" });
    expect(await screen.findByText("规则：", { selector: "strong" })).toBeVisible();
    expect(screen.getByText("const day = 26;", { selector: "pre code" })).toBeVisible();
    const content = "**规则：**默认今天。\n\n```ts\nconst day = 26;\n```";
    emit("assistant_completed", { message: message({ id: "stream", role: "assistant", content, sequence: 2 }) });
    act(() => controller.close());
    await screen.findByRole("button", { name: "发送" });
    expect(screen.getByText("规则：", { selector: "strong" })).toBeVisible();
    expect(screen.getByText("const day = 26;", { selector: "pre code" })).toBeVisible();
    expect(calls.find((call) => call.method === "POST")?.body).toMatchObject({ content: "请按**今天**整理" });
  });
});
