# Pi Coding Agent SDK 接入开发方案

状态为开发设计草案。2026-11-18 按 Pi v1.5.2 发布源码核验接口，以下验收场景待实现。新版采用完整 Pi Coding Agent 的方向已确认。[重构决策](pi-refactor-decisions.md) · [Pi v1.5.2](https://github.com/earendil-works/pi/releases/tag/v1.5.2)

## 接入方式

在新版 Meido 的 Node.js 服务端直接依赖 `@earendil-works/pi-coding-agent@1.5.2`，固定依赖与锁文件。该版本要求 Node.js 22.19.0 及以上。一次请求从文本输入、模型调用、工具执行到最终回复，全程走 SDK 的 `AgentSession`。网页和渠道调用同一个服务端入口。旧版 Python 模型适配与 Agent Loop 只用于查阅行为和既有测试场景。[上游包声明](https://github.com/earendil-works/pi/blob/v1.5.2/packages/coding-agent/package.json) · [SDK 接入文档](https://github.com/earendil-works/pi/blob/v1.5.2/packages/coding-agent/docs/sdk.md)

| 运行时需要什么 | 由谁提供 |
| --- | --- |
| 发言者、角色、本条请求来源 | Meido 接入层核验后传入。主人绑定对群聊和私聊都有效。 |
| 会话与本轮上下文 | Meido 会话模块持有历史，按当前线程、发言者和受众选取本线程的消息、工具结果及聊天摘要。角色和记忆模块提供已选上下文。执行层使用 `SessionManager.inMemory()` 为每个入站回合创建独立 Pi 会话，换端后宿主装入目标线程自己的历史，并为已绑定主人提供该角色获准使用的主人记忆。 |
| 模型、工具与资源 | 宿主显式提供模型连接、实例工作目录、设置和可信资源加载器。凭据通过服务端模型连接提供。群消息作为普通文本传给 `prompt`，关闭 `expandPromptTemplates`，避免命中同名扩展命令。 |
| 工具调用、执行反馈与取消 | 复用 Pi 的工具定义、拦截钩子、结果消息、事件订阅和 `abort()`。业务工具按自身职责返回搜索内容、任务 ID 或真实回执。 |

## 工具权限

实例已启用的全部工具构成已绑定主人的身份授权上限。每轮按入口、所在会话和结果受众进一步筛选。获准群聊中的普通群友默认使用 Shiori 外部工具清单，并应用角色配置、插件能力及显式禁用项。未列入默认清单或其他显式授权的新工具默认仅主人可用。群友名单由 Meido 配置管理。网页搜索和读取直接加载 Pi 扩展 `@laboon85/pi-web-access`；工具发现复用 SDK 的 `createToolSearchExtension()`。宿主只向本轮提供获准的工具，业务数据由对应的 Meido 模块提供。[工具来源与接入验证](pi-multichannel-development.md#工具由谁提供开发时接在哪里) · [群友工具清单](pi-multichannel-development.md#普通群友能用哪些工具) · [上游扩展契约](https://github.com/earendil-works/pi/blob/v1.5.2/packages/coding-agent/docs/extensions.md) · [官方权限拦截示例](https://github.com/earendil-works/pi/blob/v1.5.2/packages/coding-agent/examples/extensions/permission-gate.ts)

1. 运行开始时用可信身份和设置算出本次授权上限，再通过 `session.setActiveToolsByName(...)` 声明模型可直接使用的工具。群友设置调整后，下一条请求使用新设置。
2. 可信扩展订阅 `tool_call`。每次调用分别核验发起人身份、使用该工具的权限、实际参数指向的目标是否在允许范围内。生图时检查所引用的素材，随后发图时按新的工具调用检查目标聊天。撤销绑定、关闭工具、超出目标范围或无法核验时，显式返回 `{ block: true, reason }`。涉及外部写入的工具在实际提交前复核原授权。
3. 群友运行只装载获准的工具和可信扩展。默认的 `tool_search` 仅搜索宿主为本轮筛选的目录。可发现工具、解锁后的工具和实际执行的工具均受同一授权上限约束。Pi 的 deferred、codemode 等间接调用路径也须逐次核验子调用；首版不向群友装载未经筛选的发现或组合执行入口。
4. 同一 Pi 会话每次服务一个入站请求。另一位发言者在运行中插话时，在宿主侧排队，等前一轮结束后按新身份建立工具范围。运行期间不切换全局“当前用户”，也不将群友消息作为主人运行的 steering 输入。

## 执行结果

订阅会话事件后再调用 `prompt()`。工具通过原生 `content` 返回模型需要的内容，可用 `details` 携带应用展示数据；声明了输出 schema 的工具可用 `structuredContent` 提供程序读取的值。失败时抛异常或返回 `isError: true`。只把获准面向当前受众展示的内容交给网页或渠道。[原生结果与事件类型](https://github.com/earendil-works/pi/blob/v1.5.2/packages/agent/src/types.ts) · [会话生命周期](https://github.com/earendil-works/pi/blob/v1.5.2/packages/coding-agent/docs/sdk.md)

| Meido 要展示的事实 | Pi 提供的依据 |
| --- | --- |
| 回复正在生成 | 从 `message_update` 读取文本增量。 |
| 某个工具的进度或终态 | 用 `toolCallId` 匹配 `tool_execution_start/update/end`。未知工具、无效参数或调用被拦截也可能产生错误结束事件。观察所有终态使用 `tool_execution_end`，因为被拦截的调用会跳过 `tool_result`。 |
| 一条完整回复 | 从 `message_end` 选取 assistant 消息，检查停止原因、错误和是否仍含待处理的工具调用。中间回复和工具结果可用于展示进度。 |
| 本轮已结束 | 结合 `agent_settled`、取消标志以及 `prompt()` 和最终消息的错误状态收口。Pi 可能在一次 `agent_end` 之后自动重试。用户停止时调用 `abort()`，工具接收取消信号。最后释放订阅和该次运行资源。 |

例如，搜索工具成功，Pi 会把搜索内容送入下一次模型调用。生图工具受理任务时可报告“已提交”和任务 ID。实际发送图片时，渠道接口返回成功就报告“已发送”，接口报错就返回对应错误。

## 开发顺序与验收

隔离开发实例中运行真实 Pi SDK，接入可控的测试模型与记录调用次数的测试工具。观察实际发给模型的工具声明、工具执行记录、用户看到的结果和下一轮模型收到的内容。

| 开发顺序 | 通过条件 |
| --- | --- |
| 1. 打通一轮对话 | 给定会话标识发送消息，网页收到流式文本；模型发起一次测试搜索、读到结果并生成最终回复。完成态只出现一次。换会话标识能独立运行，重新打开原标识可按会话模块交付的范围续聊。 |
| 2. 接入权限设置和身份 | 在同一模拟群里，主人使用已启用工具；随后群友只看到设置开放的工具。关闭网页搜索后下一条群友请求不再声明它。直接点名隐藏工具、间接调用、运行中解绑和跨身份插话都不会触发未授权工具的实际执行。 |
| 3. 验证异常和取消 | 工具抛错、参数无效、拦截和模型失败均能看到对应结果。工具尚在运行时点停止，能收到取消信号，同会话的下一条请求正常执行。模拟 Pi 重试时终态只出现一次。生图任务受理显示任务 ID，渠道发送显示接口实际返回的结果。 |

旧版 [#370（已关闭）](https://github.com/sawaki-zexi/meido/issues/370) 的群友可见性与执行范围可复用为行为用例；[#75（已关闭）](https://github.com/sawaki-zexi/meido/issues/75) 的取消和工具往返可复用为验收场景。新版执行路径以本方案选定的 SDK 为准。数据迁移另行选择需要的人设与凭据，旧待办条目、完成状态和提醒记录均不迁移。
