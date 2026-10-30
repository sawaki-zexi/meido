# Shiori 工具可见性、说明与调用决策核验

状态：当前；Issue #277 / PR #278 的后续核验。最后核验：2026-10-30。

本次上游为 `YinFengWindy/Shiori-Agent@21e68d99d88f421e798fabc4c5d9f8c1b4ad454f`。只读 checkout `D:/shiori-tool-audit-20261030` 的 HEAD、origin 与 GitHub commit API 的 SHA 一致；未运行上游脚本。[上轮报告](shiori-live-reply-tool-loading-2026-10-30.md) 的 `d149de7` 仍是当轮历史基线，不能当成这次 current。以下是已从源码提取的事实；本地测试不能证明真实模型一定选择正确工具或真实服务耗时改善。

## 结论及已修复差异

1. 上游桌宠 `pet_action` 常驻；电脑操作与定时任务按需。本地原先恰好相反的部分已修正。`generate_image`、屏幕观察、委托和记忆仍常驻，浏览器、账号、群管理、MCP、Herdr 仍按需。
2. 上游关闭工具搜索开关的含义是全量提供 schema，仍包含常驻 `tool_search`；本地原先同时移除该工具。现在全量模式保留搜索，搜索已可见名称返回 `already_loaded`，不增加权限。
3. 上游提供未加载内置/MCP 工具名目录及完整路由决策；本地原先只有插件/MCP 命名空间和 web 指引。现在加入尚未可见的业务工具名，JSON 转义、明确为非指令元数据，与原命名空间共用 **4096 UTF-8 字节**上限；每请求重新检查 live authorization。已加载、禁用、撤权和隐藏协议工具不列入未加载目录。最小预算 provider 仍省略目录，但保留条件化的关键词搜索指引。
4. 补齐工具自身 `function.description`、已有参数语义及实际请求中的决策：可见直接调用、不可见先搜索、未搜索不声称无能力；委托时机/profile；显式记忆写入、来源核验、纠错真实回执、历史回溯瀑布；shell 实际预算/轮询、文件分页、定时模式和群笔记先读后写。没有将服务凭据、上游绝对路径或未实现参数复制到模型。
5. `pet_action`、`task_stop`、`spawn_manage`、`herdr_cancel_task` 从本地 `mutating` 改为 `external`，对应上游 `external-side-effect`。风险筛选真实搜索结果已覆盖；分类不授予执行许可。`spawn` 与 `message_push` 的有意差异见下表。

## 可复核的上游位置

固定 SHA 链接（行号按本次 checkout）：

- [共用常驻工具注册](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/apps/backend/agent/tools/meta/register.py#L25)：`tool_search`、shell 三件套、web、文件、历史与推送。
- [registry 元数据和 schema 包装](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/apps/backend/agent/tools/registry.py#L44)：默认 `always_on=False`；259 行常驻集合；288 行 deferred = 全量减常驻、元工具、已可见。
- [初始可见集合](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/apps/backend/agent/core/passive_turn/reasoning_loop.py#L217)、[未加载名称提示](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/apps/backend/agent/core/passive_turn/helpers.py#L180)、[搜索工具说明](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/apps/backend/agent/tools/tool_search.py#L49)。
- [系统调用决策](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/apps/backend/prompts/agent.py#L71)：73 行路由，74–79 行委托，94–115 行记忆纠错，121 行历史回溯。
- [spawn 注册](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/apps/backend/bootstrap/toolsets/meta.py#L62)、[schedule 注册](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/apps/backend/bootstrap/toolsets/schedule.py#L25)、[群工具注册](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/apps/backend/bootstrap/toolsets/groups.py#L43)、[MCP 注册](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/apps/backend/bootstrap/toolsets/mcp.py#L23)、[账号注册](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/apps/backend/bootstrap/tools.py#L752)。
- 插件注册：[桌宠](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/plugins/desktop_pet/backend/plugin.py#L54)、[生图](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/plugins/novelai/backend/plugin.py#L166)、[屏幕](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/plugins/screen_perception/backend/plugin.py#L22)、[电脑](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/plugins/computer_use/backend/plugin.py#L29)、[浏览器](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/plugins/browser_use/backend/plugin.py#L27)、[Herdr](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/plugins/herdr/backend/plugin.py#L61)、[记忆](https://github.com/YinFengWindy/Shiori-Agent/blob/21e68d99d88f421e798fabc4c5d9f8c1b4ad454f/plugins/default_memory/backend/tools/register.py#L39)。

## 全部已接入工具清单

“常驻/按需”只描述 **已通过宿主启用、角色与来源准入后** 的普通聊天模型可见性，不表示每个角色都能使用。下表以搜索开关开启为准；关闭时所有已准入按需 schema 直接可见。`R/W/E` 分别是本地 `read_only/mutating/external`，对应上游 `read-only/write/external-side-effect`；`T/F` 是声明 `external_allowed`，仍须经过实际来源与服务准入。未标差异的 R/W/E、T/F 与当前上游对应注册一致。

| 工具（按相同声明归组） | 可见性 | risk / external_allowed | 说明、schema 核验与适配 |
| --- | --- | --- | --- |
| `tool_search` | 常驻 | R/T | 精确 `select:A,B`、中文功能搜索、top_k、三种风险；下一次请求才可调用。原“本次运行仅以上工具”改为“本次请求”。 |
| `shell` | 常驻 | E/F | 上游 shell 指引按本机 PowerShell/POSIX、角色 workspace、真实 timeout/output/promote 预算重写；后台不免除宿主超时，保留独立受限子任务 argv 许可。 |
| `task_output` | 常驻 | R/F | 同 task_id 查询/等待，30 秒有限等待；不重启进程。上游参数名为 background_task_id，本地保持 task_id。 |
| `task_stop` | 常驻 | E/F | 对原来源进程树停止；本轮修正 risk。 |
| `web_search`, `web_fetch` | 常驻 | R/T | 搜索现况和来源正文、query/num_results/livecrawl/type；保留本地网络启用、URL/DNS/SSRF 与容量控制。 |
| `read_file`, `list_dir` | 常驻 | R/F | 分页/预览、行号、图片支持与原文读取；仅角色 workspace，保留字节/行/扫描边界，不移植上游任意路径。 |
| `write_file`, `edit_file` | 常驻 | W/F | 完整覆盖前读、精确匹配、不带行号、非请求不主动写文档；保留原子写入、4 MiB 和原有 root 校验。 |
| `fetch_messages`, `search_messages` | 常驻 | R/T | 记忆或搜索仅作线索，fetch 原文核验；本地 source_ref/批量 evidence、授权 HistoryView，不读取上游 HISTORY.md 路径。 |
| `message_push` | 常驻 | **W/F（上游 E/F）** | 本地仅写当前角色桌面会话/附件，不对外发送，见 `desktop_push.py:19`；外部投递使用 account_send。保留本地已实现语义。 |
| `spawn` | 常驻 | **E/F（上游 W/F）** | 4 步以上独立产出、research/scripting/general、默认同步、长任务后台；Meido 另有显式获准 Codex coding 分支及外部进程，保守维持 E，见 `spawn_tool.py:276`。不扩大仓库/发布许可。 |
| `spawn_manage` | 常驻 | E/F | 原来源 list/get/cancel；continue 仅本地已注册 coding job。普通子任务不允许继续；本轮修正 risk。 |
| `recall_memory` | 常驻 | R/F | 陈述式 query、answer/timeline、来源线索；本地 fact/domain/time_filter/limit 保留，外部记忆按项目既有隔离契约。 |
| `memorize`, `forget_memory` | 常驻 | W/F | 本轮明确请求才存、宿主绑定原用户消息；先定位/回源再清除、依据真实 superseded_ids/item_id 报告。主人的旧注入偏好不重复写。 |
| `generate_image` | 常驻 | E/T | 已有 NovelAI 英文 tags、参考图与一次有限付费尝试契约保留；本地用受管 asset ID，不照搬上游任意 base_image_path。**T 只是 metadata；capture_tool_context、实际 owner/source、服务与角色许可仍决定执行，本轮没有放开访客生图。** |
| `pet_action` | 常驻（本轮修正） | E/F（本轮修正） | 当前动作包真实 enum、语义位置/有限移动、真实回执；保留仅桌面主人会话和频次约束，不复制上游通用 surface/语音。 |
| `observe_screen` | 常驻 | R/F | 明确只观察，不能点击输入滚动；屏幕资料不授予权限。 |
| `schedule`, `cancel_schedule` | 按需（本轮修正） | W/F | instant 固定文本/soft 到时独立任务，at/after/every/cron、宿主接收时间、取消真实 ID/名称；本地 timezone/count/cron 扩展保留。不得把创建当作执行或送达。 |
| `list_schedules` | 按需（本轮修正） | R/T | 当前角色计划只读；实际来源准入仍检查，不扩成任意角色。 |
| `account_list`, `account_targets` | 按需 | R/T | 已配置账号、可达 target，成员资料不是发送授权；本地分页 kind/cursor 与渠道能力保持。 |
| `account_send` | 按需 | E/T | 本地 account_id/target_id、有限媒体；外部来源仅 host 许可 owner 路由，uncertain 不重发。 |
| `lookup_group_member` | 按需 | R/T | 对应上游 `lookup_member`；保留本地多渠道/冻结来源名字。 |
| `lookup_group_context` | 按需 | R/F | 角色获准群摘要与笔记，保留本地 group/account 查询范围。 |
| `update_group_context`, `set_group_listening` | 按需 | W/F | 先读群上下文再整篇合并替换，省略保留/空串清空；旁听不等于发言或跨源授权。 |
| `mcp_add`, `mcp_remove`, `mcp_list` | 按需 | E/F, W/F, R/F | 仅已预授权 server_id；连接后工具还需发现；不同于上游宽泛服务配置入口。 |
| `mcp_<server>__<remote>` | 按需 | 服务配置 risk/F | description/schema 来自冻结且校验后的真实声明；config risk 与 service grant 控制。本地不把远端注解自动升级为权限，保留 ADR-0026。 |
| `agent_browser_*`（下列 28 项） | 按需 | E/F | 上游 tools.json 的 28 份 description/inputSchema 逐项相等；本地 wrapper 保留角色隔离及新观察 ref。 |
| `computer_*`（下列 8 项） | 按需（本轮修正） | E/F | 8 份 description 相等，补回 `[Computer Use]` 前缀；4 个 schema 的本地上限更严，见下。 |
| `herdr_start_task`, `herdr_continue_task`, `herdr_cancel_task` | 按需 | E/F | cancel 本轮修正 risk；保留本地受信仓库、原生会话/attempt、有限执行/清理、发布单独批准。 |
| `herdr_list_tasks`, `herdr_get_task` | 按需 | R/F | 实际原来源任务和有界状态；不加入当前上游尚未在 Meido 实现的终端 agent list/read/prompt/keys 工具。 |

浏览器 28 项完整名称：`agent_browser_open/read/snapshot/click/fill/type/press/check/uncheck/select/scroll/wait_ms/wait_for_selector/wait_for_text/wait_for_load/screenshot/get_text/get_url/get_title/eval/close/back/forward/reload/tab_new/tab_list/tab_switch/tab_close`，其中每个斜线项都带 `agent_browser_` 前缀。逐项来源为两仓库 `plugins/browser_use/backend/tools.json` 与 `backend/app/browser_use/tools.json`。

电脑 8 项：`computer_list_windows`、`computer_get_window_state`、`computer_click`、`computer_type_text`、`computer_press_key`、`computer_hotkey`、`computer_scroll`、`computer_release`。本地 `get_window_state` 的 max_depth≤12、max_dimension≤1600、max_elements≤500、query≤256；type_text≤4000 字符、press_key≤32、hotkey≤5 键，其余 schema 相同。来源为上游 `plugins/computer_use/backend/tools.json` 与本地 `backend/app/computer_use/tools.json`。

### Meido 独有普通工具（没有同名上游注册可照搬）

| 工具 | 可见性 / risk / external_allowed | 现有调用契约（本轮保留） |
| --- | --- | --- |
| `inspect_role_image` | 常驻 R/F | 主人来源、角色实际头像/立绘 asset ID，视觉或图片引用；不猜文件名。 |
| `set_role_mood` | 常驻 W/F | 当前桌面主人的有限 enum，仅变本地插画，不发送/控制桌宠。 |
| `search_owner_knowledge` | 常驻 R/F | 已获准且启用的本地同步资料与当前角色想法，只读。 |
| `todo.search` | model_only R/F | 主人待办只读；model_only 在普通模型请求可见，**不是专用输出协议**。 |
| `todo.write`, `todo.reminder_control` | model_only W/F | 主人明确意图才改任务/提醒责任，精确 ID/标题，歧义先确认。 |
| `mail.manage` | model_only E/F | 主人明确邮件动作/已授予角色管理；只做已有同步和可审阅草稿，不发信/删信/移动。 |
| `read_attachment` | 常驻 R/T | 仅当前 QQ/引用消息的真实 TXT/MD 附件 ID，有限分页，不能借它读角色文件。 |
| `chat_meme_save`, `chat_meme_list`, `chat_meme_send` | 常驻 W/F, R/F, E/F | 窄 QQ 主人适配：当前真实图片入库、当前库候选、明确请求后发送本回合候选；原 file/send 许可交集，uncertain 不重发。 |

### 专用协议与其他独立运行域

- `finish_reply`：自然分条输出协议，独立收尾阶段，不加入普通业务候选或 deferred 名称目录。
- A01 自主 tick 的 12 项：`get_alert_events/get_content_events/get_context_data/recall_memory/get_content/web_fetch/web_search/get_recent_chat/message_push/mark_interesting/mark_not_interesting/finish_turn`，来源 `proactive_runtime/tool_schemas.py:16`；同名普通工具也不是同一执行授权。
- Drift 的专用 `meme_copy_image/shell(rule_id)/recall_memory/web_fetch/web_search/message_push/activate_skill/mount_server/finish_drift`，以及自身有限文件/历史/MCP 绑定，来源 `proactive_runtime/drift.py:36`；它们不通过普通 tool_search 解锁。Story 导演输出走自身 JSON 协议，不注册普通聊天工具。

这些独立域未改写，本轮也不把自主 tick/Drift 的许可作为普通主人或外部消息的许可。

## 最小数据流与项目适配

`main._runtime_tool_candidates`（4864 行）构建真实工具 → `_enabled_runtime_tool_names`（5193 行）与插件/service policy → `CapabilityRegistry.resolve` 冻结候选、来源、风险、审批和 live 授权 → `_deferred_discovery_tools`（5267 行）控制首请求 schema → `_runtime_namespace_context`（5233 行）生成有界已授权名称提示 → `MeidoProvider` 发送实际当前请求工具及条件化调用指引 → tool_search 返回并保存 → 下一请求发布绑定后的已选 schema → 原绑定执行器调用 → 真实回执/最终回复。

按需 schedule 仍必须在宿主初始化时取得 provider：`tools_for_binding()` 遍历本次已准入候选供绑定初始化，替代仅遍历首请求可见工具。它不发布 schema、不绕过 executor 校验或请求固定绑定。soft schedule 经发现后实际执行/完成投递的 HTTP 测试覆盖了这个容易遗漏的差异。

- ADR-0026 的冻结上界、请求定义/执行器固定、同回复不能抢先使用新工具、及时撤权、MCP grant 和有界累计声明预算不变。
- 本地技能正文已在 `skills.py` 注入；仅在正文未载入且给出了当前 workspace 可访问路径时才建议 read_file。不能为了照搬上游 SKILL.md/HISTORY.md/PROACTIVE_CONTEXT.md 文案扩大文件读取根。
- 当前上游 registry 会统一注入必填 `description` 参数作为 5–12 字调用进度，并在执行前剥离。**本轮不移植这一独立进度参数协议**：已有 Meido 工具可能使用自己的同名业务字段；普通工具进度沿用真实名称/脱敏参数与结果。此处完善的是 `function.description` 和既有参数的调用说明，不能声称逐字节 schema 完全相同。
- 最小请求保留可装入完整预算的授权工具仍是上轮明确适配差异。目录不无限扩张、minimal 可省略目录；不用一个提示改动规避真实请求预算。
- description/risk 属于完整安全声明指纹；本轮变更后旧选中缓存可能失配并丢弃，模型可重新搜索当前授权候选。既有运行请求仍使用原固定绑定，不按最新同名对象改派。

## 行为验证与限制

新增 `tests/python/test_tool_exposure_contract.py` 用本地 HTTP 模型服务与真实 `MeidoProvider/run_agent_loop/CapabilityRegistry` 验证：桌宠常驻执行；电脑中文与精确搜索后下一请求实际调用并收到窗口回执；全量模式仍含搜索且返回 already_loaded；禁用、撤权、外部来源和隐藏协议不恢复；实际请求 ordinary/minimal/empty/finish-only 指引自洽；巨大/恶意目录有界且 JSON 转义；风险筛选只返回其正确分类。不是只核对 schema 名称。

已有 schedule API 走新增发现回合，保留创建/列表/到期/取消及 soft 子任务读取文件后送达断言；电脑 HTTP 流程在原 20 次操作前加入发现回合，原状态/图像/执行回执断言保留。MCP 全量模式测试同步验证核心搜索保留；旧权限/轻量绑定与硬预算回归继续运行。

所有模型、渠道、桌宠和电脑操作均为本地合成夹具；真实原生电脑验收仍需显式 opt-in，本轮没有运行。未访问正式数据、真实模型或业务服务，未扩大文件根、owner 权限、外部事务或新增上游服务。正式慢回复的摘要 TimeoutError/ValueError 历史证据与未知项仍以上轮报告为准；本轮不补造新的正式日志结论。

本地核验记录（Python 为 `D:/meido/.runtime/.venv/Scripts/python.exe -X utf8 -m pytest`）：

| 范围 | 结果 |
| --- | --- |
| tool_exposure_contract、schedules_api、advanced_settings_api | 34 passed；随后扩展的风险筛选用例也在核心组通过 |
| computer_use_api、computer_use_receipt_api | 3 passed，1 opt-in native skipped |
| discovery/API、搜索 relevance/quality/budgets、lightweight loading、capability layer/plugins、context_windows、MCP HTTP、exposure contract | 167 passed，1 below-hard 夹具越过硬限；调整该夹具余量并增加真实预算区间断言后，context_windows 全20 passed |
| memory_tool/write、shell/background、spawn/API、file/network、coding_tools | 248 passed，1平台测试 skipped |
| chat_runtime_http、advanced_settings、discovery_google | 9 passed，1同类 below-hard 夹具失败；调整余量后 chat_runtime_http 在与 role_capabilities 合跑时全部通过 |
| role_capabilities、model_tools（最终复验） | 31 passed；常驻/按需与当前请求提示断言已同步，保留原执行/配置恢复断言 |
| npm run build | TypeScript、Vite 网页、桌宠类型与构建通过；仍有既有大 chunk 与运行时 pet URI 提示 |
| 静态源码与文档 | browser 28 项说明/schema 相同；computer 8 项说明相同、仅上述4项 schema 有界差异；变更 Python 语法、相对文档链接和 git diff --check 通过 |

未重复浏览器截图：本轮没有 UI 改动，沿用上一轮已验收截图；尚未把本地检查表述为新 PR HEAD 的 CI 或独立审查结果。

### 规格审查修复：纠正请求不得携带运行级搜索目录

审查发现原 ordinary/empty/finish-only 测试使用静态目录标记，未覆盖真实目录中的搜索路由。补入真实 CapabilityRegistry 候选与 `_runtime_namespace_context` 后，空工具和 finish-only 估算红例均额外计入目录；NaturalReplyProvider → ContextWindowProvider → 本地 HTTP 的纠正请求红例证实：schema 仅 `finish_reply`，system 却仍含 `tool_search` 和未加载电脑工具名称。

修复统一由 MeidoProvider 的同一提示入口按**本次 schema**判断：只有存在 `tool_search` 才读取/附加目录及发现说明，实际请求与 token 估算共用此规则。回归同时验证首请求仍有搜索及目录，纠正请求仅有回复协议且不再附带目录，最终回复正常提交；空请求/协议请求的估算与无目录 provider 一致。没有改变冻结授权、执行器绑定或 computer guard；关于后者的审查推测已撤回。

红例：目录估算两项失败，真实 HTTP 纠正链路一项失败。修复后运行 `pytest tests/python/test_tool_exposure_contract.py tests/python/test_natural_reply_protocol.py tests/python/test_natural_reply_api.py tests/python/test_model_tools.py tests/python/test_context_windows.py -q --tb=short`，**63 passed，27.52 秒**。`git diff --check` 通过；其他已通过的大组、构建和 UI 检查沿用前述证据，未重复执行。
