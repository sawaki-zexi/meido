# ADR-0012：角色归属的独立 TaskRuntime

## 状态

已采纳。批准时间：2026-10-15。#149 实现首个 Shell 垂直切片；spawn、调度和 Coding 执行器仍待对应 Ticket。

## 背景

后台 Shell、spawn、Coding Agent 委托和到时执行的模型任务需要在父对话结束后保有归属、预算、日志、取消和结果投递语义。现有单次 Agent Runtime 不负责这些长期生命周期，ADR-0003 已保留独立 TaskRuntime。

## 决定

1. 增加独立应用级任务模块，SQLite 保存不可变角色/来源、执行类型、父运行、能力版本、状态、预算、进度、日志引用和交付。
2. AgentLoop 通过提交、查询和取消等接口使用任务模块，不自行维护长期后台 asyncio 任务。子 agent 可复用 portable loop，但拥有独立工作集、受限工具集合和预算，不产生第二个主人会话。
3. 只有显式受管后台任务可在父对话结束后继续；普通前台工具保留取消穿透。后台 Shell 默认由 task_output 拉取结果，spawn 完成自动回灌原会话。
4. 本机进程在应用退出时由宿主管理清理；重启后缺失执行句柄的任务标记 interrupted，不虚构继续运行或盲目重跑外部事务。
5. 查询、停止、继续、日志和结果投递都校验任务角色和原始上下文。原角色或目标失效时不改投其他角色/会话。
6. Shell 保留 ADR-0004 的 argv、白名单、固定 workspace、最小环境及宿主预算。新增后台生命周期不授予任意 shell、环境或凭据。

## 取舍

任务持久化和资源所有权增加实现成本，但能集中解决后台 Shell、子 agent、定时模型任务及 Coding 委托的生命周期。把任务放入聊天连接或当前循环更简单，却会失去父对话结束后的归属与可恢复状态。

## 批准和事实来源

- 维护者于 2026-10-15 在本次对话明确批准实施规格 D01–D03。
- [完整复刻规格](../specs/shiori-tool-parity-spec.md)，D02。
- [ADR-0003](0003-tau-inspired-agent-runtime-boundary.md)、[ADR-0004](0004-role-scoped-shell-tool-boundary.md)。

当前实现与验收边界见 [后台 Shell Reference](../reference/owned-background-shell.md)；本决定不表示全部后续执行器已经完成。
