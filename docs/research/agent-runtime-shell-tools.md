# Agent Runtime Shell/Command Tool 调研

状态：当前

最后核验：2026-10-07

## 结论

Tau 和 Pi 的 coding tool 都把命令执行放在 portable Agent Loop 之外，核心可借鉴的是结构化输入、固定 cwd、stdin 断开、超时/取消终止进程树、有界输出和结构化结果。Pi 的 project trust 不是 sandbox；Tau/Pi 都不能替 Meido 定义角色权限。Codex 的 command tool 还把 workdir、timeout、输出上限、交互 session、sandbox、network 和 Allow/Prompt/Forbidden 审批组合成独立执行边界。

Meido 因此采用自有、角色范围的 `ShellTool`：默认关闭，显式 executable allowlist，固定角色 workspace，最小环境，argv 执行，超时/取消和有界 stdout/stderr。它不是操作系统 sandbox；需要强隔离时应使用容器或远程 Executor。

## 参考实现

- [Tau coding tools](https://github.com/huggingface/tau/blob/0c233ff080ba261ec1178e9232301cbb6479e648/src/tau_coding/tools.py)：固定 cwd、进程组终止、超时/取消和截断结果。
- [Pi bash tool](https://github.com/badlogic/pi-mono/blob/46c9de402bddf46b03c3b9f46487b777aaa41861/packages/coding-agent/src/core/tools/bash.ts)：AbortSignal、可替换执行后端、进程树终止和输出更新。
- [Pi security](https://github.com/badlogic/pi-mono/blob/46c9de402bddf46b03c3b9f46487b777aaa41861/packages/coding-agent/docs/security.md)：trust 不限制 shell、文件或扩展权限。
- [Codex shell spec](https://github.com/openai/codex/blob/0b863c69f50335acd92164aab971cb58d298c2fe/codex-rs/core/src/tools/handlers/shell_spec.rs)：命令、工作目录、超时、输出和审批/沙箱边界。

这些项目的 coding tool 不能直接移植到 Meido：它们的进程权限、项目资源和会话持久化边界与 Meido 角色、SQLite transcript 和 Runtime 生命周期不同。
