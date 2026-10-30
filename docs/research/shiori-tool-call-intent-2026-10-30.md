# Shiori 工具调用意图核验

状态：当前。最后核验：2026-10-30。
范围：[Issue #281](https://github.com/sawaki-zexi/meido/issues/281)，来自已批准的[规格 #279](https://github.com/sawaki-zexi/meido/issues/279)。
上游基线：`71c8b0e460530683cd28aad6bb62e873c7167c89`，只读源码核验，无真实模型调用。

## 已提取的上游行为

- [registry.py](https://github.com/YinFengWindy/Shiori-Agent/blob/71c8b0e460530683cd28aad6bb62e873c7167c89/apps/backend/agent/tools/registry.py)：`_with_progress_description` 复制 schema，只有业务 properties 不含 `description` 时才补入必填字符串。提示原文要求 5–12 个字、用户可读短语、不要复述工具名或粘贴长参数。`execute` 仅在同一条件下移除调用参数中的该字段，没有另加缺省或长度拒绝规则。
- [execution.py](https://github.com/YinFengWindy/Shiori-Agent/blob/71c8b0e460530683cd28aad6bb62e873c7167c89/apps/backend/agent/tools/execution.py)：插件先收到原始调用并可改参，最终准入检查后才调用 registry；因此宿主字段的剥离不能提前破坏 hook 参数。
- [ChatToolCalls.tsx](https://github.com/YinFengWindy/Shiori-Agent/blob/71c8b0e460530683cd28aad6bb62e873c7167c89/apps/desktop/renderer/src/chat/ChatToolCalls.tsx)：继续显示工具名、JSON 参数 chip、运行状态和展开结果；参数优先使用 final_arguments，字符串超过 120 字截断。上游没有独立的意图标题或进度面板。

## Meido 适配

[ToolDefinition](../../backend/app/agent_runtime/tools.py) 是统一 provider 声明入口，包括普通工具、搜索后加载工具、插件和子任务。业务 schema 保留原样；[CapabilitySnapshot](../../backend/app/agent_runtime/capabilities.py) 使用同一投影，[声明预算](../../backend/app/agent_runtime/search_budget.py) 计算实际增加的字段字节。既有请求预算使用实际 provider tools。

[工具循环](../../backend/app/agent_runtime/loop.py) 继续使用本票开始时的能力绑定。hook/准入看到完整调用，随后用剥离宿主字段后的参数进行业务校验和调用；同名业务字段按原 schema 传入和校验。原调用对象不被改写，已有工具开始事件、消息存储和历史投影自然携带真实意图。

[预览投影](../../backend/app/chat_presentation.py) 继续使用 120 字 JSON chip。Meido 的参数预览还有限制字段数与凭据脱敏，因此在截断前优先放置真实字符串 `description`，防止长业务参数把意图挤出。邮件仅公开该短语，仍隐藏其业务参数；浏览器输入与凭据仍经过原脱敏。没有意图的旧历史保留原名称和参数，不推断或补写短语。界面结构与折叠方式保持原样。

窄屏仍按上游单行样式省略 chip 的可见文字；原预览保留为原生 hover title 和辅助技术的描述文本。该行为不保证触屏单行能显示完整意图，也没有新增独立展示面板。

停止回合与工具最终回执是不同事实：正常合作取消能得到 `cancelled` 工具结果；HTTP 强制停止在未收到结果时关闭流，运行记为 `cancelled`，工具历史保留 `unknown`，不会伪造已完成或已撤回。

## 行为验证与范围

[调用边界测试](../../tests/python/test_tool_call_intent.py) 覆盖实际 provider schema、严格业务函数、插件改参、同名业务字段与旧式缺省调用。
[HTTP/SSE 测试](../../tests/python/test_tool_progress.py) 在工具阻塞期间读取真实监听端口上的开始事件，再验证完成/失败/停止后的重开历史。
[聊天测试](../../web/src/features/chat/ChatPresentation.test.tsx) 和[停止测试](../../web/src/features/chat/ToolProgress.test.tsx) 覆盖真实事件解析、同名独立调用、迟到事件、折叠结果及重开聊天。

当前注册表/原文窗口恢复由后续 #280 处理，本票不替代 ADR-0026；#278 通过同步 main 整合，不在此重复实现。其余兄弟票的 Herdr、用量锚点或分批摘要也不在本票范围。
