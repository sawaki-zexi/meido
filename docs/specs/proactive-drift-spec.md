# 主动决策、Drift 与来源作者技能

状态：草案，待 §3 一揽子架构批准；本文中的“采用/必须”是目标契约，不表示已经实现或验收。
关联：[Parent #137](https://github.com/sawaki-zexi/meido/issues/137)、[总 Spec A01/A02/S06/S08、T21/T22](shiori-tool-parity-spec.md)、[固定源码与 Meido 差距研究](../research/proactive-drift-runtime-portability-2026-10-16.md)。
最后核验：2026-10-16；Meido 聚合基线 `7936eaf878deb81817acb85fe18ff8a0b78192b0`，Shiori `4f266aeaf42fcd611362e2456417b91f659341cf`。

## 1. 用户结果与范围

主人分别开启角色的主动决策和 Drift，配置有限的自主来源、工具及发送目标许可后，角色在合格 tick 消费真实 alert/content/context：读取关联正文、结合角色记忆判断兴趣、去重、暂存一次草稿，再明确提交或跳过。
没有可处理事件且角色空闲时，Drift 执行角色私有目录中的长期小任务；可挂载已授权 MCP、维护工作文件，每 run 最多尝试一次推送，并按真实投递状态结束。
主人通过 S08 创建可验证、可授权激活的真实来源，通过 S06 创建后续 Drift 真正执行的长期技能；不以只有工具名称、配置草稿或依赖缺失说明验收。

本票不改变 Todo 当天提醒者、不新增关系模型、OS 空闲监视或自动目标漫游；不把主动/Drift transcript 当主人输入、记忆维护输入或第二个用户会话。
首版采用明确的默认目标及可测试的聊天忙闲规则，披露与 Shiori presence/relationship gates 的差异；完整保留两种主动运行的工具和提交协议。

## 2. 已有事实与依赖

[研究 §2–6](../research/proactive-drift-runtime-portability-2026-10-16.md#2-上游实际如何触发)证明固定 Shiori 共用主动 tick、空输入才分支 Drift，A01 草稿与 Drift 立即单次发送不同；其 ACK 回调没有宿主持久恢复，发送后只缩 schema 也不足以执行拒绝。
[TaskRuntime](../../backend/app/task_runtime/runtime.py#L137)已有全局8/角色3并发、最长600秒、不可变 origin、预算和受管取消；本文取证基线未含 keyed submit/retain-ACK。#158 最终源快照 `e992e0f` 已实现并通过两轴复核，本票使用其经验证总集成后的同一接口，不把未集成的任务分支当现状。
[ModelProvider/loop](../../backend/app/agent_runtime/loop.py#L39)、[MCP](../../backend/app/mcp_runtime.py#L165)、[文件](../../backend/app/agent_runtime/file_tools.py#L91)、[SkillRegistry](../../backend/app/agent_runtime/skills.py#L246)、[ChannelHost](../../backend/app/channels/host.py#L253)和[SessionStore](../../backend/app/session_store.py#L237)可复用；普通聊天推送的 user-row 授权不能直接用于自主 run。
遵守 [ADR-0011](../adr/0011-versioned-tool-activation.md)、[ADR-0012](../adr/0012-owned-task-runtime.md)、[ADR-0013](../adr/0013-independent-generic-schedules.md)；新增自主许可及业务恢复由本规格明确，不泛化 spawn 完成 worker。

## 3. 待批准的一揽子具体方案

| 决策 | 本草案选定的方案 |
| --- | --- |
| 模块与身份 | 一个 `proactive_runtime` 深模块拥有配置、来源快照、A01/Drift 状态、投递和 ACK；入口仅宿主触发/停用/查询与专用工具 Adapter。新增真实 AutonomyOrigin，绑定role、allowedModes、grant revision、generation、tick/task；不造user消息，不以owner=true冒充主人来源。 |
| 有限自主许可 | 角色 autonomy.activeDecisionEnabled 与 autonomy.driftEnabled 各自默认 false；独立列出 source get/poll/ACK、Drift MCP 工具、Shell 规则和可发送目标。开启时确认有限次数、可能模型费用、ACK/外部副作用；现有工具启用、MCP 握手、Skill 文本不授予该许可。 |
| 触发 | 一个宿主时钟，tick 每30分钟，无补 tick；启用/重启先进行获准 poll，首次决策等一个间隔。每角色至多一个 tick/Drift 决策在途；Drift 进入至少间隔3小时。空闲为无活跃主人 run、最近一次主人回合结束已满10分钟，且来源成功快照无 alert/content；不是 OS idle。 |
| 目标 | 主人显式选一个默认目标及最多4个许可目标，默认只含角色桌面。外部目标限该角色账号的已绑定主人私聊，通过真实目录和能力校验；A01 在 tick 冻结，Drift 在唯一发送 claim 前冻结。失效拒绝，不自动改投、广播或 presence fallback。 |
| 主人优先 | 接受真实主人 run 的宿主事务先 seal 旧自主 generation，所有新工具/提交/发送立即失效，再异步 owned cancel/join；主聊天不等网络清理。下一自主任务必须等旧执行及自有请求/进程/连接 join 完成；已 dispatch 的外部动作仍可能生效。 |
| 任务与预算 | 一个TaskRuntime kind=autonomy_tick持有source snapshot及后续A01或Drift；业务mode在快照后只选一个。整run最多600秒，A01另收紧到起点后300秒；35/20步，有限token/尝试/输出/磁盘预算见§5，poll/ACK也走同一Runtime的有限任务。 |
| A01 提交 | tick 静态来源快照；单草稿、一次 finish CAS、发送前 post-guard、一个 canonical 投递 outbox；只有真实 receipt 能置 sent，unknown 不重发，ACK 是独立业务 outbox。 |
| Drift | 角色私有工作根及受信技能；requiresMCP 绑定获准 server/定义版本，mount 只做当前 run 能力修订。网络发送前持久消耗唯一机会；成功后 schema 与实际 dispatch 同时只留写文件/编辑/finish。 |
| ACK 恢复 | 保留168/24/720小时及 alert 永久 ACK 的区别；固定初次决定的 expiry。仅验证过 ackId+绝对 expiry 幂等契约的来源自动重试；旧 ttl_hours 接口只尝试一次，不因失败重发消息。 |
| 真实结束 | finish_drift 的 message_result 明确为 sent/silent/failed/uncertain；silent 仅未 dispatch，sent 必有真实成功回执。失败/不确定可如实收尾，不能伪造完成或重新取得发送机会。 |
| 作者技能 | S06 生成合法 Meido Drift Skill，后续合格 run 消费；S08 先生成源码/配置草稿和合成验证，经过现有 MCP trust 与具体自主 grant 激活真实来源。许可配置是可完成的产品流程，不永久禁用。 |
| 验收与授权 | 本次批准包含实施、必要本地 fixture、隔离合成 MCP 的构建/运行及零真实发送的协议验收；真实账号查询/事务、外部服务写入及费用另有具体主人授权，不从架构批准推导。 |

以上是一次可批准的架构 bundle。常数为可逆的首版界限，不逐项追加审批；增大自主外部操作种类、目标范围或任意 Shell/命令权限须另审。
明确保留差异：独立 Drift 开关、固定聊天忙闲和显式目标，替代上游共用 enabled、presence/relationship fallback；保留 A01/Drift 的独立模式工具 dispatch。
实现前核验下述依赖；批准不等同验收，未实现的 AutonomyOrigin、source grant、CAS、业务 outbox、预算及 cleanup 均须真实代码和 fixture 证明。

## 4. 配置、宿主身份与执行 Interface

角色配置新增 `autonomy`：revision、activeDecisionEnabled=false、driftEnabled=false、decisionModelConfigurationId、driftModelConfigurationId、defaultTargetRef、allowedTargetRefs、sourceGrants、driftToolGrants、shellRuleIds、consentRevision。
两种模式分别绑定明确模型配置 ID，不隐式回退另一角色/active 连接；修改许可或模型生成新 revision，旧执行只能失效，不能扩权。
模型参数不得包含 role/session/grant/origin/target 身份或 command/env；所有对象拒绝未知字段。A01 草稿可用上游完整 account_id+target_id 二元组，但必须映射到本 tick 冻结许可目标，缺一项或越权拒绝。

主人 HTTP/UI 提供角色自主配置、有限尝试/费用说明、默认目标选择、来源注册/验证/启停、最近 run 与投递/ACK 状态，以及停止当前 run。
GET 目录与配置预览只读本地，不启动 MCP、不访问账号。实际外部目标目录依赖 #170；缓存 known/member 不能证明在线或可私聊。
默认关闭时仍可准备来源和技能；具体 trust/grant 完成后，下一合格 tick 真正执行。UI 保存本身不触发隐式真实 ACK 或测试发送。

应用 Interface 为 `configure`、`on_clock`、`accept_human_run`、`stop`、`query`；它内部拥有 admission、snapshot、draft/finish、dispatch/receipt、ACK 和恢复。
AutonomyOrigin只由该模块在真实宿主准入事务创建并持久化，allowedModes和grant不可变；快照后的mode选择是一次独立业务CAS事实，不修改origin或扩大候选。TaskOrigin的source_scope_id绑定自主grant ID，不继承任何历史主人run/渠道owner_context。
新增窄的 MCP 自主适配入口，验证宿主持久 grant 与真实 role/mode/generation，只选冻结的 server、远端工具和风险；复用 McpStdioClient/配置/凭据/Job，不把普通 `freeze(owner=True)` 当授权。
执行前、外部 dispatch 前、响应发布前均重验当前许可与 generation；撤销锁存，恢复开关不复活旧执行。运行开始按可用autonomy grant冻结候选，snapshot后选择mode只收窄不增权；挂载只在原server grant内追加父版本明确的能力修订，下一provider请求才可见。

accepted human run 的 seal 与自主 dispatch claim 使用同一角色同步点：先 seal 者禁止新 dispatch；dispatch 已 claim 者只尝试取消和记录真实结果，不声称撤销远端事务。
清理失败保留 closing/cleanup_pending 并阻止下一自主准入；应用 close 等待 owned cleanup，超时明确报未回收，不先标成功。普通聊天可继续。
只有真实宿主接受的主人桌面输入或当前配对主人渠道输入触发该抢占；模型意图、旁听、访客和内部完成事件不能伪造主人身份。

## 5. 尝试、时间、token 与本地资源

| 预算 | 首版硬界及计数规则 |
| --- | --- |
| 决策/Drift | 每角色24次决策准入/UTC日，Drift最多4次且包含在24次内；应用合计120次/日。进入模型前持久占用，模型出网/工具执行后不退，取消/重启/改 call 不重置；已发送意图不得重新准入。 |
| poll/ACK | 每角色 poll≤576任务/日、ACK≤512个请求/日；poll间隔≥150秒。单 poll任务≤120秒、ACK任务≤30秒；单RPC≤30秒、每角色最多2个并发RPC，同server串行；不自动重放普通 tools/call。 |
| 模型 | A01最多35步、Drift20步；每步至多1个 provider请求和1次工具动作，纠错/语义去重请求也占步。单请求max_tokens≤1024，按请求前最大值预留，A01累计≤12288、Drift≤8192；usage缺失/取消不释放已出网预留。 |
| 输入与输出 | 每provider输入≤48KiB UTF-8、整run累计≤512KiB；不支持预算参数的adapter拒绝该run。单provider≤30秒、工具≤30秒、Shell≤60秒且受最初deadline；多tool-call回复不执行任何一个，返回有界纠错，不能批量绕过步数。 |
| 来源与缓存 | 每角色≤8个来源条目；每条响应≤256KiB、≤200事件，tick归一快照≤512KiB；alert≤20、content候选≤5、context≤32KiB，正文每条≤8000字符、合计≤40000；并行抓取≤2，URL走现有web安全校验。 |
| 本地存储 | 每run日志≤64KiB、工具结果≤16000字符、文本尾部≤4000；Drift工作文件单份≤8MiB、每角色≤256项/64MiB，全局自主工作/缓存/日志≤1GiB。写前原子预留，临时文件/替换旧副本都计入，满时拒绝新写/准入。 |
| 保留 | 来源正文24小时、内部transcript7天、run摘要/已解决投递与ACK详情90天；不把正文/凭据/完整远端错误入日志。未解决 outbox、业务幂等墓碑保留压缩元数据，每角色最多20000条/16MiB，满时拒绝新自主工作，不能为腾空间遗忘未知发送。 |

这些是本地准入与请求参数上限，不是服务商费用估算或供应商输出保证；provider实际超界则停止接收/取消，远端可能已计费。
UTC日由持久账本推进，时钟回退不清零；提高额度不删历史。跨日、重启、rebudget 不释放旧 attempt 的发送/ACK claim。
TaskRuntime 的8/3并发、日志/保留配额仍实际消费；本模块额外角色准入不能代替其配额。
Drift 文件预算约束宿主管理写入。Shell 复用既有白名单及固定模板，cwd宿主固定到Drift根，保持最小env、不开放stdin，不提供通用脚本/安装/任意输出路径；它不构成OS文件系统沙箱，不能把cwd当全部副作用隔离保证。
首版自主 Shell 仅允许主人明确加入的无文件写出固定argv模板，参数受规则校验；每项先用本地 fixture 核验，未核验规则不执行，不能仅凭模型自称只读。

## 6. 来源注册、快照与兴趣

source记录绑定 sourceId、role、channel=alert/content/context、serverId/configRevision、grantRevision、getTool、可选pollTool/ackTool、定义hash、事件唯一域、ACK契约和enabled=false。
来源身份、server归属与复合键由宿主写入，拒绝 MCP 返回覆盖；复合身份为 sourceId+revision+kind+eventId，模型只拿不透明itemId。
同server多来源须声明其 eventId 唯一域；保存/删除/重建生成新版本，旧候选/ACK不流向新服务。凭据继续系统CredentialStore，源码与公开配置不保存明文env值。

宿主poll任务kind=autonomy_poll，只调用获准pollTool；tick的同一个autonomy_tick executor调用getTool取得静态快照，再选业务mode=A01或Drift，不能双开。重复get_*和get_content只读快照，不再次poll或重复网络读取。
tick在准入时持久startAt，Runtime预算600秒；snapshot阶段≤120秒，之后所有动作仍受startAt+600秒约束，A01另由同executor的有界取消监视收紧到startAt+300秒；抓取、分支、纠错和收尾不得新建任务或重置期限。
每个来源保留 ok/empty/failed、observedAt、truncated；超量、协议错、抓取失败不伪报空源。任何启用来源failed/incomplete都禁止本tick因“全空”进入Drift；其他成功来源仍可供A01判断。
无配置来源定义为明确空集合，允许独立开启的Drift；有事件但A01关闭时不转Drift冒充无事发生。tick不补偿停用/离线期间事件，来源自行返回尚未ACK事件。
content截取按来源稳定轮转，未入候选的事件不ACK；正文失败保留候选及错误，不能自动标not_interesting。

A01保留总Spec的12个schema与专用dispatch；recall_memory仅当前角色及所选目标的获准记忆，最多2项，interest标记仅tick集合，不写长期偏好。
get_recent_chat最多20条普通主人/助手片段，排除主动/Drift/内部任务、旁听及未授权线程；target retarget后不能扩大已冻结上下文。
unknown itemId（包括候选为空）全部拒绝；mark_not_interesting覆盖本tickinteresting但不等于抓取错误；工具说明不再承诺未实现HyDE/紧急severity直通道。
上下文文件只读角色获准PROACTIVE_CONTEXT文本，≤16000字符；缺失可空，版本固定本tick，内容不授予身份/工具/发送权限。

## 7. A01：草稿、post-guard、投递和 ACK

tick具有独立ID、generation、来源/目标快照和status；初始 draft=null、terminal=null。message_push只持久暂存一次非空草稿及本tick合法证据，第二次拒绝，不能覆盖/清除后重试。
finish_turn(reply)以CAS要求已有草稿，转为唯一final并清draft；skip要求无draft、合法原因及说明。重复相同finish只返回原结果，不再提交；冲突finish拒绝。
未finish、预算耗尽或取消只记aborted，草稿不外发；不自动补一个reply/skip，也不凭aborted推断不感兴趣。
post-guard检查当前generation/许可、真实目标、引用与本地24小时delivery key；归一URL/证据后再检查最近5条已发送主动消息的重复，最多一次≤600token语义请求且计入35步/总token。
语义调用失败或剩余预算不足则保守不发送，明确guard_unavailable；不能把provider故障当not_interesting或隐式追加预算。

通过guard后原子创建一条 canonical delivery_outbox，主键只含业务intentId，不含callId、deliveryMode或重试次数；同时占用引用事件，确定消息ID和目标。
外发前持久dispatch claim；一个outbox最多一次账号发送/桌面追加尝试；传原role/sourceKind/attachmentIds和授权回调，投递与消息投影复用现有seam而不借普通聊天推送授权。
sent需要真实DeliveryResult/桌面提交receipt；pending只同步原attempt回执，failed不自动重发，uncertain/重启遗失receipt保留原claim。明确原attempt仍未dispatch的本地失败才可恢复同条outbox。
投递成功但本地消息/receipt保存失败只恢复本地步骤；不换目标、改模式、换call或新tick重复该引用事件。无事件的相同归一草稿也有独立业务抑制键。
存在未解决uncertain投递时该角色暂停新的自主发送并显示对账入口，查询/静默Drift仍可继续；主人明确核对并记录处置才解封，不能把清空错误当确认未发送。

ACK决定与消息发送分离：sent后引用content=168h、interesting未引用content=24h、明确discarded=720h，被引用alert永久；context永不ACK。
post-guard判重拦截：content引用/interesting=24h、discarded=720h，本轮alert永久ACK；普通skip、确定发送失败仅处理模型明确discarded，uncertain不做成功ACK。
来源 failed、正文错误、未分类或未入选事件不自动丢弃。永久ACK仅表示此来源事件已处理；不能由一个context条目伪造。
每条ack_outbox固定 ackId、原来源版本/事件、decisionAt、expiresAt或permanent、理由；本地抑制至少到固定expiry，未解决发送事件另外保持占用。
新S08来源支持 `ack(event_ids, ack_id, expires_at, ttl_hours)` 并持久同ackId同结果；expires_at为绝对UTC期限，ttl仅兼容信息，重复不得延长。永久用expires_at=null、ttl_hours=0。
此契约经fixture验证才允许最多3次ACK尝试：立即、1分钟后、5分钟后，最晚decisionAt+10分钟且未过expiry；同ackId/expiry不变，撤权/变版停止，计入512请求日额。
旧仅ttl_hours接口有限ACK按剩余完整小时向下取整最多发一次，少于1小时不发；永久ACK仍用ttl_hours=0。失败/unknown记ack_uncertain并保留本地抑制，不能自称服务端已ACK。到期不再补发旧ACK，详情展示真实状态。

## 8. Drift：技能、挂载与真实 finish

合格tick须通过聊天忙闲、全源可信空输入、Drift独立许可与3小时冷却；进入时原子消耗冷却和日尝试，随后静默/失败也不退。
Drift是同一autonomy_tick中的独立业务mode和专用dispatch；私有根为 `roles/<roleId>/workspace/drift`，read/write/edit只接相对路径，解析真实路径及junction/symlink后校验。
角色资料/记忆与已受信内建技能由单独只读资源授权供给；不开放任意绝对读，不写builtin目录。工作文件跨run保留，临时transcript按本run归属。
技能扫描本地drift/skills及指定内建meme-manage/create-drift-skill，验证Meido frontmatter、hash、支持文件和trust；本地同名优先但不扩大工具授权。主人开启S06作者范围时同时授权专用目录产物的后续Drift激活，宿主记录作者来源/hash并显式激活通过校验的技能，不逐次审批文件写入；其他来源不继承该许可。
requiresMCP为serverId+所需远端名/定义hash；仅grant已冻结且握手目录版本匹配才available，不以进程存在当满足。缺依赖显示具体原因和配置入口。

mount_server只接server_id，连接/复用本run持有的预授权stdio实例，验证完整目录后stage/persist/publish能力修订；不得新增command/env/credential或跨角色连接意愿。
只有依赖已满足的技能可选；mount不是绕过requiresMCP的后门。远端目录变更需新run，旧挂载撤权即执行拒绝；所有连接随run取消/完成join回收。
复用recall/web/history的角色/目标范围及§5自主Shell规则；每次共享工具执行重验mode和grant，不因registry里有工具就可调用。

Drift message_push校验非空文本/最多4个当前run获准角色附件ID和显式目标后，事务置attempted=true，再进行唯一dispatch；确定失败、超时、取消也不恢复机会。
成功后provider schema和实际dispatch仅允许write_file/edit_file/finish_drift，mounted MCP、read、Shell、web、第二推送同样拒绝。
finish_drift要求有效skill_used、≤300字符one_line、≤300字符next_action及精确message_result：sent只匹配sent receipt，silent只匹配未dispatch，failed只匹配确定失败，uncertain只匹配pending/uncertain。
同run终结CAS一次，将技能进度、run_count、last_run、摘要和真实投递状态写同一SQLite事务；文件state只作可恢复投影。重复finish返回原收据，冲突拒绝。
业务finish_failed/finish_uncertain保留真实区别并映射非成功TaskOutcome；预算/取消/缺finish记aborted/interrupted，不自动冒充合法finish，也不丢已有投递claim。

## 9. TaskRuntime 交接、恢复及作者工作流

生产实现依赖 #158 完成仓库规范/需求规格两轴独立复核并进入最新已验证aggregate，确认 `submission_key`、`retain_until_ack`、结果ACK及取消结算回归。
admission先持久业务attempt/原TaskOrigin/kind/deadline和唯一launcher；调用 `submit(submission_key=attemptId, retain_until_ack=True, completion=False)`，保存taskId后query，不靠executor首次入场证明已提交。
submit返回丢失时仅原origin/kind/同key幂等查回交接；不得换key再建任务。重启不自动重交旧Task attempt，旧模型/Shell/poll/发送不重放；原任务interrupted，已dispatch保持真实pending/uncertain。
业务恢复可调度未消耗的幂等ACK retry ordinal：kind=autonomy_ack、attemptId=ackId+ordinal，同ackId/expiry、最多3次且受原10分钟截止；不能将旧unknown ordinal改回未尝试。之后新到期poll/tick另受正常时钟准入，不是恢复旧执行。
本业务恢复器只查本业务TaskRuntime结果与outbox。提交前即持久记录尚未结算的任务清理责任；taskId关联与责任标记同事务，缺taskId不能当未提交。终态准入门阻止迟到submit；原origin无法管理、角色临时disabled或SQLite busy时保留责任，不能将被过滤的空列表当不存在。
Runtime结果副本与result_ack_pending必须在同一业务事务持久提交，随后才可调用acknowledge_result；不允许先复制结果、再用另一事务补标记。独立恢复扫描覆盖所有业务终态，包括sent、skip、aborted、failed、uncertain和被post-guard抑制的记录，不能因决策或投递结束而漏扫。
仅Runtime ACK成功后才清result_ack_pending；ACK成功但清标记前崩溃时按原task/origin幂等重ACK，包括结果已按正常保留期prune的墓碑情形。暂时失败继续本地恢复；无法核实的任务保持清理责任，不重做模型、Shell、poll或发送。真正删除角色沿既有角色任务清理生命周期处理，不先删所有权留下活资源。
未ACK结果消耗原Runtime保留配额；pruned/unavailable不作为“没执行”的证据。保持TaskCompletionWorker的spawn授权/协议，不新增通用宿主或按业务泛化worker。
本地消息/进度投影、receipt查询及已获幂等许可的ACK重试是恢复；它们不允许重放模型业务动作。new tick不会清旧事件/发送占用。

S06转换为合法id/version/description/tools/activation指令，写当前角色drift/skills长期技能与working files；新/改hash重新校验并绑定获准作者来源与原工具上限，下一合格Drift必须可发现并真实执行，不以新文件授予新工具权限。
S08使用现有文件/Shell白名单准备可启动server、配置草稿与fixture，保留MIT来源署名；不把生成文件当已信任执行，不写明文凭据。
来源本地验证在无真实凭据的独立目录运行，证明三通道格式、稳定事件域、empty/error区别、正文关联、ACK幂等/固定expiry和重启持久化；旧ttl模式明确标记单次能力。
主人通过现有MCP配置流程信任固定command/cwd/秘密引用，再选择该role/source具体get/poll/ACK并启用；之后真实tick消费并展示ACK状态，不强制每次poll/ACK另问审批。
真实账号读取/ACK等仍受具体服务许可；未经授权的验收只用合成server。S08不能通过“自测”隐式执行真实pull/ACK或替角色启用任意命令。

## 10. 验收与垂直 Ticket 建议

#158 的实施依赖已在总集成 `4b1b3b8` 满足并通过两套CI；对应Ticket已移除当前阻塞边并保留完成证据。第3节架构批准仍待回答，自主读取、ACK或发送许可不因Runtime就绪而产生。

- 真实HTTP/AgentTool/SQLite+合成MCP完成alert/content/context→正文→兴趣→草稿/finish→一次投递→持久ACK；覆盖跨来源ID、空候选伪ID、failed不等于empty、缓存重复读无网络、可信最近聊天。
- 注入草稿/finish并发、post-guard超时、accepted human seal与dispatch竞态、submit返回丢失、结果副本与dirty事务切点、所有终态漏扫、ACK成功但清标记前崩溃/已prune、角色临时disabled后恢复、真实SQLite busy及retention满；证明保留额度最终释放、不重做执行，主聊天不等清理但下一自主准入必须join。
- 在远端已接受、本地receipt/消息投影/ACK各窗口断开，真实状态保留；旧key/新call/改目标/跨日/重预算不得重发，原expiry不延长，旧来源版本不被ACK。
- Drift从真实合格tick进入，创建S06技能并在后续run执行；验证目录逃逸、MCP版本/撤权、Shell拒绝、35/20步/600秒/token/disk界、单次失败机会及成功后伪造dispatch。
- 真实前后端展示默认关闭、许可/默认目标/依赖原因、run与ACK状态；零外部发送的本地平台fixture验证目录/receipt，不能据此称真实账号在线或真人收到。
- 获准实机验收另记录实际MCP版本/哈希、角色目标和许可、真实事件/ACK/收件证据；没有该授权保留未验收gate，不伪造成功、不删除整个功能。

已建立四张用户纵切票，均Parent #137，当前needs-info等待§3批准；采用Matt Pocock的What to build/Acceptance criteria/Blocked by结构：

1. [**#177：主人启用一个真实获准来源，角色提交或跳过主动消息。**](https://github.com/sawaki-zexi/meido/issues/177) 覆盖配置、可信autonomy origin、tick、A01完整管线与桌面receipt；Blocked by：§3批准。#158 的真实seam已完成两轴复核并进入经两套CI验证的总集成 `4b1b3b8`，实施依赖已满足。
2. [**#178：主人安全创建新来源并看到持久ACK和重启恢复。**](https://github.com/sawaki-zexi/meido/issues/178) 覆盖S08作者产物、合成验证、trust/grant激活、ACK契约/expiry/故障恢复；Blocked by：#177共享source/提交模块。
3. [**#179：角色空闲时执行长期Drift技能并如实收尾。**](https://github.com/sawaki-zexi/meido/issues/179) 覆盖S06、私有工作根、版本挂载、有限Shell、单次发送、finish与取消join；Blocked by：#177/#178共享主动/来源管线。
4. [**#180：主人配置真实外部默认目标并验收完整主动/Drift投递。**](https://github.com/sawaki-zexi/meido/issues/180) 覆盖已绑定主人私聊选择、目录真实性/能力、撤权和receipt；Blocked by：#177/#179、[#170](https://github.com/sawaki-zexi/meido/issues/170)真实目录集成，以及具体真实账号验收许可。

实施从协调者确认的最新已验证aggregate建独立本地任务分支，按主人已批准流程统一交付[总PR #162](https://github.com/sawaki-zexi/meido/pull/162)；不额外等待main、不建堆叠功能PR、不自行合并或关闭Issue。
开始前重读依赖实际Interface与开放工作；本票只扩必要自主MCP/投递适配及业务状态，不重建模型网关、Todo、第二套任务队列或全局MCP管理器。
