# Shiori 普通 76 工具当前覆盖审计（2026-10-17）

状态：草案，待协调者复核；固定提交静态审计，不是新的原生或真实服务验收。

最后核验：2026-10-17（Asia/Shanghai）。Meido 固定提交 `f4ac973a04cdcd8327f80f20bd58d5088fdf8b77`；上游固定提交 [`4f266aeaf42fcd611362e2456417b91f659341cf`](https://github.com/YinFengWindy/Shiori-Agent/commit/4f266aeaf42fcd611362e2456417b91f659341cf)。本文件的源码行号均针对这两个提交，后续实现不能倒填为本次已验证。

事实来源：[Parent #137](https://github.com/sawaki-zexi/meido/issues/137)、[完整规格](../specs/shiori-tool-parity-spec.md)、[原始固定源码研究](shiori-tool-system-and-portability.md)、[当前范围表](../reference/shiori-tool-parity-status.md)、下文逐项源码与测试。审计已阅读 `AGENTS.md`、`CONTEXT.md`、文档规范和相关已采纳 ADR；只新增本文，不修改生产、配置、测试、原生资源或用户数据。

## 审计结论与计数

**已提取：本表逐个列出核心 31 项和插件 45 项，共 76 个不同的上游普通工具名。75 项能沿当前生产装配追踪到实际执行路径；`pet_action` 没有实现。这个数字不是“75 项已完成验收”。** `tool_search` 虽有执行、冻结目录和版本激活，关键词召回仍存在下文 G01 的确定差距；`message_push` 还存在 G03 的访客 schema 与执行许可不一致。真实平台、付费模型、NovelAI、实际 Codex/Claude 认证及桌面扩展环境仍有独立的未验范围。

**已提取：不是全局注册了 75 个工具就会全部发给每个角色。** 宿主候选由角色和真实来源装配，接着经过开关、插件 grant、宿主依赖、外部发言者、风险、审批与 deferred 过滤；`tool_search` 在冻结候选中激活，不能增加权限。目录 API 是只读预览，还会明确未运行的依赖检查，不证明服务已连通。[候选与来源](../../backend/app/main.py#L3946)、[角色与插件策略](../../backend/app/main.py#L4190)、[运行身份限制](../../backend/app/main.py#L4299)、[冻结准入](../../backend/app/agent_runtime/capabilities.py#L396)、[目录 API](../../backend/app/main.py#L1625)、[目录预览状态](../../backend/app/agent_runtime/catalog.py#L65)。

**审计范围：** 使用 UTF-8 源码读取、stdlib AST/JSON 和固定 Git 对象比较，以及 GitHub Issue/PR 只读查询；没有导入应用、收集或运行 pytest、构建、启动浏览器/驱动、操作窗口、读取凭据、调用模型或执行平台事务。下表“行为证据”指当前测试确实包含的公开行为及对应历史验收记录；不是声称本次执行通过。8 个 Skill、A01/A02 由另一份审计负责，不混入 76 的分母；Drift 表情库 #191 也不是第 77 个普通工具。

| 范围 | 逐项条目 | 有实际执行路径及生产装配 | 确定实现缺口 / 差距 |
| --- | ---: | ---: | --- |
| 核心 C01–C10 | 31 | 31 | G01：搜索召回；G03：访客看到必拒绝的桌面推送 schema |
| 浏览器 P01 | 28 | 28 | 本次未发现额外确定缺实现；真实站点行为不能由本地页面全称覆盖 |
| 桌面 P02 | 8 | 8 | 已批准保守路由；实际辅助 VK 到件和扩展环境未验，不记作生产未实现 |
| Coding P03 | 5 | 5 | 原生宿主 + 合成 CLI 有记录；真实认证、消费与外部发布另验 |
| 屏幕 P04 | 1 | 1 | 单屏/125%/本地视觉有记录；扩展环境与真实视觉服务未验 |
| 桌宠 P05 | 1 | 0 | G02：真实原生宿主、包、动作桥和工具均未实现，方案选择待回答 |
| 生图 P06 | 1 | 1 | 有真实 HTTP 客户端和媒体账本；真实 NovelAI 与费用未验 |
| QQ 附件 P07 | 1 | 1 | 当前及引用入站已有公开测试；真实平台另验 |
| **合计** | **76** | **75** | **不能据此关闭完整目标** |

## 共同装配、配置和授权证据

下表中的门禁编号供逐项表引用；每个编号指生产行为，不是独立工具。

| 门禁 | 生产装配、角色配置与模型可见性 |
| --- | --- |
| B：基础逐项工具 | `agentConfig.enabledTools`；基础表单提供文件、网络、原文、记忆写入、发现、推送和时间任务的逐项开关。[模型](../../backend/app/models.py#L69)、[设置入口](../../web/src/features/role-management/BasicToolSettings.tsx#L3)、[保存保留](../../web/src/features/role-management/draft.ts#L28)。开关不替代执行层来源检查。[目录/真实 schema 对照测试](../../tests/python/test_role_capabilities.py#L106)。 |
| D：发现 | `tool_search` 开启后由 CapabilityRegistry 加入；候选先通过宿主许可，账号/群/浏览器/MCP/Coding 等成为 deferred；只有持久化的版本激活进入下一 provider 请求。[加入](../../backend/app/agent_runtime/capabilities.py#L375)、[deferred 集](../../backend/app/main.py#L4247)、[搜索与 pending](../../backend/app/agent_runtime/discovery.py#L341)。 |
| F：文件 | B；只限当前角色 workspace，主人来源；相对/绝对路径、链接/重解析、硬链接拒绝和取消在实际 I/O 层检查。[装配](../../backend/app/main.py#L4086)、[边界](../../backend/app/agent_runtime/file_tools.py#L119)。部分写入经过已有 Drift 作者许可包装，不给普通调用增加目录外写权。[包装](../../backend/app/main.py#L4087)。 |
| T：Shell/任务 | 独立 `shell.enabled`、非空 argv 白名单、固定 workspace、最小环境、角色预算及当前真实主人来源；任务保留准确 role/origin，不能凭 ID 接管。[装配](../../backend/app/main.py#L4109)、[实时准入](../../backend/app/main.py#L3998)、[执行](../../backend/app/agent_runtime/shell_tool.py#L82)、[持久 owner 校验](../../backend/app/task_runtime/runtime.py#L457)。 |
| N：网络 | B + 宿主 NetworkToolsConfig；允许受限外部来源，实际 DNS、逐跳重定向、字节/时间预算由执行模块检查。允许指定抓取 URL 不等于允许 localhost/内网。[装配](../../backend/app/main.py#L4122)、[真实下载](../../backend/app/network_tools.py#L141)。 |
| M：记忆 | 全局记忆开关；recall 另需角色 memoryRecall；memorize/forget 需 B；统一角色范围、真实 user message 与来源重验，不让参数覆盖身份。[宿主绑定](../../backend/app/main.py#L3913)、[RoleMemoryAccess](../../backend/app/agent_runtime/memory_write_tools.py#L36)、[角色开关](../../backend/app/main.py#L4203)。 |
| H：原文 | B；原角色桌面或仍有效的配对主人私聊可看获准主人历史；群/访客限当前线程。只查询 completed 正常文字，记忆摘要不变成原话。[装配](../../backend/app/main.py#L4130)、[真实范围](../../backend/app/agent_runtime/message_history_tools.py#L29)。 |
| C：账号 | 可用角色渠道账号/真实渠道来源才装配；D 延迟公开。owner 参数来自宿主，外部 `account_send` schema 只准 `target_id=owner`、当前来源账号；账号目录有来源代际和有界游标。[装配](../../backend/app/main.py#L4139)、[策略](../../backend/app/main.py#L4223)、[外部 schema](../../backend/app/channels/tools.py#L137)、[实际宿主发送](../../backend/app/channels/host.py#L420)。 |
| G：群/成员 | 有本地结构化资料或渠道账号时装配；D。GroupToolAccess 根据真实来源限定主人/当前群/访客，不因构造参数自称 owner 即放行。[装配](../../backend/app/main.py#L4124)、[资料可用判断](../../backend/app/main.py#L4199)、[来源与写入](../../backend/app/channels/group_access.py#L25)。 |
| S：spawn | `spawn.enabled`、有限轮数/时间/累计模型输出、当前真实主人来源；只用冻结父工具与 profile 交集。coding profile 还需独立 Coding 许可。[装配](../../backend/app/main.py#L4098)、[实时来源](../../backend/app/main.py#L4017)、[子 loop](../../backend/app/agent_runtime/spawn_tool.py#L204)。 |
| Q：时间任务 | B；真实主人输入来源，SQLite 独立于 Todo；soft 保存原模型/工具范围，计划与结果固定原来源。[装配](../../backend/app/main.py#L4093)、[来源](../../backend/app/main.py#L4054)、[冻结](../../backend/app/schedules/tools.py#L39)、[持久应用](../../backend/app/schedules/application.py#L132)。 |
| X：MCP | B + tool_search + 主人预授权固定 server 配置；D。普通运行先冻结 grant，`mcp_add` 只选择 `server_id`，不接受模型 command/env；来源改变关闭原进程，不复活权限。[装配](../../backend/app/main.py#L4134)、[替换为 run tools](../../backend/app/main.py#L4389)、[配置门禁](../../backend/app/mcp_config.py#L28)、[连接/修订](../../backend/app/mcp_runtime.py#L426)。 |
| BR：浏览器 | `browserUse.enabled`、builtin plugin 与 `browser.session` 宿主 grant；D。全部 28 个真实 BrowserTool 由插件工厂提供，角色独立 profile、run 独立进程会话，关闭/撤权收尾。[插件](../../backend/app/browser_use/plugin.py#L15)、[策略](../../backend/app/main.py#L4286)、[工具执行](../../backend/app/browser_use/tool.py#L42)、[原生 RPC](../../backend/app/browser_use/session.py#L49)。 |
| CU：桌面 | `computerUse.enabled` + `desktop.control`，仅持久证明的主人主动桌面 run；Windows 普通权限/会话/SID/主屏窗口校验。所有工具由统一工厂装配，真实 PinnedDriverSession 运行固定 direct exe；单观察与全局输入 lease。[装配](../../backend/app/main.py#L4108)、[来源过滤](../../backend/app/main.py#L4317)、[工具适配](../../backend/app/computer_use/tools.py#L11)、[真实 exe](../../backend/app/computer_use/driver.py#L31)。 |
| CO：Coding | 角色 coding + 全局 enabled、固定 native bundle、受信仓库和 CLI profile + 核验记录；D。只限真实主人输入原来源，冻结 generation 防撤权后重用。[装配](../../backend/app/main.py#L4104)、[来源](../../backend/app/main.py#L4071)、[候选开关](../../backend/app/main.py#L4215)、[实际配置解析](../../backend/app/coding_tasks/config.py#L232)。 |
| SC：屏幕 | 默认关；显式 vision configuration ID，不能 fallback 到聊天/active；仅主人当前桌面 run。独立 worker 和 adapter 返回严格摘要，不返回原图。[装配](../../backend/app/main.py#L4107)、[依赖](../../backend/app/main.py#L4169)、[身份重验](../../backend/app/screen_perception/__init__.py#L126)。 |
| NI：NovelAI | 角色 novelai 与独立 vault 服务、有限许可、当前主人真实输入/附件；固定 run/service/role/source 版本；一次 attempt 最多一次生成 POST。[装配](../../backend/app/main.py#L4091)、[可用性](../../backend/app/main.py#L4195)、[绑定与执行](../../backend/app/novelai_generation/tools.py#L31)、[真实 POST](../../backend/app/novelai_generation/http.py#L47)。 |
| AT：附件 | 只在 QQ 真实回合注入当前/引用入站附件 ID allowlist；D，允许当前外部发言者读取其获准附件；路径不是模型参数。[装配](../../backend/app/main.py#L5400)、[实际读](../../backend/app/channels/attachment_tool.py#L26)。 |

## 核心工具：31 / 31 逐项列出

“实现”表示已读到 I/O/业务服务和生产装配；“未重跑”对下表全部适用。行号可定位当前测试中的断言与实际公开流程。

| 序号 | Shiori 工具 → Meido 工具 | 实际执行实现 | 装配 / 配置 | 当前行为证据与剩余范围 |
| --- | --- | --- | --- | --- |
| 01 | `tool_search` → 同名 | [execute](../../backend/app/agent_runtime/discovery.py#L341)，冻结候选查询、risk、精确 select、待提交激活 | D | [同 run 加载真实浏览器 schema](../../tests/python/test_tool_discovery_api.py#L125)、[持久化前不发请求](../../tests/python/test_tool_discovery.py#L42)、[中英文/风险](../../tests/python/test_tool_discovery.py#L169)。**G01 确定召回差距；不能标完整。** |
| 02 | `read_file` → 同名 | [实际读行及图片](../../backend/app/agent_runtime/file_tools.py#L302)，图像解码压缩为临时 ImageContent | F | [真实 UTF-8 页](../../tests/python/test_file_tools.py#L33)、[图片到下一模型请求且不存 base64](../../tests/python/test_file_tools_api.py#L103)。角色目录限制为已确认适配。 |
| 03 | `list_dir` → 同名 | [os.scandir 单层列表](../../backend/app/agent_runtime/file_tools.py#L479) | F | [排序/单层/来源](../../tests/python/test_file_tools.py#L92)、[大目录显式截断](../../tests/python/test_file_tools.py#L243)。 |
| 04 | `write_file` → 同名 | [UTF-8 整体写](../../backend/app/agent_runtime/file_tools.py#L406)、[临时文件/fsync/replace](../../backend/app/agent_runtime/file_tools.py#L217) | F | [创建父目录/覆盖](../../tests/python/test_file_tools.py#L48)、[公开 write/read/edit/list 往返及撤权](../../tests/python/test_file_tools_api.py#L48)。 |
| 05 | `edit_file` → 同名 | [精确匹配、CRLF/BOM、diff、原子提交](../../backend/app/agent_runtime/file_tools.py#L426) | F | [BOM/CRLF/diff](../../tests/python/test_file_tools.py#L59)、[歧义拒绝](../../tests/python/test_file_tools.py#L74)、[并发实例不丢更新](../../tests/python/test_file_tools.py#L292)、[扩张前字节预算](../../tests/python/test_file_tools_memory_limit.py#L8)。 |
| 06 | `shell` → 同名 | [argv/环境/提交](../../backend/app/agent_runtime/shell_tool.py#L82) → [真实 ProcessExecutor](../../backend/app/task_runtime/process.py#L28) | T | [后台跨父回合](../../tests/python/test_background_shell.py#L25)、[15 秒提升保留硬截止](../../tests/python/test_background_shell.py#L59)、[真实子孙进程收尾](../../tests/python/test_background_shell.py#L145)。依 ADR-0004 保留 argv 白名单，**不是上游任意 pwsh/shell 脚本接口**。 |
| 07 | `task_output` → 同名 | [query 同一任务/有界 wait](../../backend/app/agent_runtime/shell_tool.py#L188) | T | [后续真实 HTTP 回合查询与停止](../../tests/python/test_background_shell_api.py#L62)、[跨角色/来源 ID 拒绝](../../tests/python/test_background_shell.py#L227)。Shell 完成主动拉取，不混同 spawn 自动回传。 |
| 08 | `task_stop` → 同名 | [to_thread stop](../../backend/app/agent_runtime/shell_tool.py#L217) → TaskRuntime owned process tree | T | [子孙实际退出](../../tests/python/test_background_shell.py#L145)、[真实 HTTP 撤权/删除](../../tests/python/test_background_shell_api.py#L139)、[崩溃后 interrupted 而非重跑](../../tests/python/test_background_shell.py#L284)。 |
| 09 | `web_search` → 同名 | [服务 payload、POST 和 JSON/SSE 解析](../../backend/app/network_tools.py#L334) | N | [Shiori 参数与全部结果](../../tests/python/test_network_tools.py#L183)、[可配置认证不泄密](../../tests/python/test_network_tools.py#L205)、[HTTP 工具往返与来源持久化](../../tests/python/test_network_api.py#L28)。当前服务可达性需具体环境诊断。 |
| 10 | `web_fetch` → 同名 | [实际下载/编码/格式转换](../../backend/app/network_tools.py#L277) | N | [真实来源状态](../../tests/python/test_network_tools.py#L19)、[逐跳与 DNS 拒内网](../../tests/python/test_network_tools.py#L56)、[有界流和转换 deadline](../../tests/python/test_network_tools.py#L91)。更严格内网边界是宿主约束。 |
| 11 | `memorize` → 同名 | [真实 user source → MemoryMutation](../../backend/app/agent_runtime/memory_write_tools.py#L121) | M | [真实引擎/SQLite/文档/HTTP 全往返](../../tests/python/test_memory_tools_api.py#L26)、[来源与 procedure 去重](../../tests/python/test_memory_write_tools.py#L141)、[迟到写完成后回滚](../../tests/python/test_memory_tools_api.py#L129)。 |
| 12 | `recall_memory` → 同名 | [过滤/时间窗/只读 query](../../backend/app/agent_runtime/memory_tool.py#L106) | M | [作用域与只读](../../tests/python/test_memory_tool.py#L36)、[真实 kind/domain/time/evidence](../../tests/python/test_memory_write_tools.py#L53)、[全预设及日期](../../tests/python/test_memory_write_tools.py#L236)。仍保留 Meido 引擎，不复制上游数据库。 |
| 13 | `forget_memory` → 同名 | [角色范围失效而非物理删](../../backend/app/agent_runtime/memory_write_tools.py#L149) | M | [owned IDs、重复 unchanged](../../tests/python/test_memory_write_tools.py#L73)、[伪身份拒绝](../../tests/python/test_memory_write_tools.py#L99)、[HTTP 记住/查找/失效](../../tests/python/test_memory_tools_api.py#L26)。 |
| 14 | `search_messages` → 同名 | [获准原文筛选/排名/分页](../../backend/app/agent_runtime/message_history_tools.py#L95) | H | [搜索后回原话](../../tests/python/test_message_history_tools.py#L40)、[群/访客隔离](../../tests/python/test_message_history_tools.py#L84)、[下一模型与持久结果](../../tests/python/test_message_history_tools_api.py#L26)。 |
| 15 | `fetch_messages` → 同名 | [source_ref/evidence/相邻原文](../../backend/app/agent_runtime/message_history_tools.py#L120) | H | [refs 和同 transcript 邻居](../../tests/python/test_message_history_tools.py#L105)、[配对变更不升级旧回合](../../tests/python/test_message_history_tools.py#L160)。 |
| 16 | `message_push` → 同名 | [附件核验 + append_desktop_push_once](../../backend/app/desktop_push.py#L31) | B；[当前角色主人来源](../../backend/app/main.py#L4030) | [文本真实 HTTP/历史](../../tests/python/test_desktop_push_api.py#L14)、[上传媒体单次投递/下载](../../tests/python/test_desktop_push_api.py#L76)、[事务最后授权](../../tests/python/test_desktop_push.py#L160)。仅桌面，外部交给 account_send；附件 ID 替代任意路径。**G03：guest 可见但必拒绝。** |
| 17 | `account_list` → 同名 | [公开入口](../../backend/app/channels/tools.py#L45) → [当前角色真实账号/连接事实](../../backend/app/channels/host.py#L200) | C | [网页与模型共享 transport 证据](../../tests/python/test_channel_directory_api.py#L281)、[Telegram 成功轮询后才 online](../../tests/python/test_channel_directory_api.py#L294)。Meido 显式 account_id 与上游隐藏 ID 不同，非缺实现。 |
| 18 | `account_targets` → 同名 | [分类查询与授权游标](../../backend/app/channels/tools.py#L74) → [query_directory](../../backend/app/channels/host.py#L268) | C | [QQ 分类/群成员范围](../../tests/python/test_channel_directory_api.py#L145)、[Telegram member 非发送目标](../../tests/python/test_channel_directory_api.py#L220)、[游标绑定代际](../../tests/python/test_channel_directory_api.py#L383)。平台支持 kinds 明确返回，不把 known 缓存冒称全目录；真实账号未验。 |
| 19 | `account_send` → 同名 | [参数映射](../../backend/app/channels/tools.py#L145) → [ChannelHost 账本/真实 adapter](../../backend/app/channels/host.py#L420) | C | [role 附件 ID](../../tests/python/channels/test_tools.py#L28)、[无真实平台 ID 不冒称 sent](../../tests/python/channels/test_delivery_receipts.py#L21)、[平台媒体协议](../../tests/python/channels/test_napcat_outbound_contract.py)。本地协议不等于真人收件；真实平台另验。 |
| 20 | `spawn` → 同名 | [profile → 独立 bounded child / coding](../../backend/app/agent_runtime/spawn_tool.py#L282) | S | [父锁内真实文件](../../tests/python/test_spawn_api.py#L24)、[后台只回原来源一次](../../tests/python/test_spawn_api.py#L64)、[父子独立真实 HTTP transcript](../../tests/python/test_spawn_api.py#L226)。非付费 fixture 不是实际模型消费验收。 |
| 21 | `spawn_manage` → 同名 | [list/get/cancel/精确 coding continue](../../backend/app/agent_runtime/spawn_tool.py#L345) | S | [后续 HTTP list/get/cancel 同一 job](../../tests/python/test_spawn_api.py#L511)、[跨角色/来源封存](../../tests/python/test_spawn.py#L159)、[重启只修 receipt 不重发](../../tests/python/test_spawn_completion_recovery.py#L49)。 |
| 22 | `schedule` → 同名 | [参数+宿主收到时间](../../backend/app/schedules/tools.py#L52) → [持久 plan/claim](../../backend/app/schedules/application.py#L132) | Q | [create/list/due/cancel HTTP](../../tests/python/test_schedules_api.py#L11)、[soft 父结束后实际只读文件一次](../../tests/python/test_schedules_api.py#L56)、[相对时间在模型前固定](../../tests/python/test_schedules_api.py#L165)、[完整时间规则](../../tests/python/test_schedule_time_rules.py#L12)。 |
| 23 | `list_schedules` → 同名 | [当前精确 role/origin list](../../backend/app/schedules/tools.py#L81) → [application.list](../../backend/app/schedules/application.py#L181) | Q | [HTTP 列表往返](../../tests/python/test_schedules_api.py#L11)、[渠道来源撤权](../../tests/python/test_schedules_authority.py#L18)。比上游“当前角色全部”更严格原来源适配；不混 Todo。 |
| 24 | `cancel_schedule` → 同名 | [join 取消事务](../../backend/app/schedules/tools.py#L81) → [application.cancel](../../backend/app/schedules/application.py#L186) | Q | [重启仍取消](../../tests/python/test_schedules.py#L55)、[至少8位/歧义拒绝](../../tests/python/test_schedules.py#L84)、[取消等待线程](../../tests/python/test_schedule_cancellation.py#L13)。已提交的送达保留，不说后来 Stop 可回滚。 |
| 25 | `set_group_listening` → 同名 | [来源限定目标与真实写入](../../backend/app/channels/group_tools.py#L331) | G；NapCat QQ 能力 | [当前群/主人约束](../../tests/python/channels/test_group_tools.py#L177)、[实际监听持久语义](../../tests/python/channels/test_listening_parity.py)、[写前取消/角色检查](../../tests/python/channels/test_group_tools.py#L282)。只旁听，不授权每条主动发言；真人群另验。 |
| 26 | `lookup_member` → `lookup_group_member` | [名称/ID/历史昵称/档案来源分页](../../backend/app/channels/group_tools.py#L370)，description 明示上游别名 | G | [重启历史昵称](../../tests/python/channels/test_member_lookup.py#L22)、[不同群档案不合并](../../tests/python/channels/test_member_lookup.py#L79)、[真实来源范围](../../tests/python/channels/test_member_lookup.py#L131)、[分页](../../tests/python/channels/test_member_lookup.py#L240)。稳定名称适配已写入总规格 C09。 |
| 27 | `lookup_group_context` → 同名 | [角色群候选/分页/结构化笔记](../../backend/app/channels/group_tools.py#L255) | G | [较早群不丢失](../../tests/python/channels/test_group_tools.py#L93)、[主人跨群但不返回原文](../../tests/python/channels/test_group_tools.py#L177)、[私聊/别角色拒绝、离线本地资料](../../tests/python/channels/test_group_tools.py#L263)。 |
| 28 | `update_group_context` → 同名 | [省略保留/显式清空/事务提交](../../backend/app/channels/group_tools.py#L298) | G | [省略与清空](../../tests/python/channels/test_group_tools.py#L134)、[并发 patch 不覆盖省略字段](../../tests/python/channels/test_group_tools.py#L320)、[最后写前权限](../../tests/python/channels/test_group_tools.py#L282)。 |
| 29 | `mcp_add` → 同名 | [management dispatch](../../backend/app/mcp_runtime.py#L34) → [真实 stdio handshake 与 service revision](../../backend/app/mcp_runtime.py#L426) | X | [公开 HTTP 真实进程/同 run 加工具/移除/恢复意愿](../../tests/python/test_mcp_http.py#L224)、[运行中新授权不得扩旧目录](../../tests/python/test_mcp_http.py#L411)。server_id 选择而非任意 command/env 为 D01 已批准收紧。 |
| 30 | `mcp_list` → 同名 | [list_services 真实 intent/client/failure](../../backend/app/mcp_runtime.py#L464) | X | [意愿与真实握手生命周期](../../tests/python/test_mcp_http.py#L224)、[已退出 peer 明确 unhealthy](../../tests/python/test_mcp_http.py#L554)。列表本身不启动服务；动态远端工具不计入固定76。 |
| 31 | `mcp_remove` → 同名 | [取消进程、清 intent、stage removal](../../backend/app/mcp_runtime.py#L454) | X | [inflight 远端及子孙进程终止/幂等](../../tests/python/test_mcp_origin.py#L257)、[same run remove/readd 新版本](../../tests/python/test_mcp_http.py#L437)。保留原 owner 清理，不把超时观察当进程退出。 |

## 插件工具：45 / 45 逐项列出

### Browser Use：28 项

**已提取：** 本次把本项目 [tools.json](../../backend/app/browser_use/tools.json) 与固定上游 Git 对象 `plugins/browser_use/backend/tools.json` 解析比较：两个数组均为 28 项、名字唯一且完整 JSON 相等（名称、说明、全部 schema）。这只证明声明一致。另已核实每项由 `load_tools → BrowserTool.execute → BrowserSessions.call → BrowserSession.call → MCP` 走真实固定浏览器路径，而非空 handler；`close` 特判进入 owned 关闭流程。[加载与执行](../../backend/app/browser_use/tool.py#L42)、[session 原生调用](../../backend/app/browser_use/session.py#L49)、[关闭](../../backend/app/browser_use/tool.py#L276)。

28 行均使用 BR 门禁与 D 发现。当前 [真实本地页面验收](../../tests/python/test_browser_use.py#L684) 最后以 `invoked == set(tools)` 检查调用全集，且有控件状态、URL、标题、tab 和 profile 的独立断言；[历史验收记录](browser-use-native-acceptance-2026-10-15.md)记载强化后的 21 passed / 0 skip。本文未启动该测试，不能把历史单机本地页面结果推广到任意当前网站、账号或发行环境。

| 序号 | Shiori / Meido 名称 | 生产声明 → 真实公共适配 | 对应行为验收（当前源行） | 剩余边界 |
| --- | --- | --- | --- | --- |
| 32 | `agent_browser_open` | [tools.json](../../backend/app/browser_use/tools.json#L15) → BrowserTool/BR | [真实本地 URL 导航](../../tests/python/test_browser_use.py#L745) | 真实站点/账号未验 |
| 33 | `agent_browser_read` | [tools.json](../../backend/app/browser_use/tools.json#L59) → BrowserTool/BR | [本地页面正文](../../tests/python/test_browser_use.py#L746) | 远端渲染差异未泛化 |
| 34 | `agent_browser_snapshot` | [tools.json](../../backend/app/browser_use/tools.json#L105) → BrowserTool/BR | [可访问性快照与引用](../../tests/python/test_browser_use.py#L752)、[失效引用](../../tests/python/test_browser_use.py#L807) | 不宣称任意站点元素稳定 |
| 35 | `agent_browser_click` | [tools.json](../../backend/app/browser_use/tools.json#L133) → BrowserTool/BR | [点击按钮并核返回文字](../../tests/python/test_browser_use.py#L777) | 实际平台事务另批 |
| 36 | `agent_browser_fill` | [tools.json](../../backend/app/browser_use/tools.json#L156) → BrowserTool/BR | [引用文本框填入](../../tests/python/test_browser_use.py#L759) | 真实网站/认证未验 |
| 37 | `agent_browser_type` | [tools.json](../../backend/app/browser_use/tools.json#L189) → BrowserTool/BR | [追加文字并由点击结果核实](../../tests/python/test_browser_use.py#L760) | 同 BR |
| 38 | `agent_browser_press` | [tools.json](../../backend/app/browser_use/tools.json#L207) → BrowserTool/BR | [Tab 键](../../tests/python/test_browser_use.py#L776) | 未逐一证明每种 OS 键组合 |
| 39 | `agent_browser_check` | [tools.json](../../backend/app/browser_use/tools.json#L225) → BrowserTool/BR | [勾选后 eval 确认 true](../../tests/python/test_browser_use.py#L766) | 同 BR |
| 40 | `agent_browser_uncheck` | [tools.json](../../backend/app/browser_use/tools.json#L243) → BrowserTool/BR | [取消后 eval 确认 false](../../tests/python/test_browser_use.py#L769) | 同 BR |
| 41 | `agent_browser_select` | [tools.json](../../backend/app/browser_use/tools.json#L270) → BrowserTool/BR | [one→two 的独立值断言](../../tests/python/test_browser_use.py#L761) | 同 BR |
| 42 | `agent_browser_scroll` | [tools.json](../../backend/app/browser_use/tools.json#L301) → BrowserTool/BR | [真实 scrollY](../../tests/python/test_browser_use.py#L772) | 不声明任意嵌套站点容器已验 |
| 43 | `agent_browser_wait_ms` | [tools.json](../../backend/app/browser_use/tools.json#L319) → BrowserTool/BR | [等待](../../tests/python/test_browser_use.py#L751)、[长等待取消](../../tests/python/test_browser_use.py#L843) | 同 BR |
| 44 | `agent_browser_wait_for_selector` | [tools.json](../../backend/app/browser_use/tools.json#L342) → BrowserTool/BR | [selector 成立](../../tests/python/test_browser_use.py#L748) | 同 BR |
| 45 | `agent_browser_wait_for_text` | [tools.json](../../backend/app/browser_use/tools.json#L364) → BrowserTool/BR | [实际文本成立](../../tests/python/test_browser_use.py#L749) | 同 BR |
| 46 | `agent_browser_wait_for_load` | [tools.json](../../backend/app/browser_use/tools.json#L391) → BrowserTool/BR | [load 状态](../../tests/python/test_browser_use.py#L750) | 同 BR |
| 47 | `agent_browser_screenshot` | [tools.json](../../backend/app/browser_use/tools.json#L441) → BrowserTool/BR | [真实截图](../../tests/python/test_browser_use.py#L780)、[HTTP 下一请求临时图](../../tests/python/test_browser_use.py#L902) | 不把临时图等同持久原图 |
| 48 | `agent_browser_get_text` | [tools.json](../../backend/app/browser_use/tools.json#L459) → BrowserTool/BR | [核按钮结果 saved:Meido-native](../../tests/python/test_browser_use.py#L778) | 同 BR |
| 49 | `agent_browser_get_url` | [tools.json](../../backend/app/browser_use/tools.json#L470) → BrowserTool/BR | [当前页/后退/前进 URL](../../tests/python/test_browser_use.py#L738) | 同 BR |
| 50 | `agent_browser_get_title` | [tools.json](../../backend/app/browser_use/tools.json#L481) → BrowserTool/BR | [每次 tab URL/title 断言](../../tests/python/test_browser_use.py#L739) | 同 BR |
| 51 | `agent_browser_eval` | [tools.json](../../backend/app/browser_use/tools.json#L499) → BrowserTool/BR | [DOM 选择/勾选/滚动及持久 storage](../../tests/python/test_browser_use.py#L761) | 网页脚本事务不获额外授权 |
| 52 | `agent_browser_close` | [tools.json](../../backend/app/browser_use/tools.json#L510) → [真实 close_run](../../backend/app/browser_use/tool.py#L276) | [关闭后重开 profile](../../tests/python/test_browser_use.py#L820)、[实际 owned PID 消失](../../tests/python/test_browser_use.py#L859) | 仅声称自有进程清理 |
| 53 | `agent_browser_back` | [tools.json](../../backend/app/browser_use/tools.json#L521) → BrowserTool/BR | [后退后核 URL](../../tests/python/test_browser_use.py#L813) | 同 BR |
| 54 | `agent_browser_forward` | [tools.json](../../backend/app/browser_use/tools.json#L532) → BrowserTool/BR | [前进后核 URL](../../tests/python/test_browser_use.py#L816) | 同 BR |
| 55 | `agent_browser_reload` | [tools.json](../../backend/app/browser_use/tools.json#L543) → BrowserTool/BR | [真实页面 reload](../../tests/python/test_browser_use.py#L819) | 同 BR |
| 56 | `agent_browser_tab_new` | [tools.json](../../backend/app/browser_use/tools.json#L561) → BrowserTool/BR | [新页及不同内容](../../tests/python/test_browser_use.py#L788) | 同 BR |
| 57 | `agent_browser_tab_list` | [tools.json](../../backend/app/browser_use/tools.json#L572) → BrowserTool/BR | [初始及关页后的实际集合](../../tests/python/test_browser_use.py#L782) | 同 BR |
| 58 | `agent_browser_tab_switch` | [tools.json](../../backend/app/browser_use/tools.json#L590) → BrowserTool/BR | [切回原 tab、按 label 切新页，各核 URL/title](../../tests/python/test_browser_use.py#L797) | 同 BR |
| 59 | `agent_browser_tab_close` | [tools.json](../../backend/app/browser_use/tools.json#L606) → BrowserTool/BR | [关闭后原 tab ID/URL 独立断言](../../tests/python/test_browser_use.py#L801) | 同 BR |

### Computer Use：8 项

**已提取：** 实现没有把鼠标/键盘动作返回 `accepted` 就当作目标效果成功。所有输入以观察身份、token 或有来源的像素映射约束；动作前验证前台活动及 `IsWindowEnabled`。开始派发后结果 `effect=unknown`，即使 driver 返回成功，也不宣称 UI 已完成。缺回执、载体歧义和错误不伪造零次。[公共 host](../../backend/app/computer_use/host.py#L235)、[动作准入](../../backend/app/computer_use/host.py#L438)、[回执校验](../../backend/app/computer_use/host.py#L57)。

8 行均使用 CU。已批准的适配为 Windows x64、普通权限 direct、主屏完整窗口、background 仅目标已是精确 foreground 的子集；不支持提权、UIAccess helper、跨屏或远程 console。上述限制见 [ADR-0015](../adr/0015-owned-windows-computer-use.md)，不是用永远拒绝的占位替代动作。

| 序号 | Shiori / Meido 名称 | 生产实现与真实映射 | 当前行为证据 | 未验范围 |
| --- | --- | --- | --- | --- |
| 60 | `computer_list_windows` | [tools 工厂加 computer_ 前缀](../../backend/app/computer_use/tools.py#L15) → [driver list_windows 并校验 PID/HWND](../../backend/app/computer_use/host.py#L307) | [专用 fixture 实际列窗](../../tests/python/test_computer_use_native.py#L127)、[错误来源不启动驱动](../../tests/python/test_computer_use.py#L115) | 非普通权限/其他会话不声明支持 |
| 61 | `computer_get_window_state` | [实际驱动 state](../../backend/app/computer_use/host.py#L290) → [_observe 图片/元素](../../backend/app/computer_use/host.py#L383) | [固定驱动来源/主屏最大化](../../tests/python/test_computer_use_native.py#L37)、[失败观察也使旧引用失效](../../tests/python/test_computer_use.py#L169) | 多DPI/多屏、双采集失败与遮挡组合未验 |
| 62 | `computer_click` | [公共输入准入与 call](../../backend/app/computer_use/host.py#L295) → 原生 click | [缩小图像真实像素命中 + token 点击](../../tests/python/test_computer_use_native.py#L137)、[禁用目标零派发](../../tests/python/test_computer_use.py#L975) | 辅助 VK 实际到件未证；真实用户应用未操作 |
| 63 | `computer_type_text` | [预算/统一 host](../../backend/app/computer_use/tools.py#L18) → 原生 type_text | [真实中文输入及最终 text 断言](../../tests/python/test_computer_use_native.py#L147)、[focus/restore 回执协议](../../tests/python/test_computer_use.py#L371) | 其他应用路由/IME 不泛化 |
| 64 | `computer_press_key` | [统一 host action](../../backend/app/computer_use/host.py#L295) → 原生 press_key | [End 实际事件](../../tests/python/test_computer_use_native.py#L148)、[五类禁用拒绝](../../tests/python/test_computer_use.py#L975) | 不把一个键的通过扩成所有键组合 |
| 65 | `computer_hotkey` | [显式 foreground 检查](../../backend/app/computer_use/host.py#L458) → 原生 hotkey | [Ctrl+A 实际事件](../../tests/python/test_computer_use_native.py#L149)、[旧观察/活动改变拒绝](../../tests/python/test_computer_use.py#L728) | 扩展应用与辅助激活未验 |
| 66 | `computer_scroll` | [统一 host](../../backend/app/computer_use/host.py#L295) → 原生 scroll | [background 真实拒绝，再以新观察分别 foreground 四方向](../../tests/python/test_computer_use_native.py#L150)、[不虚构前台调用的回执](../../tests/python/test_computer_use.py#L431) | 不承诺所有应用 background wheel |
| 67 | `computer_release` | [close_run/封存本 run](../../backend/app/computer_use/host.py#L245) | [release 后同 run 再调用拒绝](../../tests/python/test_computer_use_native.py#L163)、[owner 死亡 Job/lease](../../tests/python/test_computer_use_native.py#L193)、[真实 Stop](../../tests/python/test_computer_use_native.py#L240) | 未确认清理不能宣称归还 lease |

当前 [原生手册](../handbook/computer-use-native-acceptance.md#2026-10-17-实际调用回执修正)清楚分开：freeze4 的 6 项原生通过、freeze6 的真实 Windows 禁用零输入拒绝、freeze7 的 61 项相关非原生回归（未重跑原生）；类型为准确 `6751` 基线 9→9、零新增诊断，**不是全部类型通过**。freeze5 的辅助候选组 1 通过/1 失败保留；未观察 `0xFC`，且失败尝试调用过业务输入 API、效果未知。零 fixture 点击不证明其他窗口无影响，不能重写成“没执行输入”。本次静态审计没有复试辅助候选。

### Coding Agent：5 项

5 行均使用 CO。实际工具进入 `CodingApplication` / TaskRuntime / `HerdrExecutor` / `HerdrClient`，不是把需求交给普通聊天模型假装写代码。产物含 worktree、原生 session、attempt、固定验证和原来源交付；继续必须恢复准确原生 session。commit/PR 发布另由具体 HEAD 的一次性主人批准和 broker 执行，不作为第六个 Herdr 普通工具。[应用](../../backend/app/coding_tasks/application.py#L98)、[真实原生执行](../../backend/app/coding_tasks/executor.py#L113)、[Herdr 客户端](../../backend/app/coding_tasks/herdr.py#L111)、[发布 prepare](../../backend/app/coding_tasks/publication.py#L231)、[ADR-0017](../adr/0017-owned-coding-agent-runtime.md)。

| 序号 | Shiori / Meido 名称 | 生产实现 | 当前行为证据 | 未验范围 |
| --- | --- | --- | --- | --- |
| 68 | `herdr_start_task` | [tool dispatch](../../backend/app/coding_tasks/tools.py#L68) → [start_task](../../backend/app/coding_tasks/application.py#L129) | [公开聊天发现/启动/原来源结果](../../tests/python/test_coding_api.py#L27)、[真实 owned Herdr + 本地 worktree](../../tests/python/test_coding_api_native.py#L25) | 真实 Codex/Claude 登录、模型消费未验；fixture CLI 不能替代 |
| 69 | `herdr_list_tasks` | [tool dispatch](../../backend/app/coding_tasks/tools.py#L75) → [list_tasks](../../backend/app/coding_tasks/application.py#L183) | [五工具宿主来源/伪 argv 拒绝](../../tests/python/test_coding_tools.py#L12)、[应用归属与持久结果](../../tests/python/test_coding_application.py#L40) | 同 CO；仅本角色原来源，不是所有仓库全局进程表 |
| 70 | `herdr_get_task` | [tool dispatch](../../backend/app/coding_tasks/tools.py#L78) → [inspect](../../backend/app/coding_tasks/application.py#L175) | [真实 server、session、worktree](../../tests/python/test_coding_herdr_native.py#L91)、[固定验证命令](../../tests/python/test_coding_herdr_native.py#L117) | 原生长路径已有局限，不由短路径成功消除 |
| 71 | `herdr_continue_task` | [tool dispatch](../../backend/app/coding_tasks/tools.py#L72) → [continue_task](../../backend/app/coding_tasks/application.py#L133) | [exact resume 和两种合成 CLI](../../tests/python/test_coding_herdr_native.py#L22)、[UTF-8 全需求及原 session](../../tests/python/test_coding_herdr_native.py#L207) | 实际认证摘要/真实 CLI 行为需具体授权验收 |
| 72 | `herdr_cancel_task` | [tool dispatch](../../backend/app/coding_tasks/tools.py#L80) → [cancel_task](../../backend/app/coding_tasks/application.py#L188) | [清理失败仍保留 owner/ACK 责任](../../tests/python/test_coding_application.py#L114)、[owned process tests](../../tests/python/test_coding_owned_process.py)、[shutdown tests](../../tests/python/test_coding_shutdown.py) | 不能撤回已改文件、费用或未知外部效果 |

[当前验收手册](../handbook/coding-agent-acceptance.md)明确 7 项真实 Herdr/合成 CLI 原生用例与账号/模型边界。存在 `HerdrClient`、server 或成功合成输出，不证明真实供应商认证与任意仓库代码任务已通过。

### 屏幕、桌宠、生图与附件：4 项

| 序号 | Shiori / Meido 名称 | 生产执行与装配 | 角色 / 可见性 / 权限 | 当前行为证据与缺口 |
| --- | --- | --- | --- | --- |
| 73 | `observe_screen` | [ObserveScreenTool](../../backend/app/screen_perception/__init__.py#L443) → [observe/采集/独立视觉](../../backend/app/screen_perception/__init__.py#L166)，main 4107 | SC | [严格摘要到下一模型/SSE/历史](../../tests/python/test_screen_perception_api.py#L86)、[真实受控主屏 marker](../../tests/python/test_screen_perception_native.py#L27)。[历史记录](screen-perception-acceptance-2026-10-16.md)为单屏/125%/本地视觉；双屏混合 DPI、真锁屏/断开/frozen、真实远端模型未验。 |
| 74 | `pet_action` | **无生产工具、原生 surface、动作包播放器或桥；main 候选与插件工厂无此项** | 没有可用角色开关或 schema；**G02 确定未实现** | 总规格 D04 的 Electron/PySide6 选择仍待回答；真实透明置顶、move/play、动作枚举、冷却/每回合限制和完整生命周期均待实现。没有以网页动画、accepted 空结果或拒绝占位计作复刻。 |
| 75 | `generate_image` | [GenerateImageTool.execute](../../backend/app/novelai_generation/tools.py#L40) → [submit/真实 POST](../../backend/app/novelai_generation/application.py#L226) → 有限预算、持久 attempt/outbox/媒体历史 | NI | [公开 txt2img/img2img/原图归属](../../tests/python/test_novelai_api.py#L8)、[不重试付费 POST](../../tests/python/test_novelai_api.py#L36)、[后台父结束后原来源交付](../../tests/python/test_novelai_background.py#L54)、[真实 OneBot 协议 PNG](../../tests/python/test_novelai_channel.py#L10)。CG/历史/重生成另有实际实现与专集，见[手册](../handbook/novelai-generation-acceptance.md)；**未调用真实 NovelAI、未验费用**。 |
| 76 | `read_attachment` | [真实 ID 附件读/行页/字符页](../../backend/app/channels/attachment_tool.py#L26)，main 5400 | AT | [当前与引用入站、Runtime/历史及旧 ID 拒绝](../../tests/python/channels/test_attachment_turn.py#L21)、[多字节长行完整续页](../../tests/python/channels/test_attachment_reader.py#L33)、[2 MiB/文本限制](../../tests/python/channels/test_attachment_reader.py#L71)。真实 QQ 账号另验；不是读取任意本地路径。 |

## 确定差距与下一步

### G01：关键词发现尚未完整保留上游召回行为

**已提取：** 当前 [ToolSearchTool 374–388 行](../../backend/app/agent_runtime/discovery.py#L374)以 `re.findall(r"[\w]+", query.casefold())` 切出词后，只计算词是否出现在完整 name/description；名称命中权重 4，描述 1。没有中文单字/双字、CJK-非 CJK 边界、名称 parts 逆向包含或独立 search_hint。

静态可验证反例：设 `lookup_group_context` 已授权且 deferred，`query="查群笔记"`。当前正则产生唯一 token `查群笔记`；它不在工具名，也不在[当前描述](../../backend/app/channels/group_tools.py#L243)，因此该工具得分必为 0。上游同一查询拆出的 `群笔`、`笔记`、`群` 等会命中描述/元信息。当前[中文测试](../../tests/python/test_tool_discovery.py#L218)只测完整子串 `归档`，不能排除这条反例。本文没有通过应用导入运行此例；该结论来自字面量与算法分支，不冒称一次实际工具 RED。

固定上游证据：[_default_normalize 127–163](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/search_backend.py#L127)保留整体小写/空白词/CJK 边界/单字双字；[_score 166–200](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/search_backend.py#L166)按 name parts 精确10、部分5、完整名3（MCP前两者12/6），hint 独立4、description独立2；[102–110](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/search_backend.py#L102)只收 score>0 并稳定排序。该文件已从固定 Git 对象逐行读取，没有使用 clone 当前另一 HEAD 的工作树。

hint 是真实注册数据，不是装饰字段：[registry.py 74–123](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/registry.py#L74)保存进 ToolDocument；[groups.py 46–64](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/bootstrap/toolsets/groups.py#L46)包含群友、群笔记、其他群等补充词；[Browser 插件 23](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/plugin.py#L23)、[Herdr 插件 31](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/plugin.py#L31)也实际传 hint。因此建议在既批 D01 内先补公开 AgentTool 的自然中文、混合词、名称部分、排名及别名行为 RED，再选与现有冻结定义兼容的元信息适配；保留 risk、原授权、实时撤权、版本持久化和已加载过滤。不要只添加一个中文特殊词让该例通过。

这是一项**已实现工具的行为差距**，不影响本次“执行路径75”的计数，也不能据精确 `select:` 可用而把中文查询视为已完全复刻。

### G02：桌宠尚无实际用户闭环

**已提取：** [总规格 P05/D04](../specs/shiori-tool-parity-spec.md)要求实际透明置顶窗口、角色动作包及 move/play。当前生产 `backend/app` 没有 `pet_action` 的 ToolDefinition、handler、插件 factory 或原生窗口生命周期；[全部候选](../../backend/app/main.py#L3946)、[builtin 工厂注册](../../backend/app/main.py#L256)也未装配。现有状态表把本项保留为待实现，和源码一致。

**计划：** 保持原宿主选择问题 pending；不得因为长时间未答而推断选择、重复请求批准或先写另一种宿主。主任务可继续 G01、现有 CI/集成证据和文档核验，不需要把整个目标缩成当前75项。

### G03：访客会收到执行层一律拒绝的桌面推送声明

**已提取：** [MessagePushTool.definition](../../backend/app/desktop_push.py#L25)设置 `external_allowed=True`；角色启用此工具时，[真实渠道回合](../../backend/app/main.py#L5393)用 `is_channel_owner=owner` 装配，并在 5412 行传 `external_only=not owner`。[冻结过滤](../../backend/app/agent_runtime/capabilities.py#L398)只以该元数据决定是否排除外部发言者，当前[实时 authorize_tool](../../backend/app/main.py#L4347)也没有按访客剔除此名。因此群友或陌生私聊的模型仍可得到 message_push schema。

然而同一候选工具持有的 [desktop_push_authorized](../../backend/app/main.py#L4030)在 `is_channel_owner=False` 时直接返回 False；[execute](../../backend/app/desktop_push.py#L35)在写入前再次调用它。故这是**可见性与必拒绝的执行能力不一致，不能描述为成功越权写入**。可演示的输入是：角色明确开启 message_push，有效非主人渠道入站，provider 查询声明集合应排除此工具、伪造调用应仍拒绝。本文仅核调用链，没有发消息或执行该公开 RED。

上游固定 [common register 115–120](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/meta/register.py#L115)对 push 没有外部放行，registry 默认 false。现有桌面推送测试覆盖的是主人桌面、错误外发目标、停止/事务和媒体，未找到上述 guest schema 断言。建议通过真实渠道/公开 provider 边界补 RED，并统一元数据与已批准的主人桌面执行许可；保留当前实际执行门禁，不扩大访客权限。此项可在既有架构内修正。

## 已批准适配、仅未验收和旧文档的区分

| 类别 | 本次判断 |
| --- | --- |
| 已批准适配收紧 | Shell argv 白名单/固定目录/有限环境；Meido 稳定成员名 `lookup_group_member`；账号 account_id/target_id 与角色附件 ID；MCP 预授权 server_id；窗口仅当前主人桌面、普通权限 direct/主屏/保守 background；屏幕显式独立 vision；NovelAI 有限许可与一次 POST；Coding 固定原生宿主/受信仓库/精确会话/发布另批。不能把这些已确认取舍当“忘了复制”，也不能据此扩大批准范围。 |
| 仅未取得外部证据 | 外部平台真人收件、真实来源/账号/模型事务、NovelAI 生成与 Anlas、真实 Codex/Claude 认证和模型、具体 HEAD 的实际发布；实现中已有服务/adapter，但无真实许可和验收就不能宣称生产服务可用。 |
| 原生环境未覆盖 | CU 的辅助 VK 实际到件、多DPI/多屏、锁屏/断开/双采集失败与遮挡；SC 的多屏混合DPI、frozen、真锁屏断开；Coding 长路径限制。存在对应错误/边界分支不等于所有原生环境实测完成。 |
| 字幕政策门禁 | YouTube 字幕 CLI 补验此前被自动审批以 `blocked by policy` 拒绝，未给具体原因，实际执行0次并停止。本次没有重试或绕过。它属于 summarize Skill 的验收缺口，不扣成另一个普通工具缺实现。 |
| 文档时效 | 固定 f4ac 的状态表开头仍写 #169“待最终两轴”和旧6751基点，P02 行也仍待审查；这与任务已汇入 f4ac 的当前提交进度需要根协调者重新对照审查/CI证据更新，**不是据这些文字断言代码尚未合入**。旧验收文档的“待集成”措辞同理，只能按其固定提交阅读。本文不替协调者假定当前 CI 结果。 |

## 资源、许可与本次校验

**已提取：** 浏览器 [native-runtime.json](../../backend/app/browser_use/native-runtime.json)固定 agent-browser `0.38.2` 的 SHA `a6a9a365…73786`、Chrome for Testing `153.0.8010.52` 压缩包 SHA `df485442…4fbe1`，并分别列许可。CU [runtime.json](../../backend/app/computer_use/runtime.json)固定上游 `fc188250…0e86`、补丁 SHA `9ca2222b…0a0c`、exe `f700163b…8e587`，asInvoker/uiAccess=false；[resolve_driver](../../backend/app/computer_use/driver.py#L16)检查资源 manifest 与仓库锁完全相等且验证 exe 字节。Herdr [manifest](../../third_party/herdr-owned/manifest.json)固定 `7e26bad9…7e24f`、owned patch 和 accepted build SHA；[verify_bundle](../../backend/app/coding_tasks/native_build.py#L14)实际检查固定构建字段、exe 和 notices。许可证保留在 [Shiori](../../third_party/shiori-agent/LICENSE)、[CUA](../../native/computer-use/LICENSE-CUA.txt)、[Herdr 及依赖](../../third_party/herdr-owned/)。本文只读这些锁和校验实现，没重新构建或跑 PE。

本次可直接复核的静态检查：

1. 固定 Meido HEAD 为 f4ac；开始时工作区干净，未在 `main` 修改实现。GitHub 当前只有一个开放汇总 PR #162，目标 main；没有创建新 PR、关票或发布。
2. 浏览器两份固定完整 JSON 相等：28/28、28个唯一名字；不能单凭此项认定28行为通过，所以逐行另列真正调用和效果测试。
3. 逐项表包含序号01–76、76个唯一上游名；`lookup_member` 显式映射为 `lookup_group_member`，不重复计数；`submit_scene_observation`、`submit_scene_image_prompt`、动态 MCP、A01/A02 专用工具均排除。
4. 新文件相对链接、源码/测试行号和本表计数在交付前以 stdlib 再检查。没有将 schema、类、测试名称或历史 passed 数单独当完整业务闭环证明。

**下一项可安全开始的实现工作：G01 的公开行为 RED 与完整搜索召回适配，以及 G03 的访客声明回归。** G02 等原宿主问题答复；真实服务与费用按已有批准边界等待具体许可。本审计发现的是固定 f4ac 的状态，不替代汇总 PR 最终 CI、两轴复核和完整目标的逐项完成审计。


## 后续实施更新：P05 / #195

主人已于本次对话明确选择 Electron 独立宿主。`6fd9485`/`9592f8b`/`04a135e` 已加入 `pet_action`、动作包和独立角色配置、原生固定桥/窗口/React播放器及本地 Windows 发行；原审计固定 f4ac 的 G02 “未实现/待选择”事实保留为历史，不代表当前源码。76个普通工具名现在均有生产装配路径，静态计数仍不等于所有真实环境验收完成。

当前主人回合授权/持久代际、每run/冷却、撤权原派发门、真实125%DPI五位置/尺寸/工作区/播放回执、停用/重启/断连、两完整后端恢复及原Job退出均有本地证据，两轴复核无阻塞。细节和原始失败限制见[桌宠验收](desktop-pet-acceptance-2026-10-17.md)。总 PR 保持草稿，准确 CI、真实外部服务、模型费用与其他桌面环境的未验范围分别保留。
