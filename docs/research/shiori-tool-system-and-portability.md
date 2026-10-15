# Shiori 工具体系、使用方式与 Meido 迁移评估

状态：当前研究记录（静态源码核验；迁移方案为建议，未经落地验收）

最后核验：2026-10-15（Asia/Shanghai）

事实来源：Shiori-Agent 固定提交 [`4f266aeaf42fcd611362e2456417b91f659341cf`](https://github.com/YinFengWindy/Shiori-Agent/commit/4f266aeaf42fcd611362e2456417b91f659341cf)；Meido `main` 固定提交 `bdb4ea1d472e4205aad6e89a60f66f4266efb7ab`。Shiori 提交时间为 2026-10-15 15:39 +08:00，比用户引用的 15:30 回复稍晚，因此本文核验的是这个源码快照，不能倒推那次会话的实际配置。

本文以 **已提取** 标记源码直接证明的事实，**推断** 标记组合证据的解释，**建议** 标记尚未落实的迁移方案。工具示例用于说明协议，未经真实模型、桌面、浏览器、外部账号或发送操作实测。

## 1. 结论先行

**已提取：Shiori 的能力由注册表、上下文策略、插件和运行依赖共同决定。聊天中自报的工具清单不是运行时证据。** `ToolRegistry` 保存工具实现和元信息；被动对话每次请求重新计算 schema；插件可以动态注册和卸载工具；MCP 连接成功后才把远端工具加入注册表。[注册表](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/registry.py#L129-L244)、[对话 schema](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/passive_turn/reasoning_loop.py#L157-L202)、[插件注册](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/plugin_host/capabilities.py#L74-L142)、[MCP 注册](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/mcp/registry.py#L120-L143)。

**已提取：所谓“常驻 / 需要时加载”只在开启工具搜索时成立。** 缺失配置的回退值是 `tool_search_enabled=False`，但当前新安装模板写的是 `[agent.tools] search_enabled=true`，初始化时复制该模板。因此普通新装默认启用按需发现，既有安装要看实际配置。关闭搜索时，被动对话会把策略允许的全部已注册工具发给模型；开启时才从 `always_on + 历史已解锁工具` 开始，随后由 `tool_search` 扩充可见集合。源码没有独立的 Pi 式 `exposure` 枚举。[缺省回退](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/config_models.py#L102)、[新装模板](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/config.example.toml#L42-L43)、[复制模板](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/bootstrap/init_workspace.py#L71-L82)、[请求工具集合](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/passive_turn/reasoning_loop.py#L174-L202)、[初始可见顺序](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/passive_turn/tool_visibility.py#L7-L31)。

**建议：Meido 最值得先移植的是“真实工具清单 + 可用原因 + 完整调用反馈”，然后补读写文件、网络与消息回溯等基础工具。** 浏览器、Shell、记忆和待办应先复用 Meido 已有实现；MCP、后台子任务和桌面控制需要独立的生命周期与权限设计。下文区分可复用实现、需要适配的架构和暂不适合照搬的部分。

## 2. 工具如何装配和进入模型

### 2.1 注册层与提示词清单是两回事

**已提取：** 默认 wiring 顺序是 `meta_common → spawn → schedule → mcp`，记忆服务先创建，再按 wiring 注册工具。群聊和账号工具在其他装配点注册，插件在 runtime 启动时加载，MCP 在启动时后台重连。这些都是注册表的来源，不能只扫描 `agent/tools/` 就当作完整工具列表。[默认 wiring](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/config_models.py#L60-L72)、[注册阶段](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/bootstrap/tools.py#L243-L338)、[启动加载](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/bootstrap/tools.py#L132-L174)。

`meta_common` 明确注册 13 个 `always_on` 工具：`tool_search`、`shell`、`task_output`、`task_stop`、`web_search`、`web_fetch`、`read_file`、`list_dir`、`fetch_messages`、`search_messages`、`message_push`、`write_file`、`edit_file`。记忆工具由当前 `MemoryEngine.tool_profile()` 注入，不是无条件存在；`spawn` 和 `spawn_manage` 另受 `spawn_enabled` 控制。[common 注册](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/meta/register.py#L44-L131)、[记忆 profile](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/meta/register.py#L152-L192)、[spawn 开关](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/bootstrap/toolsets/meta.py#L41-L77)。

**已提取：** `META_TOOLBOX_GROUPS` 是另一份硬编码的人类可读清单，包含常见读写、记忆、推送、终端能力，但没有列出后台 Shell 管理和 `spawn`。它既不是从当前注册表生成，也不反映插件加载、记忆 profile 或依赖故障。[MetaToolbox 文案](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/meta/catalog.py#L12-L61)。

静态全仓检索只找到该文案的定义、导出和测试调用，未找到生产 prompt 调用；不能把它当作实际注入证据。另一个容易误解的点是设置 UI 把 `searchEnabled` 标成“联网搜索”，而此配置实际控制工具发现。[导出](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/meta/__init__.py#L1-L10)、[设置字段](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/renderer/src/settings/advancedSettingsFields.ts#L28-L33)、[配置解析](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/config.py#L152-L154)。

**推断：** 将这段提示词或模型自报清单做成 Meido 的“可用工具”页面，会继续出现“文案写着有、实际 schema 没有”或者“schema 有、底层依赖坏了”的问题。目录、可见性、执行许可和健康状态应分别来自运行时事实。

### 2.2 `tool_search` 的真实行为

**用法约定：本文 JSON 均为业务参数示意。** 普通注册表和 Drift 注册表会为原本未定义该字段的工具注入必填 `description`，要求用 5–12 个字说明此次调用意图，例如“读取角色配置文件”。真实 provider 调用应以最终 schema 为准并补齐该字段；常规主动决策的自有 schema 不经过这一注入。示例中的动态 ID、路径、引用和工具名称都必须使用真实运行结果。[schema 注入](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/registry.py#L15-L60)、[主动独立 schema](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/tools.py#L65-L76)。

**已提取：** 它搜索的是已经注册的工具；不安装插件，也不修复依赖。关键词搜索基于名称、描述、`search_hint` 加权；中文用字符和 bigram；`top_k` 默认 5，限制为 1–10；`allowed_risk` 可筛选 `read-only`、`write`、`external-side-effect`。`select:A,B` 可按名字精确加载。返回 JSON 中的 `unlocked` 被对话循环读取，下一次模型请求加入对应 schema；新解锁名字也单独存入 tool chain，供后续历史窗口恢复。[搜索接口](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/tool_search.py#L33-L173)、[精确加载](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/tool_search.py#L175-L242)、[关键词算法](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/search_backend.py#L54-L173)、[解锁与持久化](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/passive_turn/reasoning_loop.py#L648-L698)。

```json
{"query":"定时提醒","top_k":5}
{"query":"select:schedule,list_schedules,cancel_schedule"}
{"query":"联网检索","allowed_risk":["read-only"]}
```

这些是三次独立的 `tool_search` 参数示例。精确名称必须以运行中注册表为准。`allowed_risk` 只是此次搜索的筛选条件，不能当作宿主授权；搜索不到的工具不一定未实现，也可能未注册、被过滤或已经可见。

**已提取：** 开启搜索时，尚未加载 schema 的工具名还会写入上下文中的“未加载工具目录”；MCP 按 server 分组，插件工具与 builtin 合并到“内置”组。模型偷调未加载工具时，会收到引导 `tool_search(query="select:名称")` 的工具结果。[延迟目录](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/passive_turn/helpers.py#L161-L206)、[来源分组](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/registry.py#L277-L313)、[未加载调用反馈](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/passive_turn/reasoning_loop.py#L509-L543)。

### 2.3 宿主权限、角色作用域、渠道上下文分别控制什么

| 层次 | 已提取行为 | 对 Meido 的含义 |
| --- | --- | --- |
| 插件装配许可 | manifest 声明 capability，宿主只提供相应服务切面；未声明和服务不可用是不同错误 | 复用 Meido 现有 grant，不能把插件启用视为角色授权 |
| 工具发现元信息 | `risk`、`always_on`、`search_hint`、source 参与目录与可见性；`risk` 本身不触发确认 | 保留独立执行策略，不把检索筛选当成审批 |
| 外部发言者准入 | 外部群聊/陌生私聊且发送者不是已绑定用户时，只允许 `external_allowed` 工具；用户本人在群里仍不受此限制 | Meido 多渠道工具需基于发送者和上下文判定 |
| 参数收窄 | `ExternalArgumentLimit` 同时收窄模型 schema 和执行参数；pre hook 改参后再检查 | 同一工具可能只允许某些目标，不能只按名称授权 |
| 选择性上下文保护 | 只有工具显式声明在 `context_precedence` 中的键才强制取宿主值；保护范围由工具 opt-in | Meido 的角色与附件身份应统一由不可变 ToolContext 绑定 |

表格证据：[插件服务检查](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/plugin_host/runtime_context.py#L214-L257)、[注册元信息](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/registry.py#L150-L198)、[外部上下文判定](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/external_access.py#L1-L67)、[执行保护字段](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/registry.py#L314-L343)。

**已提取的限制：这不是全部工具统一具备的身份防伪造。** registry 合并顺序是先宿主 context、后模型 arguments，再恢复 opt-in 键。`memorize`、`forget_memory` 和定时三个工具未声明这组保护，仍从合并 kwargs 读取 `role_id`；SDK 参数验证只检查已声明属性，未统一拒绝额外字段。因此下文“角色范围”描述正常宿主调用的数据流，不能仅因 schema 不列 `role_id` 就声称模型无法覆盖身份。[合并顺序](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/registry.py#L324-L339)、[参数验证](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/packages/sdk/python/shiori_sdk/tools.py#L151-L160)、[记住](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/memorize.py#L16)、[失效](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/forget_memory.py#L13)、[定时](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/schedule.py#L18)。

**推断与建议：** 这些遗漏构成需要审查的身份覆盖路径，本次没有运行跨角色越界复现。迁移时使用 Meido 不可由业务参数改写的 `ToolContext` 身份，拒绝额外身份字段并在服务层重新授权，不照搬 kwargs 的优先级。

**已提取：** 动态 MCP 工具不允许声明 `external_allowed`。目前 common 工具中明确允许外部上下文的有 `tool_search`、`web_search`、`web_fetch`、`fetch_messages`、`search_messages`；Shell、本地文件、主动推送和记忆注册没有同样的放行声明。消息查询内部仍要继续检查可读上下文，外部可用不等于能看全部历史。[common 标记](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/meta/register.py#L51-L130)、[MCP 拒绝声明](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/registry.py#L170-L179)。

**已提取：** 插件代码在宿主进程内执行，插件包校验和 capability grant 不构成操作系统沙箱。外部包还需要独立的内容指纹信任；源码明确区分“可导入”和“获得信任”。[插件运行契约](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/docs/_handbook/plugin-runtime-contract.md#L1-L12)、[启用和信任门禁](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/plugin_host/kernel.py#L368-L406)。

### 2.4 工具调用、失败反馈和可观测性

```text
模型请求的 schema
  → 模型 tool_call
  → 当前回合名称/外部上下文检查
  → pre hooks（改参、拒绝、要求收尾）
  → 宿主按最终参数再次准入
  → ToolRegistry.execute（合并受保护上下文，调用实现）
  → post hooks / trace
  → tool result 追加到消息
  → 记录最终参数、结果预览、解锁事实
  → 下一次模型请求
```

**已提取：** pre hook 是唯一可改参数或直接拒绝的阶段；post hook 只观察和补充记录；宿主 `admit` 在 pre hook 之后再检查。模型得到工具文本；多模态 `ToolResult.content_blocks` 另追加到可见消息；tool chain 记录 `status`、原始/最终参数和 hook trace。[执行器](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tool_hooks/executor.py#L35-L155)、[调用与记录](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/passive_turn/reasoning_loop.py#L545-L698)、[结果追加](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tool_runtime.py#L92-L137)。

**推断：存在值得修正而非照搬的失败语义。** `ToolRegistry.execute` 捕获实现异常并返回“工具执行出错”的普通字符串；`ToolExecutor` 只有在 invoker 抛异常时才走 error 分支，因此该调用可能仍记为 `status="success"` 并走 `post_tool_use`。这不表示功能实际成功。Meido 应用明确的结构化 error/denied/timeout/cancelled 状态贯通执行器、模型回传和 UI。[异常转文本](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/registry.py#L314-L343)、[执行器成功判定](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tool_hooks/executor.py#L92-L155)。

### 2.5 普通对话、主动决策和 Drift 的工具集合不同

**已提取：** 被动对话使用共享注册表和上述发现规则；Drift 空闲主动回合另建注册表：本地 `read_file/write_file/edit_file`，借用共享的 `recall_memory/web_fetch/web_search/fetch_messages/search_messages/shell`，加上专用 `message_push/finish_drift`；有 MCP 时再加 `mount_server`。它没有被动对话的 `tool_search`，而是初始直接提供本地工具 schema，并按 server 挂载远端工具。[Drift 工具构建](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/drift_tools.py#L409-L467)、[Drift schema](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/drift_turn.py#L181-L213)。

Drift 的文件写入限定在该 Drift 目录；读取允许绝对路径，不能把它描述成完整文件沙箱。记忆固定为当前角色，消息回溯限定用户上下文；专用推送每 run 最多尝试一次，`finish_drift` 要核对实际发送状态。成功推送后，下一步 schema 只剩 `write_file/edit_file/finish_drift`。这些是 Drift 的特定语义，不代表普通聊天工具具有同样的限制。[文件与角色包装](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/drift_tools.py#L341-L467)、[单次发送](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/drift_tools.py#L45-L150)、[结束校验](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/drift_tools.py#L153-L244)。

**建议：** Meido 的工具清单至少应带“角色 + 运行类型 + 发送者上下文 + 快照版本”，否则一个全局“可用”标记无法解释主动提醒、后台任务、外部群聊和普通聊天的差异。

**已提取：** 常规主动决策还有单独的 `TOOL_SCHEMAS` 与 dispatch：读取本 tick 的 alert/content/context、查询兴趣记忆、取预抓正文、判断最近聊天、标记内容、提交发送或跳过。这里的 `message_push` 只暂存草稿，必须再调用 `finish_turn(decision="reply")` 才提交，和普通对话、Drift 的同名工具不是同一协议。[主动 schema](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/tools.py#L76-L293)、[主动分发](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/tools.py#L652-L709)。

| 常规主动工具 | 业务参数 | 使用语义 |
| --- | --- | --- |
| `get_alert_events` | 无 | 读取本 tick 已缓存的告警事件 |
| `get_content_events` | 无 | 读取本 tick 内容候选 |
| `get_context_data` | 无 | 读取状态背景，本 tick 最多一次 |
| `recall_memory` | `query` | 用正负兴趣假设召回当前角色记忆，零命中不代表不感兴趣 |
| `get_content` | `item_ids` | 按 `server:event_id` 复合键取预抓正文；先筛选再读 |
| `web_fetch` | `url` | 补读明确来源页面 |
| `web_search` | `query`；可选 `type` | 补充外部信息 |
| `get_recent_chat` | 可选 `n`，默认 20 | 看最近正常聊天；过滤主动推送与内部 context frame |
| `message_push` | `message/mood/thought`；可选 `evidence/account_channel/target_kind/target_id/...` | 只暂存一次草稿；目标仍须角色账号授权 |
| `mark_interesting` | `item_ids`；可选 `reason` | 标记本 tick 候选相关性，供后续提交策略处理 |
| `mark_not_interesting` | `item_ids`；可选 `reason` | 标记本质不感兴趣；时机不好或抓取失败不应这样分类 |
| `finish_turn` | `decision=reply/skip`；skip 必须 `reason`，可选 `note` | reply 要求已有草稿；skip 不允许已有草稿 |

这些是 12 项固定主动 schema；共享注册表可用时还追加 `account_list/account_targets`，没有 `tool_search`。典型流程为 `get_content_events → recall_memory(兴趣假设) → mark_interesting → get_content → get_recent_chat → message_push → finish_turn(reply)`。兴趣分类不是永久记忆写入；描述中的 24 小时/720 小时 ACK 要由后续策略提交。`finish_turn` 形成最终消息与终态，真实外发在外层处理，不能把它当成已送达回执。[主动装配](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/proactive_turn/judge.py#L228-L233)、[最近聊天](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/tools.py#L442-L462)、[草稿与终态](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/tools.py#L574-L644)。

## 3. 工具分类与参数用法

**计数口径：普通对话的静态候选集合共 76 个名称。** 核心/宿主管理 31 个，七个工具插件贡献 45 个。这个数是全部相关模块具备装配条件时可枚举的候选，不是出厂启用数量，更不是某个角色能调用的数量；替代 MemoryEngine 自定义工具、已连接 MCP server、常规主动和 Drift 专用工具另计。类名包装器、宿主 RPC、渠道事件、语音引擎和 Skill 都不直接计为新的模型工具。

| 来源 | 静态数量 | 组成 |
| --- | ---: | --- |
| common meta | 13 | 发现、文件 4、网络 2、Shell 3、消息原文 2、推送 1 |
| 默认 memory profile | 3 | 记住、召回、标记失效；仅启用结构化引擎时装配 |
| spawn | 2 | 创建与管理子任务；受总开关控制 |
| schedule | 3 | 创建、列出、取消 |
| 账号 | 3 | 账号、目标、投递 |
| 群环境 | 4 | 成员、群摘要/笔记、旁听 |
| MCP 管理 | 3 | 添加、列出、移除 server；不含远端工具 |
| 插件 | 45 | Browser 28、Computer 8、Herdr 5、屏幕/桌宠/生图/QQ 附件各 1 |

### 3.1 核心固定工具：31 个

例子是业务参数的精简JSON，不包含宿主注入的 role_id/session/thread 等。**除 shell 已有 description 外，注册表会给所有模型 schema 追加必填 `description`（5–12字调用意图）；下表为阅读省略了该字段，真实调用需补，例如 `{"description":"查看已连接渠道"}`。** 参数必填以最终模型 schema 为准，例子中的消息/账号/群/路径均为占位，不代表本机或用户已连接该服务。[统一意图字段](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/registry.py#L41)

| 工具 | JSON 调用示例 | 实际效果与返回 |
|---|---|---|
| `tool_search` | `{"query":"select:schedule,list_schedules"}`；`{"query":"群笔记","top_k":5,"allowed_risk":["read-only"]}` | 精确选择或关键词索引；`top_k` 实际钳到 1–10；JSON 包含 matched/unlocked/already_loaded。主循环据 unlocked 加入本回合可见 schema。不能创建尚未注册的能力，也不绕过外部准入。见 [tool_search](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/tool_search.py#L33)。 |
| `read_file` | `{"path":"C:/work/report.txt","offset":0,"limit":50}` | offset 从 0 计；输出带从 1 开始的行号；最多返回 400 行/10,000 UTF-8 字节；二进制拒绝当文本读。支持图片时返回 ToolResult + image_url content block。见 [读取](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/filesystem.py#L248)。 |
| `list_dir` | `{"path":"C:/work"}` | 仅一级子项、排序、目录/文件图标；不递归、不做 glob。见 [目录](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/filesystem.py#L520)。 |
| `write_file` | `{"path":"C:/work/out.txt","content":"hello"}` | UTF-8 整体覆盖，自动创建父目录，同文件锁。description 的“先读取、不主动建 md”是模型指引，execute 不做这些授权检查。返回文字；所谓“字节”实际 `len(content)` 字符数。见 [写文件](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/filesystem.py#L375)。 |
| `edit_file` | `{"path":"C:/work/out.txt","old_text":"hello","new_text":"你好","replace_all":false}` | 精确文本替换，自动处理 UTF-8 BOM 与纯 CRLF 文件的 LF 复制兼容；多处匹配且未 replace_all 会拒绝并提示，无匹配报错；返回统一 diff；不支持按行号 patch。见 [编辑](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/filesystem.py#L425)。 |
| `shell` | `{"command":"python --version","description":"查看 Python 版本","timeout":60}` | Windows 实际启动 pwsh 7；POSIX create_subprocess_shell。JSON 有 command/exit_code/interrupted/duration_ms/output/truncation/full_output_path；默认 60 秒，15 秒自动后台，普通 timeout 最大600，关闭 auto_promote 可达21600。见 [shell schema](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell/tools.py#L64)、[执行](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell/tools.py#L156)、[runner](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell/runner.py#L18)。 |
| `shell` 后台模式 | `{"command":"python task.py","description":"运行分析任务","run_in_background":true,"timeout":120}` | 立即返回 background_task_id/status/output_path/started_at_ms/timeout_s；显式后台若不传 timeout 则没有硬截止。前台自动转后台则实际保留默认/显式 timeout 为硬截止。见 [后台创建](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell/tools.py#L233)。 |
| `task_output` | `{"task_id":"shell_<id>","block":true,"timeout_ms":30000}` | 真实 name 就是 task_output，非类名 ShellTaskOutputTool。单次最多等30秒，返回 running/done、exit_code、elapsed_ms、since_last_output_ms、尾部output、truncation、output_path；超时/不存在给 JSON error。完成不会自动通知主会话，要主动轮询。见 [输出轮询](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell/background.py#L136)。 |
| `task_stop` | `{"task_id":"shell_<id>"}` | 杀整棵进程树，清 registry/log；返回 stopped 或 not_found。真实 name 为 task_stop。见 [停止](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell/background.py#L283)。 |
| `web_search` | `{"query":"Python asyncio 文档","num_results":5,"livecrawl":"preferred","type":"deep"}` | 向公开 `https://mcp.exa.ai/mcp` POST JSON-RPC `web_search_exa`；默认8条最多20；25秒 httpx 超时；接受 livecrawl fallback/preferred 和 type auto/fast/deep。解析 SSE 的首段 content[0].text 后封成 `{"query":...,"result":...}`；失败 JSON error；非SSE有效JSON无解析路径会给空results。代码无需API key，不等于端点永远可用/无限额。见 [搜索实现](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/web_search.py#L9)。 |
| `web_fetch` | `{"url":"https://example.com","format":"markdown","timeout":30}` | HTTP/HTTPS GET+跟随重定向；text/markdown/html；默认30秒最大120；HTML 用 html2text/lxml 转换；非HTML文字直接返回；拒绝PDF/图片/音视频；目标上限5MiB、文本上限50,000字符。JSON含url/final_url/status/content_type/format/length/text，截断带note。见 [抓取](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/web_fetch.py#L32)。 |
| `memorize` | `{"summary":"主人不喝咖啡","memory_kind":"preference"}` | 调 memory.mutate(remember)；正常宿主调用注入当前角色和 source_ref，scope绑定角色（身份覆盖限制见 §2.3）；kind可空/profile/preference/event/procedure，engine可校正分类；procedure还可tool_requirement/steps。JSON为item_id/memory_kind/status/summary。见 [默认schema](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/default_memory/backend/engine/prompts.py#L231)、[执行](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/memorize.py#L16)。 |
| `recall_memory` | `{"query":"主人关于咖啡的偏好","intent":"answer","memory_kind":"preference","limit":8}`；`{"query":"最近做过的事","intent":"timeline","time_filter":"recent_7d","limit":30}` | 默认引擎schema只暴露answer/timeline，kind/profile/preference/event/procedure，time_filter支持today/yesterday/recent_3d/7d/30d/单日/日期区间；limit1–200；宿主API也支持更多内部intent/domain但不应把其当默认模型接口。JSON含count/items/trace/cited_item_ids，各item有summary/score/evidence/source_ref；精确历史事实还需fetch_messages。见 [schema](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/default_memory/backend/engine/prompts.py#L231)、[执行](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/recall_memory.py#L30)。 |
| `forget_memory` | `{"ids":["<memory-item-id>"]}` | 正常宿主身份下按角色范围标记失效，非物理删除（身份覆盖限制见 §2.3）；清理/去重ids；JSON含requested_ids/superseded_ids/missing_ids/count/items。需先召回并按需核原文。见 [执行](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/forget_memory.py#L13)。 |
| `search_messages` | `{"query":"报错","role":"user","limit":10,"offset":0}` | grep式原始文本定位，limit1–50，分页next_offset；返回候选preview（最多50行）、source_ref、时间等。可选session_key在模型schema里，但角色回合的实际搜索范围由宿主turn_session_key/role_id/thread_id决定，不能扩到别的角色/用户上下文。见 [查询](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/message_lookup.py#L245)、[范围](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/message_lookup.py#L153)。 |
| `fetch_messages` | `{"source_ref":"<search-result-source-ref>","context":2}`；`{"evidence":[{"source_ref":"<ref>","refs":["<ref>"]}],"context":0}` | ids/source_ref/source_refs/evidence 任一输入；上下文前后0–10条；回源返回原文/seq/role/timestamp/in_source_ref，去掉内部metadata；同样按当前角色/上下文/绑定边界裁切；这是记忆摘要和搜索预览之后的证据工具。见 [回源](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/message_lookup.py#L26)。 |
| `message_push` | `{"channel":"desktop","chat_id":"role:<current-role-id>","message":"报告已经准备好"}` | 三种payload message/file/image至少一项；先验证渠道支持全部payload再发；当前角色只能自己的desktop目标，**外部channel被拒并要求account_send**。动态description列出当前已注册transport，不代表role能发全部列出渠道。返回文字“已发送/排队/失败”，底层sender可返回真实平台ID给宿主。见 [发送检查](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/message_push.py#L251)、[runtime目标边界](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/bootstrap/tools.py#L807)。 |
| `account_list` | `{}` | 查当前角色已加载的渠道账号，返回channel/platform/name/online/connection/capabilities/error，不向模型暴露account_id。见 [工具](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/account_delivery.py#L28)、[服务](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/account_delivery/service.py#L81)。 |
| `account_targets` | `{"channel":"qq","kind":"groups"}`；`{"channel":"qq","kind":"members","group_id":"<group-id>"}` | kind可friends/groups/members/known/member；member还需member_id；查当前角色在该渠道账号，由该渠道plugin RPC `account.targets`决定覆盖面与限制；不保证每个平台都能完整列好友/群。见 [工具](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/account_delivery.py#L28)、[服务](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/account_delivery/service.py#L81)。 |
| `account_send` | `{"channel":"qq","target_kind":"user","message":"提醒内容"}`；`{"channel":"qq","target_kind":"group","target_id":"<discovered-id>","mention_ids":["<id>"],"message":"消息","media":["C:/work/chart.png"]}` | 文本或图片，至少一项；user仅需channel，宿主以主人身份绑定和已知私聊解析；private/group/group_member需要实际ID，group_member需group_id，话题用message_thread_id，@只给group。目标类型能力插件校验；JSON含attempt_id/channel/target_kind/target_id/platform_message_id/ownership_current。不提供file任意附件。外部受限回合仅target_kind=user。见 [发送工具](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/account_delivery.py#L99)、[共享目标schema](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/packages/sdk/python/shiori_sdk/accounts/targets.py#L39)。 |
| `spawn` | `{"task":"目标：对比三个方案。约束：只读资料，返回中文表格。上下文：……。输出：结论与引用。","label":"方案调研","profile":"research","run_in_background":false}` | 默认同步，独立subagent不继承完整会话，需要task写足交接上下文；同步10轮、结果最多100,000字符；profile research/scripting/general，未知profile实际回落research；描述中“4步以上/非即时行动”主要是模型指引，policy实际只硬拦后台并发3。见 [spawn工具](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/spawn.py#L16)、[policy](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/policies/delegation.py#L16)、[manager](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/background/subagent_manager.py#L40)。 |
| `spawn` 后台模式 | `{"task":"……完整任务……","label":"长任务","profile":"research","run_in_background":true}` | 依赖当前channel/chat_id/role_id；建workspace/subagent-runs/<job_id>；最多50轮；后台结束走SpawnCompletionEvent回灌当前会话，结果12,000字符截断；与shell后台**不同：会自动回传**。见 [manager](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/background/subagent_manager.py#L40)。 |
| `spawn_manage` | `{"action":"list"}`；`{"action":"cancel","job_id":"<id>"}` | list返回running_count/jobs（id/label/profile/task_dir/task摘要/started_at），cancel发取消事件并取消async task，返回cancel_requested/not_found。见 [管理](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/spawn.py#L191)。 |
| `schedule` | `{"tier":"instant","trigger":"after","when":"5m","message":"起来活动一下","channel":"desktop","chat_id":"role:<current-role-id>","request_time":"2026-10-15T15:30:00+08:00","name":"活动提醒"}` | instant定时固定文本；soft到时AI执行prompt；at绝对时间（14:30或ISO）、after30s/5m/2h、every间隔或cron；默认Asia/Shanghai；after按消息接收request_time补偿模型延迟。正常调用依赖宿主 role_id，但未由 context_precedence 保护（见 §2.3）；返回人类可读注册成功/首次触发时间文字。见 [schedule](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/schedule.py#L18)。 |
| `list_schedules` | `{}` | 正常宿主身份下只列当前角色待执行任务（身份覆盖限制见 §2.3），返回文本列表/无任务，含name/tier/trigger/下次时间/内容预览/运行次数；受限外部回合也可查本角色。见 [列表](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/schedule.py#L172)。 |
| `cancel_schedule` | `{"name":"活动提醒"}`；`{"id":"<at-least-8-char-id-prefix>"}` | 正常宿主身份下在当前角色内按名字或id前缀匹配（身份覆盖限制见 §2.3），可能取消多个；返回取消数量。description说前缀至少8位，但execute没有长度校验，复刻时需修契约。见 [取消](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/schedule.py#L211)。 |
| `set_group_listening` | `{"channel":"qq","chat_id":"<group-id>","enabled":true}` | 只主人请求下开/关角色旁听，记录operator=role；桌面/用户上下文可目标任意可旁听群，主人本人在群中只能当前群；非主人受限外部回合拿不到；插件manifest需支持group_listening。旁听只让看见消息，不变成每条都主动发言。见 [工具](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/group_listening.py#L40)、[装配](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/bootstrap/toolsets/groups.py#L25)。 |
| `lookup_member` | `{"name":"小明"}`；`{"member_id":"<sender-id>","member_channel":"qq"}` | name/member_id必须且只能一项；名字匹配新旧昵称（最多10候选）；按ID返回name/nicknames/brief/profile；外部上下文只当前渠道，用户上下文名字可跨渠道，ID需明确member_channel。role来自宿主。见 [成员](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/member_lookup.py#L43)。 |
| `lookup_group_context` | `{"name":"开发群","page":1}`；`{"group_thread_id":"<discovered-id>"}` | 只当前角色的已归属群；候选可跨渠道/跨群，每页20；读指定群返回group_note/summary/summary_updated_at等；不读原始群聊/旁听原文，不读私聊或其他角色群。见 [群查询](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/group_context.py#L42)。 |
| `update_group_context` | `{"group_thread_id":"<discovered-id>","group_note":"完整替换后的群笔记"}` | group_note/summary至少显式给一项，整篇替换；省略保留，空字符串清空；通过统一群记忆写入口，不改原文；返回当前笔记/摘要/更新时间。见 [群写入](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/group_context_update.py#L15)。 |
| `mcp_add` | `{"name":"calendar","command":["python","C:/work/mcp/calendar_server.py"],"env":{}}` | 仅本地stdio子进程；name/command必填，env可选；连接握手并发现tools/list后立即注册所有服务工具，持久化workspace/mcp_servers.json。动态名形如`mcp_calendar__toolname`，服务不存在/失败返回可读错误；不提供远程HTTP/SSE的同一接口。见 [MCP管理](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/mcp/manage_tools.py#L9)、[连接注册](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/mcp/registry.py#L78)。 |
| `mcp_remove` | `{"name":"calendar"}` | 注销该服务所有工具，断开stdio进程/连接，持久化清单；模型只能按服务名操作，没有逐工具授权选项。见 [MCP管理](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/mcp/manage_tools.py#L9)。 |
| `mcp_list` | `{}` | 返回已连接服务及其工具名的文本清单。动态工具不等于预置内置能力，其依赖由所配置服务决定。见 [MCP管理](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/mcp/manage_tools.py#L9)。 |

#### 核心工具运行依赖与迁移边界

#### 文件 / Shell

主会话文件工具的 `allowed_dir=None`（common注册Write/Edit无参，read_only build也不传allowed_dir），相对路径基于后端进程cwd，绝对路径可到宿主当前OS用户能够访问的范围；**没有默认角色workspace隔离**。路径函数支持allowed_dir时用resolve/relative_to拦越界，这是可以复刻的构件，但要由Meido应用层真正配置角色根目录。[主会话装配](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/meta/register.py#L44)；[只读工具构造](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tool_bundles.py#L12)；[路径函数](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/packages/sdk/python/shiori_sdk/files/paths.py#L6)

Shell 主实例 `ShellTool()` 默认 `allow_network=True`、`working_dir=None`、`restricted_dir=None`；`_shell_env()`复制整个`os.environ`后补用户PATH；Windows要求pwsh（找不到给可读错误），通过 UTF-16LE -EncodedCommand执行命令；POSIX是真shell字符串执行。它允许串联/管道/重定向等shell语法，并不等同Meido ADR0004的白名单argv执行。[主注册](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/meta/register.py#L44)；[shell构造](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell/tools.py#L64)；[环境](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell/environment.py#L9)；[runner](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell/runner.py#L18)

前台与后台输出持续泵到临时log，返回最多30,000字符尾部；前台截断时完整内容再保留临时文件。后台完成5分钟后清注册表和log；TTL常量4小时仅在done任务被取时判断，**不代表所有运行后台4小时会自动杀**。显式后台没有timeout会长久运行；见实际测试 `test_task_output_ttl_does_not_kill_running_task_without_timeout`。[常量](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell/constants.py#L5)；[后台生命周期](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell/background.py#L86)；[输出协议](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell/output.py#L32)；[测试](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/shell/test_tools.py#L61)

后台任务 `_BG_REGISTRY` 是模块全局，task数据不存role_id；output/stop仅按task_id查表，不按角色owner判定。spawn_manage也是shared manager全部running_jobs；移植给多角色Meido时需要在服务层明确角色/任务所有者，不能靠随机ID或模型不猜ID隔离。[shell registry](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell/background.py#L26)；[spawn manager](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/background/subagent_manager.py#L255)

subagent research能只读workspace文件+联网；scripting能读整个workspace、仅write/edit指定task_dir、Shell固定cwd但未传restricted_dir；general兼有搜索和shell。**scripting的“不能网络”、shell的“仅目录”均不是操作系统沙箱**，网络检查主要识别首个curl/wget/httpie等命令；解释器/其他工具能绕过此类词法护栏。在Windows unrestricted非网络首命令会直接返回无校验。只可表述为工具集和参数护栏。[profile实际装配](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/background/subagent_profiles.py#L55)；[shell验证器](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/shell/validation.py#L24)

#### 网络

web_search走独立httpx client和Exa公开endpoint，不走共享requester，所以不能仅换全局HTTP配置就假定搜索被同一网络策略管理。生产复刻应有配置化backend、失败分层、超时、容量/限额与替代provider，而非依赖“无key”推断服务承诺。[搜索](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/web_search.py#L9)

web_fetch的SSRF检查只显式拦输入url的IP类别与.local/.localhost后缀；没有DNS解析校验，也没有逐跳重定向目标复验。`localhost`裸名也不是后缀规则命中。共享requester只负责重试/预算/HTTP，不补SSRF策略。web_fetch没有把5MiB传成max_response_bytes，5MiB检查发生在拿到resp之后，所以是**返回前内容检查，并非下载中硬上限**。这些是复刻时应加强的实际边界，不应照搬其description的安全暗示。[fetch校验与请求](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/web_fetch.py#L32)；[URL校验](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/web_fetch.py#L173)；[共享HTTP读取](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/core/net/http.py#L115)

图片read_file直接嵌base64时阈值约8M base64字符；大图用Pillow压缩、最大边1568，Pillow缺失就报可读错；按图片header识别，不只扩展名。模型不支持多模态则只提示已检测图片。read_file对大文本的400行/10KB是在扫描后截断，默认limit=None仍先保留所有选中行；可复刻其输出形式但改成有界IO。[图片](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/filesystem.py#L91)；[文本扫描](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/filesystem.py#L184)

#### 记忆 / 历史证据

三个memory工具的关键不是名称，而是通过MemoryEngine.query/mutate和MemoryScope(role_id,session_key,channel,chat_id)统一执行；默认插件提供类型 schema，正常调用由宿主注入身份；写入与失效工具的 opt-in 保护遗漏见 §2.3。可借鉴“语义召回 → 文本定位 → 原消息证据 → 有来源的答复/记忆修正”闭环。default_memory插件依赖numpy/sqlite-vec/json-repair/httpx；完整引擎不宜为了三个工具重复引入到Meido既有记忆引擎。[工具执行](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/memorize.py#L16)；[召回](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/recall_memory.py#L30)；[失效](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/forget_memory.py#L13)；[默认插件依赖](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/default_memory/pyproject.toml#L9)

message工具范围不由模型JSON说了算：角色回合绑定turn_session_key/role_id/thread_id；群内不能查用户上下文；没有可判定context的回合无法读取role共享会话；已绑定外部聊天还应用绑定边界。`fetch_messages`只返回允许的公开字段。[消息范围](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/message_lookup.py#L153)；[搜索范围实现](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/message_lookup.py#L245)；[范围测试](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/test_message_lookup.py#L71)

#### 发信 / 定时 / 后台子Agent

最新commit修正后，`message_push`角色目标唯一为`desktop:role:<role_id>`。渠道存在、在线、甚至群绑定均不足以授权message_push外发。真正外部发送走`AccountDelivery`：当前角色按 channel 找到账号 → 本地 ledger 记 attempt → 校验 payload 并 authorize 获取归属 fence → plugin RPC`account.send` → 必须拿真实message_id → 记sent或uncertain → 返回ownership_current。投递取消/连接超时/缺回执会记录uncertain，防止主循环把已发送的事务当普通失败重复重试。`account_send.target_kind=user`会按主人绑定且曾建立的私聊解析并回写角色会话；普通其他目标不走该用户私聊同步分支。[role目标](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/bootstrap/tools.py#L807)；[account服务](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/account_delivery/service.py#L81)；[发送回执](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/account_delivery/service.py#L226)；[用户投递回写](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/account_delivery.py#L99)

schedule持久化workspace/schedules.json，启动恢复时校验角色存在、处理misfire；instant固定推送，soft用process_direct并`omit_user_turn/skip_post_memory/skip_memory_retrieval`，避免将系统任务当用户回合。soft还有提前量/生成耗时跟踪。它与Meido ADR0005待办提醒领域并不等同：Shiori是“角色任意计划任务”，Meido待办是主人共享、每日固定提醒者、暂停等独立规则。[定时服务](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/scheduler.py#L174)；[恢复](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/scheduler.py#L435)；[实际执行](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/scheduler.py#L625)

**当前定时外发的源码集成缝隙（推断，未运行）**：schedule schema还允许外部channel/chat_id，但SchedulerService的instant/soft最终都调用message_push.execute且带role_id；新role validator会拒绝所有外部目标。注册成功不代表外部到时投递可用。因此用法示例选择desktop；移植时要统一按投递服务授权，不保留旧外部push捷径。[到时投递](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/scheduler.py#L625)；[当前validator](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/bootstrap/tools.py#L807)

spawn的产物和事件比普通asyncio.create_task完整：独立任务目录、role runtime快照、运行trace、profile、并发限制、取消事件、完成事件回主会话。research是可先做的窄入口；scripting/general需要Meido ADR0004的执行边界及独立TaskRuntime决策，不宜顺手并入普通对话Loop。[manager](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/background/subagent_manager.py#L40)；[profile](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/background/subagent_profiles.py#L55)

MCP管理入口与Shell一样能够启动本地程序；McpClient默认继承宿主完整环境再合并env，registry发现服务工具后统一标为external-side-effect、source_type=mcp、不声明external_allowed。因此MCP是强大的扩展接入能力，不能当“天然安全的只读插件”；若要移植，至少要有配置主人批准的服务、按角色/工具分配权限、最小环境、外部回合限制、超时取消和清理生命周期。[动态注册](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/mcp/registry.py#L78)；[进程环境](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/mcp/client.py#L78)

### 3.2 插件贡献总览

扫描全部 `plugins/**/*.py` 的工具类和注册点后，只有 7 个插件直接向模型 ToolRegistry 贡献工具，静态最大 45 个：browser_use 28、computer_use 8、screen_perception 1、desktop_pet 1、herdr 5、novelai 1、qq 1。这不是“某个角色运行时可调用 45 个”的保证；插件启停、渠道、外部发起者策略、工具发现、角色配置和宿主组件都会继续收窄集合。注册源见下表。新插件 SDK 默认 `always_on=False`、`external_allowed=False`；`external_allowed` 专指群聊/陌生私聊里非用户本人发起的回合，与是否联网不同。[apps/backend/agent/plugin_host/capabilities.py L89](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/plugin_host/capabilities.py#L89)

| 插件 | 工具数 | 新安装默认启用 | always_on | external_allowed | 注册依据 |
|---|---:|---|---|---|---|
| browser_use | 28 | 否 | 否 | 否 | [plugins/browser_use/backend/plugin.py L19](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/plugin.py#L19) |
| computer_use | 8 | 否 | 否 | 否 | [plugins/computer_use/backend/plugin.py L20](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/plugin.py#L20) |
| screen_perception | 1 | 是（manifest 不覆写默认 true） | 是 | 否 | [plugins/screen_perception/backend/plugin.py L19](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/plugin.py#L19) |
| desktop_pet | 1 | 是（manifest 不覆写默认 true） | 是 | 否 | [plugins/desktop_pet/backend/plugin.py L47](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/plugin.py#L47) |
| herdr | 5 | 否 | 否 | 否 | [plugins/herdr/backend/plugin.py L28](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/plugin.py#L28) |
| novelai | 1 | 否 | 是 | 是 | [plugins/novelai/backend/plugin.py L135](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/plugin.py#L135) |
| qq | 1 | 是（manifest 不覆写默认 true） | 否 | 是 | [plugins/qq/backend/plugin.py L26](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/qq/backend/plugin.py#L26) |

默认启用值定义为 true；browser_use/computer_use/novelai/herdr 的 manifest 分别显式设为 false。browser_use/computer_use/novelai 有升级兼容迁移：老配置缺少 enabled 时补 true，不能用新装默认值推断现有用户状态。Herdr 从加入起即默认关闭，未列入这三项迁移。[apps/backend/agent/plugin_host/manifest.py L128](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/plugin_host/manifest.py#L128)；[plugins/browser_use/manifest.yaml L13](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/manifest.yaml#L13)；[plugins/computer_use/manifest.yaml L14](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/manifest.yaml#L14)；[plugins/novelai/manifest.yaml L6](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/manifest.yaml#L6)；[plugins/herdr/manifest.yaml L7](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/manifest.yaml#L7)；[apps/backend/agent/plugin_default_enabled_migration.py L1](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/plugin_default_enabled_migration.py#L1)


### 3.3 Browser Use：28 个工具，Meido 已有对应能力

插件从固定 `agent-browser mcp --tools core` 的工具 schema 捕获 JSON，删去宿主管理字段及诊断 profiles 工具；不是把全部浏览器 CLI 或任意 MCP 服务暴露给模型。注册风险统一标为 external-side-effect。[plugins/browser_use/README.md L17](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/README.md#L17)；[plugins/browser_use/backend/plugin.py L19](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/plugin.py#L19)

下表每个名称都加前缀 `agent_browser_`：

| 后缀 | 操作/参数 | 固定源码 |
|---|---|---|
| `open` | 导航；url | [open L15](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L15) |
| `read` | 清洁正文/Markdown；url、filter、llms、outline、raw、readTimeoutMs、requireMd | [read L59](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L59) |
| `snapshot` | 可访问性快照；compact、delta、depth、full、includeUrls、interactive、selector | [snapshot L105](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L105) |
| `click` | selector 必填；human、newTab | [click L133](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L133) |
| `fill` | selector、text 必填 | [fill L156](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L156) |
| `type` | selector、text 必填；clear、delayMs | [type L189](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L189) |
| `press` | key 必填 | [press L207](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L207) |
| `check` | selector 必填 | [check L225](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L225) |
| `uncheck` | selector 必填 | [uncheck L243](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L243) |
| `select` | selector、values 必填 | [select L270](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L270) |
| `scroll` | amount、direction、selector | [scroll L301](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L301) |
| `wait_ms` | ms 必填 | [wait_ms L319](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L319) |
| `wait_for_selector` | selector 必填；waitTimeoutMs | [wait_for_selector L342](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L342) |
| `wait_for_text` | text 必填；waitTimeoutMs | [wait_for_text L364](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L364) |
| `wait_for_load` | state 必填；waitTimeoutMs | [wait_for_load L391](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L391) |
| `screenshot` | annotate、format、fullPage、ifChanged、quality、selector、threshold | [screenshot L441](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L441) |
| `get_text` | selector 必填 | [get_text L459](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L459) |
| `get_url` | 无参数 | [get_url L470](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L470) |
| `get_title` | 无参数 | [get_title L481](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L481) |
| `eval` | script 必填 | [eval L499](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L499) |
| `close` | 无参数 | [close L510](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L510) |
| `back` | 无参数 | [back L521](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L521) |
| `forward` | 无参数 | [forward L532](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L532) |
| `reload` | 无参数 | [reload L543](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L543) |
| `tab_new` | label、url | [tab_new L561](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L561) |
| `tab_list` | 无参数 | [tab_list L572](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L572) |
| `tab_switch` | tab 必填 | [tab_switch L590](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L590) |
| `tab_close` | tab | [tab_close L606](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L606) |

典型使用：
```json
{"tool":"agent_browser_open","arguments":{"url":"https://example.com"}}
{"tool":"agent_browser_snapshot","arguments":{"interactive":true}}
{"tool":"agent_browser_fill","arguments":{"selector":"@e2","text":"要输入的文字"}}
{"tool":"agent_browser_click","arguments":{"selector":"@e3"}}
{"tool":"agent_browser_tab_list","arguments":{}}
{"tool":"agent_browser_screenshot","arguments":{"fullPage":false}}
```
`@e2/@e3` 只是格式示例，必须使用本次真实快照引用。导航/引用失效重新 snapshot，标签页先 list 再用实际身份 switch/close。宿主拒绝模型传入 session、namespace、path、extraArgs、headed、timeoutMs 等逃逸字段，并自行设定截图文件和会话超时。[plugins/browser_use/backend/tool.py L11](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tool.py#L11)；[plugins/browser_use/backend/tool.py L69](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tool.py#L69)；[plugins/browser_use/backend/session.py L112](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/session.py#L112)

依赖与实际可用性：

- Windows x64 原生运行资产：agent-browser 0.38.2、Chrome for Testing 153.0.8010.52；开发准备命令 `pnpm prepare:browser-use`，发行版由 PyInstaller 打入 `native/browser-use`。运行时只找这套固定组件，不回退系统浏览器/npm；非 Windows 或文件缺失在首次动作时明确抛错。[plugins/browser_use/native-runtime.json L1](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/native-runtime.json#L1)；[plugins/browser_use/backend/runtime.py L15](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/runtime.py#L15)；[apps/desktop/scripts/runtime-pyinstaller.mjs L18](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/scripts/runtime-pyinstaller.mjs#L18)
- Python 依赖为 SDK >=3.1.0,<4、pydantic >=2.11.7。[plugins/browser_use/pyproject.toml L5](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/pyproject.toml#L5)
- 每角色 SHA256 profile + 每运行代随机 session，动作串行，profile 有跨进程 lease，Windows Job 持有进程树，取消/超时终止运行代而保留 profile 站点状态。上游截图保存到插件数据目录，每运行代覆盖最新一张，直至清理插件数据；不要把这一保存策略当成 Meido 的当前行为。[plugins/browser_use/backend/session.py L20](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/session.py#L20)；[plugins/browser_use/README.md L9](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/README.md#L9)
- 许可事实：仓库为 MIT；agent-browser 清单标 Apache-2.0；Chrome 清单标 Google Chrome Terms of Service/ABOUT/chrome://credits。不能将整套浏览器资产称为 MIT。[LICENSE L1](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/LICENSE#L1)；[plugins/browser_use/native-runtime.json L2](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/native-runtime.json#L2)
- 对 Meido 的建议：无需再移植 28 个工具。两个固定基线的 tools.json 经 JSON 解析比对，28 个名称和每项 schema 完全相等；Meido 已有按角色授权/隔离和 Windows 资源链。优先核查工具清单可见性、角色配置保存、固定原生资源安装/验收；保持 Meido 自有受限图像临时处理策略。此处对 Meido 的代码差异以主报告固定 main 核验为准。

### 3.4 Computer Use：8 个独立桌面工具

真实注册名是 `computer_` + tools.json 原生名称，和 Browser Use、桌宠、屏幕感知各自独立；结果图像送当前聊天模型，没有另启 Agent 或切换视觉模型。[plugins/computer_use/backend/tool.py L39](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/tool.py#L39)；[plugins/computer_use/README.md L3](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/README.md#L3)

“身份四元组”＝`pid` + `window_id` + `snapshot_id` + `observation_id`，都取实际 list/get 结果：

| 完整注册名 | 操作/参数 | 固定源码 |
|---|---|---|
| `computer_list_windows` | on_screen_only、pid；无必填 | [list_windows L3](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/tools.json#L3) |
| `computer_get_window_state` | pid、window_id 必填；query、max_depth、max_elements、max_dimension | [get_window_state L21](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/tools.json#L21) |
| `computer_click` | 身份四元组必填；element_token（可附匹配的 element_index）或 x/y；button、count、modifier、delivery_mode | [click L62](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/tools.json#L62)；[token 执行校验](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/targets.py#L57-L58) |
| `computer_type_text` | 身份四元组、text 必填；控件/坐标、delay_ms、delivery_mode | [type_text L141](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/tools.json#L141) |
| `computer_press_key` | 身份四元组、key 必填；控件/坐标、modifiers、delivery_mode | [press_key L209](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/tools.json#L209) |
| `computer_hotkey` | 身份四元组、keys 必填；控件/坐标、delivery_mode | [hotkey L278](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/tools.json#L278) |
| `computer_scroll` | 身份四元组、direction 必填；x/y、amount、by、delivery_mode | [scroll L344](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/tools.json#L344) |
| `computer_release` | 无参数；释放当前回合控制权 | [release L418](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/tools.json#L418) |

典型闭环：`computer_list_windows({})` → `computer_get_window_state({"pid":实际PID,"window_id":实际HWND})` → `computer_click({四元组,"element_token":真实token})` → 再 get 验证。输入/按键/滚动同理；组合键必须显式 `delivery_mode="foreground"`。窗口移动、DPI 变化、进程/窗口重建、重新观察均会令旧身份失效，不自动改用其他窗口。[plugins/computer_use/README.md L15](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/README.md#L15)；[plugins/computer_use/backend/targets.py L32](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/targets.py#L32)

运行要求和限制：

- 固定 Cua Driver 0.28.2 Windows x64，两个原生文件 `cua-driver.exe`、`cua-driver-uia.exe`；`pnpm prepare:computer-use`，打包到 `native/computer-use`。必须两文件齐全，无全局程序回退；首次调用时非 Win x64/缺文件明确抛错。来源、归档和逐文件 SHA256、MIT 许可记录在 native-runtime.json。[plugins/computer_use/native-runtime.json L1](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/native-runtime.json#L1)；[plugins/computer_use/backend/runtime.py L12](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/runtime.py#L12)；[apps/desktop/scripts/runtime-pyinstaller.mjs L19](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/scripts/runtime-pyinstaller.mjs#L19)
- SDK >=3.1.0,<4、pydantic、Pillow、psutil；直接 `mcp --direct`，禁遥测/自动更新，子进程 HOME/配置/临时目录在插件私有数据目录。只杀自己驱动进程树，不杀被操作应用。[plugins/computer_use/pyproject.toml L5](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/pyproject.toml#L5)；[plugins/computer_use/backend/runtime.py L25](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/runtime.py#L25)
- 只操作完整位于主显示器且未最小化的窗口；跨屏/其他显示器拒绝。坐标来自当前窗口截图，不是屏幕绝对坐标。固定驱动后台组合键效果不可靠，在输入前拒绝而不静默转前台；`effect="unverifiable"` 不等于成功。[plugins/computer_use/README.md L21](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/README.md#L21)
- 全桌面跨角色/宿主回合/进程独占，模型思考期间仍占用。宿主 ToolTurn 资源边界、取消、超时、release、停用负责终止动作并释放；已停止回合禁止重新开启。[plugins/computer_use/backend/desktop.py L43](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/desktop.py#L43)；[plugins/computer_use/backend/lease.py L7](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/lease.py#L7)
- 对 Meido 的可移植性（推断）：高价值但中高成本。可以复用固定 schema、MCP wrapper、窗口身份和生命周期设计；不是复制 tool.py 即可。需要 Meido 明确角色桌面授权、ToolTurn 非伪造身份、跨进程独占、Windows Job/取消清理、临时图像回传、原生资源校验/发行分发、失效引用及 DPI 实机验收。已有 Browser Use 的 MCP/进程基础可复用。无需 Electron 才能执行桌面动作；Python 宿主 + 原生 Driver 能工作，Electron 只承担上层产品 UI。

### 3.5 Screen Perception：observe_screen，按需只读摘要

真实工具只有 `observe_screen({})`；无模型可控参数，宿主注入角色身份。注册 always_on/read-only，external_allowed 默认为 false。尽管插件名“24h”，现实现没有全天持续采集；说明明确留待 #292。[plugins/screen_perception/backend/tool.py L24](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/tool.py#L24)；[plugins/screen_perception/backend/plugin.py L19](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/plugin.py#L19)；[plugins/screen_perception/README.md L7](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/README.md#L7)

数据流：Pillow `ImageGrab.grab(all_screens=False)` 捕获主屏 → 内存 PNG/base64 → 激活当前角色 `vision` 模型 → 使用禁止动作的专用中性 prompt、`tools=[]`、不保存 payload snapshot → 校验观察 JSON → 当前角色仅收到 `available/interface_summary/activity_key`。没有把屏幕原图直接交给主聊天角色；工具返回不包含控件坐标/原图。停用取消在途分析；缺角色身份明确报错；视觉模型返回工具调用/动作 JSON 也拒绝。[plugins/screen_perception/backend/capture.py L17](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/capture.py#L17)；[plugins/screen_perception/backend/model.py L41](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/model.py#L41)；[plugins/screen_perception/backend/model.py L67](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/model.py#L67)；[plugins/screen_perception/backend/tool.py L45](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/tool.py#L45)；[plugins/screen_perception/backend/tool.py L78](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/tool.py#L78)

依赖仅 SDK + Pillow，独立于桌宠和 Computer Use，也不依赖 Electron 截屏桥。静态代码没有同 Browser/Computer 的明确 Windows 限制；不能据此保证所有桌面平台/无头服务器都可截屏。[plugins/screen_perception/pyproject.toml L5](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/pyproject.toml#L5)；[plugins/screen_perception/backend/capture.py L14](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/capture.py#L14)

移植建议（推断）：中等成本，可优先做独立按需“看一眼屏幕”；需要后端运行在用户桌面会话、可用截屏权限、角色级视觉模型路由、图像不持久化及可取消请求。若 Meido 没有独立 vision 连接，可选把受限图像送当前支持视觉的聊天模型，但那属于另一种产品行为，不能声称与 Shiori 相同。

### 3.6 Desktop Pet：pet_action，一个工具并不等于完整桌宠

真实工具 `pet_action` 支持：
```json
{"action":"move","target":"bottom_right","animation":"run"}
{"action":"play","name":"当前包真实声明的动作名"}
```
`move` 只允许 top_left/top_right/center/bottom_left/bottom_right；animation 仅空、idle、run。`play.name` enum 动态取角色绑定的桌宠包动作。schema 会标示当前渠道、角色桌宠启用和包绑定状态。[plugins/desktop_pet/backend/tool.py L25](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/tool.py#L25)；[plugins/desktop_pet/backend/tool.py L82](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/tool.py#L82)；[plugins/desktop_pet/backend/tool.py L172](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/tool.py#L172)

工具 always_on/external-side-effect，但执行限 desktop channel + 当前角色/会话；未启用、未绑包、非法动作、3 秒冷却、同回合第二动作、没有前台桥均返回结构化 `accepted:false` 和原因。成功仅代表 RPC 被接受，不等于动画实际播放完毕。[plugins/desktop_pet/backend/plugin.py L47](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/plugin.py#L47)；[plugins/desktop_pet/backend/tool.py L100](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/tool.py#L100)；[plugins/desktop_pet/backend/tool.py L131](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/tool.py#L131)

完整功能依赖角色扩展存储/桌宠包导入 + backend RPC + background controller + Surface renderer + Electron 透明无框置顶窗口。backend 发 `action`，background 操作 host surface；Electron 窗口由宿主管理。桌宠插件还包含语音偏好与 Bilibili 直播等子系统，这些并未另注册模型工具，不能算“pet_action 自带所有能力”。Python 包声明 SDK、httpx、Pillow、pydantic、qrcode、websockets；大部分来自完整插件的语音/直播，不是最小 move/play 必需项。[plugins/desktop_pet/backend/plugin.py L21](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/plugin.py#L21)；[plugins/desktop_pet/background/index.ts L23](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/background/index.ts#L23)；[plugins/desktop_pet/background/controller.ts L248](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/background/controller.ts#L248)；[apps/desktop/src/surface/window.ts L14](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/src/surface/window.ts#L14)；[apps/desktop/src/surface/contract.ts L38](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/src/surface/contract.ts#L38)；[plugins/desktop_pet/pyproject.toml L5](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/pyproject.toml#L5)

移植建议（推断）：动作协议本身低成本，但若 Meido 尚无原生透明窗口/角色包/背景桥，完整桌宠是高成本独立功能。适合先定义精简动作与事件协议，再按 Meido 宿主实现窗口，不直接引入整套 Bilibili/语音引擎。

### 3.7 NovelAI：generate_image，一个外部可调用生图工具

工具 `generate_image` 必填 prompt、mode；mode＝txt2img/img2img；prompt 和 negative_prompt 要英文 tags，禁止中文/自然语言句子。可选 base_image_path、strength/noise、size_preset（square/landscape/portrait/custom）、custom_width/custom_height、steps、seed、sampler、model、intent（user_requested/scene_cg）、scene_key。scene_cg 必须 scene_key。[plugins/novelai/backend/tool.py L13](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/tool.py#L13)

示例：
```json
{"prompt":"1girl, maid, silver hair, blue eyes, garden","mode":"txt2img","size_preset":"portrait","intent":"user_requested"}
```
返回生成结果路径/模型/seed 等结构化结果，tool hook 把生成媒体挂到后续消息；同一媒体已被 message_push 消费时避免重复挂载。自动场景 CG、提示词标签库、生成历史、重生成、Studio UI 都是服务/RPC/事件，不是额外模型工具。[plugins/novelai/backend/tool.py L150](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/tool.py#L150)；[plugins/novelai/backend/plugin.py L41](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/plugin.py#L41)；[plugins/novelai/backend/plugin.py L196](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/plugin.py#L196)

默认停用；启用后 always_on，唯一显式允许外部发起者生图的插件工具之一。需要 NovelAI token、HTTP 服务、媒体存储，默认 base_url=https://image.novelai.net，默认模型 nai-diffusion-4-5-curated，默认单图 1MP/28 步/1 张。空 token 或未解析 ${ENV} 在请求前报 readiness 错误。[plugins/novelai/manifest.yaml L6](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/manifest.yaml#L6)；[plugins/novelai/backend/plugin.py L135](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/plugin.py#L135)；[plugins/novelai/backend/config.py L17](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/config.py#L17)；[plugins/novelai/backend/failures.py L36](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/failures.py#L36)；[plugins/novelai/backend/service.py L314](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/service.py#L314)

移植建议（推断）：仅手动生图中等成本；需要 provider 凭据、额度/参数上限、输入媒体归属校验、生成文件存储和前端展示/回传。自动 CG 高成本依赖场景观察与后台生命周期，可作为后续增量。代码 MIT 不意味着 NovelAI 服务/模型/生成资产适用同许可；本次未核验服务条款，不作结论。

### 3.8 Herdr：5 个本机 Coding Agent 工具

工具表：
| 名称 | 参数/结果 |
|---|---|
| herdr_start_task | repository（本机 Git 根绝对路径）、requirement（完整需求）必填；agent 可选 codex/claude；立即返回 task_id/status/agent |
| herdr_list_tasks | 无参数；限当前角色 |
| herdr_get_task | task_id 必填；刷新状态/近期终端/交付 |
| herdr_continue_task | task_id、requirement 必填；同 agent/worktree 开新回合，执行中拒绝并发 |
| herdr_cancel_task | task_id 必填；先 cancelling，Ctrl+C 后确认就绪才 cancelled，不能确认 unknown |

名称/参数来源：[plugins/herdr/backend/tool.py L14](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/tool.py#L14)；[plugins/herdr/backend/management_tools.py L34](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/management_tools.py#L34)；[plugins/herdr/backend/plugin.py L33](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/plugin.py#L33)

外部依赖 Herdr >=0.9.4 与已安装且登录的 Codex/Claude Code，选定 session 内均需可用；插件不自动安装 CLI/登录或接管当前焦点 session。启用只注册，不启动任务。默认 executable=herdr、session=default、agent=codex、CLI 45 秒、追踪 7200 秒；config agent_args 强制 Codex 的 bypass-approvals-and-sandbox / Claude 的 skip-permissions，首版仅 Full Access。[plugins/herdr/README.md L3](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/README.md#L3)；[plugins/herdr/backend/cli.py L44](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/cli.py#L44)；[plugins/herdr/backend/config.py L10](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/config.py#L10)

任务在独立 worktree 修改、验证、提交、Draft PR，结果回到原角色/原聊天，身份失效不改投他处；有交付文件、提交、验证命令和 PR head SHA 校验，不以终端 idle/done 视为完成。不自动合并/删 worktree。任务仅当前 Shiori 进程追踪；关闭/停用停止监控，Herdr 任务可能继续，重启不恢复旧追踪记录。[plugins/herdr/README.md L10](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/README.md#L10)；[plugins/herdr/README.md L42](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/README.md#L42)；[plugins/herdr/README.md L54](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/README.md#L54)

移植建议（推断）：高成本，可复用任务生命周期/交付核验思路，但需 Meido 对后台任务、原会话投递、进程资源重载、worktree/git/PR 外部事务权限设计。上游 Full Access 默认与 Meido 授权边界不可直接等同，不应照搬绕过权限参数。首版可以复刻只读“本机 CLI 可用性+任务状态”或沙盒委托，但那是有意识的不同范围。README 还注明依赖当前批次 Unreleased background_results/process_resources/openExternal SDK 契约，应钉定或改写适配层。[plugins/herdr/README.md L62](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/README.md#L62)

### 3.9 QQ：read_attachment，容易漏掉的第 45 个插件工具

`read_attachment` 支持 path 必填、offset 默认0、limit默认50（1..400），或 character_offset（每页最多2000字符，不与offset/limit混用）。只允许当前 QQ 消息及引用消息的 txt/md 附件；2MiB 文件上限、约10KB普通输出上限。宿主提供不可由模型伪造的 ToolAttachmentScope；不是通用本地文件读取。[plugins/qq/backend/read_attachment.py L21](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/qq/backend/read_attachment.py#L21)

例：`read_attachment({"path":"本回合附件列出的实际路径","offset":0,"limit":50})`。无范围/非QQ/超范围路径/错误扩展/不合参数返回明确错误；普通行页委托核心 read_file，核心读取器未注册时返回不可用；超长行自动字符分页。read-only、external_allowed=true、非always_on。[plugins/qq/backend/read_attachment.py L75](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/qq/backend/read_attachment.py#L75)；[plugins/qq/backend/read_attachment.py L110](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/qq/backend/read_attachment.py#L110)；[plugins/qq/backend/plugin.py L26](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/qq/backend/plugin.py#L26)

迁移判断：Meido main 已有同名受控附件工具，且使用附件 ID 而非路径，优先复用；应在现有 channel/attachment 边界上完善体验。QQ 多账号、NapCat 安装、群操作属于整个渠道插件/宿主通用账号工具，不是 read_attachment 自带的新模型工具。



### 3.10 完整名称速查

下表对应上述 76 个普通对话静态候选，来源和参数证据见前面的逐项表。

| 分组 | 完整工具名 |
| --- | --- |
| 发现 | `tool_search` |
| 文件 | `read_file`、`list_dir`、`write_file`、`edit_file` |
| 网络 | `web_search`、`web_fetch` |
| 进程 | `shell`、`task_output`、`task_stop` |
| 记忆和证据 | `memorize`、`recall_memory`、`forget_memory`、`search_messages`、`fetch_messages` |
| 推送和账号 | `message_push`、`account_list`、`account_targets`、`account_send` |
| 子任务 | `spawn`、`spawn_manage` |
| 定时 | `schedule`、`list_schedules`、`cancel_schedule` |
| 群 | `set_group_listening`、`lookup_member`、`lookup_group_context`、`update_group_context` |
| MCP 管理 | `mcp_add`、`mcp_list`、`mcp_remove` |
| 浏览器观察 | `agent_browser_read`、`agent_browser_snapshot`、`agent_browser_screenshot`、`agent_browser_get_text`、`agent_browser_get_url`、`agent_browser_get_title` |
| 浏览器交互 | `agent_browser_open`、`agent_browser_click`、`agent_browser_fill`、`agent_browser_type`、`agent_browser_press`、`agent_browser_check`、`agent_browser_uncheck`、`agent_browser_select`、`agent_browser_scroll`、`agent_browser_eval` |
| 浏览器等待和导航 | `agent_browser_wait_ms`、`agent_browser_wait_for_selector`、`agent_browser_wait_for_text`、`agent_browser_wait_for_load`、`agent_browser_close`、`agent_browser_back`、`agent_browser_forward`、`agent_browser_reload`、`agent_browser_tab_new`、`agent_browser_tab_list`、`agent_browser_tab_switch`、`agent_browser_tab_close` |
| 桌面控制 | `computer_list_windows`、`computer_get_window_state`、`computer_click`、`computer_type_text`、`computer_press_key`、`computer_hotkey`、`computer_scroll`、`computer_release` |
| Coding 委托 | `herdr_start_task`、`herdr_list_tasks`、`herdr_get_task`、`herdr_continue_task`、`herdr_cancel_task` |
| 其他插件 | `observe_screen`、`pet_action`、`generate_image`、`read_attachment` |

普通对话之外，主动决策另有 `get_alert_events`、`get_content_events`、`get_context_data`、`get_content`、`get_recent_chat`、`mark_interesting`、`mark_not_interesting`、`finish_turn`；Drift 另有 `mount_server`、`finish_drift`。这些工具连同若干同名不同协议工具，由各自主动 pipeline 装配，见 §2.5。[主动专用 schema](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/tools.py#L76-L293)、[Drift 工具](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/drift_tools.py#L153-L306)。

## 4. Skill 和 MCP 是怎样被使用的

### 4.1 Skill 是指令和资源，不会自动变成 function tool

**已提取：** `SkillsLoader` 合并 `workspace/skills` 与内置目录，同名时 workspace 优先；检查 `requires.bins` 与 `requires.env`；初始注入名称、描述、路径、可用状态组成的 XML 摘要。模型通常先 `read_file` 读取 `SKILL.md`；`always=true` 或用户 `$skill-name` 提及的技能正文会直接进入上下文。正文加载本身不会注册新工具。[发现与依赖](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/skills.py#L13-L100)、[摘要和 always](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/skills.py#L175-L266)、[显式提及](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/passive_support.py#L23-L36)、[正文注入](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/prompt_block.py#L576-L597)。

**已提取：** 此快照实际有八个内置 Skill：`weather`、`summarize`、`feed-manage`、`meme-manage`、`skill-creater`、`create-drift-skill`、`codex-delegate`、`create-proactive-source`。其中 README 索引仅列七个，漏了 `create-proactive-source`，以实际文件为准。[技能目录](https://github.com/YinFengWindy/Shiori-Agent/tree/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills)、[索引](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/README.md#L11-L43)、[主动源技能](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/create-proactive-source/SKILL.md#L1-L23)。

`codex-delegate` 也不是专用 Codex API 工具：它要求检查本机 `codex` CLI，先 `spawn` 后台子任务，再由子任务用 `shell(auto_promote=false)` 同步执行 `codex exec`，读取结果和 session id 回灌。要复刻，需要先具备受控进程和后台任务运行时。[委托流程](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/codex-delegate/SKILL.md#L1-L99)。

| 技能 | 做法/前提 | 源码 |
|---|---|---|
| weather | shell 调 curl → wttr.in/Open-Meteo；声明 requires curl；无独立 weather schema | [apps/backend/skills/weather/SKILL.md L1](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/weather/SKILL.md#L1) |
| summarize | shell 调 summarize CLI；URL/本地文件/YouTube；依赖其 CLI 和所选模型 API key，可选 Firecrawl/Apify | [apps/backend/skills/summarize/SKILL.md L1](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/summarize/SKILL.md#L1) |
| codex-delegate | 先 shell 检测 codex，再 spawn(background) 中 shell 阻塞 codex exec，读 result/session 文件；已有工具组合，区别于 Herdr 插件 | [apps/backend/skills/codex-delegate/SKILL.md L1](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/codex-delegate/SKILL.md#L1) |
| feed-manage | 调 `mcp_feed__feed_manage` 和 `mcp_feed__feed_query` 的 list/subscribe/unsubscribe/latest/summary/search/catalog；依赖另配 feed MCP，不是仓库内置 Tool 类 | [apps/backend/skills/feed-manage/SKILL.md L15](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/feed-manage/SKILL.md#L15) |
| meme-manage | read_file/list_dir + 写工作区 memes/<category> 和 manifest；是文件工作流 | [apps/backend/skills/meme-manage/SKILL.md L15](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/meme-manage/SKILL.md#L15) |
| skill-creater | 创建 SKILL.md、脚本、frontmatter metadata；技能维护工作流 | [apps/backend/skills/skill-creater/SKILL.md L8](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/skill-creater/SKILL.md#L8) |
| create-drift-skill | 在当前角色 roles/<role-id>/drift/skills 写长期小任务技能；依赖 drift 体系 | [apps/backend/skills/create-drift-skill/SKILL.md L1](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/create-drift-skill/SKILL.md#L1) |
| create-proactive-source | 编写 MCP server 和 proactive_sources.json；alert/content/context 协议，ACK 持久化；是开发工作流，不会凭空提供数据源 | [apps/backend/skills/create-proactive-source/SKILL.md L1](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/create-proactive-source/SKILL.md#L1) |


### 4.2 MCP 是动态工具适配器

**已提取：** 内置管理工具为 `mcp_add`、`mcp_list`、`mcp_remove`，本地 `mcp_servers.json` 持久化 server 的 `command/env/cwd`。MCP 实现是 stdio 子进程和 JSON-RPC；连接后调用 `initialize → notifications/initialized → tools/list`，支持列表分页；工具名包装成 `mcp_<server>__<tool>`。连接超时 8 秒，请求默认 30 秒；运行时解码文本、图像和 structured content。[管理装配](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/bootstrap/toolsets/mcp.py#L14-L31)、[注册与持久化](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/mcp/registry.py#L17-L163)、[连接和分页](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/mcp/client.py#L19-L138)、[命名包装](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/mcp/tool.py#L10-L34)。

**建议：** 借鉴命名空间、分页、请求关联、断连回收和多模态结果；不要直接开放模型任意 `mcp_add(command=...)`，因为这等同于增开本地进程入口。Meido 应先由用户配置 server，验证信任和角色授权，再把远端工具适配到现有能力快照。HTTP transport、OAuth、远端 server 权限和安装市场并没有被这个 stdio 实现解决。

## 5. Meido 当前能力与迁移优先级

### 5.1 比较基线

以下只比较 Meido 已合入 `main@bdb4ea1d472e4205aad6e89a60f66f4266efb7ab` 的代码。工作区另有未解决的合并状态，本次不把本地分支和进行中的 Issue 当成已发布能力。源码存在表示具备实现证据；当前角色是否授权、依赖是否就绪、端到端是否验收，仍须分别验证。

| Shiori 能力 | Meido main 的源码现状 | 迁移判断 |
| --- | --- | --- |
| `shell` | 已有同名工具；按角色开关、白名单、固定 workspace、最小环境、超时和输出预算执行 | 复用；不改成 Shiori 的任意 shell |
| `recall_memory` | 已有角色范围主动记忆工具；受全局记忆和角色开关控制 | 复用；补可见性和配置体验 |
| `memorize/forget_memory` | 有 MemoryEngine 与记忆维护能力，但没有这两个模型可调用工具 | 适配既有引擎，保持角色与来源边界 |
| 浏览器 28 工具 | 已有 `browser_use` 插件、同源工具 JSON、角色授权和独立持久 profile | 核验运行依赖和配置，避免再做一套 |
| 待办/提醒 | 已有 `todo.write`、`todo.reminder_control`，还支持声明式 `todo.search` | 保留产品语义；不直接替换为通用 schedule |
| 账号发送与群聊 | 已有 `account_list/targets/send/message_push`、群上下文/旁听/成员查询 | 代码已存在，优先复用和补实况；Shiori `lookup_member` 在 Meido 名为 `lookup_group_member` |
| `read_attachment` | 已有 QQ 当前回合附件 ID allowlist，限定角色和 session，支持 TXT/MD 分段 | 保留现有受控接口；不要换成任意本地路径 |
| 邮件 | 已有 `mail.manage` 和邮件服务；工具声明 `approval="prompt"`，运行装配未传 `approved_tools` | 不能据此认定模型已获准调用或已经验收；单独补审批链 |
| Skill/Plugin/Hook | 已有发现、正文激活、内容 hash、支持文件校验、grant、生命周期和不可变 CapabilitySnapshot | 借鉴 Shiori 用法；不替换宿主框架 |
| 工具搜索 | 已有 `deferred` 策略语义；没有 `tool_search` 工具和运行中 schema 扩展协议 | 需要设计与冻结快照兼容的激活方式 |
| MCP | 浏览器有专用 stdio MCP client；没有通用 server 管理和动态工具目录 | 复用进程与协议经验，另建受控 adapter |
| 文件/网络/消息历史 | 未见通用模型工具 `read/list/write/edit_file`、`web_search/fetch`、`fetch/search_messages` | 适合分步补齐 |
| `schedule`、`spawn`、后台 Shell | 无通用调度和独立 TaskRuntime 对应工具 | 后续独立规格；不能塞入一次聊天 Runtime |
| 桌面感知/控制、桌宠、生图、附件语音转写 | 未见对应通用模型插件 | 需求成立后按独立插件引入 |

表格的 Meido 固定提交证据：[角色工具候选与开关](https://github.com/sawaki-zexi/meido/blob/bdb4ea1d472e4205aad6e89a60f66f4266efb7ab/backend/app/main.py#L3341-L3415)、[能力快照装配](https://github.com/sawaki-zexi/meido/blob/bdb4ea1d472e4205aad6e89a60f66f4266efb7ab/backend/app/main.py#L3432-L3489)、[Shell](https://github.com/sawaki-zexi/meido/blob/bdb4ea1d472e4205aad6e89a60f66f4266efb7ab/backend/app/agent_runtime/shell_tool.py#L16-L80)、[附件](https://github.com/sawaki-zexi/meido/blob/bdb4ea1d472e4205aad6e89a60f66f4266efb7ab/backend/app/channels/attachment_tool.py#L10-L53)、[邮件](https://github.com/sawaki-zexi/meido/blob/bdb4ea1d472e4205aad6e89a60f66f4266efb7ab/backend/app/mail/agent.py#L10-L41)、[审批与 deferred](https://github.com/sawaki-zexi/meido/blob/bdb4ea1d472e4205aad6e89a60f66f4266efb7ab/backend/app/agent_runtime/capabilities.py#L291-L344)、[Skill](https://github.com/sawaki-zexi/meido/blob/bdb4ea1d472e4205aad6e89a60f66f4266efb7ab/backend/app/agent_runtime/skills.py#L27-L90)、[浏览器专用 MCP](https://github.com/sawaki-zexi/meido/blob/bdb4ea1d472e4205aad6e89a60f66f4266efb7ab/backend/app/browser_use/mcp.py#L16-L58)。

**已提取：前端角色保存确有 Shell 配置保留缺口。** `draftFromRole` 和 `payloadFromDraft` 未携带 `shell`；后端 `AgentToolConfig.shell` 默认关闭、白名单为空。普通资料编辑可能因此覆盖原有 Shell 配置。这个问题要先修，否则增加更多工具只会放大“实现存在但角色看不见”的困惑。[前端 draft](https://github.com/sawaki-zexi/meido/blob/bdb4ea1d472e4205aad6e89a60f66f4266efb7ab/web/src/features/role-management/draft.ts#L3-L31)、[后端默认值](https://github.com/sawaki-zexi/meido/blob/bdb4ea1d472e4205aad6e89a60f66f4266efb7ab/backend/app/models.py#L16-L48)。

### 5.2 分期建议

| 阶段 | 用户可验证的结果 | 建议实现范围与理由 |
| --- | --- | --- |
| P0 | 在角色详情看到真实工具、是否传给模型、为何不可用；编辑角色不再丢失已有授权 | 从 CapabilitySnapshot 和插件运行状态派生清单，显示 `implemented/enabled/granted/visible/healthy` 各自原因；补 Shell 配置入口和保留逻辑；依赖检测要覆盖真实二进制和启动错误 |
| P1 | 角色能读取获准文件、查网页、追溯自己的消息原文，并按主人指令维护记忆 | 先 `read_file/list_dir`、`web_search/web_fetch`、`fetch_messages/search_messages`；再按 MemoryEngine profile 补显式写入/失效工具。输出必须有路径/URL/消息 ID 来源 |
| P2 | 角色能在角色目录维护文件，并创建可管理的通用时间任务 | `write_file/edit_file` 做范围授权、原子写入、文本唯一匹配和错误反馈；通用 schedule 与主人待办保持不同概念，定义时区、重复、持久化、取消与补发规则 |
| P3 | 角色可把长任务交给独立工作单元，关闭/重启后有明确结果和回收 | 先 TaskRuntime，再适配 `spawn/spawn_manage`、后台 Shell、Codex delegate；明确子任务工具子集、并发、预算、结果回灌和取消 |
| 按需插件 | 角色能使用选定的外部 MCP、观察/操作桌面、生图或转写 | 每项独立授予权限、固定依赖、检查健康；不为凑工具数一次接入全部原生与外部服务 |

**建议：先做 P0 和 P1。** 基础工具单个实现并不复杂，难点是给模型传对 schema、让用户能看见权限和失败、避免角色配置保存时悄悄变化。`tool_search` 可先用于冻结候选集合内的延迟暴露；允许运行中注册新工具会改变当前快照约定，应另作已确认设计。

**已提取：** Meido ADR-0003 已把 Coding/TaskRuntime 留在独立边界，ADR-0004 明确 Shell 不允许模型指定 shell、cwd、环境或 stdin。因此复制 Shiori 的完整 shell 和 spawn 生命周期会改变已采纳决定，需要先批准新取舍。迁移应围绕现有 SQLite、MemoryEngine、ToolDefinition、ToolContext 和 CapabilitySnapshot 适配行为。[ADR-0003](../adr/0003-tau-inspired-agent-runtime-boundary.md)、[ADR-0004](../adr/0004-role-scoped-shell-tool-boundary.md)、[AI 包边界 ADR-0007](../adr/0007-ai-core-package-boundary.md)。

### 5.3 与已有工作协调

截至 2026-10-15 的 GitHub 只读快照中，相关渠道和邮件 Issue 仍开放，[开放 PR 列表](https://github.com/sawaki-zexi/meido/pulls)为空。源码已进入 main 和 Issue 尚未验收关闭可以同时成立；后续任务应先核对这些票的最终验收状态，复用已有实现。

| 现有工作 | 与本次建议的关系 |
| --- | --- |
| [#116 多端会话与渠道规格](https://github.com/sawaki-zexi/meido/issues/116)，[#117 Telegram](https://github.com/sawaki-zexi/meido/issues/117)、[#118 QQ NapCat](https://github.com/sawaki-zexi/meido/issues/118)、[#119 QQBot](https://github.com/sawaki-zexi/meido/issues/119)、[#120 飞书](https://github.com/sawaki-zexi/meido/issues/120) | 复用账号、目标目录、群环境和旁听边界，不另建一套 Shiori 渠道宿主 |
| [#121 通过可达目标目录主动投递](https://github.com/sawaki-zexi/meido/issues/121) | 原生依赖 #117–#120；账号发现和发送验收应与该票协调 |
| [#97 在对话中管理邮件与草稿](https://github.com/sawaki-zexi/meido/issues/97) | `mail.manage` 可见性与审批属于现有邮件工作范围 |
| [#126 主人资料启用与同步错误诊断](https://github.com/sawaki-zexi/meido/issues/126) | 可借鉴其状态表达，保持工具健康诊断与现有产品入口一致 |

## 6. 迁移验收方向

**建议：** 先按外部行为验收能力面板和基础工具，再考虑扩大工具数量。

1. 同一角色看到的工具清单与实际传给模型的 schema 一致；隐藏工具有可解释原因，加载失败显示真实依赖错误。
2. 更改角色配置后再次保存，Shell、浏览器、记忆和插件许可不会丢失；重新启动后配置仍一致。
3. 已启用工具由模型真实调用一次，结果进入后续模型请求；禁止的工具在伪造 tool_call、pre hook 改参、后台子任务中仍被拦截。
4. 工具异常、超时、取消、宿主拒绝和成功可分别观察，UI 与模型不会把错误文本记成成功。
5. 文件和消息读取遵守角色、根目录、附件与上下文边界；新角色和角色删除不会继承其他角色的权限或数据。
6. 延迟发现不扩大原始授权集合；同一 run 的目录、schema 和执行策略使用一致的能力版本。
7. 后台任务完成、取消、应用关闭和重载都有资源回收；外部推送有确定目标和去重，不能把“已接受”当作“已送达”。

这些是后续 Ticket 的验收方向，并非本次研究已经运行通过的测试。

## 7. 局限、许可证与后续证据

### 7.1 核验方法与验证结果

- 固定 Shiori 和 Meido main 的提交，从源码提取工具类、实际 `name`、注册点与 JSON 目录，互相交叉检查；包装类和动态占位不重复计数。
- 独立 AST/JSON 静态清单核对得到核心 31 + 插件 45 = 76 个普通对话候选；主动与 Drift 专用协议单列。
- 还排除两项辅助模型强制结构化输出契约：`submit_scene_observation` 和 `submit_scene_image_prompt`。它们分别提交场景判断与生图提示词，不是普通角色可发现、可自由调用的用户工具。[场景契约](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/core/scene/contracts.py#L12)、[生图提示词契约](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/novelai/backend/scene_prompt.py#L11)。
- 对两个固定基线的 Browser `tools.json` 做 JSON 结构比较：两边都是 28 项，工具名集合相同，每一项 schema 完全相等；本次没有运行真实浏览器。[Shiori 目录](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json#L1)、[Meido 目录](https://github.com/sawaki-zexi/meido/blob/bdb4ea1d472e4205aad6e89a60f66f4266efb7ab/backend/app/browser_use/tools.json#L1)。
- 阅读现有测试作为行为契约证据，未执行上游测试，也未把测试文件存在写成运行通过。

以下为静态阅读的具体测试契约：

- 文件核心、分页截断、图片、CRLF/BOM、replace_all、权限拒绝、同文件锁：[test_filesystem.py:29](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/test_filesystem.py#L29)，锁并发测试:224。
- 搜索payload与SSE解析：[test_web_search.py:11](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/test_web_search.py#L11)；fetch渲染/错误/二进制/超时/内网字面值：[test_web_fetch.py:46](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/test_web_fetch.py#L46)。未发现这些测试证明DNS重绑定和逐跳SSRF安全。
- Shell配置cwd、hook、流输出、尾部截断/完整log、取消杀进程、后台ID/轮询/stop/TTL/自动提升/硬超时：[test_tools.py:61](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/shell/test_tools.py#L61)；Windows真实pwsh前后台测试:1214与stop:1250，但需运行环境有pwsh才可执行。
- message_push新目标边界：[bootstrap/test_tools.py:155](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/bootstrap/test_tools.py#L155)明确私聊和旧绑定群都不授权external push；[test_message_push_role_target.py:11](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/test_message_push_role_target.py#L11)拒错角色、支持渠道描述和可操作错误。
- account工具隐藏account_id/图片无文本/user目标身份绑定与会话记录：[test_account_delivery.py:30](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/test_account_delivery.py#L30)、:173、:211。平台RPC由fake/mock提供，本次未实际外发。
- 消息原文/预览/证据/分页：[test_message_lookup.py:71](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/test_message_lookup.py#L71)；群不能读用户context:505，参数不能扩scope:540，未知context拒role历史:558，绑定边界:589。
- schedule验证、默认上海、保存role执行身份、request_time补偿、间隔/cron、列表取消：[test_schedule.py:25](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/test_schedule.py#L25)，未据此证明最新message_push限制后的外部定时端到端可用。
- spawn上下文/并发3/同步跳过后台限制/取消：[test_spawn.py:21](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/test_spawn.py#L21)；有completion-flow测试但未运行。
- 群笔记跨渠道候选/当前角色/分页/拒私聊其他角色：[test_group_context.py:62](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/test_group_context.py#L62)；写入略字段保留/空串清除/非法目标/身份伪造：[test_group_context_update.py:49](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/test_group_context_update.py#L49)；旁听只有主人+群里只能当前群：[test_group_listening.py:52](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/test_group_listening.py#L52)；成员旧昵称和跨渠道规则：[test_member_lookup.py:18](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/tests/backend/agent/tools/test_member_lookup.py#L18)。

### 7.2 许可证和运行限制

**已提取：** Shiori 主仓库采用 MIT，包含 `Copyright (c) 2026 YinFengWindy`；复制软件或实质部分时要保留版权与许可声明。浏览器、桌面原生 runtime、模型权重、语音组件及外部服务各有自己的许可证和使用约束，主仓库 MIT 不能代替逐项核验。[LICENSE](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/LICENSE#L1-L21)。

本研究是固定提交的静态源码核验：没有连接用户 Shiori 实例，没有取得其角色配置和插件健康状态，没有启动上游工具、访问账号、发送消息或调用真实模型。代码存在、单元测试存在、模型可见和本机可执行是四种不同证据；本文不会把它们互相替代。

后续落地需要按 Meido 规范先确定规格和依赖，再从最新 `main` 创建单 Issue 分支；架构取舍另记 ADR。本报告不替代已确认 Spec，也不改变现有授权边界。
