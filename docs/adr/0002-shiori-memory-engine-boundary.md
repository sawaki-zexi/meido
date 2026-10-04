# ADR-0002：采用 Shiori 风格记忆引擎边界并保留角色硬隔离

状态：已接受

事实来源：已确认规格 [`memory-system-shiori-parity-spec.md`](../specs/memory-system-shiori-parity-spec.md)；Shiori-Agent `096a4ecbfbcdae3ba77c179fedb852721846fcb9`。

## 决定

- 对话、工具、后台处理和管理代码通过统一的 `MemoryEngine` 请求/结果契约使用记忆能力；默认 SQLite 实现只负责实现该契约。
- Meido 继续使用单角色唯一会话，不复制 Shiori 的插件宿主和群聊 scope。
- `role_id` 作为结构化记录、来源写入、去重、embedding 空间、事件消费、替代记录和管理操作的硬隔离键。
- SQLite 是结构化记忆权威来源；角色级 Markdown 文档继续作为可读上下文层，由独立 maintenance 与 Optimizer 管理。

## 原因

Shiori 的 SDK 契约将调用方与默认记忆实现分开，允许对话生命周期不依赖存储 schema 和具体检索策略。Meido 当前只有一个引擎，不需要照搬插件注册、RPC 或多渠道运行时；采用稳定契约仍能让回复前检索、回复后处理、Markdown maintenance 和管理操作共享同一端口。

Shiori 基线将 role/scope 放在 `extra_json`，且唯一索引未包含 role。Meido 的产品边界是一个角色对应一个独立记忆空间，因此把 `role_id` 放在 SQLite 的受约束列和索引键中，使跨角色隔离由存储查询强制执行，而不依赖所有调用方正确解释 JSON 扩展字段。

## 后果

- 新记忆能力应先通过 `MemoryEngine` 契约表达，不能让调用方直接访问 SQLite 或 Markdown。
- schema 与迁移必须保留现有记录，并把 `role_id` 纳入所有可能跨角色影响记录的操作。
- 不引入 `channel`、`chat_id`、成员档案或跨角色共享语义。
- 当前的 `MemoryService` 可作为迁移 facade 保留，直到所有调用链转到统一契约。
