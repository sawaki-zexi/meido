# 验收确切编程成果与发布 #176

状态：本地实现和无费用夹具；独立两轴、集成与 CI 待最终记录。真实 CLI 登录/模型与 GitHub 发布均未执行。最后核验：2026-10-16。事实来源：[公开链](../../tests/python/test_coding_publication_main.py)、[真实 Git/HTTP/SQL 故障](../../tests/python/test_coding_publication.py)、[原生证据](../../tests/python/test_coding_attestation.py)、[资源与原来源恢复](../../tests/python/test_coding_publication_recovery.py)、[React](../../web/src/features/coding/CodingPublication.test.tsx)。接口见[reference](../reference/owned-coding-tasks.md)。

## 主人使用

1. 先完成已获账号/费用许可的 CLI profile 核验，登记可信仓库与固定验证。按需单独开启 localCommit、push、draftPr；发布 remote 必须是唯一 GitHub HTTPS 地址。配置许可本身不会发布。
2. 明确启动本地任务，等待真实 `validated_diff`、完整宿主证据与资源退出。在角色的编程任务页审阅 diff、原角色/来源/session、base/当前HEAD、验证命令与时间，填写作者/邮箱/提交说明，再勾选并批准本次提交。
3. 原任务提交窗口到期或重启后无法证明期限时，保留修改但不提交。回原来源明确新 continue，理解它会新计 attempt、可能再次消耗 CLI 配额；不需要时可自行保留成果。界面刷新不会续期或自动启动 CLI。
4. 单独设置发布凭据。只有已证实的宿主 commit 能准备 Draft PR，审阅确切 SHA、remote、base、唯一分支与完整 title/body 后再一次批准。首次真实发布仍须另获该测试仓库、确切 HEAD 与真实账号事务许可；当前验收只用假 token。
5. unknown 时只读核对原操作。已证实 push 且 PR 尚未派发，才会提供“审阅剩余 PR”，再次批准只创建 PR；已经派发却无法证明的事务不会重试。取消停止本地资源，不能承诺远端回滚。

## 无账号复现

收集/import main 前建立三个全新 MEIDO 根，移除 MEIDO_PROJECT_ROOT；localhost-only DNS/socket/asyncio guard 必须覆盖整条命令至所有 Runtime/Job/reader/writer/HTTP/渠道 owner 退出。使用新的短 basetemp，公开任务额外加普通 AppData 层级。专用 CLI 仅本机 Python/Rust 合成进程、假认证；Git 使用临时仓库与 bare，GitHub REST 使用真实 localhost HTTP+SQLite 夹具。不可把 fake CLI 或本地协议验收写成真实 Codex/Claude 模型任务。

```powershell
# 启动器先完成上述隔离与 guard，再运行；原生7项需已构建的固定bundle/合成launcher。
$env:MEIDO_TEST_HERDR = 'D:/meido-artifacts/coding174-herdr-bundle/herdr.exe'
$env:MEIDO_TEST_CODING_CLI = 'D:/meido-artifacts/coding174-toolchain/interactive-fixture.exe'
python -m pytest tests/python/test_coding_publication.py tests/python/test_coding_publication_main.py tests/python/test_coding_publication_recovery.py tests/python/test_coding_attestation.py -q --basetemp=D:/fresh-176-owned
python -m mypy backend/app/coding_tasks/publication.py backend/app/coding_tasks/github_publication.py backend/app/coding_tasks/evidence.py backend/app/coding_tasks/artifact_budget.py backend/app/coding_tasks/api.py --ignore-missing-imports --follow-imports=silent --check-untyped-defs
npm run test:web
npm run build
git diff --check
```

## 已记录的失败与修正

- 首个交叉组 `39 passed / 1 failed` 捕获 owner worker 写入成功事实与 Runtime 查询间竞态：真实 Draft PR 已落平台、完整证明已持久，旧 `running` 副本却覆盖为 unknown。现在同 SQLite 事务读取最新业务后复制 result+ACKdirty。保留失败 XML，收到响应的发布测试使用真实 HTTP POST 屏障稳定覆盖该顺序；原生 attempt 相同竞态也以真实 Runtime/SQLite 屏障 RED→GREEN。
- Ledger 真实 continue 曾继承上个 attempt 的期限/证据而拒绝新预算，随后旧操作回执又被错误抑制。现在仅继承 task 身份/session/host HEAD，原 attempt 和独立操作各守其预算、来源与 sent 事实；新 attempt 必须由主人明确发起。
- SQLite trigger 覆盖 approval 插入回滚、Runtime ID 写入、复制前失败、ACK 后 dirty 清理失败；Runtime 接受后返回丢失只查原 key，不重跑。同一真实 Git commit 只产生一次。最终业务写入失败时，Runtime 终态触发独立 owner 重新收尾，unknown commit 仅按预计算 SHA 只读恢复。
- 晚到真实渠道 receipt、重启 running 资源日志、原生清理失败仍排空独立 broker、凭据换代先停旧 owner、Git cleanup 失败仍关闭 HTTP 均保留实际 RED，再进行修正。probe 新 `drain()` 只收尾不关闭 DB，统一所有 owner 确认后才 close，失败可重试。
- 宿主关闭曾把待交付 outbox 误记为来源撤权。现在 shutdown 完成 copy/ACK 与资源收尾、暂缓交付；重启只恢复原来源结果，不重新执行 CLI。来源真正撤销仍抑制交付，已经 sent 的事实不会被本地 dirty 投影降级。
- push 之后远端 base 分支推进的真实 bare/HTTP 夹具曾证明 PR 仍被 POST。现 dispatch 前再核 base SHA，变化时保留已发生 push，禁止创建 PR；平台在派发后发生变化仍只能记 unknown，不能承诺 GitHub 分支原子冻结。
- Git filter 夹具在旧快照检查中确实写出 marker；目前在任何 status/worktree checkout 前拒绝 filter 配置。私有硬链接和超2GiB保留文件在写事务前拒绝。固定验证不通过、文件/index/模式/HEAD变化不能借旧 validatedHead 发布。
- 原生适配器完整 snapshot/expected HEAD 两路测试与 owner 服务的宿主 commit→精确 session continue 分开记录；Herdr boundary 夹具使用真实受管编辑进程/Git，不等于实际 Herdr/真实账号任务。原7项固定 Herdr+合成 CLI 是独立原生兼容验收。

作者本地 XML 位于 D:/meido-artifacts/coding176-*.xml，各轮不能相加或视作最终 HEAD CI。完整前端208项、TS/Vite已过，包括晚到凭据 GET 不覆盖新保存修订的 RED→GREEN；最终相关组与两轴结论以固定交付记录为准。所有真实账号、费用、实际首次/resume CLI 权限与 GitHub 发布仍需要对应许可和证据。
