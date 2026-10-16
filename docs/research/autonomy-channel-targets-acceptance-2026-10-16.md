# 自主渠道目标与原回执恢复验收

状态：#180 本地实现与定向合成验证完成，固定源双路审查和总集成 CI 待完成。各批次的准确范围与原失败列在下文；没有把跨快照结果合称一次全量通过。真实账号、服务操作与模型费用验收未获本次授权。

事实来源：[已批准规格](../specs/proactive-drift-spec.md)、[目标授权](../../backend/app/proactive_runtime/targets.py)、[原渠道投递恢复](../../backend/app/proactive_runtime/channel_delivery.py)、[最小处置输入](../../backend/app/proactive_runtime/dispositions.py)、[有界保留](../../backend/app/proactive_runtime/retention.py)、[配置与验收手册](../handbook/autonomy-channel-targets.md)。最后核验：2026-10-16；实施基点 `312b7f1f5e1ede4ca6433bbcfcd0411dc8c660a4`，最终实现以本文所在提交的源码及固定源审查记录为准。

## 已实现的边界

目标选择直接调用真实 ChannelHost 分类目录。保存使用宿主持有的不透明观察身份，并重新核对角色、配置版本、账号、目录代际、adapter 和主人 identityId/pairedAt/contextSince；解绑再配对不能通过不变的目录版本重用旧选择。保存后的目标许可与当前执行的 adapter 身份分别管理：重启不静默更换主人绑定，旧运行也不会因账号重新开启而恢复授权。

A01 与 Drift 都冻结原许可集合，使用完整账号和目标二元组选择目的地。发送使用原 AutonomyOrigin、push 身份、持续授权及原 ChannelHost send lease，不制造主人输入。外发前原事务保存不可逆 claim、目标、内容、事件占用、旧/新去重键和不超过 32 KiB 的无正文处置输入；任一步失败都没有新外发资格。附件须同时属于原角色和本次运行，原字节只留在内存，并跨越 adapter 的 await 继续使用。

生产装配的 SessionStore、MultichannelStore 和自主账本使用同一个 sessions.db。渠道投影要求 caller-owned 活动事务，并校验三方 resolved 数据库身份；不把跨库写入称为同一事务。新外发遇到注入异库时拒绝准入。发送后的本地故障保留原 attempt 和恢复责任，只查询原 marker，不再次调用发送入口。

人工 reported_sent / reported_not_sent 与平台事实分开保存，使用 requestId、原投递状态和独立 resolution revision CAS。人工报告不制造 sent、消息或成功 ACK，也不释放旧 claim、事件占用或去重键。真实迟到原 sent 回执仍能恢复原线程的一条消息，并按原许可形成合法处置。

ChannelHost 在真实宿主启动、后台任务准入前绑定原服务事件循环。跨循环发送将原 account_send 全部执行体送回原循环，保留调用者 contextvars；同循环调用保留原 Task 与流式 Queue。取消只向原任务发出一次，并等待其真实 finally/Task.done，不能用 concurrent Future.cancel 代替完成。原循环已停止或关闭时拒绝发送；有旧 adapter 的 Host 不转交新循环，应用重建使用新的 Host。

## 定向实验与原始失败

原始日志、XML、manifest、SQLite、执行脚本及 SHA256 保留在实施环境的 `D:/meido-artifacts`，每个 stage 使用新文件和三个新 MEIDO 根。以下数量分别属于各自快照，不能相加为一轮唯一全量验收。

| 范围 | 原失败与修正 | 已完成的定向结果 |
| --- | --- | --- |
| 真实目录与 A01 | 最初缺路由 404；随后发现保存授权读取嵌套写事务导致 503，改为纯只读查询。真实 A01 原反例平台收到 0 次。 | `targets-a01-identity-green`：35 passed，包含原目录/A01、主人和账号 ABA 七例、最小处置输入 26 例；465 份输入稳定。 |
| Drift 与主人对账 | 真实 OneBot 接收后用 SQLite trigger 阻止原 receipt 写入，保留 unknown；人工报告不修改原 claim/keys/progress。 | `drift-channel-initial`：2 passed；原真实首次响应经已有持久接口恢复后，一次原线程投影，无第二次发送或模型调用。 |
| 超过 24 小时的首次成功 ACK | 真实 prune 和完整 owner 重建后，原 MCP ACK 已发生一次，但原 tick 的 64 KiB 恢复预留未结算；修复原 ACK receipt 事务的本地结算。 | `late-ack-green-current`：1 passed，RED/GREEN 测试字节相同；get=1、ACK=1、模型=3、平台发送=1，原 8301/attempt/decisionAt/168h 不变，三代 owner 关闭。 |
| 原权限与截止 | unsupported、unknown、人工报告分支初轮通过；撤权/来源 ABA/原 600 秒及有限 expiry 四例只在最后预留未归零失败。 | 原七例在 `edges-green-current` 全部通过，原测试字节不变；该批整体仍有三个测试装配失败，后续按真实入口修正夹具并在业务组合覆盖，不把原失败批改称通过。 |
| 当前新许可不能补旧权 | 原来源真实声明 ACK，但原 grant 无 ACK；unknown 后只通过 HTTP 给当前 grant 新增 ACK 权。 | `fixtures-corrected-current` 中该例通过：真实 prune/重建/首次原 sent 后仍 unsupported、ACK=0。 |
| 主人 HTTP 抢占 | 首轮脚本只实现普通 structured 回复，但普通聊天实际走 rich 接口；按真实模型入口修正夹具，保留原业务断言。 | `fixtures-corrected-current` 中该例通过：主人 HTTP 在原 transport finally 的 release 前完成，下一自主准入等待旧请求 join，旧模型仍只有三次，未知回执不伪造 sent。 |
| 原附件字节 | 新 host/Attachment 合同首轮 22 failed、4 普通路径对照通过；实现后保持测试字节不变。 | `media-contract-green`：76 passed，含四真实 adapter 与 50 项既有渠道回归；另 `fixtures-corrected-current` 的真实 Drift 用例拒绝跨角色和同角色未授予 ID，在原发送授权后修改路径，OneBot 仍只收到原冻结图片。 |
| 已持久 receipt 后撤权 | 初稿在 executor 内调用临时 HTTP portal，造成 cleanup 等待自身 executor；随后在 receipt 尚未发布时撤权，其正确结果是 unknown，不能要求 sent。保留两个原失败并区分切点。 | `guards-durable-receipt-current`：4 passed，线程异常设为错误；真实原 receipt 持久后撤权仍保留 8301/attempt/一条投影。无本地 receipt 的切点由前述主人 HTTP 用例独立覆盖。 |
| 平台明确拒绝与断连 | 真实协议两例 RED：failed 仍留 64 KiB，uncertain 在 Runtime 已结束后仍保留原运行预算。按已确认的 Runtime 完成与原 attempt 事实分别结算。 | `platform-failure-green-final`：2 passed，测试字节与 RED 相同；确定失败预留归零，unknown 只保留不超过 64 KiB/8 行/8 KiB，get=1、模型=3、原 claim/attempt=1、重建后发送次数不增。 |
| 旧格式升级与物理数据库 | 旧版本没有自然产生 unknown 加旧 hash 的同一运行；迁移夹具明确使用已固定旧算法和历史格式事实，不冒称执行旧版生产。新候选使用不同远端事件身份，避免同事件占用掩盖旧 hash 查询。 | `legacy-db-persistent-first` 中旧键 4 项、异物理库 2 项、原保留回归 18 项共 24 项通过；该批仍有一项真实持久 transport 的事件循环失败，整批 exit 1。旧 expiry、原 claim 和人工处置后的占用不变；异库新准入为 0。 |
| 原持久渠道事件循环 | 真实长连接 transport 的目录在宿主循环、发送却在 executor 循环；即使平台收到 8301，原 owner 契约仍失败。随后显式 bind 夹具以缺接口失败，区别于原错误循环反例。 | `persistent-owner-first-green`：57 passed，包含新绑定原连接及四 adapter 媒体回归；`runtime-owner-cross-regression`：82 passed，包含普通渠道、NovelAI、Coding、spawn、调度及主人 HTTP 抢占两种绑定路径。原请求 Task/finally/lease 与 reader 均正常结束。 |
| 桥接取消与异常合同 | 包装器最初对空/非法/超限附件 mapping 抛 ValueError，三项 RED 固定原 UnsupportedTargetError 合同；根协调端新增的 Host 五例首跑通过，不能称五例 RED→GREEN。 | `runtime-host-final-green`：15 passed，含 Host 5、异常 3、桥 6、原持久 transport 1；481 份输入稳定、无排除文件。重复取消不打断原 finally，关闭循环不外发，同循环事件许可和流式 Queue 保持。 |

合成 MCP 普通启动预算从 3 秒改为 10 秒只用于自主边界夹具；保留原 sources_unavailable 失败记录，不将其冒称目标撤权发生后的行为。真实平台、模型和原任务预算没有放宽。

## 前端、类型与组合

当前前端完整验证为 **243 passed / 43 文件**，`tsc --noEmit` 与 Vite 构建通过（107 模块）。134 个输入包含原 133 项及实际构建入口 web/index.html，命令前后与当前输入一致。旧 242 项结果属于前一快照；后来修改的两份投递对账组件和测试已经包含在本轮 243 项内。projectionPending 的文案仅说明原意图尚有本地恢复责任，不宣称原 receipt 已存在。

静态类型按相同 flags 与准确基点比较完整诊断、包围函数和重数：六份渠道源 49→49，十四份业务源（基点十一份旧源加三份新模块）90→90，新增诊断为 0；Host 与新 runtime_owner 两份源检查通过。旧 no-redef 诊断中嵌入的行号随启动绑定增加一行，按其精确原赋值 AST 对应，原始消息完整保留。旧错误位于既有 adapter、main 和 multichannel 代码；**不是全量类型检查通过**。最初两条新增注解错误及最终包装器返回 Awaitable 引起的一条 create_task 诊断均已修复，原日志保留。最后仅新增 typing.Coroutine import 并收窄包装器返回注解为 Coroutine，精确反向重建的原字节等于 15 项通过时的 Host SHA，运行体没有变化；不把旧测试输入称为当前相同字节。

原业务组合覆盖全部当时纳入的 proactive、Drift、渠道目录/主人/source authority/receipt/history、TaskRuntime 交接与 MCP origin：控制台 460 passed，但 XML 的 461 个唯一节点中仅 458 项完整通过、2 项 teardown error、1 项条件跳过，整批 exit 1。两项清理错误来自新增恢复查询对旧保留夹具中非法 JSON 的直接提取；两个查询已增加 json_valid CASE 守卫，原 18 项保留回归随后完整通过，未改弱旧夹具。条件跳过是 Windows 创建符号链接的 WinError 1314，不计作原生通过。新异库与旧键用例当时未导入，独立在前述 24 项内通过。未因无新问题重复整批 460，最终完整源码仍须总集成 CI。

所有批次分别保留 before/after 输入、日志、XML、SQLite 与退出码；local_platform_only 内外守卫持续到全部应用 owner、线程、进程、请求和 lease 自然 join。原协议测试夹具的模型接口、HTTP portal、启动预算和 cleanup 失败记录均保留，装配修正不当作产品 RED。最终 57、82、15 项是受事件循环桥影响的必要交叉，数量互有重叠，不相加宣称一次全量测试。

## 真实验收 gate

本次只有 localhost 协议、fake vault、合成来源和脚本模型，没有真实账号目录查询、真实来源读取或 ACK、真实平台投递、认证或模型费用。上述证据不能说明真人已收到消息。

真实验收须按[手册中的有限范围表](../handbook/autonomy-channel-targets.md#有限真实验收范围表)，在固定准确 HEAD 后准备具体角色、账号、主人私聊、来源版本与工具 hash、A01 事件、Drift bundle hash、各操作次数、模型/token/金额上限、时间窗和完整收尾证据，再取得明确批准。空表不是授权。本票和总目标继续开放，本文不授予推送、合并或关票权限。
