# ADR-0003：采用 Tau 启发的自有 Agent Runtime 边界

## 状态

已采纳

最后核验：2026-10-07

## 背景

Meido 当前把角色锁、SQLite 消息写入、记忆召回、模型流式调用和 SSE 编排放在 HTTP 路由中。后续若加入受控工具，路由会同时承担模型轮次、工具执行、取消和失败恢复，难以保证持久化顺序和运行诊断。

Tau `main` 在提交 `0c233ff080ba261ec1178e9232301cbb6479e648` 提供了可复用的 `tau_agent` portable loop：provider stream → assistant message → tool calls → tool results → next turn，并以 agent、turn、message、tool execution 事件连接 provider、工具和前端。Tau 的 `tau_coding` 则额外包含 CLI/TUI、项目文件发现、shell/文件工具、JSONL session 和编码工作流。

## 决定

1. Meido 新增自己的 `Agent Runtime` seam，采用 Tau portable loop 的可验证行为语义，不把完整 `tau-ai` 包作为生产依赖。
2. Runtime 只负责一次运行内的 provider 轮次、顺序工具调用、取消、超时、事件和消息完成边界；角色设定、模型配置、记忆服务、SQLite 会话、HTTP/SSE 和权限仍由 Meido 自己负责。
3. SQLite 是会话和运行状态的权威来源。Runtime 的内存 transcript 只作为当前运行工作集；每个完整 message end 必须先完成持久化，再进入下一次 provider 请求。
4. 基础 Runtime 不引入 `tau_coding`、Coding Agent、任意文件写入、外部事务、steering/follow-up、并行工具或远程任务执行器。角色范围 Shell 工具作为后续独立增量，遵循 [ADR-0004](0004-role-scoped-shell-tool-boundary.md)；角色范围只读记忆工具由后续 #78 在 #56 完成后单独引入。
5. 如果实现复制或改写可识别的 Tau 源码，随实现保留 Tau 的 MIT license/attribution，并记录实际复制范围和同步策略；如果只按行为自行实现，则保留本 ADR 和研究链接作为选型依据，不声称是官方 Pi/Tau 移植。
6. 客户端断开、provider 请求超时和显式取消必须穿透到正在等待的 provider/tool 操作并终止运行；单个工具超时形成错误 tool result，由模型决定是否继续。角色活动运行期间删除继续返回 409，运行终态后才允许删除。
7. SQLite transcript converter 必须按 assistant 回合合并多个 tool call，并按 call ID 修复、配对或丢弃 tool result；不能把每一行直接当作独立 assistant turn 重放。

## 取舍

采用自有 seam 会增加少量 provider/message/event 适配代码，但可以把 Meido 的 `run_id`、SQLite 状态机、角色锁、密钥隔离和 SSE 兼容集中在一个深模块中，并避免 Tau 完整包的 Python 3.12、CLI/TUI 和 coding 依赖进入后端。

直接依赖完整 Tau 的优点是初始代码少，代价是 Tau 的 harness/session/coding 边界会与 Meido SQLite、角色生命周期和安全策略重叠；上游 API 演进也会把不需要的依赖和行为带入产品。直接移植 Pi 的 TypeScript 实现则增加跨语言运行时和进程边界，不能复用 Meido 当前 Python 模型适配器。

## 后果

- `AgentLoop`、`ModelProvider`、`AgentTool` 和事件模型成为可测试的 provider-neutral 接口。
- `SessionStore` 需要结构化保存 tool call/result 和 run 关联，并提供旧文本消息兼容和确定性的 transcript 修复。
- 普通无工具 SSE 必须保持现有事件名；新增 `runId`/序号和工具事件必须允许旧客户端忽略。
- Runtime 的 fake tool loop 测试覆盖通用循环；角色范围 Shell 工具另由 ADR-0004 的策略和行为测试覆盖，#78 仍负责后续只读记忆工具。
- 未来要做 Coding Agent 或远程任务，需要另建 `CodingRuntime`/`TaskRuntime` 规格，不能把职责塞回本 ADR 的 Runtime。

## 事实来源

- [Tau Agent Loop 调研](../research/tau-meido-agent-runtime.md)
- [Meido Agent Runtime 规格](../specs/agent-runtime-tau-integration-spec.md)
- [Tau `loop.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/loop.py)
- [Tau `tool_history.py`](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_agent/tool_history.py)
- [Tau README](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/README.md)

## 确认记录

维护者于 2026-10-07 明确要求阅读规格并开始实现；该指示确认本 ADR 中的 Runtime seam、SQLite 权威状态和遗留运行启动收敛策略。生产命令工具的后续边界见 ADR-0004。
