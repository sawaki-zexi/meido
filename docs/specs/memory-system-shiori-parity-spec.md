# Meido 记忆系统 Shiori 对齐规格

状态：已确认目标，待实现

目标：以 Shiori-Agent 当前实现为唯一行为基线，复刻其记忆契约、默认引擎、结构化存储、检索、回合后处理、Markdown maintenance 和管理语义；仅删除群聊及多人上下文能力。

事实来源：

- Meido 记忆引擎决策：[`ADR-0002`](../adr/0002-shiori-memory-engine-boundary.md)
- Shiori-Agent `origin/main`：`096a4ecbfbcdae3ba77c179fedb852721846fcb9`（2026-10-03）
- Shiori 调研：[`shiori-agent-memory.md`](../research/shiori-agent-memory.md)
- Optimizer / consolidation 深度调研：[`shiori-memory-optimizer-deep-dive.md`](../research/shiori-memory-optimizer-deep-dive.md)
- Meido 第一版规格：[`memory-system-spec.md`](memory-system-spec.md)

本文是实现和验收依据。除本文“Meido 宿主适配”与“明确排除”两节外，不新增 Meido 自有记忆语义。

## 1. 对齐边界

### 1.1 必须复刻的 Shiori 行为

- SDK 风格的 `MemoryEngine` 契约及 query、mutation、ingest、admin 数据类型。
- 回复前的 `context` 检索、显式 `recall_memory` 和记忆上下文注入。
- 回合提交事件驱动的语义 post-response worker。
- Markdown maintenance、近期上下文、consolidation、快照/草稿/条件提交和 `ConsolidationCommitted` 事件。
- 默认引擎的关键词、向量、RRF 融合、五类 query intent、类型/时间/作用域过滤和注入预算。
- 显式 `memorize`、`forget_memory`、`recall_memory` 工具语义。
- SQLite 结构化记忆、embedding、来源证据、幂等、reinforcement、merge 和 supersede。
- `memory_replacements` schema 与 undo 读取语义；Shiori 当前基线有写入 helper，但默认生产路径未调用它，因此不要求该表在普通 supersede 后必然有记录。
- 定时 Memory Optimizer：`PENDING.md` 快照归档到 `MEMORY.md`，并按版本规则更新 `SELF.md`。
- 默认引擎管理接口的查询、状态/来源/时间/扩展字段更新、删除、批量删除、相似查询和角色记忆失效。
- 按 Shiori 各自机制处理维护错误、事件消费和进程重启；不能把进程内事件队列写成持久任务队列。

### 1.2 Meido 宿主适配

- 调用方仍是现有 FastAPI、角色和唯一持续会话；不复制 Shiori 插件宿主、RPC、桌面插件市场和完整 Agent 生命周期。
- `MemoryScope` 只保留 Meido 所需的 `role_id`、`session_key`；不保留 `channel`、`chat_id` 等群聊字段。
- `role_id` 是 Meido 的硬隔离键，参与所有查询、去重、reinforcement、supersede、删除和事件消费。Shiori 把 role/scope 放在 `extra_json` 且唯一索引未包含 role；Meido 必须避免该跨角色隔离风险。
- 角色记忆文档仍位于 `roles/<role_id>/memory/`。

### 1.3 明确不属于目标

- 群聊、频道、旁听和旁听批次。
- 多成员 `ContextScope`、群环境摘要、成员档案和群聊可见性分层。
- 跨角色共享记忆、跨设备同步、云端记忆服务和知识图谱。
- Meido 自定义的摘要/类型编辑产品能力；管理编辑以 Shiori 字段契约为准。
- `memory_jobs` 作为 Shiori parity 数据表或公共契约。Meido 可为宿主恢复实现内部账本，但不得让它改变事件语义或验收模型。

## 2. 目标架构

```text
对话生命周期
    │
    ├─ BeforeTurn：构造 MemoryQuery，读取 Markdown，检索 SQLite，注入上下文
    ├─ TurnCommitted：助手回复成功保存后发布回合快照
    │      ├─ 语义 post-response worker
    │      └─ Markdown maintenance
    │              └─ 达到整理条件时发布 ConsolidationCommitted
    └─ 定时 Memory Optimizer
           ├─ PENDING.md snapshot -> MEMORY.md
           └─ SELF.md 规则化更新
    │
    ▼
MemoryEngine 契约
    ├─ DefaultMemoryEngine.query
    ├─ DefaultMemoryEngine.mutate / ingest
    ├─ DefaultMemoryEngine.admin
    └─ Memory lifecycle / event consumers
            │
            ▼
Semantic store
    ├─ memory_items
    ├─ embeddings / lexical index
    ├─ consolidation_events
    └─ memory_replacements（Shiori schema/helper；默认写入路径当前未接通）
```

两条 `TurnCommitted` 后台链路分别启动和报告失败，不互相等待，也不因一条失败回滚已完成的对话或另一条链路。恢复方式按各自机制处理：语义 `TurnIngested` 使用进程内队列，不承诺崩溃后重放；Markdown maintenance 保存未完成消费者状态供后续维护恢复。`ConsolidationCommitted` 只在 Markdown 窗口成功提交后发布，由默认引擎单独消费。

## 3. 关键模块与文件职责

目标代码边界应与下列职责一致；文件名可按 Meido 目录约定实现，但调用方不得绕过契约直接访问 SQLite 或 Markdown。

| 模块 | Shiori 对齐职责 |
| --- | --- |
| `memory/engine.py`、`build.py` | `MemoryEngine`、能力描述、依赖构造和资源所有权 |
| `memory/context.py`、`committed.py`、`events.py` | scope、回合提交快照、`TurnCommitted`/`ConsolidationCommitted` 事件 |
| `memory/default_engine/query.py` | intent 策略、过滤、混合检索和注入规划 |
| `memory/default_engine/mutation.py`、`policy.py` | memorize、forget、纠正、状态变更和写入策略 |
| `memory/default_engine/admin.py` | 管理查询、字段更新、删除、失效和相似查询 |
| `memory/semantic/embedder.py`、`retriever.py`、`memorizer.py` | embedding、关键词/向量召回、强化、合并和 supersede |
| `memory/semantic/post_response_worker.py` | 回合提交后的显式保护、纠正处理和隐式提取 |
| `memory/semantic/store/` | SQLite、向量索引、来源和事件持久化；包含 Shiori 当前尚未接入默认写路径的 replacement schema/helper |
| `memory/markdown/maintenance.py`、`consolidation.py` | 近期上下文、窗口整理、快照/草稿/条件提交和 journal |
| `memory/markdown/user_layer.py` | `SELF.md`、`MEMORY.md`、`HISTORY.md`、`PENDING.md` 等角色用户层文档 |
| `proactive/memory_optimizer.py`、`bootstrap/proactive.py` | 定时 Optimizer、逾期补跑、PENDING 两阶段快照、MEMORY/SELF 更新 |

Meido 现有 `MemoryService`、`MemoryStore`、`MemoryMaintenance` 和 `MemoryOptimizer` 只能作为迁移 facade；完成迁移后，对话入口只依赖 `MemoryEngine` 和事件，不依赖旧门面或文档格式。

## 4. 作用域、文档和数据权威

### 4.1 作用域

单次请求必须携带：

```text
MemoryScope
  role_id
  session_key
```

缺失或不匹配的 `role_id` 必须拒绝。Meido 不实现 Shiori 的 `channel`、`chat_id`、成员和群环境 scope。

### 4.2 Markdown 用户层

```text
roles/<role_id>/memory/
├─ SELF.md              角色对自身、主人和关系的认识
├─ MEMORY.md            可读的稳定长期记忆视图
├─ RECENT_CONTEXT.md    近期上下文视图
├─ HISTORY.md           consolidation 产生的历史事件
├─ PENDING.md           consolidation 产生、等待 Optimizer 归并的候选缓冲（不直接注入固定 prompt）
└─ journal/             按日期保存的整理记录
```

Markdown 是可读的用户层和 maintenance 输出，不是结构化记忆的权威数据库。`HISTORY.md`、`PENDING.md` 和近期上下文由 maintenance 写入；`MEMORY.md`、`SELF.md` 由 Optimizer 低频更新并在回复前全文读取。

### 4.3 SQLite 权威数据

`memory_items` 是结构化记忆的权威来源，至少表达：

```text
id, memory_type, summary, content_hash, extra_json,
source_ref, happened_at, status, created_at, updated_at,
reinforcement, emotional_weight, embedding
```

Shiori 当前把 role/scope 放在 `extra_json`，SQLite item 没有独立 `role_id` 列；Meido 为硬隔离应增加明确的 `role_id` 列或等价受约束键。`role_id + memory_type + content_hash` 是 Meido 的精确去重范围。`memory_domain` 若保留，标记为 Meido 的单角色适配字段，不宣称是 Shiori 原样字段。

目标表：

| 表 | 职责 | 来源 |
| --- | --- | --- |
| `memory_items` | 当前结构化记忆、状态、embedding 和来源 | Shiori |
| `consolidation_events` | 已提交的 Markdown 整理事件及消费幂等 | Shiori |
| `memory_replacements` | 为 replacement/undo 保留旧新条目快照；当前没有默认生产写入调用 | Shiori schema/helper |
| `memory_jobs` | 回合任务账本 | Meido 宿主内部可选实现，不是 parity 契约 |

## 5. MemoryEngine 契约

必须提供与 Shiori 等价的稳定类型：

- `MemoryScope`：`role_id`、`session_key`。
- `MemoryQuery`：文本、intent、`stateful/read_only` effect、scope、filters、上下文、数量和时间约束。
- `MemoryQueryFilters`：memory type、domain、时间范围和引擎提示。
- `MemoryQueryResult`：结构化记录、注入文本、trace 和原始候选。
- `MemoryRecord`：ID、类型、摘要、分数、signals、evidence、source 和注入状态。
- `EvidenceRef`：消息、回合或 consolidation 来源的可解析引用。
- `MemoryIngestRequest` / `MemoryIngestResult`：带来源类型、scope、hints 和 metadata 的摄入契约及接受结果。
- `MemoryMutation` / `MemoryMutationResult`：remember、forget 和状态变更结果。
- `MemoryEngine`：`query`、`mutate`、`ingest`、`reinforce_items_batch` 和 admin 操作。
- `MemoryEngineDescriptor` / `MemoryToolProfile`：引擎名称、profile、能力、说明及工具 schema/risk 描述。

工具只构造 query/mutation，不直接访问 store。未启用引擎时，对话仍可生成回复。

## 6. 回复前流程

```text
用户消息
  │
  ├─ 读取 SELF.md / MEMORY.md / RECENT_CONTEXT.md
  ├─ MemoryQuery(intent="context", 当前 role scope)
  ├─ lexical lane + vector lane
  ├─ scope/status/type/time 过滤
  ├─ RRF 融合、类型配额、阈值和字符预算
  └─ MemoryQueryResult + trace
           │
           ▼
       模型生成回复
```

`context` 不使用 answer 专用 HyDE。embedding 不可用时保留关键词路径；检索或向量失败不能阻塞正常回复。RRF 分数只用于排序，注入阈值使用各召回 lane 的原始信号和类型策略，不能把二者混为一个分数。

## 7. 回复后两条独立链路

### 7.1 TurnCommitted 和语义 post-response

只有助手消息成功保存为 `completed`，才发布包含用户消息、助手回复、工具结果和作用域的 `TurnCommitted`。worker 顺序固定为：

1. 保护本轮显式 `memorize` 已写入的 item IDs。
2. 处理用户纠正、否定、forget 和旧条目失效。
3. 在 token/时间预算内提取隐式 profile、preference、procedure、event 或 fact。
4. 以 active 记忆作为去重上下文，执行 content hash、reinforcement、merge 或 supersede。
5. 在同一 mutation 语义下写入 `memory_items` 和 embedding；replacement 关系按 Shiori 当前生产路径的实际调用情况处理，不假设 helper 已被接线。

相同 `stable_source_key` 重复提交必须幂等。失败只记录 post-response 阶段状态，不能撤销已完成回复。

### 7.2 Markdown maintenance 和 consolidation

`TurnCommitted` 同时独立触发 Markdown maintenance：

1. 读取已完成消息和 maintenance cursor。
2. 每个成功回合可更新 `RECENT_CONTEXT.md`。
3. 未达到窗口条件时只提交近期上下文，不生成历史窗口，不推进 consolidation cursor。
4. 达到 Shiori 配置的消息窗口或输入 token 压力条件时，创建 snapshot 和 draft。
5. 从窗口生成 `HISTORY.md`、`PENDING.md` 和日期 journal。
6. 提交前验证消息前缀、消息 ID 和 cursor 未变化；变化则丢弃 draft 且不推进 cursor。
7. 文档提交成功后发布 `ConsolidationCommitted`，事件消费者再把整理事件写入结构化记忆。

空结果、非法结构、超时和文档写入失败均不推进 cursor；保留可重试输入。稳定 source key 使重复窗口不会重复追加。

Meido 宿主通过 `MemoryMaintenance` 的 consolidation provider seam 接入窗口整理模型；在宿主未配置该 provider 时，使用本地确定性候选提取作为降级路径。provider 返回空结果或非法结构时只保留近期上下文、保留原 cursor，下一次维护可重试；provider 超时或抛错直接结束本次维护，同样不推进 cursor。该 seam 保持 Shiori 的 snapshot/draft/conditional commit 和失败语义，待宿主模型调用能力接入后无需改变事件契约。

触发口径固定为：

- 被动助手回合完成并保存后产生 `TurnCommitted`；主动消息等有各自处理规则，本规格删除群聊触发类型，但保留 Meido 自身实际存在的非普通回合来源处理。失败、中断和未提交回复不触发被动回合链路。
- 每个成功回合都会调用一次 maintenance 调度器。调度器至少读取 cursor 并检查近期文档；没有新的已完成消息时可以短路，不调用 LLM。
- `RECENT_CONTEXT.md` 的刷新是每轮维护中的独立步骤；只有存在新的已完成消息且快照仍有效时才提交更新。
- consolidation 不是每轮必做。普通 maintenance 的默认窗口判断为 `ready = message_count - keep_count - last_consolidated`，且 `ready >= max(5, keep_count // 2)`；另有输入预算/历史压缩路径，在消息即将从模型历史移除时调用 `ensure_memory_for_window` 补齐该窗口所需的整理，不应误解为每次输入 token 压力都直接触发普通 consolidation。未达到条件时只维护近期上下文。
- consolidation 成功提交后推进 cursor 并发布一个 `ConsolidationCommitted`；失败、输入变化或条件未满足都不推进 cursor。
- `PENDING.md` 不在每轮 consolidation 后立即归并；由独立定时 Optimizer 按周期或启动补跑条件消费。

当前 FastAPI 入口在构造模型输入前发现角色会话超过两倍近期保留窗口时调用 `ensure_memory_for_window`；该调用是 best effort，维护失败只记录错误，不阻塞回复。现有宿主尚未执行 token 级裁剪，因此该入口以消息窗口作为裁剪前保障信号。

### 7.3 Memory Optimizer

Optimizer 是 Shiori 的独立后台生命周期，与 `TurnCommitted`、语义 post-response 和 `ConsolidationCommitted` 解耦：它不负责写入 `memory_items`，也不等待结构化事件消费者完成；它负责维护注入模型上下文所需的稳定 Markdown 长期层。

默认行为：

1. 当 `memory_optimizer_enabled=true` 且模型注册可用时，应用启动注册 `MemoryOptimizerLoop`；默认启用，默认间隔为 `64800` 秒（18 小时），循环实际间隔下限为 60 秒。
2. 启动时先扫描角色：没有优化时间、时间戳非法、优化时间已超过间隔，或 `SELF.md` 规则版本落后时，立即补跑一次。之后每个定时 tick 遍历角色执行 optimize；空记忆且不需 SELF 重写的角色会短路。
3. 每次按角色串行执行；Optimizer 进程级互斥，同一时刻只处理一个角色。
4. 原子取得 `PENDING.md` 快照；快照期间新增的候选继续写入新的 `PENDING.md`，不与本次归档混淆。
5. 读取快照和现有 `MEMORY.md`，调用独立整理提示词，执行保留、合并、冲突替换和去重，并写入新的 `MEMORY.md`。
6. 合并返回非空结果并写入成功后提交快照；provider 异常会被 Shiori 转成空结果并回滚快照。若进程在 snapshot 后、显式 commit/rollback 前因文件写入等异常退出，残留 snapshot 在启动或下一次快照时恢复，候选不得丢失。
7. 随后按固定规则更新 `SELF.md`；规则版本变化时允许一次完整重写，平时只更新约定章节。尚未生成首版 SELF 的角色会将规则版本标记为当前版本，跳过这次全量重写。`SELF.md` 更新失败不回滚已经成功的 `MEMORY.md` 合并。
8. 若 `MEMORY.md` 与 `PENDING.md` 都为空且无需 SELF 重写，直接跳过本轮；否则流程末尾记录角色的 `last_memory_optimized_at`。Shiori 当前实现即使 MEMORY 或 SELF provider 调用失败，也可能在 SELF 步骤后写入该时间戳，详见下方基线细节。

Optimizer 的主要触发条件是时间而不是 `PENDING.md` 数量：默认每 18 小时整点运行一次；启动补跑只对逾期角色执行。其设计目的，是把高频 consolidation 产生的候选与低频全文注入文档分离，减少 `MEMORY.md` 每轮变化导致的 prompt cache 失效。

基线实现的失败细节必须单独记录：Shiori 当前 `_merge_memory()` 将 provider 异常转换为空结果，随后回滚 PENDING snapshot，但流程仍可能继续 SELF 更新并写入 `last_memory_optimized_at`。因此“候选不丢失”和“失败必然在下次启动立即补跑”是两个不同保证；Meido 若选择增强为失败必重试，必须在实现和验收中标明这是宿主安全增强，而不是 Shiori 原样行为。

## 8. 五类检索 intent

| intent | 固定行为 |
| --- | --- |
| `context` | 当前对话上下文检索；原始用户 query 的关键词 + 向量召回，不使用 answer HyDE。 |
| `answer` | 并行生成 event/general 两个受限 HyDE 假设；关键词 lane 保留原文，向量 lane 使用有效辅助 query。模型调用受 token 与超时上限约束，失败时退回原始查询。 |
| `timeline` | 必须提供时间范围；按 event/发生时间过滤和排序。 |
| `interest` | 默认查询 preference/profile，遵守类型配额和状态过滤。 |
| `procedure` | 默认查询 procedure/preference，使用 procedure query builder，并优先保留可执行候选。 |

所有 intent 都必须先按 `role_id`、active 状态、类型、domain 和时间过滤，再执行预算裁剪；无时间范围的 timeline 请求必须失败或明确要求补充范围。召回失败时返回可诊断的空结果，不阻塞普通回复。

## 9. 显式工具与管理接口

必须提供：

```text
memorize       -> MemoryMutation
forget_memory  -> MemoryMutation
recall_memory  -> MemoryQuery
```

管理端按 Shiori `MemoryAdminApi` 支持：

- 按 role、状态、类型、domain、来源和 embedding 状态分页查询/查看；记忆事件另提供时间范围查询。
- 更新状态、扩展字段、来源、发生时间和情绪权重。
- 单条删除、批量删除和角色记忆失效。
- 查找相似条目。
- 查看 source/evidence。

管理更新字段严格限于 `status`、`extra_json`、`source_ref`、`happened_at`、`emotional_weight`；摘要和类型编辑不属于该 SDK 管理契约。只有实际改变摘要或类型的写入才需重算 `content_hash` 和 embedding；仅修改来源、状态或情绪等元数据时不得无依据地作废向量。`memory_replacements` 留作内部 schema/undo 兼容，公开管理协议不要求列表查询。

## 10. 失败、幂等和恢复

- 回复前检索、embedding、post-response 和 Markdown maintenance 失败不能撤销已完成回复。
- Optimizer 的 `MEMORY.md` 合并、`SELF.md` 更新和 PENDING snapshot 提交分别处理；合并失败保留/恢复候选，SELF 失败不回滚已提交的 MEMORY，未处理 snapshot 由文件级恢复逻辑找回。
- Shiori 的事件观察队列是进程内队列；`TurnIngested` 不因此获得崩溃后持久重放保证。不得把这一基线描述成通用持久 job queue。
- Markdown consolidation 会持久化未完成消费者输入/发布状态并可在后续维护重试；SQLite 写入依靠 `source_ref`/事件幂等避免重复。
- Optimizer 用 PENDING snapshot 文件恢复候选；其失败时间戳行为按第 7.3 节记录，不承诺失败后必定立即补跑。
- Meido 可增设持久任务账本作为宿主增强，但必须标明为适配能力，并保持 source/event 幂等。
- Markdown 使用 snapshot/draft/conditional commit 和原子写入；输入变化时 cursor 不推进。
- SQLite 写入使用事务或等价一致性边界；source key 和事件 ID 幂等。若 Meido 接通 replacement writer，该新增行为必须单独定义事务和幂等语义。
- 角色删除前停止该角色事件接收并清理其 Markdown、`memory_items`、embedding、事件和 replacement 数据；其他角色不受影响。
- 私人记忆只传给当前角色允许的模型调用，不写入普通日志。

## 11. 迁移要求

1. 保留现有结构化记忆的 ID、来源、状态、时间和 reinforcement。
2. 补充 `role_id` 隔离、domain、emotional weight、统一 `EvidenceRef` 和 content hash。
3. 创建/迁移 `consolidation_events`；不把 `memory_jobs` 冒充为 Shiori 事件表。`memory_replacements` 按 Shiori 现状保留 schema/undo 兼容，但默认生产写入是否接通作为独立实现选择，不伪称 Shiori 已有调用路径。
4. 为现有 Markdown 增加受控 managed block、source marker 和维护 cursor，不覆盖用户保留内容。
5. 保留 Optimizer 的 `last_memory_optimized_at`、`self_rules_version` 和 `PENDING.snapshot` 恢复语义；已有 pending 候选不得因迁移丢失。
6. 重建缺失 embedding 和索引；embedding 维度变化时按版本重建，不能混用。
7. 迁移失败可重试、可恢复且不删除原始数据；稳定 source key 防止重复写入。
8. 旧 `MemoryService`/`MemoryStore` facade 仅在迁移期间保留，最终由统一 `MemoryEngine` 接管调用。

## 12. 验收标准

### 契约与隔离

- [ ] 所有查询、写入、工具和管理操作都经过 `MemoryEngine`。
- [ ] 缺失或错误 `role_id` 被拒绝；不同角色相同内容不会合并、强化或互相 supersede。
- [ ] query intent、scope、record、evidence、trace 和 mutation 结果结构稳定。

### 回复前

- [ ] `context`、`answer`、`timeline`、`interest`、`procedure` 的过滤和 query builder 符合第 8 节。
- [ ] lexical/vector/hybrid 路径可分别验证；embedding 不可用时关键词仍可回复。
- [ ] 注入只包含 active、当前角色、预算内的条目，并包含 evidence/trace。

### 回复后与事件

- [ ] 未完成或失败的助手回复不发布 `TurnCommitted`。
- [ ] 语义 post-response 与 Markdown maintenance 独立触发、独立失败，并分别遵循进程内队列和持久 maintenance 状态的恢复边界。
- [ ] 显式记忆优先，纠正可 supersede 旧条目，隐式提取受预算限制。
- [ ] 相同 source/event 重放不重复写入；`ConsolidationCommitted` 仅在文档条件提交成功后发布。`TurnIngested` 的崩溃恢复能力与内存事件队列边界明确。

### Markdown 与管理

- [ ] 每个成功回合可更新 `RECENT_CONTEXT.md`；未达窗口时不创建 consolidation 窗口。
- [ ] 达到条件后幂等生成 `HISTORY.md`、`PENDING.md`、journal，并可被结构化事件消费者处理。
- [ ] 消息或 cursor 变化使 draft 失效且不推进 cursor。
- [ ] 管理端提供 Shiori SDK 规定的状态、来源、时间、扩展字段、删除、相似和角色失效操作；不额外要求公开 replacement 列表 API。

### Optimizer

- [ ] 默认 18 小时周期和启动逾期补跑可配置、可观察，且每角色串行执行。
- [ ] `PENDING.md` 快照期间产生的新候选不会丢失；成功提交清除旧快照，失败恢复旧快照。
- [ ] Optimizer 更新 `MEMORY.md` 和 `SELF.md` 的边界符合 Shiori 规则，不改变角色设定。
- [ ] Optimizer 的空合并/provider 错误会回滚 pending 快照且不丢候选；文件操作中断留下的 snapshot 可恢复。SELF 更新失败不会回滚已提交的 MEMORY。启动重跑遵循 Shiori 时间戳语义，不额外声称任何 provider 错误都会立即重试。

### 排除项

- [ ] 不创建群聊、旁听、成员档案、群环境或多人 `ContextScope` 数据路径。
- [ ] 不要求群聊相关的 listening、member profile 或 group environment 才能运行 Optimizer。

## 13. 实施顺序

1. 固定本规格的 Shiori 基线和排除范围。
2. 建立 `MemoryEngine`、scope、query/mutation、committed 和 event 契约。
3. 拆分现有服务为 default engine、semantic store 和 Markdown runtime。
4. 建立 `memory_items`、`consolidation_events`、`memory_replacements` schema/迁移和事务边界，并记录 replacement writer 是否属于 Meido 增强。
5. 实现五类 intent、hybrid retrieval、注入规划和 explicit tools。
6. 接入 `TurnCommitted` 的 post-response 与 Markdown maintenance 两条独立链路。
7. 接入 `ConsolidationCommitted` 消费、Optimizer 定时/启动补跑、管理接口和各自的失败恢复。
8. 执行数据迁移，保留兼容 facade，移除群聊路径。
9. 完成契约、存储、检索、事件、Markdown、Optimizer、管理和重启恢复测试。

## 14. 完成定义

只有以下条件全部满足才算完成：

- 目标契约、默认引擎、semantic store、Markdown runtime 和事件边界已落地。
- 回复前、post-response、maintenance 和 consolidation 行为符合本规格及 Shiori 基线。
- 所有验收场景通过，且角色隔离、幂等、失败恢复和迁移可验证。
- 不存在群聊/多人路径或将 `memory_jobs` 当作 parity 契约的实现；Optimizer 已按 Shiori 生命周期落地。
- Python 测试、类型检查、前端构建、文档链接检查和两轮 code review 通过。
