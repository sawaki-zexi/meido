# Meido 与 Shiori 记忆模块源码对照

状态：当前研究记录  
最后核验：2026-10-07
Meido 基线：`a9cfb10`（Agent Runtime PR #81，包含记忆策略 PR #71/#73/#74）
Shiori 基线：`629e5ba6967a9fa2f4477b38d55e315c8c2b58d7`（`main`）

本文只记录源码核验结果，不把 Shiori 的群聊能力列为 Meido 的缺陷。Meido 当前明确采用单角色、单会话边界；Shiori 的 `channel`、`chat_id`、用户线程和旁听层是对照项，而不是本项目本轮的实现范围。

## 结论

两者已经共享同一组上层概念：回复前查询、回复后异步处理、Markdown maintenance/consolidation、PENDING 到 MEMORY 的 Optimizer、SQLite 结构化记录、向量检索、显式记忆工具和管理接口。Meido 的 #55–#62 实现确实参考了 Shiori 的端口和生命周期，但当前实现不是 Shiori 的逐文件复制，关键差异仍然存在：

1. Shiori 把 `MemoryEngine` 放在 SDK，把默认 SQLite/向量实现放进可替换的 `default_memory` 插件；Meido 把端口、默认引擎、存储和 FastAPI 生命周期放在同一个 `backend/app`。
2. Shiori 使用事件总线把 `TurnCommitted` 转成 `TurnIngested`，由 LLM post-response worker 提取隐式长期记忆；Meido 现在也支持结构化 LLM 提取，默认启用，并可通过 `MEIDO_MEMORY_IMPLICIT_EXTRACTION_ENABLED=false` 关闭，关闭时保留显式记忆操作。
3. Shiori 的 SQLite schema 是跨角色表，角色和 scope 放在 `extra_json`；Meido 给 `memory_items` 和相关事件表增加了强制 `role_id` 列与索引，因此隔离更强，但这不是 Shiori 原样 schema。
4. Shiori 的检索器有真实的 HyDE、类型阈值、热度、时间和 scope 过滤及注入配额；Meido 已接入受限 HyDE、hotness、procedure rule、RRF、sqlite-vec/SQLite 回退和注入预算，但策略与证据 trace 仍比 Shiori 简化。
5. Shiori 的 Markdown maintenance 支持 ContextScope、用户线程可见性、近期摘要 LLM 和持久消费者进度；Meido 针对一个角色会话使用 `.maintenance.json` 游标和确定性的近期消息窗口。
6. Meido 的角色删除是比 Shiori 当前默认路径更强的安全流程：阻止新写入、等待任务、暂存角色目录、清理全部角色结构化数据，并用持久 marker 在重启时恢复；Shiori 的角色删除监听器主要把该角色的结构化条目标为 `superseded`。

## 两套系统的实际架构

### Meido 当前实现

```text
FastAPI / backend.app.main
  ├─ 回复前：读取 SELF/MEMORY/RECENT_CONTEXT
  │          └─ DefaultMemoryEngine.query
  │               └─ MemoryService.recall_async
  │                    └─ MemoryStore（SQLite + sqlite-vec/SQLite embedding 回退）
  └─ 回复成功后：发布 TurnCommitted
       └─ MemoryWorker（每角色锁、进程内任务）
            ├─ MemoryService.process_turn
            │    └─ 显式记住/忘记/拒绝/纠正 + 可选 LLM/规则候选 → SQLite
            ├─ MemoryMaintenance.maintain
            │    └─ RECENT_CONTEXT / HISTORY / PENDING / journal
            │         └─ ConsolidationCommitted → SQLite consolidation_events
            └─ MemoryOptimizerLoop（低频）
                 └─ PENDING.snapshot → MEMORY/SELF
```

关键文件：

- [`backend/app/memory_engine.py`](../../backend/app/memory_engine.py)：Meido 自己的 `MemoryScope`、五类 query intent、query/mutation/ingest/admin 类型和 `DefaultMemoryEngine`。`MemoryScope` 强制 `session_key == role:<role_id>`。
- [`backend/app/memory_service.py`](../../backend/app/memory_service.py)：结构化写入、规则抽取、纠正/拒绝/忘记和 lexical/embedding 混合召回；`MemoryWorker` 接收完成回合并并行运行语义处理与 Markdown maintenance。
- [`backend/app/memory_store.py`](../../backend/app/memory_store.py)：SQLite schema、角色级唯一键、source 幂等、embedding 维度、consolidation event、replacement 和角色删除清理。
- [`backend/app/memory_maintenance.py`](../../backend/app/memory_maintenance.py)：每次成功回合维护近期上下文；新消息达到 `max(5, RECENT_MESSAGE_LIMIT // 2)` 且窗口超过保留数时整理。当前 `RECENT_MESSAGE_LIMIT=12`，因此普通 consolidation 至少需要 6 条可整理消息；失败不推进游标，`pendingEvent` 可在启动后重试。
- [`backend/app/memory_optimizer.py`](../../backend/app/memory_optimizer.py)：默认 18 小时循环、启动补跑、PENDING 快照、managed block、SELF 规则版本、`self_update_pending` 和失败恢复。
- [`backend/app/main.py`](../../backend/app/main.py)：装配引擎、事件消费者、维护和 Optimizer；回复前组合 Markdown 与 query 结果，回复成功后发布 `TurnCommitted`，删除角色时执行删除屏障和全量清理。

需要特别区分一个当前事实：`main.py` 定义了 `_persist_consolidated_memories()`，`MemoryOptimizer` 也提供可选的 `persist_structured` 回调，但应用装配时没有传入该回调。因此当前运行时的 SQLite 写入来源是回合后结构化处理和 `ConsolidationCommitted` 消费，不是 Optimizer 每次归档后再镜像 MEMORY.md。测试可以单独注入该回调，但不能据此描述生产装配行为。

### Shiori 当前基线

```text
Agent / runtime / tools
  └─ SDK MemoryEngine（query / mutate / ingest / admin）
       └─ plugins/default_memory
            ├─ engine/query.py：按 intent 选择检索策略
            ├─ semantic/retriever.py：向量 + 关键词 + RRF + 热度 + 注入预算
            ├─ semantic/store：SQLite memory_items + sqlite-vec + 事件/replacement
            ├─ semantic/post_response_worker.py：否定旧规则 + 隐式长期记忆提取
            └─ engine/lifecycle.py：事件总线接线

Markdown runtime（宿主公共层）
  └─ TurnCommitted → maintenance/consolidation
       └─ HISTORY/PENDING/RECENT_CONTEXT/journal
            └─ ConsolidationCommitted → default_memory 写入结构化事件

proactive_v2/memory_optimizer.py
  └─ 低频 PENDING 快照 → MEMORY.md，再更新 SELF.md
```

关键一手来源：

- [SDK memory engine contract](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/packages/sdk/python/shiori_sdk/memory/engine.py)
- [TurnCommitted / lifecycle events](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/packages/sdk/python/shiori_sdk/memory/committed.py) 和 [events](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/packages/sdk/python/shiori_sdk/memory/events.py)
- [DefaultMemoryEngine lifecycle](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/plugins/default_memory/backend/engine/lifecycle.py)、[query](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/plugins/default_memory/backend/engine/query.py)、[mutation](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/plugins/default_memory/backend/engine/mutation.py)
- [SQLite schema and conversion helpers](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/plugins/default_memory/backend/semantic/store/common.py)、[writes](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/plugins/default_memory/backend/semantic/store/write.py)、[retrieval](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/plugins/default_memory/backend/semantic/retriever.py)
- [post-response worker](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/plugins/default_memory/backend/semantic/post_response_worker.py)
- [Markdown maintenance](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/apps/backend/core/memory/markdown/maintenance.py)、[consolidation](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/apps/backend/core/memory/markdown/consolidation.py)、[user layer](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/apps/backend/core/memory/markdown/user_layer.py)
- [Optimizer](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/apps/backend/proactive_v2/memory_optimizer.py) 和 [default memory README](https://github.com/YinFengWindy/Shiori-Agent/blob/629e5ba6967a9fa2f4477b38d55e315c8c2b58d7/plugins/default_memory/README.md)

## 回复前：检索和上下文注入

| 项目 | Meido 当前 | Shiori 当前 | 影响 |
|---|---|---|---|
| 端口 | 本地 `DefaultMemoryEngine`，所有请求必须有有效 `role_id` 和 `role:<role_id>` session | SDK `MemoryEngine`，`MemoryScope` 还携带 `channel`、`chat_id`；默认引擎可按 scope 过滤 | Meido 的调用边界更窄，且隔离由 Python 校验和 SQL `role_id` 同时保证 |
| `context` | 原 query 走一条 `MemoryService.recall_async` lane | `_query_context` 走 Retriever，可带 procedure query builder、domain、时间和 scope 过滤 | Meido 保留语义但没有 Shiori 的完整 scope/domain policy |
| `answer` | 原 query 保留关键词 lane；模型可生成最多两个受限 HyDE 查询并只走语义 lane，失败回退原 query | 并行调用轻量模型生成 event/general 两个受限假设（80 tokens、3 秒），失败退回原 query | Meido 已有真实 HyDE，但生成预算、注入规划和 trace 仍更简单 |
| `timeline` | 必须提供时间范围；默认只保留 `event`，按 `happened_at` 排序 | 必须有时间范围；直接查询 event 时间索引并遵守 role/domain/scope | 两者接口形状接近，Shiori 的 SQL 时间预过滤和 scope 条件更完整 |
| `interest` / `procedure` | interest 默认 `preference/profile`；procedure 默认 `procedure/preference`，支持规则标签、置信度和显式信号过滤 | interest 固定偏好/画像；procedure 通过 query builder、标签/工具触发规则并可优先注入 procedure | Meido 有基础 procedure guard，但还没有 Shiori 完整的 trigger/tool planner |
| 召回 | `MemoryStore.query_hybrid` 用摘要关键词/中文二元组 + embedding 召回；可用时使用 sqlite-vec，不可用时回退 SQLite 扫描，引擎层用 RRF 排序 | Retriever 先做 vector/keyword lanes，再 RRF；有类型阈值、全局阈值、relative delta、热度衰减和 sqlite-vec KNN，缺少 sqlite-vec 时全表回退 | Meido 已具备可用的 hybrid 检索，但排序信号和过滤层次较少 |
| 注入 | `SELF.md`、`MEMORY.md`、`RECENT_CONTEXT.md` 直接全文读入，query block 总字符上限 4000，各类型最多 3 条 | Markdown runtime 读取可见的 self/long-term/recent context；Retriever 再按 forced/procedure-preference/event-profile 配额、每行和总字符预算生成 block | Shiori 的“检索结果”和“注入结果”分离得更清楚；Meido 由 `context_block` 做简单裁剪 |
| 证据/观测 | `MemoryRecord` 有 `EvidenceRef`、source、lane/rank/score/hotness/reasons；`MemoryQueryResult.trace` 和 `RetrievalCompleted` 记录候选、过滤、裁剪和索引降级 | 结果含 record/evidence/trace，并由插件写召回观察 JSONL | Shiori 仍有更完整的持久诊断轨迹；Meido 通过引擎结果和角色记忆 API 提供请求内 trace |

### SQLite 结构差异

Shiori `memory_items` 的基线字段是 `id、memory_type、summary、content_hash、embedding、reinforcement、emotional_weight、extra_json、source_ref、happened_at、status、created_at、updated_at`；唯一索引是 `(content_hash, memory_type)`。角色、domain、频道和聊天 scope 通过 `extra_json` 过滤，`consolidation_events` 以 `source_ref` 为主键，`memory_replacements` 保存旧/新条目的快照。`MemoryStore2` 另外初始化 `sqlite-vec` 的 `vec_items` 虚拟表，并在不可用时回退全表余弦。

Meido `memory_items` 在相同核心字段上增加了 `role_id`，唯一索引为 `(role_id, memory_type, content_hash)`；`consolidation_events`、`memory_replacements`、`semantic_memory` 和 `memory_embedding_spaces` 也都有角色键。embedding 按角色记录维度，优先写入 sqlite-vec 虚拟表并保留 SQLite 扫描回退；维度变化时清理旧向量。这个变化是 Meido 的隔离适配，不是 Shiori schema parity；它避免两个角色写入相同摘要时共享一条唯一记录。

## 回复后：两条异步链路

### Shiori

1. 宿主在成功回合发布带工具链、时间、角色和 scope 的 `TurnCommitted`。
2. 默认引擎观察该事件，只把它转换为 `TurnIngested` 放入 EventBus 的进程内观察队列；主回复不等待语义后处理。
3. `PostResponseMemoryWorker` 先解析本轮 `memorize` 工具返回的 `item_id` 并保护这些条目，再用轻量模型识别用户明确否定/纠正的 procedure 或 preference，最后在每轮约 1000 token 预算内提取隐式长期记忆，交给 `Memorizer` 做 embedding、去重、reinforcement 和 supersede。
4. Markdown maintenance 同时观察 `TurnCommitted`。达到窗口门槛时写 HISTORY/PENDING/近期上下文并发布 `ConsolidationCommitted`；默认引擎消费该事件，把每个 history entry 写入 SQLite，并可继续做一轮隐式长期记忆提取。

### Meido

1. 只有保存为 `completed` 的 assistant 消息才由 `main.py` 发布 `TurnCommitted`；事件只含当前角色、单会话和两个 `Message`，没有 Shiori 的 tool chain、channel/chat 或 request metadata。
2. `MemoryWorker.publish()` 创建进程内 asyncio task，按角色锁串行；语义处理和 Markdown maintenance 在同一个角色锁内并行执行。
3. `MemoryService.process_turn()` 先处理 `记住`、`忘记`、`拒绝记忆` 和偏好纠正，再按配置调用结构化 post-response LLM；模型失败或非法输出时回退规则提取。embedding 失败只记录错误，回复不会回滚。
4. `MemoryMaintenance` 在每个成功回合被调用。未达到窗口阈值时重写确定性的最近 12 条消息；达到阈值时写 HISTORY/PENDING/journal，在 `.maintenance.json` 保存 `pendingEvent`，事件消费者再以 `source_key` 幂等写入 SQLite。

因此，Meido 的“事件和两条链路”拓扑接近 Shiori，但语义后处理的智能提取、工具链保护、事件元数据和事件总线抽象仍有明显差距。

## Markdown maintenance / consolidation

| 维度 | Meido | Shiori |
|---|---|---|
| 作用域 | 每个角色一个 `roles/<role_id>/memory`，产品只有一条角色 session | 角色目录仍是用户层，但维护按 `ContextScope`/线程游标处理；还支持群旁听、成员档案和群环境 |
| 近期上下文 | 每轮维护用最近 12 条消息和 4000 字符做确定性 Markdown | 每轮也可刷新 Recent Turns；consolidation 额外调用轻量 LLM 生成 Compression/Ongoing Threads，并给文档写 `shiori-recent-context:v1` 可见性标记 |
| consolidation 条件 | 新消息超过保留窗口且 `ready >= 6`；`ensure_memory_for_window` 可在输入裁剪前强制 | `max(5, keep_count // 2)` 的最小新增量；按不同 ContextScope 分窗，输入窗口准备可按游标强制整理 |
| 产物 | `HISTORY.md`、`PENDING.md`、`RECENT_CONTEXT.md`、日期 journal 和 `.maintenance.json` | 同样的用户层文件，另有 session maintenance progress、消费者 backlog、外部层快照和可见性 provenance |
| 提交 | 写入前比较消息签名、游标和文件 hash；失败不推进游标；`pendingEvent` 启动重试 | 先准备 draft，再由 session owner 条件提交；消息前缀、ContextScope 游标、群环境/成员快照变化会使 draft 过期 |
| 事件载荷 | `ConsolidationCommitted` 直接携带 candidates、message ids/range | 事件携带 history entry payload、source_ref、conversation 和 channel/chat scope；候选由用户层持久化，结构化引擎消费事件 |

Meido 的文件层已经具备 Shiori 的幂等和条件提交核心，但它把 Shiori 的可见性、外部层和持久 session consumer backlog 收缩成单角色游标文件；这符合本项目排除群聊的边界。

## Optimizer

两者都采用低频 `PENDING.md → MEMORY.md → SELF.md`，默认周期均为 18 小时，并在启动时检查上次时间和规则版本。两者的共同动机是避免每次 consolidation 都修改全文注入的 `MEMORY.md`，从而减少 prompt cache 失效。

差异如下：

- Shiori 的 `MemoryOptimizer` 是 `proactive_v2` 中由角色运行时提供模型的异步组件，进程级锁保证一次只处理一个角色。`_merge_memory()` 的 provider 异常会转为空结果并回滚 PENDING snapshot，流程随后仍可能更新 SELF 和 `last_memory_optimized_at`；这是当前基线的实际失败语义。
- Meido 的 `MemoryOptimizer` 在 `backend/app` 运行，worker 另有全局 Optimizer 锁和角色锁。它要求候选存在时输出非空且保留所有 source；历史或 PENDING 在提交前发生变化会重试，模型/文件失败会恢复 snapshot 并保持 `self_update_pending`，下次扫描继续处理。这是比 Shiori 更强的失败重试保证。
- Shiori Optimizer 只维护 Markdown 用户层；结构化 SQLite 由 post-response 和 `ConsolidationCommitted` 消费者维护。Meido 的生产装配同样没有把 `_persist_consolidated_memories` 传给 Optimizer，所以也不应把 Optimizer 描述成 SQLite 权威写入者。
- Meido 支持 managed block、`structuredPending` 可选 outbox 和角色删除时恢复；Shiori 的 Markdown store 自己提供 PENDING snapshot 提交/回滚，但不承担角色级 SQLite 全量删除。
- Shiori 的 `SELF_RULES_VERSION` 当前为 2，Meido 当前为 1；两者 SELF 提示词和哪些内容允许进入 SELF 的规则不能直接互换。

## 管理、删除和恢复

### 管理接口

Shiori SDK 的 `MemoryMutation` 公开 `remember`/`forget`；`MemoryAdminApi` 提供按角色、状态、类型、domain、source、embedding 状态分页查询，更新 status/extra/source/time/emotional weight，删除、批量删除、角色失效和相似搜索。默认 store 的管理更新状态主要是 `active`/`superseded`。

Meido 在同一端口上扩展了 `update`、`delete`、`reject`、`state_change`，支持 `active/rejected/forgotten/superseded`，并由 FastAPI 提供角色级详情、编辑、批量删除、失效和相似条目接口。管理写入都经过角色锁，并在需要时回写 `MEMORY.md`/`PENDING.md`；这是更强的产品管理面，不是 SDK 的逐字 parity。

### 删除和失败恢复

- Shiori 的角色删除监听器调用 `invalidate_role_memories(role_id)`，把该角色结构化条目标为 `superseded`，再发布 `RoleDeleted`；当前默认语义 store 的 schema 不带角色列，因此事件/replacement 行也没有角色删除级联键。
- Meido `DELETE /api/roles/{role_id}` 先设置 deletion barrier，停止 TurnCommitted、Optimizer 和管理写入，等待进行中的任务；随后写 `.deleting-<role>-<token>.json` marker、暂存角色目录、删除角色/session/所有角色记忆表，成功后清理 marker。启动时会根据 manifest 是否还存在恢复暂存目录或继续清理，失败保留 marker 重试；角色 tombstone 阻止本进程重新接收事件。
- 两者都不应让回复前的记忆故障阻塞已经开始的回复。Meido 的 `TurnCommitted` 和 MemoryWorker 仍是进程内，崩溃后不会重放未完成的语义后处理；它只对 Markdown `pendingEvent`、Optimizer snapshot 和角色删除 marker 提供持久恢复。Shiori 的 EventBus `TurnIngested` 同样是进程内队列，但 Markdown session progress 会保留未完成消费者输入并在下次维护重试。

## 需要保留的判断和待关注差异

以下差异是当前源码事实，不能在项目说明中继续写成“已经一比一复刻 Shiori”：

1. Meido 已有 post-response 隐式 LLM 提取、HyDE、procedure rule、热度/类型阈值、注入配额和请求内诊断 trace；仍缺少 Shiori 更细的提取预算、forced evidence、relative-delta 和持久召回观察。
2. Meido 没有 Shiori 的 SDK 插件构造、可替换 engine、`MemoryScope.channel/chat_id`、ContextScope、多用户线程和旁听路径；群聊排除是本项目有意边界。
3. Meido 的 `role_id` SQL 硬隔离、角色级事件/replacement 清理、删除屏障和持久删除恢复是本地增强，不是 Shiori schema 或删除流程的原样复制。
4. Meido 当前 Optimizer 不负责 SQLite 镜像；`main.py` 中未接线的 `_persist_consolidated_memories` 不能作为运行时行为写入架构图。
5. Meido 的 Markdown 近期上下文是确定性消息列表；若目标是继续向当前 Shiori 靠拢，最有价值的增量是近期 Compression、可见性来源标记和 durable consumer backlog，而不是复制群聊数据模型。

本次调研未修改应用代码，也未运行完整测试；结论均来自上述固定提交源码、官方 README/handbook 和当前 Meido 源码。
