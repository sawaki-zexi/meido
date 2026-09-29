# Shiori-Agent 记忆系统研究

研究基线：`main`，提交 `6e560dbdceaf09a317ba7aec11968136ae6e6e8f`（2026-09-29）。以下内容按源码核对；Shiori 持续演进，采用前应再次确认接口和 schema。

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
