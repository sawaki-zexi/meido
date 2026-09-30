# Meido 前端风格与结构

状态：当前
事实来源：`web/src/`、`web/src/App.test.tsx`
最后核验：2026-09-30（Issue #20）

## 风格原则

> 需要她时，她就在；不需要时，她安静地留在那儿。

- **克制**：暖白纸面、低对比线条、一个深棕主色。次要操作用 `ghost` 按钮，只有“保存”“发送”“创建角色”“进入会话”使用主色。
- **角色可辨**：每个角色由稳定 ID 派生一个柔和色相（`--role-hue`），只用于头像、女仆消息边框和名字。改名不改变颜色。
- **对话为焦点**：聊天页去掉侧栏，消息居中，输入框固定在底部；时间、会话标识等元信息使用弱化文字。
- **状态一致**：
  - 加载：`Placeholder loading`（旋转点 + 文字，`role="status"`）
  - 空内容：`Placeholder`
  - 失败：`Notice`（`role="alert"`，可关闭）；成功提示用 `Notice tone="success"`
  - 生成中 / 未完成：消息标题上的状态标签；未完成回复使用虚线边框
- **响应式**：760px 以下为单列，聊天输入框贴底并留出安全区。

## 目录结构

```text
web/src/
  api/             后端请求、错误文本、SSE 解析和共享类型
  ui/              与业务无关的展示组件（Avatar、Placeholder、Notice）
  features/
    role-management/  角色列表、详情编辑、创建草稿和删除
    chat/             唯一会话、消息发送和流式回复
    model/            模型连接设置
  App.tsx          视图切换和跨功能协调
  styles.css       设计变量（:root）和全部样式
```

- 功能状态放在各自的 `use*` Hook 中，组件只负责渲染；`App.tsx` 只协调视图切换和跨功能动作（例如删除角色后清空其会话）。
- 流式事件先由 `api/stream.ts` 解析为类型化事件，再由 `applyStreamEvent` 纯函数更新消息列表。
- 目录名避免使用 `roles/`：它被 `.gitignore` 用于本地角色数据。

## 测试

前端使用 Vitest + Testing Library，在 jsdom 中渲染完整 `App` 并模拟后端接口，只断言用户可见行为。运行：

```bash
npm run test:web
```
