# Shiori 当前上下文检查源码核验

状态：当前；最后核验：2026-11-01。
事实来源：Shiori 固定提交 `c14152ed225954a2a50da775075fe64dbf7b8b50`；Meido [#297](https://github.com/sawaki-zexi/meido/issues/297)。

## 已提取

- [desktop_bridge/context_requests.py](https://github.com/YinFengWindy/Shiori-Agent/blob/c14152ed225954a2a50da775075fe64dbf7b8b50/apps/backend/desktop_bridge/context_requests.py) 解析角色唯一会话，在聊天忙碌时返回状态，再交给当前模型窗口检查。
- [context_window_request.py](https://github.com/YinFengWindy/Shiori-Agent/blob/c14152ed225954a2a50da775075fe64dbf7b8b50/apps/backend/agent/core/passive_turn/context_window_request.py) 使用保存窗口和同一个提示渲染器；`include_current_message=False`，没有草稿、附件或检索文本。工具来自当前原文窗口，读取后校验窗口是否变化。
- [useChatContextStatus.ts](https://github.com/YinFengWindy/Shiori-Agent/blob/c14152ed225954a2a50da775075fe64dbf7b8b50/apps/desktop/renderer/src/chat/useChatContextStatus.ts) 对模型、工具、角色、会话和终态事件失效并重读；发送期间不检查，完成时有一次忙碌交接重试。

## Meido 适配

Meido 通过只读 HTTP 接口接入现有圆环，使用 `_desktop_provider`、`ContextWindowProvider`、`MeidoProvider` 和 `UsageAnchors`。窗口和锚点的只读模式仅投影数据，不执行正常发送路径中的失效水位写入、LRU 更新和锚点清除。独立 I/O 进入 worker，当前工具注册表和用量估算仍由事件循环持有。

Meido 的聊天历史视图在普通回合中固定追加围栏，因此检查完成前另建同角色当前视图比较已保存事实，避免读取期间新增消息被旧围栏忽略。其他角色新增消息不会使本角色检查失效。

MCP 的持久连接意愿跨回合保留，但实际 peer 在回合收尾时释放。宿主已经有按配置 revision/grant 校验的有界观察目录；检查复用这个目录以及真实 MCP 声明生成函数。没有有效目录时返回暂不可用，不连接外部 peer，也不新增缓存或数据库表。

本次只移植独立检查行为，不引入上游手动压缩。代码归属声明与 MIT 许可见 [Shiori LICENSE](../../third_party/shiori-agent/LICENSE)。
