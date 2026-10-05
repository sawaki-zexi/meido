# Meido 与 Shiori 记忆模块差异复核（PR #71 后）

状态：基于当前 PR #71 分支 `49b2981` 与 Shiori-Agent `096a4ecbfbcdae3ba77c179fedb852721846fcb9` 的源码复核。

## 已对齐

- 两者都在成功回合后发布回合边界，异步执行语义记忆处理和 Markdown maintenance。
- 两者都把 Markdown 作为可读的稳定层，把 SQLite 作为可过滤、可追踪的结构化检索层。
- 两者都使用 `PENDING.md` 缓冲 consolidation 候选，由独立 Optimizer 低频合并到 `MEMORY.md` 并更新 `SELF.md`。
- 两者都支持 source key 幂等、纠正/失效、embedding 失败降级和 Markdown 条件提交。
- Meido 已补齐 answer HyDE provider、HyDE 失败回退、语义专用 lane、procedure hints、hotness tie-break 和事件观察接口。

## 仍然不同

### 架构边界

Meido 将 MemoryEngine、默认策略、SQLite store、模型调用和 FastAPI 生命周期集中在 `backend/app`；Shiori 在 SDK 定义 MemoryEngine 契约，由 `plugins/default_memory` 实现策略，`apps/backend/memory2` 提供存储，宿主通过事件总线连接。Meido 更简单，替换记忆后端和独立演进策略的边界更弱。

来源：[Meido memory_engine.py](../../backend/app/memory_engine.py)、[Shiori engine.py](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/packages/sdk/python/shiori_sdk/memory/engine.py)。

### 回合后提取

Meido 在有模型配置时调用结构化 LLM，提示中带本轮用户/助手消息和最多 40 条 active 记忆；超时或非法输出回退规则提取。显式“记住/忘记/拒绝/纠正”仍主要依赖文本规则。

Shiori 的 `TurnCommitted` 还携带工具链结果。worker 先保护本轮 `memorize` 已写入的 item ID，再处理否定和纠正，最后在 token 预算中运行隐式提取。因此 Meido 尚未具备工具写入 ID 的保护语义，也没有完整的 tool-chain provenance。

来源：[Meido memory_service.py](../../backend/app/memory_service.py)、[Shiori post_response_worker.py](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/semantic/post_response_worker.py)。

### Markdown consolidation

Meido 的 consolidation provider 现在可以返回 `history`、`recentContext` 和 `pendingItems`，并保留 `.maintenance.json` 游标、pending event、条件提交和失败重试；没有模型时使用规则候选。

Shiori 的 Markdown maintenance 是独立队列，按 ContextScope 管理游标和可见消息，并由专门 consolidation 模块生成草稿、完成用户层幂等提交。Meido 是单角色单会话，因此没有 Shiori 的 channel/chat/member scope；近期上下文和 Markdown 草稿校验也更简单。

来源：[Meido memory_maintenance.py](../../backend/app/memory_maintenance.py)、[Shiori maintenance.py](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/core/memory/markdown/maintenance.py)、[consolidation.py](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/apps/backend/core/memory/markdown/consolidation.py)。

### 回复前检索

Meido 当前流程是：原始 query 走 lexical+vector；answer 的 HyDE 查询由模型生成后只走 semantic lane；RRF 负责排序，hotness 只作 tie-break，procedure 可通过 rule hint 做标签、置信度、类型配额和原始 lane 分数过滤。

Shiori 的 Retriever 还具有默认的 intent 策略、全局/类型阈值、relative-delta、forced/procedure/event/profile 配额、总注入预算和更完整 trace。Meido 的 `min_score`、`type_limits` 目前主要由调用方 hints 提供，`context_block()` 仍是固定 4000 字和每类型最多 3 条，策略层次更薄。

来源：[Meido memory_engine.py](../../backend/app/memory_engine.py)、[Shiori query.py](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/engine/query.py)、[retriever.py](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/semantic/retriever.py)。

### 存储和向量索引

Meido 用 `role_id` 作为 SQL 硬隔离键，embedding 默认写入 `embedding_json`，按角色记录维度；当前提供可注入 `VectorIndex` 协议，未装配时使用 SQLite 全表余弦扫描，并在维度变化、删除时清理外部索引。

Shiori 使用 `sqlite-vec` 虚拟表做 KNN，失效时回退全表扫描；其 `memory_items` 唯一键没有 role_id，scope 存在 `extra_json`，因此 Meido 的隔离更强但 schema 并非原样 parity。Meido 当前生产装配没有 sqlite-vec 具体适配器，这是剩余的实际差异。

来源：[Meido memory_store.py](../../backend/app/memory_store.py)、[Shiori store/common.py](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/semantic/store/common.py)、[vector.py](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/semantic/store/vector.py)。

### 事件和观测

Meido 有进程内 `MemoryEventBus`，记录最近 2000 个 `TurnCommitted`、`TurnIngested`、`RetrievalCompleted`、`MemoryWritten` 和 `ConsolidationCommitted`，并提供角色级诊断接口；观察失败不阻塞主流程。

Shiori 的事件总线主要是生命周期连接和 worker 输入，召回观察还有结构化 trace/日志。两者都不把内存事件队列当成崩溃后持久任务队列；Meido 的事件历史是诊断缓存，重启后丢失，Markdown 和 SQLite 通过各自游标/source key 恢复。

来源：[Meido memory_events.py](../../backend/app/memory_events.py)、[Shiori events.py](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/packages/sdk/python/shiori_sdk/memory/events.py)。

## 差异带来的实际影响

- Meido 已经具备 Shiori 的主要生命周期，但检索质量仍主要取决于 embedding 和调用方 hints；Shiori 把更多策略固化在 Retriever。
- Meido 的角色隔离和删除恢复更严格，代价是不能直接复用 Shiori 的跨 scope schema。
- Meido 的模型失败更容易降级为规则/词法路径；Shiori 的 LLM 提取和向量路径更完整，但运行时依赖更多。
- 若要继续向 Shiori 看齐，优先级应是：生产 sqlite-vec 适配、把 tool-chain item IDs 纳入 TurnCommitted、将 intent 默认阈值/配额下沉到引擎、以及把 Markdown draft 校验和 retrieval trace 做成独立策略模块。
