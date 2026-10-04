# Shiori-Agent 记忆模块深度调研：Optimizer、Consolidation 与 SQLite

状态：当前源码研究

最后核验：2026-10-04

事实来源：Shiori-Agent `main` 提交 `096a4ecbfbcdae3ba77c179fedb852721846fcb9`（远程确认时间：2026-10-04）。链接均固定到该提交，避免后续 main 变化造成结论漂移。

本文只记录当前实现行为。群聊、旁听、成员档案和群环境是 Shiori 的现有扩展；Meido 若排除这些能力，只需采用同一生命周期的单角色部分。

## 结论先行

Shiori 有两条互补的记忆流水线，以及一个独立的 Markdown Optimizer：

```text
回合提交
  └─ TurnCommitted
       ├─ 语义 post-response（异步）
       │    ├─ TurnIngested
       │    ├─ 显式 memorize 保护
       │    ├─ 纠正/失效/supersede
       │    ├─ 隐式长期记忆提取
       │    └─ 写入 SQLite memory_items
       │
       └─ Markdown maintenance（异步）
            ├─ 未达窗口：刷新 RECENT_CONTEXT.md
            └─ 达到窗口：consolidation
                 ├─ HISTORY.md / PENDING.md / journal/
                 ├─ RECENT_CONTEXT.md
                 └─ 发布 ConsolidationCommitted
                      └─ 默认语义引擎消费并写 SQLite

定时 MemoryOptimizer（独立后台循环）
  └─ 对每个角色读取 PENDING.md + MEMORY.md
       ├─ LLM 合并为新的 MEMORY.md
       ├─ 成功后清空本次 PENDING 快照
       └─ 再按规则更新 SELF.md
```

因此 Optimizer 不等于 SQLite 写入器，也不替代 consolidation。consolidation 负责把对话窗口变成事件和候选；Optimizer 负责低频地把候选归并进全文注入的长期 Markdown；SQLite 由 post-response 和 `ConsolidationCommitted` 消费者写入。

## 1. 组件边界

| 组件 | 当前源码位置 | 责任 |
| --- | --- | --- |
| SDK 契约和事件 | [`packages/sdk/python/shiori_sdk/memory/engine.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/packages/sdk/python/shiori_sdk/memory/engine.py)、[`committed.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/packages/sdk/python/shiori_sdk/memory/committed.py)、[`events.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/packages/sdk/python/shiori_sdk/memory/events.py) | 定义 `MemoryQuery`、`TurnCommitted`、`TurnIngested`、`ConsolidationCommitted` 等跨模块端口。 |
| Markdown 维护 | [`apps/backend/core/memory/markdown/maintenance.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/core/memory/markdown/maintenance.py) | 监听回合提交、排队、窗口判断、近期上下文刷新、条件提交和失败状态。 |
| Markdown consolidation | [`apps/backend/core/memory/markdown/consolidation.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/core/memory/markdown/consolidation.py) | 用 LLM 从归档窗口提取 `history_entries` 和 `pending_items`，并生成近期上下文草稿。 |
| 用户层落盘和事件发布 | [`apps/backend/core/memory/markdown/user_layer.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/core/memory/markdown/user_layer.py) | 按 `source_ref` 幂等写 Markdown；成功后发布 `ConsolidationCommitted`。 |
| Markdown store | [`apps/backend/agent/memory.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/agent/memory.py) | 读写 `SELF.md`、`MEMORY.md`、`PENDING.md`、`HISTORY.md`、`RECENT_CONTEXT.md`，并实现 PENDING 快照两阶段提交。 |
| Optimizer | [`apps/backend/proactive_v2/memory_optimizer.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/proactive_v2/memory_optimizer.py) | 低频合并 `PENDING.md → MEMORY.md`，更新 `SELF.md`，维护角色优化时间和 SELF 规则版本。 |
| Optimizer 装配/调度 | [`apps/backend/bootstrap/proactive.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/bootstrap/proactive.py)、[`config_models.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/agent/config_models.py) | 根据配置创建 Optimizer 和循环；默认间隔为 64800 秒（18 小时），最小实际间隔为 60 秒。 |
| 默认语义引擎 | [`plugins/default_memory/backend/engine/lifecycle.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/engine/lifecycle.py) | 接收 `TurnIngested` 做回复后提取，接收 `ConsolidationCommitted` 把 consolidation 事件和隐式长期记忆写入 SQLite。 |
| SQLite schema | [`plugins/default_memory/backend/semantic/store/common.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/semantic/store/common.py) | 定义 `memory_items`、`consolidation_events`、`memory_replacements`。 |

## 2. 模型回复前

回复前不是 Optimizer 的执行点。默认引擎通过 SDK 的 `MemoryQuery` 按意图检索结构化记忆；Markdown 运行时读取角色的 `SELF.md`、`MEMORY.md`、`RECENT_CONTEXT.md`。`HISTORY.md` 不作为全文固定块注入，而是 consolidation 和历史检索的材料。

默认引擎的意图分派在 [`query.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/engine/query.py)：

- `context`：上下文检索，不生成 answer HyDE。
- `answer`：并行生成受限制的 event/general 假设，再与原始查询做关键词和向量召回。
- `timeline`：必须提供起止时间，查询事件时间范围。
- `interest`：固定检索 `preference` 和 `profile`。
- `procedure`：使用 procedure query builder，并在 procedure 类型上检索。

role、channel、chat 等 scope 在存储查询中作为过滤条件；RRF/融合分只负责排序，后续注入仍使用候选原始信号和类型预算。该阶段只读结构化记忆和 Markdown，不推进 consolidation 游标，也不触发 Optimizer。

## 3. 回合提交后的两条链路

### 3.1 TurnCommitted 到语义 post-response

生命周期在 [`after_turn.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/agent/lifecycle/phases/after_turn.py) 构造并发布 `TurnCommitted`。默认引擎在 [`lifecycle.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/engine/lifecycle.py) 将它转换为 `TurnIngested` 入队；主回复不等待该异步处理。

`PostResponseMemoryWorker`（[`post_response_worker.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/semantic/post_response_worker.py)）的处理顺序是：

1. 从工具链读取本轮 `memorize` 产生的结构化 item ID，并把这些条目标记为受保护，避免自动纠正路径立刻将其失效。
2. 对用户明确否定/纠正的旧 procedure 或 preference 做候选召回和 supersede 判断。
3. 在剩余 token 预算内运行隐式长期记忆提取，使用当前 active profile、preference、procedure 作为去重上下文。
4. 由 `Memorizer` 写入 SQLite，建立 embedding、content hash、source、时间和情绪权重；重复条目强化，纠正/替代会更新条目状态。当前源码定义了 `memory_replacements` 表和 `record_replacements()` helper，但检索整个默认插件后未发现生产写入调用，不能据此声称替代关系会被实际记录。

该链路的来源是 `TurnCommitted`，而不是 Markdown consolidation；即使本轮没有达到 Markdown 窗口，也可以产生结构化长期记忆。

### 3.2 TurnCommitted 到 Markdown maintenance

`MarkdownMemoryMaintenance.on_turn_committed()` 将 session 放入每会话队列。实现位于 [`maintenance.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/core/memory/markdown/maintenance.py)。队列按 session 串行执行，并记录最近失败原因。

判断逻辑是：

```text
ready = 消息总数 - keep_count - last_consolidated
consolidation_min_new_messages = max(5, keep_count // 2)
```

若没有足够的未整理消息，维护仍可刷新 `RECENT_CONTEXT.md` 的 Recent Turns；若达到阈值，则准备 consolidation 窗口。窗口准备期间会保存消息 ID、前缀指纹、游标和上下文归属；LLM 完成后提交前再次核对。消息新增、消息前缀改变、其他流程推进游标或外部 Markdown 快照改变时，草稿被判定过期，游标不推进，下次重试。

`consolidation.py` 的用户层调用模型得到：

- `history_entries[]`：追加到 `HISTORY.md` 和日期 `journal/`，每项可带 `emotional_weight`。
- `pending_items[]`：追加到 `PENDING.md`，等待 Optimizer 低频归档。
- `RECENT_CONTEXT.md`：近期上下文压缩结果和近期回合预览。

`user_layer.py` 以 `source_ref` 对 HISTORY/PENDING/journal 写入做幂等保护。Markdown 产物成功落盘后才发布 `ConsolidationCommitted`；事件消费失败不会把已经落盘的 Markdown 回滚，消费者可以单独重试。默认引擎消费该事件时，先把 history entries 写入 `memory_items`，再对整段 conversation 做一次隐式长期记忆提取。因此该事件并非只有“写历史事件”这一种作用。

## 4. Optimizer 的准确行为

### 4.1 它解决什么问题

`MEMORY.md` 和 `SELF.md` 是回复前全文注入的稳定 Markdown 块。consolidation 每几轮都发生，如果每次都重写 `MEMORY.md`，全文 prompt 会频繁变化，降低 prompt cache 命中率。Shiori 将新候选先放入 `PENDING.md`，Optimizer 默认约 18 小时才改变一次 `MEMORY.md`，在成本和新鲜度之间取平衡。PENDING 不直接作为固定系统提示注入。

### 4.2 单次 optimize 的顺序

`MemoryOptimizer.optimize(role_id=...)` 在一个进程内锁住整个角色维护过程；缺少 role ID 或已有运行会直接失败。具体步骤如下：

1. 解析该角色的 Markdown store，并读取角色状态。
2. 检查 `self_rules_version`。角色的 SELF 规则版本落后时，本轮会额外安排一次 SELF 全量重写；若 SELF seed 状态仍为 pending，说明首版 SELF 尚未生成，则直接标记当前版本并跳过重写，避免默认模板导致 seed 状态误判。
3. 调用 `snapshot_pending()` 原子改名 `PENDING.md → PENDING.snapshot.md`。后续 consolidation 可以继续写新的 PENDING，不会污染本次快照。
4. 读取当前 `MEMORY.md` 和快照内容，用 `_MERGE_PROMPT` 要求模型输出完整的三分类长期记忆：关于你、你的偏好、你希望我记住的事。提示词要求去重、处理 correction、删除短期状态和 agent 执行规则。
5. LLM 返回非空内容时备份旧 MEMORY、写入新 MEMORY；把本次 PENDING 归档说明追加到 HISTORY；然后 `commit_pending_snapshot()` 删除快照并保证空的 PENDING 文件仍存在。
6. 合并返回空或调用失败时，回滚快照到 PENDING；已有长期记忆不被清空。
7. 等待 15 秒，调用 `_SELF_PROMPT` 更新 SELF。普通更新只在约定的三个 section 内调整；规则版本升级时进行一次性重写。SELF 更新失败不会撤销已经成功的 MEMORY 合并。
8. 除 MEMORY/PENDING 为空且无需 SELF 重写的早退外，流程末尾在角色 `memory_init_state` 写入 `last_memory_optimized_at`；SELF 重写成功后写入 `self_rules_version`。这不保证前面的模型调用均成功。

实现来源：[`MemoryOptimizer`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/proactive_v2/memory_optimizer.py)、[`MarkdownMemoryStore` 的快照方法](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/agent/memory.py)。

### 4.3 触发和恢复

`MemoryOptimizerLoop` 在启动时先扫描所有角色：没有 `last_memory_optimized_at`、时间戳非法、距离上次优化已超过 interval，或 SELF 规则版本过旧的角色会立即补跑。之后按 interval 边界运行，并在每个 tick 遍历所有角色调用 `optimize()`（不只处理启动时逾期角色）。默认 interval 是 64800 秒（18 小时），配置值在循环中至少被限制为 60 秒；`memory_optimizer_enabled=false` 或没有模型注册时不会装配循环。

Optimizer 的恢复边界是文件级而不是通用任务队列：进程启动或下一次快照前若发现 `PENDING.snapshot.md`，先将旧快照和运行中新增的 PENDING 合并回去。这样模型调用期间崩溃不会丢候选。需要注意当前实现的失败语义：`_merge_memory()` 捕获 provider 异常后返回空字符串，调用方回滚快照但仍可能继续 SELF 步骤，并在流程末尾写入 `last_memory_optimized_at`；SELF 更新失败也不会阻止这个时间戳写入，所以一次失败不保证下一次启动立即补跑。若要把“失败必重试”作为 Meido 验收条件，应额外持久化失败状态或只在对应步骤成功时更新时间戳。角色的 MEMORY 和 PENDING 都为空且无需 SELF 重写时，当前实现直接返回，也不会写优化时间戳。

### 4.4 Optimizer 与 SQLite 的关系

Optimizer 源码只依赖 `MarkdownMemoryStore` 和角色状态，不调用默认语义引擎的 `MemoryStore2`，也不发布 `ConsolidationCommitted`。因此 `MEMORY.md` 的归并不会自动把一份“最终 Markdown”同步成一条 SQLite 记录。

SQLite 的结构化写入来自两处：

1. `TurnIngested` 的 post-response worker：本轮消息和回复的隐式提取。
2. `ConsolidationCommitted` 的默认引擎消费者：Markdown consolidation 产生的 history entries 和 conversation 的隐式提取。

这形成有意的双表示：Markdown 是稳定、可读、全文注入的角色视图；SQLite 是可过滤、可向量检索、带来源和状态的结构化检索库。二者共享对话来源，但不是任意双向同步。

## 5. SQLite 结构与事件幂等

固定提交的 [`common.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/semantic/store/common.py) 创建：

- `memory_items`：`id`、`memory_type`、`summary`、`content_hash`、embedding、reinforcement、emotional_weight、extra/source、happened_at、status 和时间字段。唯一索引是 `(content_hash, memory_type)`。
- `consolidation_events`：以 `source_ref` 为主键，记录某批 consolidation 是否已经关联过 SQLite item，防止重放重复写入。
- `memory_replacements`：schema 可保存旧/新条目快照、关系类型、source 和时间；admin undo 会读取该表，但当前默认生产写入路径未调用 `record_replacements()`，所以表可能为空。

这里有一个必须单独处理的实现事实：Shiori 的唯一索引没有把 `role_id` 放进 SQL key；角色和 scope 存在扩展元数据并由查询过滤。Meido 若要求角色硬隔离，不能原样复制这个索引，应让 role ID 参与去重、强化和 supersede 的查询条件。

## 6. 回合级完整时序

```text
回复前
  读取 SELF/MEMORY/RECENT_CONTEXT
  + 按 intent 从 SQLite 检索并规划注入
        ↓
模型回复并提交
        ↓
发布 TurnCommitted
  ├─ 入队 TurnIngested → post-response → SQLite
  └─ Markdown maintenance
       ├─ 未达阈值 → Recent Turns 刷新
       └─ 达到阈值 → LLM consolidation
            → 条件提交 HISTORY/PENDING/RECENT_CONTEXT/journal
            → 发布 ConsolidationCommitted
            → 默认引擎写 history/event + 隐式长期记忆到 SQLite
        ↓
后台定时 Optimizer（独立于上述两条链）
  snapshot PENDING → LLM 合并 MEMORY → commit/rollback snapshot
  → 更新 SELF → 记录角色优化时间
```

关键边界：`TurnCommitted` 的两个消费者互不要求对方完成；Markdown 已提交后，SQLite 事件消费可以稍后重试；Optimizer 不等待 SQLite，也不把 SQLite 反向生成 Markdown。失败恢复分别由 Markdown maintenance 的游标/消费者状态、SQLite 的 `source_ref` 幂等和 Optimizer 的 PENDING snapshot 负责。

## 7. 对 Meido parity 实现的直接要求

若目标是“除群聊相关功能外一比一复刻”，Optimizer 必须作为目标组件保留，至少实现以下可验收行为：

1. consolidation 只追加 `PENDING.md`，不在每轮直接改 `MEMORY.md`。
2. Optimizer 按角色独立、低频、可配置地运行；启动时补跑过期角色。
3. Optimizer 使用 PENDING snapshot 两阶段提交，模型失败不丢候选，运行期间新增候选不被覆盖。
4. MEMORY 合并成功和 SELF 更新是两个步骤；SELF 失败不回滚已经提交的 MEMORY。
5. `TurnCommitted → TurnIngested` 与 Markdown maintenance 独立触发；`ConsolidationCommitted` 只在 Markdown 产物成功落盘后发布。
6. SQLite 仍由结构化 post-response/consolidation 消费者写入；不要把 Optimizer 当成 SQLite 同步器。
7. Meido 的 `role_id` 应加入所有去重、强化、supersede 和查询条件，作为单角色隔离的适配增强。

`memory_replacements` 的生产写入调用不属于当前 Shiori 基线；如果 Meido 决定接通该路径，应把它记录为明确的 Meido 增强，而不是 parity 已有行为。

这些是从固定提交源码抽出的行为要求，不等同于“复制 Shiori 的群聊、旁听、成员档案或插件运行时”。

## 来源清单

- [MemoryOptimizer 与 MemoryOptimizerLoop](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/proactive_v2/memory_optimizer.py)
- [MarkdownMemoryStore 与 PENDING snapshot](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/agent/memory.py)
- [Markdown maintenance 队列和窗口条件](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/core/memory/markdown/maintenance.py)
- [Consolidation 窗口和 LLM 提取](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/core/memory/markdown/consolidation.py)
- [用户层落盘及 ConsolidationCommitted 发布](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/core/memory/markdown/user_layer.py)
- [默认引擎生命周期和事件消费者](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/engine/lifecycle.py)
- [SQLite schema](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/semantic/store/common.py)
- [SDK memory events](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/packages/sdk/python/shiori_sdk/memory/events.py)
- [Shiori Markdown handbook（Optimizer 设计动机）](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/docs/_handbook/memory-markdown.md)
