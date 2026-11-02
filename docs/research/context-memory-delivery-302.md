# #302 交付验证

状态：PR #304 初审的三项阻塞已完成本地修复，等待新提交的 CI 和增量双轴复核。最后核验：2026-11-02。批准规格见[上下文与记忆对齐](../specs/shiori-context-memory-alignment.md)，实现契约见[参考](../reference/context-memory.md)，迁移与回滚见[手册](../handbook/context-memory-migration.md)。

工作分支为 `feat/302-shiori-context-memory`，起点为 `69b9c4a6f3aaeb3b3cc74c39a679822c3c6d9e10`。仅使用独立开发端口、临时数据、本地受控聊天/embedding HTTP 服务，没有正式数据、付费模型或真实渠道调用。固定上游为 `YinFengWindy/Shiori-Agent@c14152ed225954a2a50da775075fe64dbf7b8b50`，授权见[现有第三方声明](../reference/third-party-notices.md)。

## 检查结果与代码时点

本地日志位于被忽略的 `.data/context-memory-evidence/`。下表中的文件名均相对此目录；它们是本机证据，不是公开 CI 链接。检查入口已纳入 [CI](../../.github/workflows/ci.yml)。

| 检查 | 结果 | 日志 |
| --- | --- | --- |
| `npm run test:web` | 77文件、582项通过；之后没有前端修改 | `frontend-29.log` |
| `npm run build` | TypeScript、Web、桌宠构建通过 | `build-31.log` |
| 当时 CI 的全部 Python targets，另加受影响渠道与高级设置测试 | 656项通过，461.84秒；2条 websockets 弃用警告 | `ci-complete-52.log` |
| `node scripts/e2e/chat-e2e.mjs`，完整真实浏览器 | 13场景通过；开发实例正常退出0 | `browser-complete-55.log` |
| `pytest tests/python/test_memory_tools_api.py -q --tb=short -o faulthandler_timeout=60` | 5项通过，18.34秒 | `rollback-green-59.log` |
| `pytest tests/python/test_context_memory_parity.py tests/python/test_memory.py -k 'model_failure or fixed_model_credentials or old_markdown_generation or memory_api or memory_management or memory_admin' -q --tb=short -o faulthandler_timeout=60` | 13项通过，71项未选，8.11秒 | `management-green-60.log` |
| `pytest tests/python/test_proactive_context.py::test_tick_freezes_context_then_next_tick_reads_new_version_without_granting_tools tests/python/test_proactive_owner_history.py -q --tb=short -o faulthandler_timeout=60` | 5项通过，26.31秒；新增冻结宿主文档断言已纳入 CI | `proactive-61.log` |

Python 均由 `C:/Users/Elysia/AppData/Local/Programs/Python/Python313/python.exe -m pytest` 执行。52轮命令记录在本地 `.data/validate-context-memory.ps1`：提取当时 CI 中全部 Python targets，追加 `test_advanced_settings.py` 及渠道 `test_commands.py`、`test_delivery_runtime_cancel.py`、`test_external_memory_runtime.py`、`test_member_lookup.py`、`test_runtime_delivery.py`，去掉已由完整文件覆盖的单个 selector。

52/55轮执行期间有两项最后修复，不能把它们描述为最终源码的整套重跑：Markdown 草稿的普通模型异常也必须记为已知失败；显式记忆工具在 Stop 与向量返回竞态中必须保留原有数据库回滚。后者恢复现有语义锁内的纯数据库快照/回滚，不再恢复或重投影宿主 Markdown。59/60轮覆盖这两项最终差异；61轮只补主动临时上下文的测试，不改产品。前端与未受影响的通过证据直接复用。

## PR #304 初审修复

父代理确认 [PR #304](https://github.com/sawaki-zexi/meido/pull/304) 的 `6d799297e9fb63df91a1b4a11a0c9d1ef85b5af9` 已通过7项 CI。随后独立两轴审查发现以下三项阻塞，本轮修复位于该提交之后，旧提交的绿色 CI 不代表本轮修复已验证：

- 消费者的语义发布及其 ACK 现在逐条占用现有同角色语义写入锁，与工具的数据库快照/回滚串行。单条发布所需的语义生成也在该锁内，事件之间释放；关系消费、查询、宿主 Markdown 和优化器不进入此锁。桌面、主人群发言/旁听、启动恢复和重新启用均使用该协调，等待后重新核验来源。
- 初始化时将含旧向量且模型身份为 NULL/空字符串的空间标记为不可验证，保留向量、记录和原身份；重复启动不重新绑定。只读 availability 仍只检查已保存元数据。空库首次配置、已证明兼容的旧身份继续可用。
- 存储相似搜索默认只使用语义分，30天前的 preference 仍可按0.9门槛替代，同工具 procedure 按语义门槛合并；正常 Retriever 仍显式启用0.2热度权重。

新增[审查回归](../../tests/python/test_memory_review_regressions.py)已进入 CI 的 context-preparation 组。红测 `review-red-62.log` 为14失败、1通过：并发发布被回滚/来源撤销失效、缺少协调后的恢复入口、旧身份误接受、替代/合并受热度影响均复现；没有删除这些失败目标。补充空字符串空库参数后的 `review-green-63.log` 为16通过，10.57秒。

受影响组合 `review-affected-64.log` 为 **198通过，106.90秒**，包括上述16项；产品修改在该轮开始前完成，运行期间未改产品代码。准确命令（Python 路径同前）：

```powershell
& 'C:/Users/Elysia/AppData/Local/Programs/Python/Python313/python.exe' -m pytest tests/python/test_memory_review_regressions.py tests/python/test_context_memory_parity.py tests/python/test_memory.py tests/python/test_memory_engine.py tests/python/test_memory_runtime_parity.py tests/python/test_embeddings.py tests/python/test_memory_write_tools.py tests/python/test_memory_tools_api.py tests/python/channels/test_owner_memory_progress.py tests/python/channels/test_consolidation_parity.py -q --tb=short -o faulthandler_timeout=60 --durations=5
```

这轮同时复验了现有真实 HTTP Stop/回滚、查询超时、12,000条记忆期间 HTTP 响应性、已知旧模型身份导入和渠道来源撤销。前端/构建及未受影响证据复用；本地未再运行完整浏览器，新提交的 Windows CI 负责完整13场景验证。P3 候选解析去重建议保留为非阻塞项，本轮未扩展处理。

## 行为证据

- [上下文测试](../../tests/python/test_context_windows.py)、[十协议计量](../../tests/python/test_input_usage_calibration.py)覆盖稳定帧次序、缺失工具结果修复后的真实帧位置、同源估算和发送、软/硬边界、合法 minimal、当前输入与已执行工具结果保留。
- [维护生命周期](../../tests/python/test_context_memory_parity.py)覆盖记忆先于工作摘要、独立消费者 ACK、合法尾部追加、来源编辑失效、取消短提交、配置修复后的失败恢复、旧配置迟到结果隔离、删除/回滚及中断迁移。
- [检索运行时](../../tests/python/test_memory_runtime_parity.py)在12,000条记忆查询期间验证其他 HTTP 可继续处理，查询不补向量；HyDE 3秒、查询向量8秒，以及独立写入 HTTP30秒/总40秒预算分别受检。实际异步 HTTP 取消、procedure 合并的第二次向量取消均不留下迟到写入。
- [语义引擎](../../tests/python/test_memory_engine.py)、[工具 HTTP](../../tests/python/test_memory_tools_api.py)覆盖角色/来源隔离、embedding-first、独立配置、旧冒号模型身份导入、失配保留向量和结构化 CRUD 不改宿主文档。
- [渠道进度](../../tests/python/channels/test_owner_memory_progress.py)、[渠道整理](../../tests/python/channels/test_consolidation_parity.py)覆盖主人私聊/群发言/旁听各自进度与撤销；正常 running→completed 不被当作来源改写。既有主动/Drift 历史授权边界继续通过，宿主文档按临时任务快照读取。
- [手动检查](../../tests/python/test_context_inspection.py)与前端 Hook 测试覆盖阈值以下手动压缩、失败后重读真实状态、模型改变后的刷新和角色切换废弃迟到结果。

完整浏览器报告在 `.data/chat-e2e/artifacts/mvynorr3/report.json`，截图包括 `tools-and-ring.png`、`memory-scope-isolation.png`、`ring-mobile.png`、`optimizer-enabled.png`。脚本使用实际后端、SSE、SQLite、受控聊天/向量 HTTP；并非关闭记忆的旧基线：

| 场景 | 观测 |
| --- | --- |
| 工具跨阈值、下一轮及重启 | 请求次数3→2→2；没有因刷新重放副作用；覆盖版本2复用 |
| 软失败/硬失败 | 普通正文请求分别1次/0次；保留原始消息 |
| 慢记忆/慢向量期间打开设置 | 325毫秒/229毫秒 |
| 前台亲自开始的 Markdown 草稿 Stop | 150毫秒；真实模型 HTTP 关闭，游标与工作窗口不迟到推进 |
| 独立后台草稿 | 前台取消等待不取消后台任务，由对应生命周期测试验证 |
| 手动圆环和 `/compact` | 草稿保留，不创建普通聊天消息；16条既有消息仍在；角色/外部范围隔离 |
| 优化器开启、关闭、再次开启 | 两代请求；关闭立即断开旧 HTTP 并恢复 PENDING；旧快照和并发新增的三个候选均保留，再次合并/SELF 成功 |
| 语义引擎关闭 | 宿主文档仍进入请求；优化器独立工作；上下文检查未被独立优化阻塞 |

这些耗时仅说明本地受控等待期间的响应性，不代表真实供应商延迟或缓存收益。

## 失败记录与限制

首轮52项失败保留完整清单于本地 `.data/context-memory-checkpoint.md`，没有删除或跳过该范围：旧配置借用、读时向量修复、结构化文档强同步、硬失败宽泛降级等断言按批准规格替换；帧定位、取消、迁移与回滚缺陷均补行为回归。40轮2失败随后修复；45/48轮旧工具契约已迁移；56/57轮真实晚写由数据库回滚修复，59轮通过。58轮在补 import 前启动，10个失败为缺少 `run_memory_io` 导入及连带未进入工具；导入修复后的59/60轮覆盖全部失败目标。完整52轮的656项通过与最后补验分别报告，不叠加成不实的全量计数。

完整4,178项的18轮探索在约10%时中止，且与实现修改重叠，不是通过证据。53轮浏览器启动中止、没有完整报告，也不计通过；55轮完整通过。最终无已知未解决的本票测试失败。构建仍有既有大 chunk 提示和桌宠资源运行时解析提示；没有屏蔽警告。

首次交付的19份修改 Markdown 本地链接检查通过；本轮交付收据的本地链接及 `git diff --check` 再次通过。没有调用真实渠道、付费供应商、正式数据或正式端口；没有测量供应商 KV 缓存命中收益。父代理负责本轮修复的提交、推送、新 HEAD CI 和增量双轴复核；本轮未合并或部署。
