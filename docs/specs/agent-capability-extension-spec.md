# Meido Agent 能力扩展层规格

状态：草案

最后核验：2026-10-07

事实来源：

- [Pi 与 Shiori 能力扩展机制调研](../research/pi-shiori-capability-extension.md)
- Codex 官方 Skills、Plugins、MCP 和 Hooks 文档
- Claude Code 官方 Skills、Plugins、MCP、Hooks、Permissions 和 Subagents 文档
- 具体 URL 和核验事实见 [四方能力扩展机制调研](../research/pi-shiori-capability-extension.md)
- [Meido Agent Runtime 规格](agent-runtime-tau-integration-spec.md)
- [ADR-0002：Shiori 风格记忆引擎边界](../adr/0002-shiori-memory-engine-boundary.md)
- [ADR-0003：Tau 启发的 Agent Runtime 边界](../adr/0003-tau-inspired-agent-runtime-boundary.md)
- Meido 当前源码：`backend/app/agent_runtime/`、`backend/app/memory_engine.py`

本文定义在 Agent Runtime 之上增加能力扩展层的目标架构。它不改变现有角色对话入口和 Agent Loop 的核心语义。

## 1. 问题与目标

Meido 已经有 provider-neutral Agent Loop 和基础 `ToolRegistry`。如果以后继续把记忆、资料、外部服务、Skill 和第三方能力直接写入 HTTP 路由或 Loop，角色对话主流程会随着能力增加而变化，权限和生命周期也会散落在不同模块。

本规格的目标是：

- 让角色对话主流程保持稳定；
- 通过统一能力层注册 Tool、Skill 和 Plugin；
- 在运行开始时冻结能力、权限和 prompt/tool loadout；
- 让插件只能通过显式宿主服务和 capability grant 工作；
- 保持 `MemoryEngine`、`SessionStore`、SQLite 和 `MemoryWorker` 的核心边界；
- 为以后接入只读记忆工具、资料工具、Provider 和 TaskRuntime 留出稳定端口。

## 2. 明确不属于本规格

- 不引入 Pi TypeScript runtime 或完整 Coding Agent；
- 不实现插件市场、远程插件、动态安装或沙箱；
- 不把全部现有模块改成 Tool；
- 不把自动记忆召回改成模型可选择的普通工具；
- 不在 Skill 中执行任意 Python、Shell 或外部事务；
- 不实现 Codex/Claude 执行器、远程任务、审批工作流和长期任务；
- 不把 MCP server、Subagent 或外部 Executor 当作当前角色 Agent Loop 的普通 Plugin；
- 不替换 SQLite 为插件 session 文件或内存 transcript。

## 3. 术语和职责

### 3.1 Tool

模型在一次运行中可以调用的结构化动作。Tool 必须提供名称、描述、JSON Schema、风险等级和执行结果。Tool 不能直接读取全局对象或越过宿主服务边界。

### 3.2 Skill

声明式的任务指导和资源集合。Skill 可以贡献 prompt sections、任务步骤、适用条件和允许使用的工具，但不直接执行代码。Skill 通过当前角色和运行策略筛选后注入上下文。

### 3.3 Plugin

宿主可加载的能力包。Plugin 可以注册 Tool、Skill、Provider 或生命周期模块，并声明所需的宿主 capability、配置和资源清理方法。首版 Plugin 只允许进程内、静态装配和显式启用。

### 3.4 Core service

保障产品一致性的核心服务，包括角色、会话、RuntimeManager、MemoryEngine、SessionStore、SQLite、MemoryWorker 和 HTTP/SSE。核心服务不依赖模型是否选择某个 Tool。

### 3.5 MCP adapter

外部 MCP server 到 Meido `Tool` 的连接适配器。MCP server 的连接、健康状态、工具发现、超时和输出预算由适配器管理；MCP server 不直接获得 `MemoryEngine`、SQLite 或其他核心服务对象。MCP 是连接协议，不是 Plugin、Skill 或权限授予本身。

### 3.6 Hook and policy

Hook 是在稳定生命周期 slot 上执行的策略贡献。它必须声明匹配范围、模式、超时、来源信任和失败策略；PolicyEvaluator 决定 Tool 是否声明给 provider、是否可以执行以及是否需要后续审批。Hook 不能绕过 PolicyEvaluator。

### 3.7 TaskRuntime

用于 Codex/Claude Executor、Subagent、远程任务、审批和长期运行的独立运行边界。TaskRuntime 不属于本规格，当前 Capability Layer 只为一个角色的一次 AgentLoop 提供能力。

## 4. 目标架构

```text
FastAPI / SSE
    |
    v
RuntimeManager
    |
    +--> RunContextBuilder
    |      +--> role/model/memory snapshot
    |      +--> CapabilityResolver
    |      +--> Skill prompt compiler
    |      +--> MCP adapter discovery (后续)
    |
    +--> CapabilitySnapshot
           +--> declared tool schemas
           +--> tool policies and hooks
           +--> skill sections
           +--> plugin generations/config versions
           +--> source/trust/approval decisions
    |
    v
AgentLoop
    |
    +--> ToolExecutor -> PolicyEvaluator -> Tool
    +--> AgentEvent -> persistence/SSE/audit
    |
    v
SQLite / SessionStore / MemoryWorker
```

Agent Loop 只接受已经解析好的 provider、messages、tools、timeouts 和 cancellation token。它不发现插件、不读取角色配置、不决定记忆是否提交。

## 5. 能力契约

以下是稳定概念，不要求首版一次实现全部字段：

```text
CapabilityManifest
  id: string
  version: string
  kind: tool | skill | plugin
  source: builtin | role | project | installed
  requestedCapabilities: string[]
  configSchema: object | null
  trustLevel: builtin | project | installed | external

CapabilityDescriptor
  name: string
  version: string
  description: string
  risk: read_only | mutating | external
  enabledByDefault: boolean
  exposure: direct | model_only | deferred | hidden
  approval: auto | prompt | writes | deny
  timeoutMs: integer
  outputLimit: integer
  contentHash: string | null

CapabilityContext
  pluginId: string
  roleId: string
  sessionKey: string
  runId: string
  signal: CancellationToken
  grantedCapabilities: tuple[str, ...]
  services: HostServiceView

CapabilitySnapshot
  snapshotId: string
  runId: string
  tools: tuple[ToolDescriptor, ...]
  skills: tuple[SkillDescriptor, ...]
  pluginVersions: dict[str, str]
  policyDecisions: dict[str, PolicyDecision]
  skillHashes: dict[str, str]
  sourceTrust: dict[str, string]
  generation: string
```

`CapabilitySnapshot` 必须在第一次 provider 请求前建立，并作为运行快照的一部分持久化。运行期间能力注册、配置和权限的变化不得改变当前 snapshot。

## 6. CapabilityRegistry

### 6.1 注册边界

建议从当前 `ToolRegistry` 演进为以下分层，而不是让一个 registry 同时承担所有生命周期：

```text
CapabilityRegistry
  ├─ ToolRegistry
  ├─ SkillRegistry
  ├─ PluginRegistry
  └─ PolicyEvaluator
```

Registry 至少需要：

- 拒绝重复的稳定 ID；
- 校验 manifest、版本和 capability 名称；
- 根据 role、source、risk、feature flag 和配置生成 snapshot；
- 在第一次 provider 请求前完成 Skill 描述筛选、MCP 工具发现和工具 policy 决策；
- 对外只返回 descriptor，不暴露插件内部对象；
- 在运行结束或插件卸载时执行幂等 cleanup；
- 对 snapshot 生成结果提供可诊断原因。

### 6.2 Tool policy

Tool 的可见性和可执行性分为两步：

1. `declared`：是否把 schema 发送给 provider；
2. `callable`：即使模型构造了调用，是否允许执行。

下列条件任一不满足时，Tool 不应进入 provider schema，或在执行前返回结构化错误：

- 角色启用了对应能力；
- Plugin 具有所需宿主 capability；
- 风险策略允许当前运行；
- Tool 的作用域与 `role_id/session_key/run_id` 匹配；
- 输入通过 JSON Schema 和大小限制；
- timeout、output limit 和 cancellation 已配置。

`read_only`、`mutating` 和 `external` 必须是机器可读字段。风险标记不能单独充当沙箱，但可以驱动后续审批和审计。`approval`、timeout 和 output limit 也必须是机器可读字段；它们与 risk、role policy、来源 trust 分开计算。对外部 MCP Tool，server 连接失败、工具未启用或工具搜索尚未完成时，必须返回确定性的 unavailable 诊断，不能把半初始化工具加入 provider schema。

### 6.3 Tool result

Tool 结果同时包含：

- 面向模型的有限文本；
- 可选结构化 `details`；
- `is_error`；
- status、duration、truncated、outputChars 等执行元数据；
- parent tool call ID（未来嵌套调用使用）。

结果必须限制大小。完整敏感内容不能因为存在 `details` 就自动进入模型上下文或日志。

## 7. Skill 规格

首版 Skill 使用 Markdown 文件和受限 front matter，并采用 progressive disclosure：启动时只读取 descriptor，只有在 Skill 被显式调用或策略激活后才读取正文和 supporting files。Skill 的目录可以兼容 `SKILL.md`、`references/` 和 `assets/`，但这些文件不具有执行权限。

```yaml
id: study-plan
version: 1.0.0
description: 制定学习计划
tools:
  - memory.search
  - memory.get
activation: explicit | role_default | keyword
```

正文只包含：

- 角色可理解的行为规则；
- 输出格式或步骤；
- 可用工具的使用说明；
- 失败和边界处理。

Skill 编译器必须：

- 校验 front matter 和文件大小；
- 根据角色配置、来源信任级别和 activation 规则筛选；
- 先用 `description` 和 activation 判断是否加载正文，避免所有 Skill 常驻上下文；
- 删除不存在或未授权的工具引用；
- 给每个 section 标记来源、版本和优先级；
- 记录正文和 supporting files 的大小、hash 和 trust level；
- 在 `CapabilitySnapshot` 中保存最终启用的 Skill；
- 不允许正文触发任意代码、Shell 或网络请求。

Skill 只影响 prompt，不拥有 MemoryStore、SessionStore 或 RuntimeManager。

## 8. Plugin 规格

首版 Plugin 使用进程内 Python 包，通过静态 manifest 和宿主装配加载。Plugin 是多个 Tool、Skill、Hook 和后续 MCP adapter 的分发/装配单元，不是一个新的 Agent Loop，也不是安全 sandbox：

```text
plugin/
  manifest.yaml
  backend/plugin.py
  skills/
  tests/
```

Manifest 至少包含：

- `id`、`version`、`runtime_api`；
- `capabilities`：插件请求的宿主能力；
- `tools`、`skills` 或 lifecycle contributions；
- 配置 schema 和默认值；
- 风险声明和资源目录。
- 组件路径必须位于 Plugin 根目录内；组件 ID 在 Plugin 命名空间中稳定；
- Plugin generation、manifest hash 和 trust level 必须进入 `CapabilitySnapshot`。

宿主加载流程：

```text
读取 manifest
  -> 校验 ID、版本、capabilities
  -> 计算 granted capabilities
  -> 创建隔离 PluginContext
  -> setup/register
  -> 验证注册项和依赖
  -> active generation
```

插件只能从 `PluginContext` 获取服务。未声明的服务抛出 `CapabilityNotGranted`；声明了但宿主未装配时抛出明确的 `HostServiceUnavailable`。插件创建的后台任务、文件句柄、连接和 watcher 必须通过 effect 注册，并在 shutdown/reload/失败回滚时幂等释放。

首版不支持：插件自定义 FastAPI 路由、直接修改 SQLite schema、覆盖核心角色状态、修改 Runtime 状态机和未声明的网络/进程访问。
Plugin 可以声明 MCP adapter，但首版不连接远程 server；后续连接仍必须经过独立的 server/tool policy、审批、超时和输出限制。

## 9. Lifecycle slots

首版只开放有限的稳定 phase：

```text
before_run
before_prompt
before_provider_request
before_tool_call
after_tool_result
after_run
```

每个 contribution 声明：

- `mode`: observe | transform | deny | request_follow_up；
- `requires` 和 `produces`；
- timeout 和失败策略。
- `matcher` 和来源 trust；

默认规则：

- `observe` 失败只记录诊断；
- `transform` 失败保留上一个合法值并阻止继续污染上下文；
- `deny` 只能阻止当前 Tool，不得伪造成功结果；
- `request_follow_up` 首版禁用，避免插件制造无限 Agent Loop；
- `after_run` 不得改变已提交的运行终态。

Hook 的 `transform` 和 `deny` 只对当前事件生效。并发 Hook 的执行顺序和合并规则必须由宿主定义；首版采用按 priority、plugin ID、contribution ID 的稳定顺序串行执行，避免不同启动顺序产生不同的工具参数或 prompt。

自动记忆召回和 MemoryWorker 提交仍由核心流程调用，不通过可选 lifecycle module 决定是否执行。

## 10. 运行集成和持久化

每次运行的顺序固定为：

```text
创建 run
  -> 角色/模型/记忆/能力快照
  -> 编译 system prompt + Skill sections + tool schemas
  -> AgentLoop
  -> 每个 tool call 经过 policy/hooks/executor
  -> message_end 和 tool result 按现有 Runtime 规则持久化
  -> completed 后提交 MemoryWorker
```

SQLite 新增或扩展的字段只记录可诊断快照：

- `capability_snapshot_id`；
- plugin ID/version；
- enabled skill IDs/version；
- policy decision 摘要；
- tool source/trust/approval、MCP server 状态和 hook generation；
- Skill/tool content hash；
- 不包含 API Key、完整插件配置密钥或未必要的记忆全文。

工具和 Skill 的配置变化必须在下一次运行生效，不影响当前 run。SSE 可以增加 capability/tool 事件，但旧客户端必须仍能完成普通文本对话。

## 11. 安全和失败策略

- 插件不是 sandbox；需要强隔离时使用独立 Executor/容器；
- Plugin load、MCP connection、Tool approval 和 OS sandbox 是四个独立的信任层；
- role scope 从 `ToolContext` 和宿主服务注入，不能由模型参数覆盖；
- 外部副作用能力默认不注册；
- 工具超时、取消和失败统一返回结构化错误结果；
- Plugin setup 失败只使该 plugin generation 不可用，不回滚核心 Runtime；
- cleanup 失败必须记录诊断并继续释放其他资源；
- capability snapshot 生成失败时，运行不能静默使用不完整的工具集合；
- Skill 解析失败不应阻塞普通文本对话，但必须记录 source、版本和错误；
- 插件和工具日志必须做密钥、token、记忆内容和环境变量脱敏。
- 项目/用户来源的 Skill、Hook、MCP 和 Plugin 必须经过显式来源校验；未信任来源不得隐式扩大工具权限。

## 12. 实施阶段

### Phase 1：Capability Registry 基础

- 将现有 `ToolRegistry` 补充 descriptor、source、risk、exposure 和 policy；
- 增加 `CapabilitySnapshot` 和运行开始冻结逻辑；
- 让当前 ShellTool 通过统一策略进入 snapshot；
- 不引入动态第三方插件。

### Phase 2：声明式 Skill

- 支持角色/项目 Skill 目录发现；
- 解析 front matter、工具引用和 activation；
- 采用 descriptor-first/progressive disclosure，记录正文 hash 和来源 trust；
- 将启用 Skill 编译进 system prompt，并记录版本；
- 用 fake Skill 验证不会绕过 Tool policy。

### Phase 3：进程内 Plugin

- manifest 校验和 capability grant；
- PluginContext、配置、effect cleanup、生命周期 slots；
- 用内置 memory tool plugin 验证 MemoryEngine 端口；
- 外部网络、远程任务和动态安装继续关闭。

### Phase 3.5：MCP adapter（后续，可独立 Ticket）

- 只接入已配置的本地 STDIO 或受信任的 HTTP server；
- 将 server/tool discovery、enabled/disabled、approval、timeout 和 output limit 映射到 snapshot；
- 连接失败或工具不可用不破坏普通无工具对话；
- 不允许 MCP server 直接访问 Meido 核心服务。

### Phase 4：独立 TaskRuntime（后续规格）

- Codex/Claude、远程 Executor、审批、排队和长期任务另行设计；
- 不扩展当前 Agent Loop 的职责。

## 13. 验收测试决定

至少覆盖：

- 相同角色在能力配置变化前后创建的两个 run 使用不同 snapshot；同一个 run 内 snapshot 不变；
- 未启用、未授权、风险不匹配和输入非法的 Tool 不会进入 provider schema，或返回确定性错误；
- Tool result 的文本、结构化 details、错误标记和大小限制保持一致；
- Skill 只贡献经过校验的 prompt sections，不能执行代码或启用未授权 Tool；
- Plugin 未声明的宿主 capability 被拒绝，宿主缺少已声明服务时返回不同错误；
- Plugin setup、运行取消、session shutdown 和 reload 的 cleanup 幂等；
- 一个 Plugin 失败不会破坏普通无工具对话；
- MemoryEngine 只读工具只能访问当前角色，自动记忆召回和 MemoryWorker 仍由核心流程执行；
- 工具调用、Skill snapshot 和 Plugin 版本可诊断但不泄露密钥；
- 现有 Agent Runtime、SSE、前端构建、mypy 和 Ruff 继续通过。

## 14. 设计取舍

统一能力层会增加 manifest、snapshot、policy 和生命周期代码，但能把可选能力与核心对话流程隔离。Pi 和 Shiori 提供运行时扩展与 capability grant 的实现参考；Codex 和 Claude Code 进一步证明 Skill 的按需加载、Plugin 的分发边界、MCP 的连接/审批分离和 Hook 的 trust/decision 语义值得采用。直接采用这些产品的完整宿主会引入 TypeScript/Coding Agent、CLI 工作区信任、多渠道或 IDE 组件等超出 Meido 的职责。Meido 只借鉴接口思想，保留 Python、SQLite、角色硬隔离和现有 Runtime 边界。
