# Drift 技能作者与执行验收记录

状态：当前 / #179 功能分支的本地合成验收记录；Windows 冻结组合、后续窄修及 WSL 核心复核已结束，待独立两轴审查，未合并。

事实来源：[具体规格](../specs/proactive-drift-spec.md)、[ADR-0018](../adr/0018-explicit-autonomy-and-drift-grants.md)、[操作手册](../handbook/drift-skills.md)、[作者闭环](../../tests/python/test_proactive_drift_authoring_api.py)、[MCP](../../tests/python/test_proactive_drift_mcp.py)、[投递](../../tests/python/test_proactive_drift_delivery.py)、[恢复](../../tests/python/test_proactive_drift_recovery.py)。最后核验：2026-10-16；准确基点 `fa122c3d5986515028144c1a6285e36425e7b9cf`，开工时该基点两套完整 CI 均通过。

## 已提取的行为

正常主人聊天显式 `$create-drift-skill`，脚本化本地模型实际读取分发 S06 正文中的三份模板，通过真实 FileTools 写入当前角色 workspace。宿主从原主人 user row、实际激活 builtin hash、能力快照、调用 ID 和实际已发布文件身份登记作者证据；之后的真实时钟 tick 发现并消费同一 bundle。两次真实 run 与重启共用 work 文件及 SQLite nextAction，不重新生成产物。

作者范围与执行许可分离。作者可仅持 FileTools 而声明待主人配置的 MCP/Shell；同一产物补齐许可后可执行。扩大作者范围后旧证据不自动扩大，新预览和主人明确采用可重用同一 hash。opaque previewId 持久绑定实际文件及祖先目录身份；相同字节换目录的 ABA 反例先返回错误 200，再修为旧预览 409、新预览明确采用 200。

MCP 许可目录只读实际主人 `tool_search → mcp_add` 握手留下的缓存。后续真实 Drift 对同一合成 stdio 服务握手，显式激活依赖，stage/persist/publish 新能力版本；下一次真实模型请求才包含所需操作。owner 多许可的无关 poll 不会进入技能 schema。真实目录定义变化、版本持久化 SQLite trigger 失败、挂载后服务停用均拒绝实际 get_events。所有原 MCP 进程和 owned 资源在终态退出。

有限 Shell 真实执行宿主 Python `--version`，自造 command 参数被 schema 拒绝；使用原 tick TaskRuntime 身份、原 deadline、最小环境及 ProcessExecutor。原生 Windows 专项还在真实主人 HTTP 输入时取消 held Shell 根进程及子孙进程，完成 owned join，不依赖 MCPhost 存在。此机制不宣称同账号操作系统安全沙箱。

Drift 经原 main wrapper 调用真实 MemoryEngine，在当前角色只读 context 范围内至多两条命中，外角色记录不泄漏，原存储不变；RetrievalCompleted 记录 read_only。web_fetch/web_search 使用原 NetworkTools，不绕 SSRF；本地合成 HTTP peer 被拒绝，未接受业务请求。history 使用原 MessageHistoryTool，返回原本角色普通聊天 source_ref，外角色与内部自主消息不入范围。独立角色资料资源只能用显式 @resource ID 读取，不能写回设定。

一次发送先持久占用唯一 claim，再提交真实桌面 canonical message 与 sent receipt；后续模型 schema 和实际 stale dispatch 同时收窄为 write/edit/finish。同角色已有公共附件经原上传接口创建，再按本 run 冻结 ID 和实际字节身份发送；外角色 ID 拒绝，不向模型暴露本地路径。确定失败与真实合成 localhost 断开造成的不明回执分别保留 failed/uncertain；四种 finish 与真实 claim/receipt 精确匹配。并发相同 finish 只增加一次进度，冲突拒绝。

重启只消费原 Runtime 结果、ACK、原回执及本地 journal/进度投影，不重做模型、Shell 或消息。真实 claim 后 SQLite trigger 拒绝 uncertain 回执写入，原 Runtime abort/ACK 完成后仍留下 dispatching：该反例先 RED，再通过 receipt-only 恢复把同一 intent 保留为 uncertain，不补 finish、不重发。没有 definitive receipt 不能证明未发送，也不解封后续发送。

写入使用原 FileTools 受管 I/O、取消 shield/join，增加 opt-in 配额策略。实际旧文件、临时新副本、目录及未知文件都占预算；并发争抢最后一份空间仅一个写入成功。临时文件被同路径外来 inode 替换时不得删除，保留原 journal 和预留；恢复原 inode 后才能回收原孤儿。custom 8 MiB UTF-8 limit 同时约束 schema、实际写入与读取；普通 FileTools 默认仍读取原动态 4 MiB 常量。STATE 大小写别名不能覆盖宿主 state 投影。

原 24 次角色日额度中最多四次 Drift，三小时冷却跨重启保留；失败和 token 预算耗尽都不退尝试。实际请求预留 16 次 × 512 token 后累计 8192，没有第十七次模型调用或假 finish。每次仍受原 600 秒 TaskRuntime 和 20 步上限。文件与实际 ended Runtime 日志纳入原全局 retention，pending projection/journal 和未 ACK 结果不会被当成已结算删除。

## 探索失败与证据边界

以下是不同字节或不同场景的独立记录，不能把局部通过数累加为同一完整验收快照：

- 最初作者模板、MCP 下一模型 schema、Shell 目录、进度投影恢复、作者范围扩大和同字节目录 ABA 都有对应真实 RED；夹具缺 profile、错误模型方法签名、未先 tool_search 加载 mcp_add、把旧目录移入同一发现根、错误能力版本 trigger 等 setup 错误单独保留，不冒称生产根因。
- FileTools 扩大组最初为 50p/3f/2guardE：三项旧动态 limit 回归由 opt-in 默认值捕获常量造成，修复后相关新旧组 61p。另两项旧 API 夹具意外启用记忆维护，产生 dashscope.aliyuncs.com 连接尝试（3 次和 1 次），均被内外 local-only guard 阻断，无真实请求；精确复用已审 memory-disabled 夹具，不改变生产记忆或清除 blocked 历史。不能把该历史写成整轮 guard0。
- 首次探索性完整 proactive 组合为 202p/4f/1skip/1warn，590.79 秒。该组启动后 WIP 字节继续变化，因此只用于发现问题，不是最终固定快照。三项旧 poll 用例缺作者范围时应维持 drift_dependency_pending，已修为先检查该依赖，保留原断言。
- 该组剩余 poll-entered 5 秒等待失败，隔离同例为 1p/9.16 秒。原 runner 使用 pytest 默认临时目录保留，后续多轮后原 fixture DB 被 pytest 自动清理；原 XML/trace 仍保留，但没有原 RPC/peer 阶段观察，根因未证实。没有借 #178 冷启动历史推断同因，也没有调大任何生产或夹具超时。后续 runner 使用每轮唯一 basetemp，实际数据保留到全部 owner 退出。
- 原生 SQLite BUSY/LOCKED 的可控反例证明恢复须短暂延后，保留原 50ms timeout、同 key、dirty/ACK 和独立 owned 关闭；新增入口只捕获带原生 BUSY/LOCKED 码的短暂争用，初始恢复读的非 BUSY SQLite 错误原样抛出，既有逐条恢复处理保留。忙库期间原 Runtime 权限监视器可合法取消任务，结果为 aborted/uncertain，不伪造合法 finish_uncertain。此问题与 #178 旧 ACK 测试时序不是同根因结论。
- receipt-write 中断先有模型夹具遗漏 max_tokens 参数的 setup 失败，真实协议修正后才观察到 dispatching 未恢复的真实 RED；随后原 delivery+builtin 组 6p/45.60 秒。超量读取与大小写 state 别名 2 个真实 RED 后，文件/资源/builtin 组 9p/46.16 秒。
- 后续完整冻结组合为 222p/1skip/1f/1warn，669.87 秒，558 输入 hash 前后完全一致。唯一失败为旧协议用例在真实空源 tick 已 no_events/RuntimeACK 后，用旧 origin 手动重新构造 DecisionTurn；新的 pending→A01 CAS 正确拒绝终态复活。原 poll 五秒等待用例及三项 pending_dependency 用例本轮通过，但不能据此回溯原 poll 失败根因。
- 协议夹具保留真实空源 no_events/model0，并新增旧终态拒绝；原空快照/get_content/draft/finish 并发、幂等和冲突检查整体移入第二次真实 on_clock 准入、原 Runtime 仍 live 的协议 hook。原 14 条语句的完整 AST（含所有异常及断言）相同，closure 异常重新抛给 pytest；检查后故意在投递前中断，再验证原 Runtime 副本、ACK 与 owned 释放。该 hook 不表示正常空源会进入 A01 模型循环，真实空源→Drift 由独立 S06 闭环证明。没有为通过夹具放宽生产 CAS。
- 完整组退出后才进入终态自查：缺技能 Shell 规则时错误显示 available 的两种作者 scope、底层 FileTools error 未同步外层模型错误位、definitive failed 桌面回执未释放 64 KiB 未用恢复预留，共得到 4f/1p/1warn，31.33 秒。窄修仅补齐缺依赖/作者 Shell 声明校验、Drift tool-result 错误位、原 mode=drift 且匹配真实 failed receipt 的结算；A01 的保守规则和 uncertain 占用保留。
- 第一轮窄相关组 51p/1f/1warn，160.45 秒，唯一失败为新协议 hook 在异步 loop 直接执行原同步 asyncio.run 断言，另有 coroutine 未等待 RuntimeWarning；这是夹具装配错误。用原 app._owned 在线程中执行并 join 原同步检查后，该协议用例隔离通过。另一个同因 Shell 目录反例证明 sidecar 已声明规则、即使技能不调用 shell，未知规则也须整项 pending_dependency；1f/1p/1warn，12.53 秒后最小扩大依赖检查条件，没有扩大执行权限。
- 普通共享模块固定快照组为 349p/1f/2guardE/1warn，292.21 秒。准确 fa122c3 基点隔离同三例复现为 2p/1f/2guardE，15.48 秒：tool_discovery 与 message_history 两项 API 夹具缺记忆隔离，分别产生 2 次、3 次 dashscope.aliyuncs.com 连接尝试，均被 guard 阻断，没有真实请求。精确复用既有 memory-disabled 夹具后，两个完整模块为 8p/1warn，17.57 秒，原业务断言保持。
- 该共享组唯一业务失败与基点相同：旧 background_shell 断言要求 Python 子进程在 0.4 秒硬期限内执行脚本并写标记。实际 Popen 观察先得到同一 1f/1warn，5.24 秒，证明真实子进程已创建而 marker 未写；生产 ProcessExecutor、TaskRuntime 与 Shell 相比基点零改动。测试改为委托原 Popen 并观察成功创建的真实 PID、原 argv 和唯一次数，保留 0.4 秒 hard deadline、timed_out、duration < 1.5、原连续 poll 与日志断言。完整 background_shell 与 API 模块为 36p/1warn，48.09 秒。这是旧测试观测修正，不是 #179 生产修复。
- 进度投影的真实目录替换反例为 1f/1warn，12.79 秒：SQLite 已提交 finish 后、初始目录检查与实际 FileTools 写入之间替换 Drift 根，原实现覆盖了 foreign state。修复将原进度 row 的不可变 root_identity 传入实际授权和 I/O 门，并在清除 pending 前再次检查；不从新目录重建信任，也不为空身份回退。首轮恢复/配额/作者/采用专项为 15p/1warn，80.17 秒；重建应用恢复原同版本与旧记录缺身份拒绝的最终检查另行记录。
- 最后的重建应用检查首轮为 10p/1f，58.22 秒：新增夹具误把 silent 场景不存在的 delivery row 转为 dict，属于观测 setup 错误。修正为比较原完整 delivery 行集合（此处为空），没有变更生产。随后恢复/配额两个完整模块为 11p/1warn，54.66 秒；foreign sentinel 不变，原目录恢复并重建应用后，同一版本进度完成投影，原 autonomy_runs 整行、Runtime 副本/ACK、delivery 集合和模型两次请求均不变。旧 row 缺 root_identity 时不授权写入，pending 保留。

每轮 Python 在 collection 前设置三份新的 MEIDO data/roles/owner 根并移除 MEIDO_PROJECT_ROOT，内外 local-only guard 保持到所有 owned 关闭。只用本地脚本模型、合成 stdio/HTTP、实际 SQLite 和本地进程；没有真实账号、付费模型、服务读取/ACK 或外部业务投递。

## 检查状态

冻结前的最后专项：MCP 故障矩阵 4p/37.91 秒，只读资源与并发 finish CAS 2p/18.52 秒；它们不替代完整组合。完整 Windows 组合包括全部 test_proactive 模块，558 个生产/测试/前端及构建输入的 SHA-256 为 `52aa145c0155cd7ae94af0963dcb38086bddaf9c656feeacde63526cb1d7e181`，检查前后相同；运行期间只编辑文档和外部验收记录。该组合为上文的 222p/1skip/1f，后续协议夹具与三项自查窄修须单独记录最终输入和验证范围。

最终源重新冻结为 558 输入 `adfbb680966ed9bb47e0dfac5c06d16a746df7ace07541bd9e32685364c6b178`；全部 Drift 模块、完整原 A01 tool_protocol 与 retention 共 70p/1warn，282.31 秒，运行前后 hash 相同，全部 owner/guard 收尾完成。该组包括原空源协议 hook、三类终态窄修、未使用 Shell 依赖、新实际 socket.connect 观测及双动作响应零执行/原 step-token 预留。它不替代旧完整 222p/1f 组合的历史，也不和此前零散通过数相加。普通共享模块回归、WSL 核心闭环和最终类型检查另行记录。

后续投影补丁快照为 `68326210edd97a090de1409e49d59352214dd9be8d79757c11c7ed2ddba46d2f`，首轮 15 项检查前后相同。与 adfbb 的输入清单完全一致，仍是 558 个源码、测试、前端与构建输入，未新增文档或扩大选择范围；仅两处投影生产文件、恢复测试及上述三个旧测试夹具有字节变化。因此原 70 项与 349 项结果不能写成已经覆盖最后的投影补丁。

最后只增强恢复夹具后，558 输入为 `d7d40e09f8c03c4709eb2734048b0f157a1973635a9423c3a6b5230f71f4e470`；Windows 最终 11 项检查前后相同。各轮唯一隔离根和 XML/log 保留在外部验收工件中，不把独立轮次加总为一个完整测试数。

同一最终输入在 WSL 的 kali-linux、既有 Python3.12.7 venv 上通过 17p/2warn，227.45 秒，检查前后 hash 相同，全部 owned 资源关闭。覆盖正常 S06、同产物跨次进度、四种实际 MCP 故障、恢复与原目录替换/缺身份、真实固定 Shell 及真实单发送与 failed/uncertain。两条警告为原 conftest 重写提示和该环境 Starlette/httpx 弃用提示；原日志保留，没有跳过或放宽失败断言。

前端叶子最终 216 tests / 38 files / 32.45 秒，TypeScript 与 Vite 99 modules / 1.39 秒通过，120 个输入 hash 前后相符，所有进程退出。保留旧 A01/S08 流程，采用只能由主人明确确认当前 opaque previewId；409 清旧预览，503 使用原 ID/版本重查，ABA 晚到响应隔离。最终源不再改该九个前端文件，交接前另核 hash。

最终窄类型检查覆盖 proactive_runtime 与 Capability/discovery/MCP 必要接缝，28 个源文件通过；compileall 覆盖 backend/app 与 tests/python 通过。宽类型检查使用相同已安装依赖与 `--check-untyped-defs --ignore-missing-imports --follow-imports=silent`。准确 fa122c3 基点为 88 个诊断；最后代码为 87 个、3 个文件，归一化路径及嵌入行号后新增 0、减少 1。原 main/SessionStore/FileTools 既有宽检查仍不通过，不能写成全仓库类型通过。

## 独立审查后的四项修正

冻结源 `ecde3b4ebd01d4886ad051eef88fb8991eda01c0` 的完整规范与规格两轴审查各发现两个 P2；按 [公开修正计划](https://github.com/sawaki-zexi/meido/issues/179#issuecomment-6216517351)在同一功能树修正。原独立审查、真实故障数据库和未完成的原生崩溃轮均保留；崩溃轮不计独立第三项 finding，不宣称其 owner 正常 join。

永久行为回归的初始纵切分别记录，不能相加成一轮完整验收：

- 真实 S06→桌面 canonical sent→原 Runtime 副本/ACK 后仅注入本地 receipt 字段丢失，永久回归得到 `NOT NULL constraint failed: autonomy_deliveries.error`，1f/1warn/12.59秒；只将成功恢复的 error 从 None 改为空字符串后，同测试1p/1warn/10.24秒。原 intent、run/finish/进度整行、canonical消息唯一及模型三次请求均保留。
- 三十二条合成历史 unknown 后，第33条真实原 run/消息/ACK 在应用重建后仍未恢复，正式永久RED为1f/1warn/11.26秒。最初夹具仅重排claim而没重排run，目标先被查询的1f/12.04秒属于装配错误，单独保留；没有把该轮计入生产RED。修复持久单行INTEGER扫描位置、按delivery rowid有序查询并逐条在lookup前推进；同两项回执测试2p/1warn/15.43秒，批次仍≤32、unknown不删除、无模型或发送重放。
- 正常角色PUT仅启用Shell专用开关与精确解释器白名单，旧通用enabledTools误判导致公开选项不可选，永久RED1f/1warn/9.17秒。统一公开选项、技能目录和冻结执行授权的角色开关解释后，同原完整固定Python版本进程及非法command拒绝链1p/1warn/10.48秒。普通Shell/ProcessExecutor/TaskRuntime不改。
- 真实静默finish及Runtime ACK后，八日完整工具结果仍含激活指令，永久RED1f/1warn/10.84秒；接入七日结果清理和九十日已解决finish压缩后，同测试1p/1warn/9.65秒。调用身份/hash墓碑、当前技能进度保留，未ACK/unknown/投影职责另有保护回归。

各轮新数据根、唯一basetemp、原日志/XML及输入前后哈希位于 `proactive179-fix-<阶段>-manifest.json` 等外部工件；上述正式RED/GREEN均内外local-only guard0、全部main owner自然join、运行前后输入零变。后续必要保护组、完整Drift/A01协议/retention及跨平台结果另按最终固定输入交接，不将旧70/349/222项或未变UI216项当成新修正已重跑。

保护组17p/1warn/74.99秒：首条reader异常后跨应用继续、Shell专用开关/白名单/Drift grant/技能preset四种缺项、真实Runtime已终态但尚未ACK、原投影pending、真实unknown投递claim与finish保护均通过。旧版本已经compacted但遗留finish详情的升级对照另有1f/1warn/10.55秒→同测试1p/1warn/9.60秒；它属于同一保留策略缺陷，修正让已过期且已解决的旧标记行同样清理，不延长或缩短原期限。

最终Windows必要集合覆盖全部12个Drift模块、完整A01协议/retention、普通Shell/API与能力/角色配置，130p/1skip/1warn/327.40秒。明确kali-linux既有Python3.12.7的核心修正闭环8p/2warn/107.37秒，两轮全部owner自然关闭、内外guard0、输入前后零变。Windows既有`test_shell_tool.py::test_absolute_allowlist_preserves_case_sensitive_path`按平台跳过，reason为requires a case-sensitive executable path；不将skip计通过。WSL的两条pytest警告为既有conftest重写与Starlette/httpx弃用提示，启动时本机代理警告原日志保留。

本轮选择器递归包含backend/tests/web/src，排除__pycache__/pyc/pyo，共554输入；Windows/WSL相同SHA256 `ac811dcb708adbcd103e020e7d5771f3ed6bebb9acb800671e99e496b45e0813`。这与原558选择器不同：原独有ci.yml、package.json、package-lock.json、requirements.txt、tsconfig.json、vite.config.ts、vitest.config.ts；新独有backend/__init__.py及两个新增永久回执/retention测试。原558路径全存在，完整差集/原始SHA留在交接，不以数量差推断删除文件，也不把新hash当原558同集合hash。120个原前端输入零变，保留原216项与构建证据，没有重跑UI。最终窄mypy同原flags的28源通过；宽类型旧88→87仅是先前证据，本轮不冒称重新全仓通过。

## 明确限制

本票桌面投递，外部主人目标目录、平台回执查询和真实账号验收由 [#180](https://github.com/sawaki-zexi/meido/issues/180) 沿现有 ChannelHost push seam 承接。不伪造普通聊天主人 user row，不改通用 MessagePushTool 来绕权。

内置 meme-manage 真实发现和 materialize，但 Drift 无可见图片核验、表情库写入或复制能力，整项 pending_dependency，提示回主人聊天；不会靠有限 Shell 勾选冒充 available。S06 文本/MCP/有限 Shell 长期闭环实际执行，不以通用缺依赖占位替代目标。

普通聊天不扫描 Drift 私有技能根、不接受新的 requiresMCP frontmatter，也不继承自主身份。文件限额约束宿主管理操作；同账号外部程序可改变文件系统，边界检查拒绝这些变化，不能称作 OS 隔离。超过有限扫描边界的未知目录会拒绝准入和写入，不宣称已经遍历并精确统计不可扫描的全部字节。
