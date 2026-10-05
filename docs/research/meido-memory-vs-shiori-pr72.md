# Meido 与 Shiori 记忆模块差异复核（PR #72）

状态：实现前基线为 Meido `origin/main`（已包含 PR #71），Shiori 基线为提交 `096a4ecbfbcdae3ba77c179fedb852721846fcb9`。

## 本次补齐内容

- Meido 的 `TurnCommitted` 现在可携带显式工具记忆 item IDs 和 tool metadata；`MemoryWorker` 将 item IDs 传给 post-response，纠正逻辑不会误处理受保护条目。Shiori 的对应来源是 [`post_response_worker.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/semantic/post_response_worker.py)。
- Meido 为五类 intent 下沉默认 lane 分数、类型配额和 4000 字注入预算；调用方 hints 只能收紧配额。Shiori 的对应策略在 [`query.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/engine/query.py) 和 [`retriever.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/semantic/retriever.py)。
- Meido 增加 `SQLiteVecIndex`，按 embedding dimension 建立持久 `vec0` 虚拟表，查询时按 `role_id` 过滤；sqlite-vec 不可用、加载失败或索引查询失败时回退 SQLite 扫描。Shiori 的存储基线也使用 sqlite-vec，并在 [`vector.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/plugins/default_memory/backend/semantic/store/vector.py) 提供向量路径。
- `RetrievalCompleted` trace 现在包含实际注入数、裁剪数、预算、类型配额和向量后端；向量重启、维度变化和删除行为保持索引一致。

## 仍然不是一比一复制

- Meido 仍是单角色单会话，不实现 Shiori 的 channel/chat/member scope。
- Meido 的工具记忆 ID 通过 `SendMessageInput.toolMemoryIds` 传入，尚未实现完整的 Agent tool-call 生命周期和 Shiori 的 tool result 对象。
- Meido 的默认阈值和配额是基于 Shiori 策略的单角色适配，不包含 Shiori 的全部 relative-delta、forced evidence 和复杂 procedure trigger。
- Meido 的事件观察仍是进程内有界缓存；Markdown 游标和 SQLite source key 才负责重启恢复。

来源：

- [Meido memory_events.py](../../backend/app/memory_events.py)
- [Meido memory_engine.py](../../backend/app/memory_engine.py)
- [Meido memory_store.py](../../backend/app/memory_store.py)
- [Meido memory_service.py](../../backend/app/memory_service.py)
- [Shiori MemoryEngine SDK](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/packages/sdk/python/shiori_sdk/memory/engine.py)
- [Shiori memory events](https://github.com/YinFengWindy/Shiori-Agent/blob/096a4ecbfbcdae3ba77c179fedb852721846fcb9/packages/sdk/python/shiori_sdk/memory/events.py)
