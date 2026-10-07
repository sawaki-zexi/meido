# Agent 能力扩展机制调研：Pi、Shiori、Codex 与 Claude Code

状态：当前研究记录

最后核验：2026-10-07

事实来源：

- Pi `badlogic/pi-mono` main：`b30a6dd779340f7bc2f3ffa60f4c0a5f914ba9ae`
- Shiori-Agent `YinFengWindy/Shiori-Agent` main：`c7ad4423cc63ae5157cfada21e408dd40b360e63`
- Codex 官方文档：[Skills and Plugins](https://learn.chatgpt.com/docs/skills-and-plugins.md)、[Build Skills](https://learn.chatgpt.com/docs/build-skills.md)、[Build Plugins](https://learn.chatgpt.com/docs/build-plugins.md)、[Hooks](https://learn.chatgpt.com/docs/hooks.md)、[MCP](https://learn.chatgpt.com/docs/extend/mcp.md)
- Claude Code 官方文档：[Skills](https://code.claude.com/docs/en/skills.md)、[Plugins overview](https://code.claude.com/docs/en/plugins/overview.md)、[Plugin components](https://code.claude.com/docs/en/plugins/components.md)、[MCP](https://code.claude.com/docs/en/mcp.md)、[Hooks](https://code.claude.com/docs/en/hooks.md)、[Subagents](https://code.claude.com/docs/en/sub-agents.md)、[Permissions](https://code.claude.com/docs/en/permissions.md)
- Meido 当前基线：`a9cfb10`（Agent Runtime PR #81 合入 `main`）

本文只提取与 Meido 能力扩展层有关的源码事实和设计启发，不建议把 Pi 的 TypeScript Coding Agent、Codex CLI 或 Claude Code 的完整宿主直接移植到 Meido。

## 结论

Meido 应在现有 Agent Loop 之上增加一个小型的 `Capability Layer`，但首版不应直接建设完整的动态插件市场。该层负责把可选能力解析成一次运行的稳定快照，再交给 Agent Loop 使用。

建议的职责分界是：

```text
Core domain      角色、会话、MemoryEngine、SQLite、MemoryWorker
Runtime          provider 轮次、tool call、取消、超时、事件、运行状态
Capability layer 工具、Skill、插件注册、权限、生命周期、运行快照
Transport        FastAPI、SSE、前端和管理 API
```

工具、Skill 和插件不是同一个概念：

| 类型 | 模型可见性 | 作用 | 首版建议 |
| --- | --- | --- | --- |
| Tool | 可选 | 一次模型可调用的结构化动作 | 立即支持，沿用 `AgentTool` |
| Skill | 提示词/资源 | 一套行为规则、上下文资料和允许工具 | 先支持声明式 Markdown |
| Plugin | 宿主能力 | 注册多个 Tool、Skill、Provider 或生命周期处理器 | 先做进程内、显式启用 |

## Pi：扩展是 Agent Session 的一等公民

### Extension API

Pi 的扩展入口在 `packages/coding-agent/src/core/extensions/types.ts` 的 `ExtensionAPI`，加载与注册在 `packages/coding-agent/src/core/extensions/loader.ts`，事件执行在 `packages/coding-agent/src/core/extensions/runner.ts`。

扩展可以注册：

- `registerTool`：模型可调用工具；
- `registerCommand`、`registerShortcut`、`registerFlag`：用户交互入口；
- `registerProvider`、`registerVirtualModel`：模型供应商或路由；
- `registerMcpServer`：外部 MCP 能力；
- `on(event, handler)`：会话、模型、消息和工具生命周期处理器；
- 资源发现：Skill、Prompt 和主题路径；
- session entry、custom message 和 UI renderer。

官方文档 [`packages/coding-agent/docs/extensions.md`](https://github.com/badlogic/pi-mono/blob/b30a6dd779340f7bc2f3ffa60f4c0a5f914ba9ae/packages/coding-agent/docs/extensions.md) 明确说明扩展运行在 Pi 进程内，拥有与宿主相同的操作系统权限，可以观察 prompt、工具调用、文件、凭据和 session history。

### 生命周期和事件

Pi 提供 `session_start`、`session_shutdown`、`before_agent_start`、`turn_end`、`agent_before_settle`、`agent_settled`、`tool_call`、`tool_result`、`resources_discover` 等事件。

几个可借鉴的行为：

1. 异步扩展 factory 完成后才继续启动，因此可以在启动期间注册 provider 或配置。
2. 长期资源不应在加载 factory 中创建，而应绑定 `session_start`，并在幂等的 `session_shutdown` 中释放。
3. 变换型事件按扩展注册顺序串行执行；工具调用前可以修改参数或阻止执行。
4. `agent_before_settle` 是最后一个可请求继续运行的边界；`agent_settled` 只用于通知。
5. 扩展错误通常被记录并继续运行，但工具策略错误会阻止该工具调用。

这说明扩展层应有明确的生命周期和可操作边界，不能只提供一个全局 `register()` 函数。

### 工具暴露级别

Pi 的工具系统比 Meido 当前 `ToolRegistry` 更细：

- `direct`：注册后直接声明给模型；
- `model-only`：模型可调用，但其他工具不能嵌套调用；
- `codemode`：可被脚本/编排工具调用，但默认不声明给模型；
- `deferred`：需要 `tool_search` 激活；
- `hidden`：保留注册状态但不可达。

工具还带有 `readOnlyHint`、`destructiveHint`、`idempotentHint`、`openWorldHint`。这些提示不是安全沙箱，但可以让宿主权限策略决定是否需要确认。

Pi 支持 `outputSchema` 和 `structuredContent`，同时保留模型可读的文本 `content`。这对 Meido 很有价值：工具结果可以同时服务模型、审计和后续 UI，而不必从文本重新解析。

Pi 还允许工具通过 `ctx.executeTool()` 调用其他工具。嵌套调用不会作为新的 transcript message 写入，但会在父工具结果中保留有界的 `nestedCalls` 诊断记录。Meido 当前不应立即引入嵌套工具，但可以保留 parent call ID 和诊断模型的扩展位。

### Skill 和资源

Pi 的 Skill 位于资源发现流程中，主要由 `SKILL.md` 和资源目录组成。Skill 描述如何完成某类任务，工具仍由 Extension 或内置工具注册。Skill 不等于可执行插件，也不应绕过工具权限。

Pi 会把当前 Skill、系统 prompt 和工具 loadout 作为上下文的一部分记录下来；工具或 prompt 发生变化时追加系统 checkpoint，而不是静默改变历史。

### Session 与持久化启发

Pi 的 session 是 JSONL 树，entry 通过 `id`/`parentId` 分支。系统消息会记录 prompt sections 和工具增删，`custom` entry 保存扩展状态但不进入模型上下文，`custom_message` 才进入模型上下文。Compaction entry 保存系统 checkpoint 和保留边界。

Meido 不应复制 JSONL session，因为 SQLite 已经是消息和运行状态的权威来源。但应借鉴“扩展状态”和“模型上下文”分离的原则：

- capability 配置和运行快照要可诊断；
- 工具诊断数据不应自动进入普通对话上下文；
- prompt/tool loadout 变化必须有明确的运行边界；
- context 压缩不能破坏工具调用与结果配对。

### Pi 的限制

Pi 官方安全文档明确指出，Extension 和 Project Trust 不是 sandbox；扩展默认以宿主进程权限执行。Pi 适合作为扩展 API 和 Agent Session 的参考，不适合作为 Meido 的角色权限实现或隔离机制。

## Shiori-Agent：插件是受 capability grant 约束的宿主扩展

### Manifest 和 capability grant

Shiori 的插件清单位于各插件的 `manifest.yaml`，例如 [`plugins/tool_loop_guard/manifest.yaml`](https://github.com/YinFengWindy/Shiori-Agent/blob/c7ad4423cc63ae5157cfada21e408dd40b360e63/plugins/tool_loop_guard/manifest.yaml)。清单声明 `runtime_api`、`id`、版本、配置模型和 `capabilities`。

SDK 的 `packages/sdk/python/shiori_sdk/runtime.py` 定义 `KNOWN_CAPABILITIES`、`parse_capabilities`、`CapabilityNotGranted` 和 `HostServiceUnavailable`：

- 未知 capability 在加载时拒绝；
- 插件只能访问 manifest 声明的宿主服务；
- capability 已声明但宿主服务未装配时，抛出与“未授权”不同的 `HostServiceUnavailable`；
- 插件上下文带有 `plugin_id`、隔离的 `plugin_dir`、授予列表和清理 effect。

这是 Meido 最应该借鉴的安全边界：插件能力必须显式声明和授予，而不是把整个 `main.py` 或全局依赖注入给插件。

### Tool contract 和风险

SDK 的 [`tools.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/c7ad4423cc63ae5157cfada21e408dd40b360e63/packages/sdk/python/shiori_sdk/tools.py) 定义模型工具的名称、描述、JSON Schema 和 `ToolResult`。工具注册接口支持：

- `risk`；
- `always_on`；
- `search_hint`；
- `external_allowed`。

[`tool_hooks.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/c7ad4423cc63ae5157cfada21e408dd40b360e63/packages/sdk/python/shiori_sdk/tool_hooks.py) 进一步把工具执行前策略抽象为 `PreToolCtx` 和 `HookOutcome`，允许重写参数、拒绝调用或要求宿主收尾。

Shiori 的 `tool_loop_guard` 插件是一个很好的例子：它只声明 `tool_hooks` 和 `config`，不直接依赖宿主内部类；配置由 manifest 指定，资源释放由宿主 effect 管理。

### Lifecycle phase slot

SDK 的 [`lifecycle.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/c7ad4423cc63ae5157cfada21e408dd40b360e63/packages/sdk/python/shiori_sdk/lifecycle.py) 把扩展处理分为固定 phase slot：

```text
before_turn
before_reasoning
prompt_render
before_step
after_step
after_reasoning
after_turn
```

每个 `LifecycleModule` 声明 `requires` 和 `produces`，宿主负责排序和验证。这比让插件任意订阅所有内部事件更容易维持确定性。

对 Meido 的启发是：能力层可以提供少量稳定的运行钩子，但必须把“可观察”“可修改”“可阻止”“可请求继续”区分开。

### MemoryEngine 端口

Shiori 的 `packages/sdk/python/shiori_sdk/memory/engine.py` 将记忆引擎作为 SDK 端口；默认 SQLite/向量实现放在 `plugins/default_memory`。插件通过 engine contract 使用记忆，不直接依赖具体 schema。

Meido 已有 [`backend/app/memory_engine.py`](../../backend/app/memory_engine.py) 的 `MemoryEngine`、`MemoryEngineDescriptor`、`MemoryToolProfile` 和 `MemoryScope`，这已经是能力层的一个成熟宿主服务，不应再为插件暴露 `MemoryStore`。

### Shiori 的限制

Shiori 插件系统覆盖渠道、账户、UI、模型、记忆和后台服务，包含多渠道 scope、RPC、依赖和版本兼容。Meido 当前是单主人、单角色会话；复制 Shiori 的全量宿主会把渠道、多用户和桌面插件生命周期一起引入，超出 Agent Runtime 的职责。

## Codex：Skill、Plugin、MCP 和 Hook 分层

### Skill 是按需加载的指令与资源

Codex 遵循 open agent skills standard。一个 Skill 目录必须包含 `SKILL.md`，并可以附带 `scripts/`、`references/`、`assets/` 和 `agents/openai.yaml`。启动时只读取 Skill 的名称和描述，选中后才读取完整正文；官方文档把这称为 progressive disclosure，并对初始 Skill 列表设置上下文预算。Skill 可以显式调用，也可以根据描述隐式匹配；`agents/openai.yaml` 可控制隐式调用和工具依赖。

这证明 Skill 应该是 prompt/resource 层，而不是一个隐式的 Python 插件。Meido 可以兼容同一目录形态，但必须在激活时重新校验角色、来源和工具策略。

### Plugin 是分发单元，MCP 是连接协议

Codex Plugin 是可安装的 bundle，可以同时携带 Skills、Apps/connectors、MCP server 和 lifecycle hooks。Plugin 解决安装、版本和分发边界，不能替代 Tool 或 Skill 的运行时语义。MCP 则负责连接外部工具，支持 STDIO 和 Streamable HTTP，并单独配置 server 是否启用、启用/禁用哪些工具、启动/调用超时、输出 token 限制和每个工具的 approval mode（例如 `auto`、`prompt`、`writes`、`approve`）。

因此，Meido 不应把 MCP server 当作 Plugin 的同义词：Plugin 是宿主装配包，MCP 是 Tool 的外部适配器。后续若接入 MCP，应把 server 连接状态、工具暴露和审批策略映射到 `CapabilitySnapshot`，不能让远端 server 直接获得 Meido 的核心服务对象。

### Hook 是 Agent Loop 的策略边界

Codex Hook 由事件、matcher 和 handler 组成，事件覆盖 `SessionStart`、`SessionEnd`、`SubagentStart`、`PreToolUse`、`PermissionRequest`、`PostToolUse`、`PreCompact`、`PostCompact`、`UserPromptSubmit`、`SubagentStop`、`Stop` 和 `Interrupt`。Handler 可以是 command 或 MCP tool；匹配的同类 hook 并发启动，非 managed hook 需要按当前定义的 hash 进行 review/trust。Hook 可以返回额外上下文或阻止决策，但 hook 错误、超时和格式错误的阻止语义是分开的。

Codex 还允许 Plugin bundle hooks，并通过 managed hooks 与用户/项目 hooks 区分信任层级。对 Meido 的直接启发是：Hook 必须有事件、匹配范围、超时、失败策略和信任来源，不能设计成任意订阅内部对象的回调。

## Claude Code：统一组件包和受限 Subagent

### Skill 与长期指令分开

Claude Code 的 Skill 同样采用 `SKILL.md` 目录，并支持 supporting files、front matter、动态上下文注入、显式/隐式调用、工具预授权和在 Subagent 中预加载。Skill 正文只有在使用时才加载；项目、用户、企业、插件和嵌套目录是不同的发现来源，并有明确的同名优先级和命名空间规则。`CLAUDE.md` 负责持续的项目/用户指令，Skill 负责按需流程，两者不是同一个层次。

Meido 已有角色设定和核心运行 prompt，不应复制 `CLAUDE.md` 的整套文件优先级。适合的做法是保留角色设定作为核心上下文，把受限 `SKILL.md` 作为可选任务流程，并把 Skill 的来源、版本、激活原因和最终正文 hash 记录到运行快照。

### Plugin 可以装配多个组件

Claude Code Plugin 是带 `.claude-plugin/plugin.json` manifest 的目录，组件可以包括 Skills、Agents、Hooks、MCP servers、LSP servers、可执行文件、默认设置和 UI 资源。Plugin skill 使用命名空间，Plugin 中的 MCP server 只在插件启用时连接，组件路径必须保持在插件根目录内。Marketplace 是分发目录，不是运行时权限本身。

这与 Meido 的目标相符，但首版只需要静态、进程内 Python Plugin manifest；不引入 marketplace、LSP、UI 资源或动态安装。

### Hook、权限和信任是独立层

Claude Code Hook 通过 command、HTTP 或 MCP tool handler 接入生命周期，覆盖会话、用户 prompt、模型工具调用、权限请求、Subagent、压缩和停止等事件。`PreToolUse` 可以修改或阻止工具调用，`PostToolUse` 可以补充结果信息；matcher 负责按工具名或事件原因筛选，handler 有明确输入输出、退出码、超时和失败处理。

Claude Code 的 permissions 还有 `allow`、`ask`、`deny` 和 managed settings，workspace trust 决定项目提供的 allow 规则、MCP server 和部分插件组件何时生效；sandbox 是操作系统层约束，和模型权限判断分开。这个分层很重要：Meido 的 Plugin manifest/capability grant、Tool policy 和未来 sandbox 应该是三个不同概念，不能用“插件已加载”代表“工具已获准执行”。

### Subagent 是另一种运行边界

Claude Code Subagent 使用 Markdown front matter 声明名称、描述、工具 allowlist、模型、permission mode、最大轮次、预加载 Skill、MCP server、持久化 memory、hooks、后台运行和 worktree isolation。它可以在前台或后台运行，但仍是受宿主管理的独立 Agent 运行单元。

Meido 当前不应把 Subagent 作为普通 Plugin 能力。Codex/Claude Executor、远程任务、审批和长期运行需要独立的 `TaskRuntime`；在此之前，能力层只负责当前角色的一次 `AgentLoop`。

## 四个系统的统一比较

| 维度 | Pi | Shiori-Agent | Codex | Claude Code | Meido 采用方式 |
| --- | --- | --- | --- | --- | --- |
| Tool | Extension 注册，支持 exposure、annotations、structured result | manifest/SDK 注册，带 risk、hooks、external_allowed | 内置/Plugin/MCP 工具，支持 enabled/disabled、approval、timeout、output budget | 内置/Plugin/MCP 工具，受 permissions、trust 和 tool search 管理 | 保留 `ToolRegistry`，补齐 source/version/risk/exposure/approval/timeout/output limit |
| Skill | Skill/resource discovery，工具由 Extension 提供 | 主要由 Plugin/Module 提供 | `SKILL.md`，progressive disclosure，open standard | `SKILL.md`，按来源发现、命名空间、动态上下文和预加载 | 采用受限 `SKILL.md`，只贡献 prompt/resource，不执行代码 |
| Plugin | 进程内 Extension，权限等同宿主 | manifest + capability grant + PluginContext | 安装 bundle，可带 Skill/MCP/Hook | manifest bundle，可带 Skill/Agent/Hook/MCP 等 | 静态进程内 Python manifest，显式 grant，幂等 cleanup |
| MCP | Extension 可注册 server | 可由插件/宿主接入 | STDIO/HTTP，server/tool/approval/timeout/output 分层 | 多 transport、scope、trust 和 tool availability | 后续作为外部 Tool adapter，隔离连接与宿主服务 |
| Hook/Lifecycle | session/tool/agent 事件，部分可变换 | 固定 phase slots，requires/produces | matcher + command/MCP hook，trust/review/managed | matcher + command/HTTP/MCP，decision control | 少量稳定 slots，区分 observe/transform/deny，冻结失败策略 |
| Permission | exposure/annotations，不是 sandbox | capability grant 和 risk | approval、trust、managed policy、sandbox | allow/ask/deny、workspace trust、sandbox | role scope + policy evaluator + 后续独立 sandbox |
| Context loading | Skill/resource 与 prompt checkpoint | lifecycle prompt render | Skill 描述先行、正文按需，工具可延迟搜索 | Skill 正文按需，CLAUDE.md 持续加载 | descriptor 先行，激活正文；snapshot 记录 hash 和原因 |
| Persistence | JSONL session tree/custom entries | Runtime/Plugin state | hook/plugin 配置和运行上下文 | settings、memory、session/worktree | SQLite transcript 为权威，snapshot/diagnostics 单独记录 |
| Distribution | npm/本地 Extension | Python Plugin/manifest | Plugin bundle/marketplace | Plugin/marketplace | 先内置和项目目录，动态市场后置 |

## 对 Meido 的收敛判断

四个系统共同说明能力扩展层至少要拆成六个边界：`Tool`（结构化动作）、`Skill`（按需指令/资源）、`Plugin`（分发和宿主装配）、`MCP`（外部工具连接）、`Hook`（生命周期策略）和 `Policy/Trust`（权限与来源）。Pi 和 Shiori 分别提供了运行时 API 与 capability grant 的实现参考；Codex 和 Claude Code 进一步证明 Skill 的 progressive disclosure、Plugin 的 bundle 边界、MCP 的连接/审批独立性以及 Hook 的 trust/decision 语义是可复用的通用设计。

Meido 的首版应采用 Python 原生、provider-neutral 的能力层：启动时只解析 descriptors，运行开始前生成不可变 `CapabilitySnapshot`，Skill 正文在通过策略后按需编译，Tool schema 和 policy 在当前 run 内保持稳定。Plugin 只能访问显式授予的宿主端口；MemoryEngine 仍是核心服务，`memory.search` 等是受角色作用域约束的 Tool 视图。远程 MCP、Codex/Claude 调用和 Subagent 留给独立 TaskRuntime。

## Meido 当前缺口

当前 Meido Runtime 已有：

- `AgentTool`、`ToolDefinition`、`ToolContext`、`ToolRegistry`；
- `RuntimeManager` 的角色运行锁、取消、超时和持久化回调；
- `MemoryEngine` 的查询、写入、admin 和 tool profile 端口；
- ShellTool 的角色级 allowlist 和执行边界。

当前缺少：

1. 工具的来源、暴露级别、风险提示、启用条件和版本；
2. Skill 资源的发现、解析、优先级和运行快照；
3. 插件 manifest、capability grant、配置和生命周期；
4. 运行开始时的统一 capability snapshot；
5. 工具调用前策略链、审计上下文和插件级资源清理；
6. 能力变更对 prompt/tool loadout 的可诊断记录。

## 设计结论

Meido 应实现一个 provider-neutral 的 `CapabilityRegistry`，而不是把 `ToolRegistry`、Skill 和 Plugin 直接混成一个对象：

```text
CapabilityRegistry
  ├─ ToolRegistry       模型可调用的结构化动作
  ├─ SkillRegistry      声明式提示词和资源
  ├─ PluginRegistry     显式授权的进程内扩展
  └─ PolicyEvaluator    角色、运行、风险和来源策略
```

每次运行创建 `CapabilitySnapshot`，冻结：

- provider tool schemas；
- active skills 和 prompt sections；
- 插件版本和配置版本；
- role/session/run scope；
- 每个工具的 risk、timeout、output limit 和权限结果。

运行过程中可以产生诊断事件，但不能改变 snapshot 中的核心能力集合。动态启用工具、远程 MCP、Codex/Claude Executor 和长期任务应由后续 TaskRuntime 规格决定。

## 可借鉴与不可照搬

### 可借鉴

- Pi 的工具 exposure、annotations、structured result、resource discovery 和 prompt/tool loadout checkpoint；
- Pi 的 session-scoped lifecycle 和幂等 shutdown；
- Shiori 的 manifest capability grant、host service unavailable 区分、effect cleanup；
- Shiori 的 tool hook 和 lifecycle phase slot；
- Shiori 的 MemoryEngine 端口和 engine-owned tool profile。

### 不可照搬

- Pi 的 TypeScript runtime、Coding Agent、文件工具和 Project Trust；
- Shiori 的多渠道/群聊 scope、RPC/UI 插件市场和完整 runtime API 兼容矩阵；
- 任何把插件进程权限当作角色安全边界的方案；
- 用扩展状态或内存 transcript 替换 Meido 的 SQLite 权威状态；
- 让 Skill 绕过工具注册和权限直接执行 Python/命令。

## 事实来源

- Pi Extensions：[docs/extensions.md](https://github.com/badlogic/pi-mono/blob/b30a6dd779340f7bc2f3ffa60f4c0a5f914ba9ae/packages/coding-agent/docs/extensions.md)
- Pi Skills：[docs/skills.md](https://github.com/badlogic/pi-mono/blob/b30a6dd779340f7bc2f3ffa60f4c0a5f914ba9ae/packages/coding-agent/docs/skills.md)
- Pi Session：[docs/session-format.md](https://github.com/badlogic/pi-mono/blob/b30a6dd779340f7bc2f3ffa60f4c0a5f914ba9ae/packages/coding-agent/docs/session-format.md)
- Pi Extension API：[src/core/extensions/types.ts](https://github.com/badlogic/pi-mono/blob/b30a6dd779340f7bc2f3ffa60f4c0a5f914ba9ae/packages/coding-agent/src/core/extensions/types.ts)
- Pi loader/runner：[loader.ts](https://github.com/badlogic/pi-mono/blob/b30a6dd779340f7bc2f3ffa60f4c0a5f914ba9ae/packages/coding-agent/src/core/extensions/loader.ts)、[runner.ts](https://github.com/badlogic/pi-mono/blob/b30a6dd779340f7bc2f3ffa60f4c0a5f914ba9ae/packages/coding-agent/src/core/extensions/runner.ts)
- Shiori SDK runtime：[runtime.py](https://github.com/YinFengWindy/Shiori-Agent/blob/c7ad4423cc63ae5157cfada21e408dd40b360e63/packages/sdk/python/shiori_sdk/runtime.py)
- Shiori lifecycle：[lifecycle.py](https://github.com/YinFengWindy/Shiori-Agent/blob/c7ad4423cc63ae5157cfada21e408dd40b360e63/packages/sdk/python/shiori_sdk/lifecycle.py)
- Shiori tools：[tools.py](https://github.com/YinFengWindy/Shiori-Agent/blob/c7ad4423cc63ae5157cfada21e408dd40b360e63/packages/sdk/python/shiori_sdk/tools.py)、[tool_hooks.py](https://github.com/YinFengWindy/Shiori-Agent/blob/c7ad4423cc63ae5157cfada21e408dd40b360e63/packages/sdk/python/shiori_sdk/tool_hooks.py)
- Shiori plugin manifest：[tool_loop_guard/manifest.yaml](https://github.com/YinFengWindy/Shiori-Agent/blob/c7ad4423cc63ae5157cfada21e408dd40b360e63/plugins/tool_loop_guard/manifest.yaml)
- Meido Runtime：[agent-runtime-tau-integration-spec.md](../specs/agent-runtime-tau-integration-spec.md)、[ADR-0003](../adr/0003-tau-inspired-agent-runtime-boundary.md)
- Meido MemoryEngine：[memory_engine.py](../../backend/app/memory_engine.py)、[ADR-0002](../adr/0002-shiori-memory-engine-boundary.md)
