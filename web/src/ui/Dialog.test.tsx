import { useState } from "react";
import { createPortal } from "react-dom";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it } from "vitest";
import { Dialog } from "./Dialog";

afterEach(cleanup);
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
