# Shiori-Agent 记忆系统研究

## 2026-10-03 当前 main 深度复核

本节以 Shiori-Agent 当前远程 `origin/main` 提交
`096a4ecbfbcdae3ba77c179fedb852721846fcb9`（2026-10-03）为准。下方的
2026-10-01 章节保留为历史快照；其中 `apps/backend/core/memory/engine.py`、
`apps/backend/memory2/` 等路径不能再用来描述当前 main。

### 当前架构地图

当前 Shiori 的记忆系统仍由两个相互配合的面组成，但边界已经重新组织：

当前内置路线是 `default` engine；Akasha 已从内置插件移除，配置
`memory.engine = "akasha"` 不再启动。旧 Akasha 数据不会被自动迁移或删除。

```text
Agent lifecycle / retrieval / tools
            │ 只依赖 SDK MemoryEngine 契约
            ▼
shiori_sdk.memory
  engine.py       query / mutation / ingest / admin 数据类型与 Protocol
  build.py        插件构造依赖、存储能力和资源所有权
  committed.py    TurnCommitted 回合提交快照
  events.py       TurnIngested / ConsolidationCommitted 等事件
            │
            ▼
plugins/default_memory
  backend/engine/       DefaultMemoryEngine 的 query / mutation / policy / admin
  backend/semantic/     embedding、SQLite、向量检索、memorizer、post-response worker
  backend/memory_plugin.py  引擎构造、数据库路径和向量空间兼容性
  backend/plugin.py     RPC、生命周期观察和召回轨迹记录
            │
            ├─ ConsolidationCommitted：把 Markdown 整理出的用户层事件写入结构化记忆
            └─ MemoryStore2：SQLite 条目、向量、状态、来源和时间索引

apps/backend/core/memory/markdown
  maintenance.py       回合提交后的窗口维护、队列、锁、失败状态和条件提交
  consolidation.py     LLM 整理窗口，提取 HISTORY/PENDING/近期语境
  recent_context.py     近期语境压缩和对话预览
  recent_context_document.py 近期文档的可见性来源标记
  listening*.py         群聊旁听记录的批次触发和整理
  user_layer.py         用户本人层落盘并发布 ConsolidationCommitted
  runtime.py            角色作用域 Markdown store 与读取运行时
```

`MemoryEngine` 没有消失，而是从宿主 `apps/backend/core/memory/engine.py` 迁到
`packages/sdk/python/shiori_sdk/memory/engine.py`。这使宿主 Agent、默认插件和未来
独立安装的记忆插件共享同一端口；宿主只负责装配和生命周期，不再把默认 SQLite 实现
当成核心模块。当前默认插件仍是一个完整的 `DefaultMemoryEngine`，只是它实现的接口
来自 SDK。

`apps/backend/memory2` 也不是功能消失，而是整体迁移为
`plugins/default_memory/backend/semantic`。因此 `embedder.py`、`retriever.py`、
`memorizer.py`、`post_response_worker.py` 以及 `store/*` 的主要语义仍然存在；变化
在于这些代码现在由默认插件拥有，插件可以独立打包、测试和替换。当前插件 README
明确将 `backend/semantic` 定义为 SQLite、向量检索、去重、规则标签和回复后处理的
所有者。

### 模型回复前：被动检索链

1. Agent 的 `DefaultMemoryRetrievalPipeline` 将当前输入、`role_id`、会话、频道和
   聊天 ID 转成 SDK `MemoryQuery(intent="context")`。
2. `MemoryRuntime` 只转发请求给当前 engine；它不理解 SQLite 表，也不拼接默认引擎
   的策略。
3. `DefaultMemoryEngine` 在硬作用域过滤后按请求意图选择路径：`context/procedure`
   走上下文检索，`answer` 可并行生成两个受限 HyDE 假设再检索，`timeline` 走时间
   范围查询，`interest` 只查偏好和画像。
4. `semantic/Retriever` 负责 embedding、关键词/向量候选、相似度阈值、热度、类型
   配额和 scope 过滤；注入规划再次限制 procedure、preference、event、profile 的
   数量和总字符预算。带 `tool_requirement` 的 procedure 可在安全规则允许时优先保留。
5. 返回值同时包含给上下文使用的 `text_block`、结构化 `MemoryRecord`、`EvidenceRef`
   和 trace。摘要是检索线索，精确原话需要通过消息查询工具回源，不能把摘要当成原文
   证据。
6. 默认插件还会把本次上下文准备和显式 `recall_memory` 结果写入召回观测 JSONL，
   用于诊断实际注入了哪些条目。

Markdown 固定上下文由 `MarkdownMemoryRuntime` 按角色读取 `SELF.md`、`MEMORY.md`
和带来源可见性校验的 `RECENT_CONTEXT.md`。当前近期文档带有
`shiori-recent-context:v1` 可见性标记；如果用户上下文线程边界发生变化，旧文档会
被拒绝注入，避免把不再可见的群聊/线程内容带入新会话。

### 回复提交后：两条异步链路

回合完成时宿主发布 `TurnCommitted`。这条事件包含用户消息、助手回复、工具链、角色
和会话作用域；默认引擎只把它转换为 `TurnIngested` 入队，主回复链路不等待语义记忆
处理。

**语义记忆 post-response worker：**

1. 从工具链解析本轮 `memorize` 的结构化 `item_id`，把显式写入条目标记为受保护，
   防止后续自动纠错逻辑立即退休它。
2. 用轻量模型识别用户明确否定或纠正的旧 procedure/preference；先向量召回候选，
   再在 token 预算内判断并批量 `supersede`。
3. 在剩余预算内将本轮用户消息和助手回复交给隐式长期记忆提取器；提取结果结合
   当前 active profile/preference/procedure 去重后写入 SQLite。
4. `Memorizer` 在写入时建立 embedding、内容哈希和来源；重复内容 reinforcement，
   procedure/preference/profile 按类型和相似度 merge 或 supersede。失败会向生命周期
   边界抛出，不能静默报告成功。

**Markdown maintenance/consolidation：**

1. `MarkdownMemoryMaintenance.on_turn_committed()` 为会话入队；群友回合可以跳过
   角色语义提取，但仍可能推进 Markdown 整理。
2. 每个会话有自己的维护队列、锁、游标和失败状态。维护准备阶段读取会话窗口、上下文
   scope、近期文档和外部群聊层快照。
3. 未到窗口条件时仍可刷新 `RECENT_CONTEXT.md`；近期上下文可能由独立的轻量模型压缩，
   也会保留用户消息和助手预览，而不是每次重写整段长期记忆。
4. 达到窗口条件后，`consolidation.py` 用 LLM 生成结构化
   `history_entries` 和 `pending_items`。`user_layer.py` 幂等写入 `HISTORY.md`、
   `PENDING.md`、`journal/` 和角色近期文档，然后发布 `ConsolidationCommitted`，由
   默认引擎把 history entries 写入 SQLite。
5. 提交不是“生成后直接推进游标”：提交时会校验消息前缀、各 ContextScope 游标以及
   群环境/成员档案快照；如果期间有新消息或外部编辑，草稿被判定过期，游标不推进，
   下次重试。
6. 群聊旁听由 `listening_trigger.py` 在新记录入库时检查，并每 30 分钟扫描一次有未
   整理记录的群。达到批量条数或跨天条件才调用模型；外部消息更新群环境和成员档案，
   用户本人消息仍走用户层 `HISTORY/PENDING`，不混入角色的近期上下文。

### Memory Optimizer：独立的长期 Markdown 归档层

Shiori 的 Optimizer 不是旧版文档中的抽象概念，而是当前代码中的独立后台任务：
`apps/backend/proactive_v2/memory_optimizer.py` 定义 `MemoryOptimizer` 和
`MemoryOptimizerLoop`，`apps/backend/bootstrap/proactive.py` 负责按配置注册循环。
它与语义 post-response worker、Markdown consolidation 和
`ConsolidationCommitted` 消费者是并行的生命周期：consolidation 产生 `PENDING.md`
候选，Optimizer 低频把候选归并进全文注入的 `MEMORY.md`，而结构化 SQLite 事件消费
仍由默认记忆引擎负责。

Optimizer 的完整流程如下：

```text
应用启动
  ├─ 扫描角色的 memory_init_state
  ├─ SELF 规则版本落后 / 没有 last_memory_optimized_at / 已逾期 -> 立即补跑
  └─ 注册 MemoryOptimizerLoop（默认 64800 秒）

定时 tick
  └─ 每角色串行 optimize
       ├─ snapshot_pending()：原子移走 PENDING.md
       ├─ LLM 合并 MEMORY.md + snapshot
       ├─ 成功：写 MEMORY.md，commit snapshot
       ├─ 失败/空结果：rollback snapshot，保留候选
       ├─ 更新 SELF.md（规则版本允许一次重写）
       └─ 写入 last_memory_optimized_at
```

触发是时间驱动而不是候选数量驱动：默认周期为 18 小时，启动时只补跑逾期角色；配置
值最小为 60 秒。Optimizer 使用进程级锁保证同一时刻只有一个角色归档。它不会等待
`ConsolidationCommitted` 的 SQLite 消费，也不会因为自身失败回滚已经提交的 Markdown
整理或结构化记忆事件。

`PENDING.md` 的快照是两阶段提交：`snapshot_pending()` 原子 rename 后，期间新追加的
候选写入新的 PENDING 文件；合并成功调用 `commit_pending_snapshot()` 删除旧快照，
失败调用 `rollback_pending_snapshot()` 将旧快照和新追加内容合并。进程启动或下一次
快照前会恢复残留的 `PENDING.snapshot.md`，因此崩溃不会静默丢候选。

合并步骤读取 `MEMORY.md` 与快照，使用“缺席成本”判断长期价值，执行新增、冲突替换、
去重和压缩；成功后才清理快照。随后单独更新 `SELF.md` 的约定章节。`SELF.md` 有规则
版本状态，规则升级时触发一次完整重写；普通运行只做受限章节更新。`SELF.md` 更新失败
不会回滚已成功的 `MEMORY.md` 合并，但角色的优化时间仍应由实现按成功状态记录，下一次
启动可继续处理。

Optimizer 存在的产品原因是 prompt cache：`MEMORY.md` 和 `SELF.md` 在回复前以稳定全文
块注入；如果每次 consolidation 都直接修改 `MEMORY.md`，system prompt 会频繁变化，
缓存命中率下降。让 PENDING 高频追加、MEMORY 低频更新，可以把长期文档变化集中到一次
归档，代价是新候选在归档前不会进入全文长期块，但仍可通过 PENDING/结构化事件路径被
后续处理。

主要源码：[`memory_optimizer.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/proactive_v2/memory_optimizer.py)、[`proactive.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/bootstrap/proactive.py)、[`agent/memory.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/agent/memory.py)、[`memory-markdown.md`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/docs/_handbook/memory-markdown.md)。

### 显式工具与管理边界

`memorize`、`forget_memory`、`recall_memory` 现在由 engine 的 `tool_profile()` 描述
并由 Agent 工具注册器装配。工具只构造 SDK `MemoryMutation`/`MemoryQuery`，不直接碰
默认插件的数据库。所有调用都要求 `role_id`，频道和聊天作用域作为硬过滤条件；管理
端通过 engine admin API 分页查看、编辑、删除、失效角色记忆和查相似条目。

### 与旧研究文档的差异

| 旧研究结论（2026-10-01） | 当前 main 的准确说法 |
| --- | --- |
| `core/memory/engine.py` 定义契约 | 契约迁移到 `packages/sdk/python/shiori_sdk/memory/engine.py`；宿主通过 SDK 使用 |
| `apps/backend/memory2` 是共享实现层 | 代码整体迁移到 `plugins/default_memory/backend/semantic`，成为默认插件私有实现 |
| 默认引擎、memory2、Markdown 是相对静态三层 | 仍是三类职责，但新增 SDK 构造/资源所有权、事件总线和生命周期运行时边界 |
| Markdown 主要围绕单角色会话窗口 | 当前维护按 ContextScope 和线程游标工作，并包含旁听群、群环境、成员档案和可见性 provenance |
| `RECENT_CONTEXT.md` 是简单近期摘要/截断 | 当前是带来源标记的派生文档，注入前校验当前可见线程边界 |
| 旧研究中的 `memory2` 文件路径可直接引用 | 旧路径只能作为迁移来源；引用当前实现应使用 plugin semantic 路径 |

因此，用户之前看到的“差别很大”主要确实来自 Shiori 在 2026-10-01 之后的大版本重构，
不是旧调研完全错误。旧文档对数据语义（Markdown + SQLite、来源、scope、异步 post-response
和 consolidation）的判断仍然成立，但对代码归属、构造方式和 Markdown 维护触发模型已经过时。

### 对 Meido 当前实现的含义

Meido 可以继续采用当前规格中的单角色闭环，不需要复制 Shiori 的插件和群聊运行时。应
优先吸收四个稳定原则：

1. 保留 `role_id` 参与精确去重和所有查询条件；这比旧 Shiori `content_hash + memory_type`
   的跨角色唯一键更安全。
2. 将“摘要用于召回、原文用于证据”作为检索结果语义，逐步补充 source ID 和 trace。
3. 把 post-response、Markdown consolidation、Optimizer 的失败状态分开记录，并保证
   source/cursor 幂等；不要把进程内异步 worker 误认为持久任务队列。
4. 如果未来需要第二种记忆后端，再抽取类似 SDK 的小型端口；当前单一 Meido engine
   没有必要引入完整插件装配和群聊 ContextScope。

## 2026-10-01 复核

本次对比以 Shiori-Agent `main` 提交 `8f6c02e905e1f128498f238b67fefa4710cc2420`（2026-10-01）和 Meido 当前提交 `2a1bbc9ba6f45639f4d6c99e1bc005212b4c6686` 为准。Shiori 的模块说明、引擎契约、默认引擎生命周期、Markdown 维护、`memory2` 检索器及回复后 worker 均按该提交源码核对。

Meido 当前实现也采用角色级 Markdown 文档和 SQLite 结构化记忆两层：[MemoryService/MemoryWorker](../../backend/app/memory_service.py) 负责回合后提取、忘记/拒绝和角色串行队列；[MemoryMaintenance](../../backend/app/memory_maintenance.py) 以 20 条已提交消息为 consolidation 门槛，写入 `HISTORY.md`、`PENDING.md`、`RECENT_CONTEXT.md` 和日期 journal；[MemoryOptimizer](../../backend/app/memory_optimizer.py) 将候选合并到 `MEMORY.md`/`SELF.md` 并镜像回 SQLite；[MemoryStore](../../backend/app/memory_store.py) 保存来源、状态、reinforcement、supersede 和 embedding；回复前执行关键词与 embedding 融合检索，并把 `SELF.md`、`MEMORY.md`、`RECENT_CONTEXT.md` 和检索条目组成上下文（[API 接线](../../backend/app/main.py)）。

Shiori 当前额外提供稳定的 [`MemoryEngine` ingest/query/mutate/admin 契约](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/core/memory/engine.py)，由[默认记忆插件](https://github.com/YinFengWindy/Shiori-Agent/tree/8f6c02e905e1f128498f238b67fefa4710cc2420/plugins/default_memory/backend/engine)实现策略，[`memory2`](https://github.com/YinFengWindy/Shiori-Agent/tree/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/memory2)提供 SQLite/`sqlite-vec` 存储、向量检索和注入块，Agent retrieval 负责上下文接入。其检索支持显式 `MemoryQuery` intent、domain、时间和频道/聊天 scope、阈值/热度/类型配额；[`recall_memory` 工具](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/agent/tools/recall_memory.py)返回 evidence、score、trace 并要求引用。Shiori 的 [Markdown maintenance](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/core/memory/markdown/maintenance.py)由事件总线驱动，支持角色会话之外的群聊/成员上下文；[post-response worker](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/memory2/post_response_worker.py)和 consolidation worker 共用引擎，并受 token 预算控制。

因此两者共同的数据语义是“原始消息为证据、结构化存储为检索权威、Markdown 为可读视图、来源可追溯、角色隔离、异步整理”。主要差异是：Meido 是单应用内的具体实现，隐式提取目前使用规则匹配，使用进程内每角色锁和 SQLite/JSON embedding；Shiori 是可插拔引擎架构，隐式提取由 LLM 策略驱动，支持多种 scope、群聊成员层、事件总线、`sqlite-vec`、更细的召回策略和管理 API。Meido 当前不包含 Shiori 的插件记忆引擎契约、显式 recall 工具协议、证据引用格式和群聊/多成员记忆。

## 深度调研：Shiori 的模块职责与数据流

### 1. 记忆引擎契约与实现分层

Shiori 在 `core.memory.engine` 定义请求和返回类型：`MemoryScope`、`MemoryQuery`/filters/results、`MemoryRecord`/`EvidenceRef`、`MemoryMutation`、ingest/query/write/admin Protocol、能力描述及工具配置。默认实现位于 `plugins/default_memory`，存储、向量检索和写入实现位于 `apps/backend/memory2`，Agent retrieval 负责把检索适配到生成上下文。此边界让 Agent lifecycle 不需要理解 SQLite schema 和默认提取策略。来源：[engine contract](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/core/memory/engine.py)、[default engine lifecycle](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/plugins/default_memory/backend/engine/lifecycle.py)、[module map](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/docs/knowledge/modules/memory.md)。

需要区分接口稳定性与架构复杂度：契约确实让实现可替换，但默认 engine 自身仍持有大量策略，`MemoryEngine` admin Protocol 也相当宽；Meido 现在只有一个 engine，直接照抄整套 capability/plugin/tool 注册系统没有已知收益。

### 2. 两种互补的数据表示

Markdown 是按角色组织、可读和可管理的长期视图：`SELF.md` 表达自我/关系认识，`MEMORY.md` 放紧凑画像，`HISTORY.md` 保留事件脉络，`PENDING.md` 缓冲候选，`RECENT_CONTEXT.md` 服务近期话题；群聊环境和成员资料另有各自的上下文层。结构化 SQLite 保存可检索条目、embedding、状态、强化值、时间、source 和替代关系。Markdown consolidation 产生的事件通过 `ConsolidationCommitted` 再进入结构化库；用户资料层的写入和结构化记忆引擎是相关但不同的路径。来源：[markdown schema](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/core/memory/markdown_schema.py)、[markdown maintenance](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/core/memory/markdown/maintenance.py)、[memory2 schema](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/memory2/store/common.py)。

Shiori 的结构化条目用 `extra_json` 带 `role_id`、频道/聊天 scope 和 domain 等元数据，而不是 Meido 这种 `role_id` 原生列。其优势是可以容纳更多业务维度；代价是隔离正确性高度依赖统一的 JSON scope filter 和所有调用路径都传对 scope。Meido 只有角色会话，独立列与 SQL 条件更简单直接。

### 3. 写入、去重和生命周期

写入不是单纯“摘要后 insert”：精确内容哈希触发 reinforcement；procedure/preference 可按向量相似度合并或 supersede；profile 的状态/购买类别只在同 category 内替代；时间、来源和 emotional weight 一起保存。consolidation source 通过唯一 source event 做幂等。回复后 worker 先处理模型工具链已经显式保存的 item IDs，保护这些条目，再处理用户纠正/失效和隐式提取，并按 token budget 控制模型调用。隐式提取与 consolidation 都以现有 active 画像作为去重上下文。来源：[memorizer](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/memory2/memorizer.py)、[store writes](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/memory2/store/write.py)、[post-response worker](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/memory2/post_response_worker.py)。

重要边界：`source_ref`/幂等写入可避免同一事件重放成重复条目，但这不代表整个内存队列在进程崩溃后一定可恢复；不要把 Shiori 的异步 worker 等同持久任务队列。还发现一个应避免复制的数据隔离风险：当前 Shiori `memory2` 的唯一索引是 `(content_hash, memory_type)`，没有 `role_id`；role scope 存在 `extra_json`。upsert 先按这两个全局字段找已有行，命中时只强化行，不重写 role metadata。因此同一数据库中不同角色写入相同类型和内容时，第二次写入可能强化第一条跨角色记录，而不会创建属于第二角色的条目。来源：[schema and index](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/memory2/store/common.py)、[upsert](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/memory2/store/write.py)、[role metadata on write](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/plugins/default_memory/backend/engine/mutation.py)。Meido 的唯一键包含 `role_id`，应保留这一隔离约束。

### 4. 查询不是单一相似度排序

`MemoryQuery.intent` 将 context、answer、timeline、interest、procedure 分开。默认引擎可为显式 answer recall 生成受时限和 token 限制的 HyDE 辅助 query；向量 lane 以辅助查询召回，关键词 lane 保留用户原始措辞，再以 RRF 融合。role/channel/chat、memory type/domain 和时间范围在存储查询时过滤。注入阶段再次按类别阈值、procedural guard、置信度、条数与字符预算筛选；候选超限时优先保留带显式 tool requirement 的 procedure。来源：[query policy](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/plugins/default_memory/backend/engine/query.py)、[retriever and injection planner](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/memory2/retriever.py)、[recall tool](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/agent/tools/recall_memory.py)。

返回记忆线索也不是原文证据。Shiori 的 recall 输出 evidence、score、trace，并要求引用记忆 ID；提示词还规定如果需要原话细节须调用 `fetch_messages` 回源。这个“摘要用于找线索，原文用于作证”的区分能减少摘要幻觉。来源：[message lookup tool](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/agent/tools/message_lookup.py)、[default memory prompts](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/plugins/default_memory/backend/engine/prompts.py)。

### 5. Markdown consolidation 与多用户上下文

Markdown maintenance 监听 turn committed，按消息数或下一次模型输入 token 预算决定是否整理。角色共享会话中的不同用户消息可分成独立 `ContextScope` 游标；整理采用 snapshot/draft/conditional commit，检查消息 ID 与上下文游标仍匹配后再提交，避免并发新消息或外部编辑导致过期结果覆盖。群聊的环境摘要和成员档案作为独立层更新。来源：[maintenance lifecycle and conditional commit](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/core/memory/markdown/maintenance.py)、[scope and consolidation window logic](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/core/memory/markdown/formatting.py)、[external layer commit](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/core/memory/external_writes.py)。

## 对 Meido 的学习建议

以下建议针对 Meido 当前提交 `2a1bbc9` 的源码，不表示 Shiori 的每个机制都应照搬。

### 高优先级：正确性和可诊断性

1. **把来源和状态一致性作为端到端不变量。** Meido 已有稳定 source key、SQLite `source_ref`、consolidation cursor 和 Markdown 管理视图；可进一步为 forget/reject/update/supersede 写跨 SQLite、`PENDING.md`、`MEMORY.md` 的回归验收，保证候选不会在下一次优化时复活，摘要、embedding、固定上下文和检索结果始终一致。相关入口：[mutation + document sync](../../backend/app/main.py)、[document projection](../../backend/app/memory_documents.py)、[store update/status](../../backend/app/memory_store.py)。

2. **让检索结果可解释，并把摘要与原文证据分开。** Meido UI 能从来源定位消息，但对话注入只给类型和摘要；建议先在内部检索结果增加 `score`、命中信号、source IDs 与 retrieval trace，在 UI/调试日志可查看。遇到“哪天说过什么/准确原话”等问题时，再以 source ref 回查原始消息，不把摘要冒充原话。这能直接借鉴 Shiori 的 `MemoryRecord`、`EvidenceRef`、trace 和 fetch-messages 约束，而不必复制强制引用符号协议。

3. **维护失败要可观测并可重试。** Meido worker 收集 `errors`，Optimizer 会扫描待处理 Markdown 恢复；当前回合后任务还会写入 SQLite `memory_jobs`，记录稳定 source key、状态、尝试次数和错误，并在启动时恢复 `pending/failed` 任务。仍应区分提取、维护、optimizer、embedding 四类错误；这一本地账本用于进程恢复，不是通用持久 job queue。Shiori 有明确生命周期错误传播和 consolidation source 幂等。

### 中优先级：检索与提取质量

4. **将不同的“记忆问题”建模成 query intent。** 当前 Meido 对每条用户输入走同一混合检索和固定注入块。若产品要回答历史时间线、程序性偏好或兴趣，可以增加少量可验证 intent，并分别设过滤/阈值/预算；先建立样例评测，再引入 HyDE 或更多重排策略，避免靠堆 prompt 调分。

5. **对 LLM 提取采取结构化约束，而非直接换成自由生成。** Meido 现在用规则从用户句子提取，简单而便宜但覆盖窄；Shiori 的启示是结构化输出、active-memory 去重上下文、token/超时预算、明确来源和逐字段验证。可先把规则提取换为可选的 extractor seam，并以准确率、误记率、成本和失败回退做比较；不要在没有评测时让模型自行改写整份记忆。

6. **把记忆注入看作独立决策层。** Shiori 先取候选，再按分数阈值、类型配额、置信度文案、procedure 安全条件和总预算决定注入。Meido 已有限制 4000 字和每类型三条，但可增加最低相关性、显式当前话语优先、低置信度标记，并记录被选/被裁剪原因。

7. **把检索行为做成固定基线评测。** Meido 已融合词法和向量候选，但词法 lane 先截到 top-k，向量 lane 对有 embedding 的角色记忆扫描并计算 cosine；建议保存一组匿名化/合成问题和相关条目标注，分别衡量 recall@k、排序、无关注入率和 embedding 失败降级，再调融合策略。Shiori 的实现也有需要小心借鉴的细节：其 RRF 控制候选合并顺序，但下游注入门槛读取原始 lane 的 `score`，并非统一的 RRF score；所以即使采用 RRF，也应定义并测试“排序分”和“注入置信度”各自语义，不能把分数混为一谈。来源：[Shiori RRF merge and injection](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/memory2/retriever.py)、[Meido hybrid query](../../backend/app/memory_store.py)。

8. **分离聊天模型与 embedding 配置，并控制回填成本。** Meido 的 embedding adapter 使用角色的聊天模型配置访问兼容 `/embeddings` endpoint、单条同步请求且超时 2 秒；提供独立 embedding model/base URL、批量回填和显式超时指标，可避免聊天模型不支持 embedding 时长期降级并减少网络往返。Shiori 有独立 embedding 配置和批量 embedding 接口。先确认本地模型服务实际支持的接口，再让 embedding 配置可选；失败时保留词法路径。来源：[Meido embedding adapter](../../backend/app/embeddings.py)、[Meido model configuration wiring](../../backend/app/main.py)、[Shiori embedder](https://github.com/YinFengWindy/Shiori-Agent/blob/8f6c02e905e1f128498f238b67fefa4710cc2420/apps/backend/memory2/embedder.py)。

### 按需求再引入

9. **有第二实现/多端需要时再抽 MemoryEngine。** 当前 `MemoryService` 已是 facade。若出现本地/云端多后端、插件或不同记忆策略，再抽小型 ingest/query/mutate/admin Protocol；现阶段不需要复制 Shiori 全部 capabilities 和工具插件注册。

10. **扩展到群聊时先定义 scope 数据模型。** Shiori 的 role/channel/chat/member scope 和独立成员档案值得学习；Meido 当前一个角色对应唯一会话，现有 `role_id` 列足够。未来支持多人共享角色时，应把 user/member scope 变成存储查询中的硬过滤，并做跨 scope 隔离测试，不能只写进提示词。

11. **仅在规模证明需要时上向量扩展。** Shiori 的 `sqlite-vec` 与 fallback 能支持更大规模；Meido 当前逐条 embedding 存 JSON、在 SQLite 中扫描 cosine。先测量每角色记忆量、查询耗时、embedding 调用和召回质量，再决定引入 extension、索引或独立服务。

### 不建议直接照搬

- 完整插件系统、事件总线扩展、群聊监听和多层成员档案：这是 Shiori 的多渠道产品需求，Meido 当前范围没有对应用户行为。
- HyDE、多路查询、情绪权重、procedure tool guard 等所有召回 heuristics：每一项会增加成本或潜在误召，先以 Meido 自己的检索失败样本证明收益。
- 把 Markdown 与 SQLite 做双向任意编辑同步：Meido 当前采用 SQLite 权威、Markdown 受控投影；继续维持单一写入权威更易保证忘记/拒绝语义。
- Shiori 当前的跨角色唯一索引设计：Meido 已把 `role_id` 纳入去重键，应通过跨角色相同内容测试锁定该隔离属性。
- 把 `memory_replacements` 表当成已经完整工作的替代审计：Shiori 当前仓库能看到 schema 和记录方法，但未发现生产路径调用 `record_replacements`；Meido 若需要替代关系历史，应从 mutation transaction 建立并测试真实写入路径。

历史研究基线：`main`，提交 `6e560dbdceaf09a317ba7aec11968136ae6e6e8f`（2026-09-29）。上方“2026-10-01 复核”是本次对比使用的当前基线；以下原有章节保留作较早的详细记录。

## 总体结构

Shiori 将记忆拆成两个配合的存储面：角色工作区中的 Markdown 记忆文档，以及 `memory2` SQLite 结构化/向量记忆。`core/memory/engine.py` 提供稳定的 ingest、query、mutate、admin 契约；`plugins/default_memory/` 实现默认策略；`apps/backend/memory2/` 实现 embedding、持久化、检索和写入。调用方通过契约访问引擎，不直接依赖默认实现。

- 契约：[engine.py](https://github.com/YinFengWindy/Shiori-Agent/blob/6e560dbdceaf09a317ba7aec11968136ae6e6e8f/apps/backend/core/memory/engine.py)
- 默认引擎装配：[lifecycle.py](https://github.com/YinFengWindy/Shiori-Agent/blob/6e560dbdceaf09a317ba7aec11968136ae6e6e8f/plugins/default_memory/backend/engine/lifecycle.py)
- 项目维护的模块说明：[memory.md](https://github.com/YinFengWindy/Shiori-Agent/blob/6e560dbdceaf09a317ba7aec11968136ae6e6e8f/docs/knowledge/modules/memory.md)

## Markdown 文档层

每个角色有自己的记忆目录，运行时路径为 `roles/<role_id>/memory/`。文档职责分离：`MEMORY.md` 保存紧凑的长期用户记忆，`SELF.md` 保存角色自我认知和关系基线，`HISTORY.md` 追加事件时间线，`PENDING.md` 缓冲待归档事实，`RECENT_CONTEXT.md` 保存近期上下文。Markdown 提供人类可读/可编辑的视图；不是向量记录的替代品。

- 角色记忆服务：[memory_service.py](https://github.com/YinFengWindy/Shiori-Agent/blob/6e560dbdceaf09a317ba7aec11968136ae6e6e8f/apps/backend/core/roles/memory_service.py)
- Markdown schema：[markdown_schema.py](https://github.com/YinFengWindy/Shiori-Agent/blob/6e560dbdceaf09a317ba7aec11968136ae6e6e8f/apps/backend/core/memory/markdown_schema.py)
- 注意：[旧 handbook](https://github.com/YinFengWindy/Shiori-Agent/blob/6e560dbdceaf09a317ba7aec11968136ae6e6e8f/docs/_handbook/memory-markdown.md) 中共享工作区路径示例已过时；生产代码以角色 ID 隔离。

## 写入与整理

回合提交后，生命周期将记忆工作放入后台队列。Markdown consolidation 根据消息数量门槛处理一段会话，提取时间线事件和候选长期事实，按来源引用做幂等追加；另有定时 optimizer 把 `PENDING.md` 归并进紧凑长期画像。`memory2` 消费 consolidation 事件，并由 post-response worker 处理隐式抽取。显式 `memorize`、`forget_memory` 也通过同一引擎契约。

- Markdown maintenance：[maintenance.py](https://github.com/YinFengWindy/Shiori-Agent/blob/6e560dbdceaf09a317ba7aec11968136ae6e6e8f/apps/backend/core/memory/markdown/maintenance.py)
- 合并逻辑：[consolidation.py](https://github.com/YinFengWindy/Shiori-Agent/blob/6e560dbdceaf09a317ba7aec11968136ae6e6e8f/apps/backend/core/memory/markdown/consolidation.py)
- 响应后 worker：[post_response_worker.py](https://github.com/YinFengWindy/Shiori-Agent/blob/6e560dbdceaf09a317ba7aec11968136ae6e6e8f/apps/backend/memory2/post_response_worker.py)
- 默认引擎生命周期接线：[lifecycle.py](https://github.com/YinFengWindy/Shiori-Agent/blob/6e560dbdceaf09a317ba7aec11968136ae6e6e8f/plugins/default_memory/backend/engine/lifecycle.py)

## 结构化记忆与召回

结构化记忆包含类型、摘要、来源引用、发生时间、扩展元数据、状态及 embedding 等信息。默认存储为 SQLite；可用时加载 `sqlite-vec`，不可用时回退到扫描路径。写入支持相同内容强化、重复来源幂等处理和旧条目 supersede。召回按显式 `role_id` 等 scope 过滤，返回记录、证据和分数；运行时可在回复前注入召回结果，也提供显式 recall 工具。

- SQLite/向量初始化：[connection.py](https://github.com/YinFengWindy/Shiori-Agent/blob/6e560dbdceaf09a317ba7aec11968136ae6e6e8f/apps/backend/memory2/store/connection.py)
- 写入与幂等：[write.py](https://github.com/YinFengWindy/Shiori-Agent/blob/6e560dbdceaf09a317ba7aec11968136ae6e6e8f/apps/backend/memory2/store/write.py)
- 检索：[retriever.py](https://github.com/YinFengWindy/Shiori-Agent/blob/6e560dbdceaf09a317ba7aec11968136ae6e6e8f/apps/backend/memory2/retriever.py)
- 明确召回工具：[recall_memory.py](https://github.com/YinFengWindy/Shiori-Agent/blob/6e560dbdceaf09a317ba7aec11968136ae6e6e8f/apps/backend/agent/tools/recall_memory.py)

## 对 Meido 的启示

适合借鉴的边界是：角色级 Markdown 记忆文档用于可视化编辑；SQLite 记忆记录用于来源、状态和召回；用小型引擎接口隔离上下文组装与存储；回合后异步提取，并以来源引用保证幂等；查询必须由后端按角色范围过滤。

Shiori 的完整实现还包括插件运行时、工具调用、事件总线、embedding 服务、主动任务和维护任务。Meido 当前尚无这些运行时边界，照搬其整套实现会扩大首版范围。参考项目的 `MEMORY.md`/`SELF.md` 与用户导入的日记、学习笔记等资料也应分开建模：前两者是角色记忆视图，后者是来源文档资源。
