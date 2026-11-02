# 隔离验证记录

日期：2026-11-02。状态：实施前固定基线。源码提交与范围见[研究报告](shiori-context-memory-20261102.md)。本文件仅记录研究阶段的真实运行结果；#302 的后续实现验收见[交付验证](context-memory-delivery-302.md)。

## 环境与数据

- Shiori：`D:/shiori-context-memory-research-20261102`，`c14152ed225954a2a50da775075fe64dbf7b8b50`。
- Meido：`D:/meido-279-chat-architecture`，`69b9c4a6f3aaeb3b3cc74c39a679822c3c6d9e10`。
- Python 3.13；Shiori 使用该研究工作区新建的 `.venv`，SDK 与 host-testing 以本工作区 editable 包安装。没有修改全局 Python 依赖或产品源码。
- pytest 临时目录保存测试数据库；模型与向量响应受控，没有真实模型调用或真实渠道。
- Shiori 仓库默认 `-W error`，这暴露了其 SQLite 连接关闭警告；没有屏蔽该警告。

## 上游现有测试

工作目录为 Shiori 研究工作区。

```powershell
& '.venv/Scripts/python.exe' -m pytest tests/backend/session/test_manager.py tests/backend/core/memory/markdown/test_consolidation.py tests/backend/core/memory/markdown/test_host.py tests/backend/agent/core/passive_turn/test_compaction.py -q
```

结果：28 passed / 1 error。错误出现在 `test_minimal_preflight_counts_required_reply_schema` 的 setup，来自垃圾回收阶段 `sqlite3.Connection` 未关闭的 `ResourceWarning`，转换为 `PytestUnraisableExceptionWarning`。

对相关压缩模块单独运行：

```powershell
& '.venv/Scripts/python.exe' -m pytest tests/backend/agent/core/passive_turn/test_compaction.py -q
```

结果：`6 passed in 1.35s`，exit 0。不能据此抹掉前一组资源关闭错误。

补充运行文档、整理与普通检索边界：

```powershell
& '.venv/Scripts/python.exe' -m pytest tests/backend/core/memory/markdown/test_host.py tests/backend/core/memory/markdown/test_consolidation.py tests/backend/agent/retrieval/test_default_pipeline.py -q
```

结果：`1 failed, 7 passed in 2.04s`，exit 1；`test_extraction_snapshots_the_group_even_when_the_model_updates_one_field[group_note]` 运行阶段及退出阶段均出现未关闭 SQLite 连接引起的 warning-as-error。未修改上游去消除这个问题。

## Meido 现有回归

工作目录为 Meido 研究工作区。

```powershell
& 'C:/Users/Elysia/AppData/Local/Programs/Python/Python313/python.exe' -m pytest tests/python/test_context_windows.py tests/python/test_memory_engine.py tests/python/test_memory.py -q
```

结果：`119 passed in 28.40s`，exit 0。此结果说明现有行为符合现有测试，并不说明它们已经与上游行为一致。比如 Meido 的“尾部新增使维护草稿失效”本身就是现有测试要求。

## 本轮对照探针

文件均在研究目录，不是产品测试/源码改动。

- Shiori 探针：研究机器上的本地未提交文件 `D:/meido/.data/plans/shiori-context-memory-research-20261102/probes/test_shiori_architecture.py`。
- Meido 探针：同目录本地未提交文件 `test_meido_architecture.py`。下列命令和结论保留为阶段记录，不把本地路径当作公开附件。

```powershell
# 工作目录：D:/shiori-context-memory-research-20261102
& '.venv/Scripts/python.exe' -m pytest 'D:/meido/.data/plans/shiori-context-memory-research-20261102/probes/test_shiori_architecture.py' --asyncio-mode=auto -q

# 工作目录：D:/meido-279-chat-architecture
& 'C:/Users/Elysia/AppData/Local/Programs/Python/Python313/python.exe' -m pytest 'D:/meido/.data/plans/shiori-context-memory-research-20261102/probes/test_meido_architecture.py' -q
```

最终结果分别为 `3 passed in 3.15s` 和 `3 passed in 2.98s`，均 exit 0。

| 探针 | 实际验证边界 |
| --- | --- |
| Shiori 消费者失败/重启 | 真正执行 Markdown 提取、文件与会话状态保存；关系消费者故障后切点可提交；新 SessionManager 读取摘要/切点/payload；重试消费者不重新提取或重复发布 |
| Shiori 顺序/追加/低预算门槛 | 摘要调用时真实记忆游标已覆盖；摘要期间追加新轮次仍能提交旧前缀；重开后低预算请求不调用摘要；请求 body 是受控小输入，并非真实长历史模型端到端 |
| Shiori 动态 frame 排列 | 使用真实 MessageEnvelopeBuilder，变化只出现在既有 system+history 之后 |
| Meido 普通检索补建 | 真 MemoryStore 中造 8 条缺向量记录；一次 recall_async 触发查询 + 8 条补建，返回前全部写入；下一次仅查询 |
| Meido pendingEvent | 真正提取并保存文档及游标，未 ACK 时 ensure_coverage 为 false；后续 ACK 后 true，提取不重复 |
| Meido 动态 frame 排列 | 真实 MeidoProvider 生成 rich 请求，动态 frame 在全部历史之前 |

探针初稿有两类测试编写问题，均只修研究探针：误把 `(messages, result)` 当单值，以及未固定时间；第二次运行还直接调用了压缩执行器来模拟低预算普通请求，修为真实 `RequestCompaction.ensure` 请求门槛后通过。没有修改上游代码使探针满足预期。

## 研究阶段尚未完成的验证

以下描述实施前的范围；后续受控浏览器证据见[交付验证](context-memory-delivery-302.md)。真实供应商和正式数据仍不属于本轮自动化验收。

- 没有运行真实供应商或真实渠道端到端，没有测供应商的实际首 token/排队时间。
- 没有测生产角色锁占用、后台优化与普通回复竞争的具体耗时。
- 没有定位现场 `WindowConflict` 的精确谓词。
- 没有以真实供应商 A/B 测量动态 frame 调整带来的缓存命中率变化。
- 没有全量执行 Shiori 的插件/宿主/SDK 所有测试。
- 前轮浏览器 E2E 关闭了记忆/embedding，不能补足上述缺口；后续交付必须覆盖记忆开启的完整路径。
