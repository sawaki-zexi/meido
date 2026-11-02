# 上下文与记忆升级、验证和回滚

状态：当前。最后核验：2026-11-02。
事实来源：[迁移代码](../../backend/app/context_memory_migration.py)、[迁移回归](../../tests/python/test_context_memory_parity.py)、[配置契约](../reference/context-memory.md)。

## 升级

1. 先停止待升级的实例，按原运行手册备份其数据目录和角色目录。分支验收只能使用隔离目录，不能复用正式账号、数据或4288/5289端口。
2. 首次启动在数据目录 `backups/context-memory-v1/` 保存一致 SQLite 备份、原 embedding/settings 配置以及角色 memory 文件。每个完成副本只写一次，临时副本不视为成功；最终完成才写 `context-memory-v2.json`。
3. 原共享 embedding 配置仅在独立文件缺失时复制到 `memory-embedding-config.json`。原主人资料配置不改写。已知旧模型身份按保存配置准确匹配（含冒号的模型名），验证维度后记录新 fingerprint；不删除不明/失配的向量。
4. 原 `.maintenance.json` 一次导入会话进度并归档为 `.migrated`；重复恢复若内容不同则保留另一份摘要命名副本。缺少可信来源证明的旧待处理事件保留为待核验历史，不作为已授权新来源重放。
5. SELF/MEMORY 仅移除旧 optimizer 托管标记，标记内外正文均保留。PENDING、快照、事件、原消息 ID、合法工作窗口及结构化记录不删除。无持续双写。

## 验证

在记忆设置中检查引擎向量模型，主人资料页单独检查原配置。引擎不可用时应显示具体原因，文档仍可读。对有完整历史的角色使用圆环或 `/compact`；草稿应保留，聊天消息数不增加。发送下一条消息或重启后应复用已保存窗口。消费故障可留下待处理进度，不能让已提交覆盖重新提取。

受控浏览器验收运行 `npm run test:e2e`；只运行记忆和优化器可设置 `MEIDO_E2E_ONLY=memory,optimizer`。脚本创建并标记自己的 `.data/chat-e2e/runs/` 工作区，选择空闲非正式端口，只关闭自己的开发进程。日志、报告和截图保存在 `.data/chat-e2e/artifacts/`，模型/embedding 均为本地受控服务。`MEIDO_DEV_PYTHON` 可指定已安装依赖的 Python，`MEIDO_E2E_BROWSER` 可指定 Chromium 路径。

## 回滚

停止实例，先另外保留升级后数据（避免丢失升级后的新消息）。使用升级前的完整备份恢复数据目录和角色目录，再切回匹配的旧代码。自动备份可用来恢复对应 SQLite、原配置和角色 memory 文件；恢复时也移除本次新建的独立 embedding 文件及完成标记，前提是升级前确实不存在这些文件。不要把旧 SQLite 与新角色文档交叉拼接，也不要覆盖正在运行的数据库。

仅恢复配置不能回滚已经发生的对话/整理。自动备份是一份首次升级快照，重试不会覆盖它；有升级后新数据时，应先评估保留或迁移范围。旧向量仍在，失配应通过明确的模型/维度选择或主动管理修复，不靠普通检索重建。

下面把升级前完整备份恢复到**新的隔离目录**，便于先验证再切换。将示例路径替换为实际完整备份与一个尚不存在的目标；`data` 与 `roles` 对应第一步分别保存的数据目录和角色目录。不会直接覆盖运行实例。

```powershell
$restoreSource = (Resolve-Path -LiteralPath 'D:\backups\meido-before-302').Path
$restoreTarget = 'D:\meido-rollback-302-check'
if (Test-Path -LiteralPath $restoreTarget) { throw '请指定尚不存在的恢复目录' }
New-Item -ItemType Directory -Path $restoreTarget | Out-Null
Copy-Item -LiteralPath (Join-Path $restoreSource 'data') -Destination $restoreTarget -Recurse
Copy-Item -LiteralPath (Join-Path $restoreSource 'roles') -Destination $restoreTarget -Recurse
Get-ChildItem -LiteralPath $restoreTarget -Recurse -File | Get-FileHash -Algorithm SHA256
```

用备份时的代码版本和指向该隔离目录的配置启动验证实例，确认角色、原消息数量和 ID、手工文档及 pending 内容与备份一致后关闭。正式切换仍通过现有实例管理手册执行，且必须在该实例已停止时替换成整套已验证数据；不要只恢复部分文件后继续运行。
