# 待办与提醒规格

状态：当前

最后核验：2026-10-09

实现状态：Issue #89 首版已在 `feat/89-todo-plugin` 功能分支实现，尚未合入 `main`。实现包括应用级 `TodoPlugin`、SQLite 待办仓储、仅保存来源引用的成功回合 outbox、异步整理、可诊断和可重试的日期/精确提醒 inbox、待办 API、聊天抽屉和只读 `todo.search` capability；相关行为测试位于 `tests/python/test_todo_plugin.py` 和 `web/src/App.test.tsx`。外部通知渠道、循环事项、动态插件安装仍不在范围内。

事实来源：

- [`CONTEXT.md`](../../CONTEXT.md)
- [`agent-runtime-tau-integration-spec.md`](agent-runtime-tau-integration-spec.md)
- [`agent-capability-extension-spec.md`](agent-capability-extension-spec.md)
- [`backend/app/agent_runtime/runtime.py`](../../backend/app/agent_runtime/runtime.py)
- [`backend/app/agent_runtime/plugins.py`](../../backend/app/agent_runtime/plugins.py)
- [`backend/app/agent_runtime/capabilities.py`](../../backend/app/agent_runtime/capabilities.py)
- [`backend/app/session_store.py`](../../backend/app/session_store.py)

本文定义待办功能的用户行为，以及它作为应用级插件接入 Agent Runtime 的方式。待办是主人级运营数据，不属于角色记忆、主人资料或角色理解。

## 1. 目标

主人可以在任一角色的对话中自然表达要做的事情。Meido 在对话成功完成后异步识别明确的行动意图，创建或更新待办；主人也可以从聊天页的待办抽屉直接管理事项。对话需要时，角色可以读取少量相关待办；到期时，Meido 在应用内呈现可诊断、可恢复的提醒。

待办作为一个可启停的静态、进程内 `TodoPlugin` 提供。它负责待办数据、回合整理、查询上下文、提醒调度和待办管理界面接入。关闭插件后，不整理对话、不注入待办上下文，也不产生新提醒。提醒还可以单独暂停：暂停期间仍可查看和编辑待办，既有提醒记录保留，不创建或投递新的提醒；恢复后继续检查已到期事项。

这里的“插件”指产品功能模块，不是把全部行为实现成模型 Tool。Agent Runtime 的 Plugin/Tool/Skill 是 TodoPlugin 的运行期适配方式；待办存储与调度不依赖模型是否调用工具。

## 2. 用户流程

```text
聊天页
  ├─ 正常与角色对话
  ├─ 成功回合提交后，TodoPlugin 异步整理用户消息
  ├─ 回复生成前，TodoPlugin 按需提供相关待办上下文
  ├─ 到期时呈现独立提醒事件
  └─ 打开待办抽屉查看和管理主人级待办
```

待办抽屉是聊天页的辅助界面，不新增首期主页面。提醒以独立 UI 事件呈现，不伪装成 assistant 消息，不进入普通会话历史。

## 3. 范围

包含：

- 对成功提交的用户消息识别明确的创建、修改、延期、完成、取消意图；
- 为明确但无日期的事项创建收件箱记录；
- 在抽屉查看、编辑、完成、取消和恢复待办；
- 保存标题、说明、状态、截止日期、可选具体提醒时刻和来源；
- 为当前问题提供少量相关待办上下文；
- 产生有持久状态、支持重试且不会重复送达的应用内提醒；
- 记录操作来源和诊断状态，支持定位来源消息。

首期不包含：

- 普通生活分享自动创建待办；
- 循环待办、子任务、多人协作、跨设备同步；
- 邮件、系统通知、聊天平台等外部渠道；
- Meido 关闭期间准时运行的系统级常驻进程；
- 由模型 Tool 直接执行待办写操作；
- 动态插件安装、远程插件或操作系统沙箱。

## 4. 待办识别

整理器只处理已成功提交回合中的用户消息，不根据 assistant 回复创建事项。识别目标是行动意图，而不是日期词：

- “提醒我周五交实验报告”是明确请求；
- “记一下月底续费”是明确请求；
- “过几天我要去看电影”是分享，不创建待办；
- “我下周可能要搬家”是可能性，不创建待办。

抽取器返回经过 schema 校验的操作候选；TodoPlugin 决定是否应用并负责幂等。候选操作包括 `create`、`update`、`complete`、`cancel`、`restore`，可带标题、说明、目标待办引用和时间字段。

- 没有日期的明确事项进入 `inbox`，不为了普通待办打断对话。
- 只有日期时保存本地 `due_date`，不编造 `due_at` 或某个钟点的 `reminder_at`。
- “提醒我周五”表示日期级提醒，在主人本地日期进入当天后可触发；它不是一个虚构的精确时刻。明确给出钟点时才设置精确 `reminder_at`。
- 无法可靠解释相对日期时保留为待安排事项，或至多询问一次。
- 同一来源消息的重试不重复执行；相似表达优先强化/更新已有事项。
- 可能匹配多个待办的修改不得静默选错；记录为待确认操作，或请求主人澄清一次。

## 5. 插件与宿主接入

当前 Agent Plugin 由一次运行的能力解析创建，并随该次运行结束清理；其 Tool Hook 也围绕工具调用。因此它不能独自托管长期运行的 Worker 或 Scheduler。

TodoPlugin 使用两个生命周期适配面，但仍是一个待办功能插件：

```text
Application host
  └─ TodoPlugin.start / close
       ├─ TodoStore
       ├─ TodoWorker      <- 已提交的 TurnCommitted
       ├─ TodoScheduler   <- 到期检查与提醒投递
       ├─ Todo HTTP routes
       └─ Todo UI entry

Agent Runtime run
  └─ TodoPlugin capability adapter
       ├─ TodoContextProvider
       └─ optional read-only todo.search Tool
```

应用宿主在启动时静态装配 TodoPlugin，负责调用其 `start`/`close` 并挂载管理路由。运行期适配器通过 `PluginContext` 获取宿主提供的 Todo 只读端口；不得直接取得 TodoStore、SessionStore 或 SQLite connection。首期不建设通用动态应用插件平台。

成功回合事件必须在 run 与最终 assistant 持久化为 completed 后发布。事件至少包含稳定 `event_id`、`run_id`、`role_id`、`session_key`、用户消息 ID 和 assistant 消息 ID。事件分发由宿主负责，不能把 `MemoryWorker` 当作待办事件总线；记忆和待办是独立消费者，彼此失败互不影响。

事件必须可恢复：成功回合提交和待办事件 outbox 记录应处在同一个持久化提交边界。TodoPlugin 可独立重试消费；在“待办已应用但 outbox 尚未确认”的崩溃窗口内，按 `event_id` 幂等去重。

## 6. 数据和归属

待办属于唯一主人，不属于创建它的角色。角色/会话/消息只是来源证据；不同角色可以协助管理同一清单。

领域数据模型：

```text
TodoItem
  id, title, description
  status: inbox | scheduled | completed | cancelled
  due_date                 # 主人本地日历日期，可空
  due_at                   # UTC 精确截止时刻，可空
  reminder_at              # UTC 精确提醒时刻，可空
  reminder_date            # 主人本地日期级提醒，可空
  timezone                 # IANA 时区
  source_role_id, source_session_key, source_message_id
  created_at, updated_at, completed_at, cancelled_at

TodoOperation
  id, todo_id, operation, source_message_id, event_id
  created_at, result: applied | ignored | needs_review | failed
  error

TodoReminderEvent
  id, todo_id, due_key, status: pending | delivered | failed
  due_at, attempts, last_error, created_at, updated_at, delivered_at
```

TodoPlugin 拥有待办表和仓储；它不读写 MemoryEngine 的表或记忆文档。待办可单独使用 SQLite 数据库。跨库可靠性由会话/Runtime 数据库中的 `TurnCommitted` outbox 和 TodoPlugin 侧的 `event_id` 唯一键保证，而不是试图用跨库事务。

删除角色时保留主人级待办。删除角色的会话消息随角色删除；待办保留来源 ID 供诊断，但来源链接显示不可用。该规则不改变现有角色删除流程中的会话、记忆清理契约。

删除角色前会先消费其成功回合 outbox；仍有待办整理失败或插件停用导致的未消费事件时，删除会暂时返回冲突，避免丢失已提交回合的明确待办意图。outbox 只保存消息引用，整理器从仍存在的来源消息读取用户文本。

主人时区以 IANA 标识保存为应用设置；前端可检测初始时区并允许主人修改。解释相对日期时使用当前设置，创建后每条待办也保存时区快照。绝对时刻存 UTC，日期型字段不转成 UTC 日期。

## 7. 提醒

TodoScheduler 只处理待办提醒，不解析语言、不修改待办语义，也不调用记忆模块。它在应用运行期间轮询到期记录，先以稳定 `due_key` 创建/认领提醒事件，再投递到应用内提醒 inbox。

- 日期级提醒在该时区的本地日期开始后进入待投递状态；精确提醒按 `reminder_at` 到期。
- `enabled` 与 `remindersEnabled` 是两个独立设置：前者控制整个待办插件，后者只控制提醒调度和未读提醒刷新。
- `remindersEnabled=false` 时不创建或投递新的提醒；已存在的 `pending`、`delivered` 和 `failed` 记录保留，待办仍可查看、编辑和由对话整理。
- 恢复 `remindersEnabled=true` 后，调度器继续检查有效的到期事项；暂停期间到期的事项按正常规则进入提醒流程，并可标记为延迟提醒。
- 完成或取消待办会使尚未投递的提醒失效。
- 重新安排提醒产生新的 `due_key`；同一个 `due_key` 只允许成功投递一次。
- 投递失败留下 `failed` 状态和错误，后续可重试；不能显示为已送达。
- 多条同时到期的提醒在抽屉中分别保留，可在同一视图分组，不合并丢失待办引用。
- 应用关闭时不承诺准时通知。重启后，已过期但仍有效的提醒进入待投递状态，并在应用内呈现为延迟提醒。

首期使用前端对后端提醒 inbox 的轮询刷新，不引入新的常驻连接协议。提醒不是普通 `Message`，不会进入 transcript、模型历史或 MemoryWorker。

## 8. 对话上下文和 Agent Tool

TodoContextProvider 在每次运行开始前按主人当前消息、当前本地日期、逾期状态和标题关键词查询少量候选。只注入相关事项，并明确标记为“当前待办信息”；不固定注入完整清单，也不伪装成角色记忆或主人事实。

Runtime 已有能力快照和 Plugin Tool 机制后，可为显式启用该能力的角色提供只读 `todo.search`。Tool 只能经宿主注入的 TodoReadPort 查询当前主人的数据；模型参数不能指定其他主人、跨域访问 SQLite 或绕过待办归属。

首期 Tool 仅只读。创建、修改、完成和取消由主人 UI 操作或回合后 TodoWorker 应用经过校验的用户意图，不向模型暴露直接写入 Tool。

Tool call/result 保持 Agent Runtime 内部 transcript 语义，不显示为普通聊天消息，不进入 MemoryWorker 输入。没有配置可用 Tool 的模型仍可使用预先构建的相关待办上下文。

## 9. 前端交互

聊天页提供待办抽屉：

- 按收件箱、已安排、已完成、已取消筛选；
- 创建和编辑标题、说明、截止日期与提醒；
- 完成、取消和恢复事项；
- 显示日期、状态、来源和待处理/失败诊断；
- 从事项定位仍存在的来源消息；
- 展示未读、延迟和失败提醒，并允许打开对应待办。

插件停用时隐藏待办入口，待办数据保留；重新启用后恢复使用并补做未处理提醒检查。提醒单独暂停时不隐藏待办入口，抽屉保留待办清单和历史提醒，并显示提醒已暂停状态。

## 10. 验收方向

1. 明确待办意图在回合成功后被记录，普通分享不会创建待办。
2. 无日期事项进入收件箱；日期型事项不被赋予未表达的精确钟点。
3. 修改、完成、取消、恢复与抽屉编辑在刷新后仍保持一致。
4. 同一回合事件重试不会重复创建或应用操作，崩溃恢复不会丢失已提交回合。
5. 待办是跨角色的主人级清单；删除来源角色后事项仍存在且来源不可用状态可诊断。
6. 待办 worker、查询或调度失败不影响对话提交和 MemoryWorker。
7. 相关待办可进入模型上下文，无关待办不会进入；可选只读 Tool 只能读取主人当前清单。
8. 提醒事件幂等、可重试；完成/取消后不再投递，应用关闭期间不承诺准时提醒。
9. 提醒不出现在普通聊天历史、记忆文档、结构化记忆或近期上下文中。
10. 插件停用时不会整理对话或运行调度器，重新启用后能恢复待处理状态。
11. 主人可以在前端独立暂停或恢复提醒；暂停不影响待办查看、编辑、对话整理和上下文注入，恢复后暂停期间到期的事项可以进入提醒流程。

## 11. 实现与测试边界

实现以 GitHub Issue [#89](https://github.com/sawaki-zexi/meido/issues/89) 为准，从包含 Agent Runtime 和 Agent Capability Extension 的最新 `main` 开始。不要基于旧的记忆功能分支叠加实现。

按用户可验证的纵向行为实现和验收，重点测试：

- 抽取候选的结构校验、普通闲聊负例、重复消息和歧义目标；
- outbox 重放、TodoOperation 幂等、TodoPlugin 启停与进程恢复；
- 主人级归属、跨角色来源及角色删除后的保留；
- 主人时区、日期级提醒和精确时刻提醒；
- Scheduler 的完成/取消过滤、重试、重复投递防护和延迟提醒；
- TodoContextProvider 的相关性、数量预算和来源标记；
- `todo.search` 的能力快照、只读策略和作用域限制；
- 前端待办抽屉、提醒状态和普通会话/记忆隔离。

重复事项模糊匹配、日期抽取和 LLM 输出不得直接成为数据库写入依据；所有候选都必须通过 schema、归属、状态转换和幂等检查。插件停用不删除既有待办或提醒记录。
