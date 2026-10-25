import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "../../App";
import { fakeBackend, message, role } from "../../test/fakeBackend";

const alice = role({ id: "r1", name: "爱丽丝" });
const session = { sessionKey: "role:r1", roleId: "r1", createdAt: alice.createdAt, updatedAt: alice.updatedAt };
const attachment = { id: "animated", name: "动画.gif", kind: "image", mediaType: "image/gif", size: 6 };
beforeEach(() => { sessionStorage.clear(); localStorage.clear(); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("聊天图片反馈", () => {
  it("文件选择器允许 GIF，原文件上传后预览失败可重试且不重复上传", async () => {
    const { calls } = fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [] } }),
      "POST /api/roles/r1/attachments": () => ({ body: attachment }),
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole("heading", { name: "爱丽丝" });
    const file = new File(["GIF89a"], "动画.gif", { type: "image/gif" });
    const picker = screen.getByLabelText("附件文件");
    await user.upload(picker, file);
    const preview = await screen.findByLabelText("待发送附件");
    const uploads = calls.filter(call => call.path.endsWith("/attachments"));
    expect(uploads).toHaveLength(1);
    expect(uploads[0].body).toBe(file);
    await waitFor(() => expect(picker).toHaveValue(""));
    expect(within(preview).getByRole("status")).toHaveTextContent("图片加载中");
    const image = within(preview).getByRole("img", { name: "动画.gif" });
    fireEvent.error(image);
    expect(within(preview).getByRole("status")).toHaveTextContent("图片加载失败");
    await user.click(within(preview).getByRole("button", { name: "重新加载图片 动画.gif" }));
    const retried = within(preview).getByRole("img", { name: "动画.gif" });
    expect(retried).not.toBe(image);
    expect(retried).toHaveAttribute("src", "/api/roles/r1/attachments/animated");
    fireEvent.load(retried);
    expect(within(preview).queryByRole("status")).not.toBeInTheDocument();
    expect(calls.filter(call => call.path.endsWith("/attachments"))).toHaveLength(1);
    expect(screen.getByRole("button", { name: "发送" })).toBeEnabled();
    await user.click(within(preview).getByRole("button", { name: "移除 动画.gif" }));
    expect(screen.queryByLabelText("待发送附件")).not.toBeInTheDocument();
  });

  it("历史图片显示加载和失败状态，重试保留原图新标签页链接", async () => {
    fakeBackend({
      "GET /api/roles": () => ({ body: { roles: [alice] } }),
      "GET /api/roles/r1/session": () => ({ body: { session, messages: [message({ id: "m1", role: "assistant", content: "", metadata: { attachments: [attachment] } })] } }),
    });
    const user = userEvent.setup();
    render(<App />);
    const image = await screen.findByRole("img", { name: "动画.gif" });
    const messages = within(within(screen.getByRole("region", { name: "与爱丽丝的会话" })).getByRole("list"));
    const link = screen.getByRole("link", { name: "动画.gif" });
    expect(link).toHaveAttribute("href", "/api/roles/r1/attachments/animated");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noreferrer");
    expect(messages.getByRole("status")).toHaveTextContent("图片加载中");
    fireEvent.error(image);
    expect(messages.getByRole("status")).toHaveTextContent("图片加载失败");
    const retry = screen.getByRole("button", { name: "重新加载图片 动画.gif" });
    expect(link).not.toContainElement(retry);
    await user.click(retry);
    const retried = screen.getByRole("img", { name: "动画.gif" });
    expect(retried).not.toBe(image);
    fireEvent.load(retried);
    expect(screen.queryByText("图片加载中…")).not.toBeInTheDocument();
    expect(screen.queryByText("图片加载失败")).not.toBeInTheDocument();
    expect(retried).toBeVisible();
  });
});
