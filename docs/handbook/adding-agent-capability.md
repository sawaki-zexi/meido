# 添加 Agent 能力

状态：当前

事实来源：

- [`backend/app/agent_runtime/tools.py`](../../backend/app/agent_runtime/tools.py)
- [`backend/app/agent_runtime/skills.py`](../../backend/app/agent_runtime/skills.py)
- [`backend/app/agent_runtime/plugins.py`](../../backend/app/agent_runtime/plugins.py)
- [`backend/app/agent_runtime/capabilities.py`](../../backend/app/agent_runtime/capabilities.py)
- [`agent-runtime-tool-result.md`](../reference/agent-runtime-tool-result.md)

能力在运行开始时解析为一份冻结的 `CapabilitySnapshot`。新增能力应接入 Registry 和宿主服务端口，不修改 FastAPI 路由、Agent Loop、MemoryWorker 或 SQLite 的核心流程。

解析完成后，Tool 的实现对象会和本次运行的 Tool 定义绑定。不要在运行期间修改 Tool 的名称、Schema、超时或输出上限；需要改变契约时注册新版本，让下一次运行生成新的 generation。

## 选择扩展类型

使用 **Tool** 处理需要模型主动调用的可执行动作。Tool 应声明唯一名称、输入 JSON Schema、来源、版本、风险、暴露方式、审批策略、超时和输出上限。执行函数只接收宿主注入的 `ToolContext`，不能从全局对象读取角色、会话或密钥。

使用 **Skill** 提供工作说明、提示词片段和资源索引。Skill 放在角色目录的 `skills/<id>/SKILL.md` 或项目目录的 `.agents/skills/<id>/SKILL.md`，正文只能通过受控 Markdown front matter 激活。Skill 可以引用 Tool，但不能执行代码、启用未授权 Tool 或直接访问宿主服务。

使用 **Plugin** 组合多个 Tool、Skill 或 Tool Hook，并管理它们的生命周期。当前 Plugin 是静态、进程内 Python manifest/factory，必须声明宿主 capability grant；低信任 Plugin 需要显式启用。Plugin 的 cleanup 必须可重复执行，setup 失败必须能回滚已经创建的资源。关闭期间收到取消时，已启动的异步 cleanup 会完成，随后继续清理其他资源并传播取消。

## Tool 接入步骤

1. 实现 `AgentTool` 的 `definition` 和 `execute`，保持输入只来自 `arguments` 与 `ToolContext`。
2. 为输入声明有限 JSON Schema，拒绝未声明字段和不符合类型、范围、枚举的参数。
3. 在宿主的 `CapabilityRegistry` 中注册 Tool，并通过角色 allowlist、风险集合和审批集合控制暴露。
4. 在执行函数中观察 `context.signal`，为阻塞 I/O 设置 Tool 自身的超时；Runtime 会再次施加调用级超时和输出上限。
5. 返回 `AgentToolResult`。Runtime 会统一补齐 `status`、`durationSeconds`、`outputChars` 和 `truncated`，领域字段可以保留在 `details` 中。
6. 测试成功、参数错误、拒绝、超时、取消、异常、输出截断和跨角色作用域；确认 Tool transcript 不进入普通会话上下文和 MemoryWorker。

## Skill 接入步骤

`SKILL.md` 至少包含以下 front matter：

```markdown
---
id: study-plan
version: 1.0.0
description: 制定学习计划
activation: explicit
tools:
  - memory.read
---
先读取相关资料，再生成分阶段计划。
```

发现阶段只记录 descriptor、文件 hash 和 supporting files。激活阶段才加载正文；文件在发现后发生变化时，Skill 会被拒绝并写入诊断。`tools` 中未进入当前能力快照的引用会被过滤，不会扩大 provider schema。

## Plugin 接入步骤

1. 创建 `PluginManifest`，声明稳定的 plugin ID、版本、来源、信任级别、runtime API 和所需 capability。
2. 在 factory 中通过 `PluginContext.require()` 获取已经授权的宿主服务。
3. 返回 `PluginContribution`，只包含声明范围内的 Tool、Skill 和 Hook；Tool ID 列在 `declared_tools`，Skill ID 列在 `declared_skills`，Hook ID 列在 `lifecycle_contributions`。
4. 为资源注册 cleanup；验证 setup 异常、关闭、取消和 reload 都能释放资源。
5. 测试未知 capability、缺少宿主服务、未声明的贡献、重复 Tool/Skill/Hook、低信任显式激活和跨来源名称冲突。PluginContext 中的宿主服务映射是只读快照。

当前不支持动态安装、远程 Plugin、MCP 或操作系统 sandbox。需要这些边界时，应另建进程或任务执行规格，不把隔离责任隐含在 Plugin API 中。

## 验收清单

- Tool schema 只暴露当前快照允许的能力。
- 运行期间修改注册表、角色配置或 Skill 文件不会改变已经冻结的快照。
- ToolContext 的角色、会话和运行 ID 来自宿主，模型参数不能覆盖。
- 失败、超时和取消返回结构化错误，不能伪装成成功结果。
- 能力来源、版本、策略和 generation 可在运行快照中诊断，且不包含 API Key 或插件配置密钥。
- 相关 Python 行为测试、Runtime mypy、Ruff、compileall 和 `git diff --check` 全部通过。
