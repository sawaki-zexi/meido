# Shiori 主动决策、Drift 与作者技能的运行时迁移核验

状态：草案；用于 T21/T22 具体 Spec 准备，未批准新的自主操作范围，未实施生产代码。
最后核验：2026-10-16。
上游固定版本：Shiori `4f266aeaf42fcd611362e2456417b91f659341cf`；下列上游链接均固定该提交。
Meido 基线：聚合分支 `feat/137-shiori-tool-parity`，`a0161f5f1cc7206c34b62670a50785d4faa5b512`。
核验方式：只读固定源码、当前源码与相关 Issue；没有运行 MCP poll/ACK、发送消息、读取真实凭据或安装依赖。
标记：“已提取”为源码事实；“推断”为差距解释；“建议”为尚待具体 Spec/人工架构批准的取舍。

## 1. 范围与结论

本研究承接[总工具研究 §2.5、§4](shiori-tool-system-and-portability.md#25-普通对话主动决策和-drift-的工具集合不同)，只展开 A01、A02、S06、S08 的运行协议，不重列完整工具清单。
[总 Spec](../specs/shiori-tool-parity-spec.md#63-后台定时主动和-drift)已要求真实来源、草稿/finish、持久 ACK、Drift 单次发送尝试及两份技能的后续消费。
[ADR-0012](../adr/0012-owned-task-runtime.md)和[ADR-0013](../adr/0013-independent-generic-schedules.md)已批准共享任务生命周期与独立 Schedule；它们没有把每种自主 MCP、Shell、账号发送自动授权。
[领域上下文](../../CONTEXT.md)区分角色唯一会话、渠道线程、外部事务及不确定投递；[ADR-0005](../adr/0005-todo-tools-and-proactive-reminders.md)的当天提醒者不能替代本功能。

**已提取：** Shiori 的 A01 与 Drift 共享每角色主动 tick；Drift 是空输入时的分支，不是另一个操作系统空闲守护程序。
A01 的 `message_push` 暂存草稿，`finish_turn(reply)` 才形成待投递结果；Drift 的同名工具会立即尝试发送且消耗本 run 的机会。
上游 ACK 是来源回调，消息投递成功、源 ACK 成功、本地完成记录是不同事实；不能据此宣称跨 MCP/平台恰好一次。
证据分别见[分支入口](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/proactive_turn/phases.py#L204)、[A01 草稿](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/tools.py#L613)、[Drift 发送](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/drift_tools.py#L90)。

**推断：** Meido 已有模型循环、任务、MCP、文件、Skill 和投递 seam；缺的是一个拥有自主来源、候选快照、业务提交与恢复协议的深模块。
建议实现一个主动运行模块，内部区分 A01/Drift 的执行策略；共享基础资源，不共用忽略运行类型的同名工具 dispatch。
两份作者 Skill 需要这个运行模块实际消费产物；仅转换 Markdown，不能满足 S06/S08。

## 2. 上游实际如何触发

**已提取：** 启动装配只为 `role.proactive.enabled` 的角色创建循环和角色私有状态；tick 由宿主构造 `source="proactive", work_kind="proactive_tick"` 上下文并冻结 chat 模型。
它不是给主人伪造一条用户消息后启动普通对话。[装配入口](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/bootstrap/proactive.py#L73)、[宿主上下文](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/bootstrap/proactive.py#L178)。

首次先完成 feed poll，再启动独立 poll 循环，并等待一个 tick interval 后决策；后续 tick 串行，不是启动即推送。
poll 默认约 150 秒；主动间隔由 presence/energy 和 s0/s1/jitter 推算，无 presence 时回退固定 interval。
配置里存在历史评分项，不证明这些评分项仍参与当前 A01 兴趣判定。[启动/间隔实现](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/loop.py#L423)、[配置默认值](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/config.py#L1)。

gate 先解析一个默认目标，再检查 passive busy 和注册的主动 gates；通过才抓取来源。
默认目标优先在场桌面，其次最近收到主人消息的在线绑定账号私聊，再按绑定顺序选择外部私聊，最后保留桌面；不广播、不默认选群。
A01 默认目标在 gate 冻结；有意 retarget 走完整账号/目标校验；Drift 则在实际说话时重新调用目标 resolver。
[gate](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/proactive_turn/phases.py#L84)、[目标算法](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/target_selection.py#L43)、[Drift 目标时点](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/agent_tick_factory.py#L320)。

alerts/content 均空且 context fallback 未开启时，relationship fallback 先有机会接管，之后才进入 Drift。
Drift 有独立“上次进入”冷却，默认 3 小时；进入过就记时间，静默、未成功推送也会消耗本次冷却。
因此“空闲”至少涉及角色忙闲、gate、来源快照和冷却，不能简化成“最后一条用户消息过去 N 分钟”或“OS 鼠标未动”。[分支和冷却](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/proactive_turn/phases.py#L204)。

配置 loader 直接令 `drift_enabled=bool(enabled)`；旧 `drift.enabled` 不是独立有效开关。
`stop()` 仅停止未来调度；owner 负责取消在途 tick，已交 outbound 的内容仍可能发出。
[loader](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/config_loader.py#L293)、[停止语义](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/loop.py#L484)。

**建议：** Spec 应明确采用哪种忙闲和目标策略、首次/重启是否补 tick，以及 Meido 是否增加独立 Drift 许可；这些均是可见兼容差异。
可复用 Schedule 的宿主时钟和 TaskRuntime，不新增 Todo 项，也不为每份技能另建无限常驻循环。

## 3. 来源、候选与兴趣的真实数据流

**已提取：** `proactive_sources.json` 声明 server/channel/get/ack/poll，MCP 启动定义另来自 `mcp_servers.json`。
上游每角色 pool 持有连接，单 server 调用串行；配置在连接时读取，失败连接丢弃。
get/ACK 默认不自动重试；特定允许重放的读取才有一次 transport 重试；内容 poll 可单独使用长达 180 秒 timeout。
[配置与连接](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/mcp_sources.py#L43)、[调用策略](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/mcp_sources.py#L120)、[poll](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/mcp_sources.py#L351)。

alert/content 提取器要求 list 中的 dict 和正确 kind；context 可为 dict 或 dict list，不要求事件 ACK。
空 channel 仍走旧 alert/content 兼容路由；不能把这项兼容误写成规范必须双发。
提取器用 `setdefault("ack_server", server)`，来源自己提供的同名字段不会被覆盖；Meido 必须由宿主写来源身份，不能让 MCP 输出决定 ACK 发给谁。
[提取/路由](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/mcp_sources.py#L227)。

gateway 在模型决策前并发拉取三路数据，形成 tick 内静态快照。
content 默认最多 5 条，提前抓 URL 正文并截到默认 8,000 字符；以 `server:event_id` 关联轻量元数据和正文缓存。
`get_content` 只读本轮复合键缓存，不是任意 URL 抓取接口；读取事件/context 工具也不重新 poll。
[gateway](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/gateway.py#L57)、[缓存读取](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/tools.py#L366)、[预取标记](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/proactive_turn/phases.py#L186)。

来源失败在上游被日志化并折成空列表，正文失败保留候选但正文为空。
这是错误信息损失，不是已证明“没有内容/不感兴趣”的事实；直接复制还可能错误进入 Drift。
Meido 应保留 `ok/empty/failed` 和正文抓取状态，失败不触发负兴趣 ACK，也不作为可信的全源空闲证据。[失败路径](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/gateway.py#L85)。

A01 `recall_memory` 以 interest 意图、只读、角色/会话/目标范围检索，默认取少量记忆；`mark_interesting/not_interesting` 改本 tick 的集合。
它们没有把模型判断直接写成长期主人偏好，也不意味着后台已运行一套完整 HyDE 排名器。
标记函数还有 `if unknown and valid`，候选集为空时可接受猜造 ID；移植应无条件拒绝不在本快照内的 ID。
[检索](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/tools.py#L305)、[兴趣集合](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/tools.py#L465)。

近期聊天仅取合适的上下文帧，排除主动助手消息；内部任务 transcript 不能反过来伪装主人最新聊天。
`PROACTIVE_CONTEXT.md` 是可重读的行为文本，读取失败保留旧文本；它不授予来源或发送权限。
[聊天过滤](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/tools.py#L441)、[文本加载](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/loop.py#L275)。

**推断：** 需要三个不同概念：来源事件的稳定身份、tick 内候选分类、最终投递的重复判定。
同一 server 的多个来源应声明 event_id 的唯一域；换 server 配置/授权版本不能让旧 ACK、旧候选自动命中新来源。
上游 SQLite 中存在 seen_items 表，不足以证明当前 gateway 已逐条执行完整 seen 预过滤；实际 post-guard 去重见下一节。

## 4. 一次草稿、finish、投递与 ACK

**已提取：** A01 `message_push` 校验文本、证据和可选完整 retarget，已有草稿时拒绝覆盖，没有外发操作。
`finish_turn(reply)` 要求已有草稿，移入 final 并清草稿；`skip` 要求无草稿且有合法理由。
模型耗尽步骤或未 finish 不会隐式把草稿提交；默认 A01 上限 35 步，纠错也受步数约束。
[草稿/finish](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/tools.py#L574)、[有界决策循环](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/proactive_turn/judge.py#L74)。

finish 后仍有 post-guard：按引用 URL/来源标题/事件证据形成 delivery key，检查持久窗口；还可与最近主动消息做模型语义判重。
URL 归一会去 fragment、规范 host 等；key 退化时可用消息前缀。语义判重是发送前辅助，不替代原子发送 claim。
[键构造](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/proactive_turn/resolution.py#L49)、[窗口/语义判重](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/proactive_turn/resolution.py#L282)。

投递成功后，已引用 content ACK 168 小时，未引用但 interesting 的 content ACK 24 小时，明确 discarded ACK 720 小时；被引用 alert 走 alert ACK。
post-guard 拦截另有 24 小时 ACK 策略，并可 ACK 本轮未引用 alert；普通 skip/投递失败只处理明确 discarded。
这些是“暂缓再次推荐/明确丢弃/事件已处理”的不同语义，不是统一“已读”。
[TTL 与分支](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/proactive_turn/resolution.py#L22)、[ACK 细则](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/proactive_turn/resolution.py#L102)、[成功/失败副作用](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/proactive_turn/resolution.py#L416)。

MCP ACK 回调按 server/event 分组调用，异常只记日志；上游没有由此证明可恢复的宿主 ACK outbox。
来源 Skill 要求服务端持久 ACK、TTL 缺省或 0 为永久、不 ACK 就继续返回；服务端持久化与宿主确认成功不能混为一谈。
[回调](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/mcp_sources.py#L378)、[来源契约](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/create-proactive-source/SKILL.md#L70)。

orchestrator 将真实发送插在消息提交前；发送结果为真才运行成功副作用，副作用异常又被日志化。
远端已接受、本地消息未提交、delivery key 未记、ACK 未成功之间仍有崩溃窗口。
上游 bool 结果也没有表达 Meido 的 uncertain；不能将“超时/断线”自动当确定失败重发。
[提交/发送顺序](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/turns/orchestrator.py#L107)、[副作用和真实发送](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/turns/orchestrator.py#L156)。

**建议：** 一个 tick 只能 CAS 到一次终结决定；reply 创建一个 canonical 业务 outbox，claim dispatch 前再次核验角色、许可、目标与忙闲 generation。
按 tick/业务意图去重，不能换 tool call、重启、改 deliveryMode 或改投目标再次发送；投递回执与会话投影分别恢复。
sent 才生成成功 ACK；uncertain 保留本地事件占用，不伪造成功 ACK，也不让下一 tick 重发同一业务意图。
ACK 独立持久化原来源版本、事件、原因和截止时间；只有服务端契约支持幂等时才重试，重试不无意延长 TTL，更不重发消息。
这是满足总 Spec 的必要本地保证；跨平台无幂等键时只能保证“不自动重复尝试”，不能保证远端恰好一次。

## 5. Drift 的目录、挂载、预算与收尾

**已提取：** Drift 工作根在角色主动状态目录下的 `drift/`，扫描本地 `skills/*/SKILL.md`，再补指定内建技能；本地同名优先。
frontmatter 的 name/description 必须可解析；`requires_mcp` 与当前已连接 server 集合求依赖，缺失技能不进入本轮可用列表。
内建名单仅含 `meme-manage`、`create-drift-skill`；不是八个普通技能自动全挂。
[构造](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/agent_tick_factory.py#L249)、[扫描/元数据](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/drift_state.py#L90)、[依赖过滤](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/drift_turn.py#L119)。

run 输入包含角色上下文、技能元数据、每技能进度、最近 Drift 记录；工作文件跨 run 保留，transcript 则是本次内部工作集。
finish 将技能 run_count、next_action、时间等写 state.json，再写 drift.json；两份文件各自原子写，不是一个事务。
内建技能路径可能被用于写 state，Meido 应把内建技能运行状态保存在角色私有存储，不能修改分发目录。
[状态读取/写入](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/drift_state.py#L129)。

本地 read/write/edit 相对路径以 Drift 为根，写编辑限制根；但 read 对绝对路径复用一般 ReadFileTool，不构成完整读沙箱。
还复用 recall/web/history/shell；“工作目录隔离”不能推导 Shell 或所有共享工具都受该目录约束。
[读取差异](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/drift_tools.py#L374)、[本地工具装配](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/drift_tools.py#L410)。

`mount_server` 仅从共享 registry 中已连接的指定 MCP server 取工具，追加本 run 的挂载集合。
它不创建任意 command/env、不取得新账号许可，也不自动满足尚未连接的 `requires_mcp`。
[挂载实现](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/drift_tools.py#L245)。

Drift 默认 20 步；一次模型请求只处理一个工具调用；没有在该循环中建立完整 wall-clock、token、磁盘总预算。
`message_push` 在非空且已配置后、await 发送前置 attempted=true；确定失败也消耗机会，只有真实 bool 成功置 sent。
[执行循环](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/drift_turn.py#L182)、[一次尝试](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/drift_tools.py#L90)。

成功后下一请求的 schema 只剩 write_file/edit_file/finish_drift，但实际 dispatch 仍接受 registry 中工具或 mounted MCP；单靠 schema 收窄不是执行许可。
`finish_drift` 校验 skill、摘要、next_action 和 message_result：sent 要已有成功，silent 不能掩盖已成功发送。
循环预算退出或无工具调用只记录日志，不自动形成合法 finish；不能据 entered=true 声称任务已完成。
[schema 与 dispatch](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/drift_turn.py#L204)、[finish 校验](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/proactive_v2/drift_tools.py#L146)、[退出](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/core/drift_turn.py#L317)。

**建议：** 保留长期工作根及专用工具名，复用 Meido 文件真实路径校验；明确只读角色资源/内建技能根，禁止继承上游任意绝对读。
Shell 用现有 argv/白名单/最小环境/受管进程，cwd 由宿主固定到任务根；这不是新的 OS 沙箱。
发送机会持久原子 claim 在实际 dispatch 前；恢复不能再发，记录 pending/sent/failed/uncertain，finish 不把 unknown 写成成功或静默未尝试。
成功后在 schema 和执行器同时收窄，已挂载 MCP 也必须被拒；保留写进度与 finish，超限/取消则记 interrupted/failed，不自动伪造 finish。

## 6. 两份作者 Skill 实际依赖什么

**已提取：** S06 指导在当前角色 Drift skills 下创建/更新长期任务 SKILL.md、working files 和 finish 约定。
它不是自动调度工具；没有扫描器、可用性判定、实际 Drift run 和持久进度，创建出来的文件不会自行执行。
[完整 S06](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/create-drift-skill/SKILL.md#L8)。

S08 产物是实际 MCP server、来源注册与本地验证，优先复用已有 server；新建示例依赖 Python/FastMCP、venv、命令执行和配置写权限。
alert/content 必须有稳定 event_id，服务端持久 ACK；context 不要求 event_id/kind/ACK；content 可选 poll 工具。
源端不应一次性过滤用户兴趣；兴趣由主动管线决定，未 ACK 的事件继续返回。
[产物与协议](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/create-proactive-source/SKILL.md#L28)、[作者流程](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/create-proactive-source/SKILL.md#L137)、[约束](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/create-proactive-source/SKILL.md#L348)。

S08 文案仍称 alert severity 快通道、content HyDE 评分；当前提取/gateway/judge 调用链不能证明这些旧说法仍执行。
本地验证范例直接调用拉取与 ACK 函数；迁移后应先用隔离合成来源和持久 ACK fixture，不能把“运行 Skill 自测”当真实账号读写许可。
[旧文案](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/create-proactive-source/SKILL.md#L18)、[验证流程](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/create-proactive-source/SKILL.md#L166)。

**建议：** 转换两份 Skill 的 id/version/description/tools/activation、路径和来源署名，保留真正的工作流，不复制明文 env/随意 command 注册。
S06 新产物须经现有 hash/信任和 Drift 工具许可，在下一次合格 run 被发现并执行；普通角色 Skill 与 Drift Skill 的发现范围应明确。
S08 可先生成文件、配置草稿和合成验证结果；经过宿主信任及具体 source grant 后再激活真实读取/poll/ACK，不能永久停在“生成了模板”。
真实 MCP 依赖不能只检测 bin/env；需绑定已授权 server 及工具定义版本，缺失时解释并提供完成配置路径。

## 7. Meido 当前能复用的 seam 与缺口

以下均为当前聚合源码事实，不能将尚在 #158 分支的能力当成已集成。

| 已有 seam | 可直接复用的事实 | A01/A02 尚缺的行为 |
| --- | --- | --- |
| [TaskRuntime](../../backend/app/task_runtime/runtime.py#L137) | 全局/角色并发限制、最长 600 秒、不可变 origin、预算监视、取消调用与 executor 结束后释放 | 主动 tick/poll executor、业务重复 claim；当前 submit 无 submission_key/retain_until_ack |
| [portable loop](../../backend/app/agent_runtime/loop.py#L39) / [spawn executor](../../backend/app/agent_runtime/spawn_tool.py#L155) | 独立模型循环、有界 provider/tool 调用、取消穿透的实现模式 | 不能直接调用 SpawnTool 冒充 Drift；其子工具集合刻意不含 MCP/发送等能力 |
| [McpHost/McpRun](../../backend/app/mcp_runtime.py#L165) | 角色 grant、冻结定义/版本、撤销、每 run 连接和受管 stdio；add 可追加能力修订 | 宿主主动 source registry、显式无用户回合 origin、只允许 get/poll/ack 的来源配置与恢复 |
| [CapabilityRegistry/SkillRegistry](../../backend/app/agent_runtime/skills.py#L246) | 信任、hash、显式激活、tools 交集和 bins/env 可用性 | 两份内建技能、Drift 发现根/状态、requires_mcp 的已授权连接依赖，不是复制 frontmatter |
| [文件](../../backend/app/agent_runtime/file_tools.py#L91) / [Shell](../../backend/app/agent_runtime/shell_tool.py#L65) | role root、真实路径/链接校验；受限 argv、环境与受管进程 | 宿主绑定 Drift 根和只读资源；自主 Shell 许可与整 run 资源归属 |
| [SessionStore](../../backend/app/session_store.py#L237) / [渠道投递](../../backend/app/channels/host.py#L253) | 确定性消息追加、角色/账号/目标校验、附件 ownership、真实 DeliveryResult | 草稿/finish CAS、业务 outbox、ACK outbox；现有 append helper 不等于这些协议 |

角色模型只保存 `proactiveConfig`，当前没有对应主动消费模块；内建目录没有 S06/S08。
普通 `MessagePushTool` 立即追加桌面消息，主装配授权又要求当前真实聊天 run 的完成用户文本；不可为自主 run 伪造 user row 来复用。
McpHost 自身不强制存在用户消息，但普通聊天装配使用渠道 owner_context；自主许可必须显式表达，不能随手传 owner=true 冒充来源。
[模型字段](../../backend/app/models.py#L83)、[普通推送](../../backend/app/desktop_push.py#L31)、[聊天授权](../../backend/app/main.py#L3870)、[MCP 装配](../../backend/app/main.py#L4113)。

现有取消会调用 executor.cancel 并等待结束；若 executor 不释放则抛超时，不能据任务状态声称子进程、MCP、网络都已停止。
重启将旧 running/queued 标为 interrupted、旧 sending 标为 uncertain，不自动重跑。
主动 executor 需 join 自有请求/进程/连接；主人新消息应立即封住旧 generation 的新动作，再受管取消，已 dispatch 的远端动作可能仍生效。
[取消/释放](../../backend/app/task_runtime/runtime.py#L346)、[重启](../../backend/app/task_runtime/runtime.py#L179)。

TaskCompletionWorker 当前应用装配服务 spawn 文本完成，不拥有主动候选/ACK/Drift finish。
建议主动任务使用自己的业务完成 outbox，不扩大 spawn 的授权谓词；若依赖 #158 的 keyed submit/retain-ACK，需在其获准聚合集成后使用真实 Interface。
[完成 worker](../../backend/app/task_runtime/completion.py#L31)、[当前 submit](../../backend/app/task_runtime/runtime.py#L189)、[#158](https://github.com/sawaki-zexi/meido/issues/158)。

## 8. 具体 Spec 应提交人工批准的真实取舍

D01–D03 已批准的基础设计无需重问；以下细化会影响自主外部操作、隔离范围或恢复，不能只留一句“按需授权”。

| 决定 | 建议的最小可落地方案 | 需要明确的兼容差异 |
| --- | --- | --- |
| 自主许可 | 角色主动/Drift 默认关闭；主人配置来源 get/poll/ACK、可发送目标、可用 Shell/MCP 的有限 grant；运行绑定版本并持续重验 | 上游一个 enabled 同时打开 Drift；工具可见性/Skill 文本不等于无人值守许可 |
| 触发与忙闲 | 一个宿主时钟，单角色一个在途 tick；区分来源 failed/empty，合格空输入才 Drift；新主人回合原子封旧 generation，取消/join 后再准入下一自主 run | 不宣称 OS 空闲；relationship/scene motives 未迁移时明确采用何种可测 gate，不暗中增加一套关系模型 |
| 默认目标 | 复用真实角色账号和 owner 私聊目标；A01 默认目标在 tick 选定、Drift 在一次发送 claim 前选定并冻结；任何失效均不自动改投 | 桌面 presence 信号必须真实存在才声称复刻在场优先；若先用显式默认目标，说明与上游算法差异 |
| 提交与投递 | A01 单草稿+一次终结 CAS，Drift 单发送 claim；各自 canonical outbox、原目标、真实 receipt；uncertain 不自动重发 | 本地防重不等于远端恰好一次；发送后失联仍可能已生效 |
| ACK 与事件去重 | 来源版本+稳定事件域；成功/明确丢弃/暂缓分理由；ACK 独立持久重试及截止，inflight/uncertain 阻止再推荐同意图 | 保留 TTL 兼容语义，修复上游无 durable ACK 与空候选接受伪 ID，不新增长期偏好自动写入 |
| Drift 执行范围 | 私有工作根+只读角色/内建资源；现有 Shell 规则；35/20 步参考值外再给整 run <=600 秒及 token/输出/抓取/工作文件限额 | 步数不是总资源保证；挂载只激活已获 grant 工具，成功后 schema 与实际 dispatch 都收窄 |
| 作者产物激活 | S06 的长期技能在后续 Drift 实行；S08 先产草稿/合成验证，再由具体 trust/grant 激活真实来源；复用现有 MCP 配置与秘密引用 | 不让生成 Skill 自动执行新任意 command/env；不把永久禁用真实消费当完成 |

无需为此重建模型网关、通用事件总线、第二套任务队列、独立全局 MCP 管理器或通用操作系统沙箱。
可将 source snapshot、finish、claim、receipt、ACK 恢复藏在一个主动深模块后，外部只接宿主 tick/停用/查询与已有工具适配。
具体预算常数、保留期和轻量 UI 可在 Spec 中落定；本研究不把它们假称上游保证，也不要求批准前先实现生产管线。

## 9. 后续验收应证明什么

- 用合成 MCP 覆盖三路来源、重复 event_id 域、来源版本更换、poll 与 get 区别、正文失败及 failed 不等于 empty；读缓存不二次访问来源。
- 证明记忆检索有角色/目标范围；unknown/空候选 ID 拒绝；近期聊天不读主动/内部帧；interest 不是直接长期偏好写入。
- 通过真实 AgentTool+SQLite 验证草稿重复、无草稿 reply、有草稿 skip、重复 finish、预算退出；一次提交跨 tool call/重启/目标变化仍不可重复。
- 在远端已接受、本地回执/消息投影/ACK 未写的每个窗口注入崩溃；恢复只修复本地事实及合法 ACK，uncertain 不再发消息。
- Drift 进入/冷却/主人抢占、取消 join、目录与链接逃逸、挂载 grant 撤销、发送失败/unknown、第二次尝试、成功后的越权 dispatch 都走实际 Interface。
- S06 创建一份合法长期技能，下一次合格 Drift 真正执行并保留进度；S08 创建可启动的测试源，验证 ACK 经重启持久后，再在单独获准环境演示真实来源。
- 架构批准不代表真实账号发送、网络服务写入或费用许可；实机验收仍需对应主人配置/授权，保留总 Spec 的真实完成目标。

## 10. 证据局限与工作顺序

本研究未运行固定 Shiori 或生产 Meido 主动任务；对远端平台、来源 ACK 幂等能力和实际 MCP 服务的结论，仅限当前源码所表达的契约。
S08 的旧评分/紧急文案与当前执行链不一致，实施时应更新技能说明，不能据旧文案扩大 A01 保证。
T21 应先完成自主来源与 A01 提交/投递/ACK 纵切面，T22 再复用它实现 Drift 与 S06；S08 纳入 T21，避免先造无人消费的作者工具。
共享任务/调度代码的顺序按[总 Spec T21/T22](../specs/shiori-tool-parity-spec.md#9-垂直-ticket-划分和依赖)与实际开放工作协调；本聚合采用已获准集成流程，不虚构必须等待 main 的额外批准。
下一产物应是具体主动/Drift Spec 及必要 ADR 细化；本研究没有授予新的自主许可，也没有修改已批准总目标。

