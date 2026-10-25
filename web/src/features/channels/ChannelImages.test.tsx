import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChannelAttachments } from "./ChannelAttachments";
import { PhoneMessage } from "./PhoneMessage";
import { messagePresentation, type ConversationMessage } from "./channelMessagePresentation";
import { role } from "../../test/fakeBackend";

afterEach(cleanup);
const attachment = { id: "animation", name: "动画.gif", kind: "image" };

describe("渠道图片反馈", () => {
  it("失败后可重试同一地址，成功通知滚动；角色更换后重置旧图片状态", async () => {
    const onMediaLoad = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<ChannelAttachments roleId="r1" attachments={[attachment]} onMediaLoad={onMediaLoad} />);
    const first = screen.getByRole("img", { name: attachment.name });
    expect(screen.getByRole("status")).toHaveTextContent("图片加载中");
    fireEvent.error(first);
    expect(screen.getByRole("status")).toHaveTextContent("图片加载失败");
    await user.click(screen.getByRole("button", { name: "重新加载图片 动画.gif" }));
    const retried = screen.getByRole("img", { name: attachment.name });
    expect(retried).not.toBe(first);
    expect(retried).toHaveAttribute("src", "/api/roles/r1/attachments/animation");
    onMediaLoad.mockClear();
    fireEvent.load(retried);
    expect(onMediaLoad).toHaveBeenCalledOnce();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    fireEvent.error(retried);
    rerender(<ChannelAttachments roleId="r2" attachments={[attachment]} onMediaLoad={onMediaLoad} />);
    expect(screen.getByRole("status")).toHaveTextContent("图片加载中");
    expect(screen.queryByRole("button", { name: /重新加载图片/ })).not.toBeInTheDocument();
    const replacement = screen.getByRole("img", { name: attachment.name });
    expect(replacement).toHaveAttribute("src", "/api/roles/r2/attachments/animation");
    fireEvent.load(retried);
    expect(screen.getByRole("status")).toHaveTextContent("图片加载中");
    fireEvent.load(replacement);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("保留普通点击预览与修饰键原图打开，重试不会误开预览", async () => {
    const onOpenImage = vi.fn();
    render(<ChannelAttachments roleId="r1" attachments={[attachment]} onOpenImage={onOpenImage} />);
    const link = screen.getByRole("link", { name: attachment.name });
    for (const modifier of ["ctrlKey", "metaKey", "shiftKey", "altKey"]) {
      const click = new MouseEvent("click", { bubbles: true, cancelable: true, [modifier]: true });
      fireEvent(link, click);
      expect(click.defaultPrevented).toBe(false);
    }
    const middleClick = new MouseEvent("click", { bubbles: true, cancelable: true, button: 1 });
    fireEvent(link, middleClick);
    expect(middleClick.defaultPrevented).toBe(false);
    expect(onOpenImage).not.toHaveBeenCalled();
    fireEvent.click(link);
    expect(onOpenImage).toHaveBeenCalledExactlyOnceWith("/api/roles/r1/attachments/animation", attachment.name);
    fireEvent.error(screen.getByRole("img", { name: attachment.name }));
    await userEvent.setup().click(screen.getByRole("button", { name: "重新加载图片 动画.gif" }));
    expect(onOpenImage).toHaveBeenCalledTimes(1);
  });

  it("手机大图失败可重试并成功显示，关闭和 Escape 恢复缩略图焦点", async () => {
    const message: ConversationMessage = { id: "m1", role: "user", sequence: 1, content: "", createdAt: "2026-10-25T00:00:00Z", metadata: { attachments: [attachment] } };
    const view = messagePresentation(message, [message], role({ id: "r1", name: "诗织" }), null);
    const user = userEvent.setup();
    render(<PhoneMessage message={message} view={view} roleId="r1" timeLabel="" delivery={null} source={null} onMediaLoad={vi.fn()} />);
    const opener = screen.getByRole("link", { name: attachment.name });
    fireEvent.load(screen.getByRole("img", { name: attachment.name }));
    await user.click(opener);
    const dialog = screen.getByRole("dialog", { name: "图片预览" });
    expect(dialog).toHaveFocus();
    expect(within(dialog).getByRole("status")).toHaveTextContent("图片加载中");
    const image = within(dialog).getByRole("img", { name: attachment.name });
    fireEvent.error(image);
    expect(within(dialog).getByRole("status")).toHaveTextContent("图片加载失败");
    await user.click(within(dialog).getByRole("button", { name: "重新加载图片 动画.gif" }));
    const retried = within(dialog).getByRole("img", { name: attachment.name });
    expect(retried).not.toBe(image);
    expect(retried).toHaveAttribute("src", "/api/roles/r1/attachments/animation");
    fireEvent.load(retried);
    expect(within(dialog).queryByRole("status")).not.toBeInTheDocument();
    expect(retried).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "关闭" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("dialog", { name: "图片预览" })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });
});
