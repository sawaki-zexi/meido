# ADR-0004：角色范围 Shell 工具边界

## 状态

已采纳

## 背景

Agent Runtime 的 Loop 已能执行 provider 返回的结构化工具调用，但生产模型适配器此前只支持文本。角色任务需要有限的本地命令能力时，直接引入 Tau/Pi 的 coding agent 会同时带入项目发现、文件工具、交互终端和长期任务边界。

## 决定

1. Meido 只实现自己的 `ShellTool`，不引入完整 Tau、Pi 或 Coding Agent 依赖。
2. Shell 权限默认关闭，并通过角色的 `agentConfig.shell` 配置启用。空白命令白名单不产生工具。
3. 模型只能提交一个命令字符串；Runtime 使用无 shell 的 argv 执行，命令可执行文件必须匹配显式白名单。模型不能指定 cwd、环境变量、shell、stdin 或超时上限。
4. cwd 固定为角色自己的 `workspace` 目录；子进程使用最小环境，不传递 API Key、数据库凭据或其他会话机密。
5. 每个工具调用受角色配置的超时和输出字符上限约束。stdout/stderr 持续消费并有界保留；结果包含状态、退出码、耗时、截断标志和输出摘要。
6. 策略拒绝、未知可执行文件、非零退出、超时和取消都形成 `is_error=true` 的结构化 tool result。工具结果在 `message_end` 时进入 Runtime transcript，中间更新只用于事件流。
7. 当前实现不是操作系统沙箱。若需要阻止命令访问 workspace 以外的资源、网络或宿主凭据，必须另建容器/远程 Executor 和审批策略，不通过角色白名单宣称已隔离。

## 取舍

固定 workspace、最小环境和 argv 执行保持了 Runtime 的可扩展工具契约，也避免把 coding agent 的跨语言运行时带进后端。代价是命令能力仍拥有运行 Meido 进程的操作系统权限；强隔离、远程 Codex/Claude 和长期任务继续属于独立 `TaskRuntime` 范围。

## 事实来源

- [Tau/Pi/Codex 命令工具调研](../research/agent-runtime-shell-tools.md)
- [Agent Runtime 规格](../specs/agent-runtime-tau-integration-spec.md)
