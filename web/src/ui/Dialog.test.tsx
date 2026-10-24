import { useState } from "react";
import { createPortal } from "react-dom";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { Dialog } from "./Dialog";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function pointerEvent(type: string, values: Record<string, number>) {
  const event = new Event(type, { bubbles: true });
  for (const [key, value] of Object.entries(values)) Object.defineProperty(event, key, { value });
  return event;
}
it("关闭账号详情只关闭最上层对话框并将焦点返回角色账号页", async () => {
  function Example() {
    const [outer, setOuter] = useState(true);
    const [inner, setInner] = useState(false);
    return outer && <Dialog title="角色资料" onClose={() => setOuter(false)}>
      <button type="button" onClick={() => setInner(true)}>账号详情</button>
      {inner && createPortal(<Dialog title="账号详情" onClose={() => setInner(false)}>连接账号</Dialog>, document.body)}
    </Dialog>;
  }
  render(<Example />);
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "账号详情" }));
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog", { name: "账号详情" })).not.toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: "角色资料" })).toBeVisible();
  expect(screen.getByRole("button", { name: "账号详情" })).toHaveFocus();
});

it('顶层模态独占键盘，下层提前卸载也能在最后关闭时恢复应用', async () => {
  function Example() {
    const [outer, setOuter] = useState(true);
    const [inner, setInner] = useState(false);
    return <><button type="button">后台入口</button>
      {outer && <Dialog modalFocus title="记忆详情" onClose={() => setOuter(false)}><button type="button" onClick={() => setInner(true)}>打开来源</button></Dialog>}
      {inner && <Dialog modalFocus title="记忆来源" onClose={() => setInner(false)}><button type="button" onClick={() => setOuter(false)}>原角色已移除</button></Dialog>}
    </>;
  }
  render(<Example />);
  const user = userEvent.setup();
  expect(screen.queryByRole('button', { name: '后台入口' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '打开来源' }));
  expect(screen.queryByRole('dialog', { name: '记忆详情' })).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '原角色已移除' }));
  await user.tab({ shift: true });
  expect(screen.getByRole('dialog', { name: '记忆来源' })).toContainElement(document.activeElement as HTMLElement);
  await user.keyboard('{Escape}');
  expect(screen.getByRole('button', { name: '后台入口' })).toBeVisible();
});

it('手机会话可以从顶部拖动并更新浮层位置', () => {
  render(<Dialog variant="phone" title="手机会话" onClose={() => undefined}><p>会话内容</p></Dialog>);
  const handle = screen.getByRole('button', { name: '拖动手机会话' });
  const panel = screen.getByRole('dialog', { name: '手机会话' });
  vi.spyOn(panel, 'getBoundingClientRect').mockReturnValue({ left: 300, top: 20, width: 416, height: 400, right: 716, bottom: 420, x: 300, y: 20, toJSON: () => ({}) } as DOMRect);

  fireEvent(handle, pointerEvent('pointerdown', { pointerId: 1, button: 0, clientX: 100, clientY: 100 }));
  fireEvent(handle, pointerEvent('pointermove', { pointerId: 1, clientX: 140, clientY: 130 }));

  expect(panel).toHaveStyle('transform: translate3d(40px, 30px, 0)');
  fireEvent(handle, pointerEvent('pointerup', { pointerId: 1, clientX: 140, clientY: 130 }));
});

it('手机拖动到左侧后，缩窄窗口仍保留可操作边距', () => {
  vi.stubGlobal('innerWidth', 1440);
  vi.stubGlobal('innerHeight', 1000);
  render(<Dialog variant="phone" title="手机会话" onClose={() => undefined}><p>会话内容</p></Dialog>);
  const handle = screen.getByRole('button', { name: '拖动手机会话' });
  const panel = screen.getByRole('dialog', { name: '手机会话' });
  const bounds = vi.spyOn(panel, 'getBoundingClientRect');
  bounds.mockReturnValue({ left: 966, top: 90, width: 416, height: 820, right: 1382, bottom: 910 } as DOMRect);
  fireEvent(handle, pointerEvent('pointerdown', { pointerId: 1, button: 0, clientX: 1174, clientY: 110 }));
  fireEvent(handle, pointerEvent('pointermove', { pointerId: 1, clientX: 324, clientY: 110 }));
  fireEvent(handle, pointerEvent('pointerup', { pointerId: 1, clientX: 324, clientY: 110 }));
  vi.stubGlobal('innerWidth', 600);
  bounds.mockReturnValue({ left: -692, top: 70, width: 430, height: 860, right: -262, bottom: 930 } as DOMRect);
  fireEvent(window, new Event('resize'));
  expect(panel).toHaveStyle('transform: translate3d(-150px, 0px, 0)');
});
