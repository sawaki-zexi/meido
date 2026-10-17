# Shiori 工具体系完整复刻规格


2026-10-17 授权修订：主人要求基础工具默认自带，并选择普通 Shell 默认执行。此前逐项基础授权和普通 Shell 默认关闭的描述由[默认工具规格](default-builtin-tools-spec.md)与[ADR-0021](../adr/0021-default-builtin-tools.md)替代；特殊插件和真实服务许可保持独立。
状态：实施中 / 目标规格，完整目标尚未完成。用户已批准总体复刻目标，并在 2026-10-15 本次对话明确批准第 5 节 D01–D03；D04 已于 2026-10-17 获主人明确选择 Electron 独立宿主，具体实施见[桌宠规格](desktop-pet-spec.md)。完成本文件、首批基础工具或部分 PR 均不代表完整目标完成。

最后核验：2026-10-15（Asia/Shanghai）

Parent：[GitHub #137：复刻 Shiori 完整工具体系](https://github.com/sawaki-zexi/meido/issues/137)。

事实来源：Shiori `4f266aeaf42fcd611362e2456417b91f659341cf`；Meido `origin/main@4daeef288ee40358e7faa57f5b7a1d33ad81d5ed`；[完整调研](../research/shiori-tool-system-and-portability.md)、[领域词汇](../../CONTEXT.md)、[协作规范](../../AGENTS.md)、[文档规范](../agents/documentation.md)、ADR [0003](../adr/0003-tau-inspired-agent-runtime-boundary.md)、[0004](../adr/0004-role-scoped-shell-tool-boundary.md)、[0005](../adr/0005-todo-tools-and-proactive-reminders.md)、[0007](../adr/0007-ai-core-package-boundary.md)。

本文中“现状”仅指上述 main 的源码证据；“目标”“拟议”“验收”均不表示已经实现或运行通过。调研比较的 Meido 基线较旧，实施必须继续核对最新 main、开放 Issue 和 PR。

## 1. 问题和目标

角色目前不能可靠说明自己实际拥有的工具；某些能力已有实现但未授权或未进入模型 schema，角色编辑还可能覆盖未在表单表达的配置。用户已要求把 Shiori 的工具实现复刻到 Meido。最终结果是角色可以发现、使用和解释完整对应能力，主人可以配置许可、依赖和目标，工具有真实执行结果、生命周期和可核验证据。

完整范围包含调研固定版本中的 **31 个核心工具、45 个插件工具、8 个内置 Skill、常规主动决策和 Drift 专用工具及其必要运行管线**。已有能力要复用并补齐差距，不能把源码同名或 schema 相同当成行为已验收。基础工具是交付顺序，不能把最终目标缩为 P0/P1、只读工具或目录展示。

Meido 不改用 Shiori 的数据库、角色身份、会话系统或整个插件宿主。适配后的能力保留 Meido 单一主人、不可变角色身份、唯一会话、SQLite 权威状态、MemoryEngine、ToolContext、角色授权、插件 grant 和 AI 核心包边界。边界差异必须明确记录；不能把静默删掉能力、假实现或永远不可用的占位工具称为复刻。

## 2. 用户结果

1. 主人在角色设置中授权工具，看到真实名称、用途、是否传给模型、为何不可用及依赖修复方法；保存其他角色资料后配置仍保留。
2. 角色能够读取和维护获准文件、联网检索和抓取网页、追溯当前允许上下文的历史消息原文，并显式维护角色记忆。
3. 角色能发现延迟工具；发现与调用不能扩大角色授权，也不能绕过外部发言者和目标限制。
4. 长命令和子 agent 在独立后台任务中运行，能够查询、停止、完成回传；前台对话结束后任务行为有明确契约。
5. 角色能建立、查看和取消固定文本或到时执行的通用时间任务，不改变主人待办提醒语义。
6. 角色复用现有浏览器、渠道、附件能力；能够按授权连接 MCP、观察屏幕、控制桌面、生图、操作桌宠和委托 Coding Agent。
7. 八个 Skill 能被发现、显式激活并实际使用依赖工具；常规主动和 Drift 能按自己的发送、提交和结束协议运行。

## 3. 完整范围追踪

下表的编号是验收标识，不是 GitHub Issue 号。后续 Ticket 和 PR 必须引用对应标识，并登记行为证据。Meido 现有工具可保留其稳定名称；若名称不同，需提供明确映射或兼容入口，不为追求同名重复实现。

| 标识 | Shiori 工具 / 数量 | main 现状与复用差距 | 最终验收结果 |
| --- | --- | --- | --- |
| C01 | `tool_search` / 1 | 有 `deferred` 策略，无模型搜索入口；未激活工具目前被排除在运行快照外 | 名称精确选择、中文/英文关键词、1–10 条、risk 筛选、already-loaded、未加载调用反馈和下一次请求 schema 激活都生效 |
| C02 | `read_file`、`list_dir`、`write_file`、`edit_file` / 4 | 无通用工具；复用角色 workspace 和图像内容块协议 | 行页、图片、一级目录、整体写入、精确替换、diff、BOM/CRLF、并发写和根目录越界都有确定行为 |
| C03 | `shell`、`task_output`、`task_stop` / 3 | Shell 已有白名单、固定目录、最小环境；无后台任务协议 | 受控命令支持前台、显式后台、超时转后台、输出轮询、完整日志和停止进程树；保留 ADR-0004 边界 |
| C04 | `web_search`、`web_fetch` / 2 | 无通用模型工具 | 支持上游检索参数和抓取格式、引用 URL、大小/时间/内容类型约束；搜索服务可配置并有真实错误 |
| C05 | `memorize`、`recall_memory`、`forget_memory`、`search_messages`、`fetch_messages` / 5 | 已有 MemoryEngine 和角色 recall；无两个显式写入工具和消息工具 | 当前角色记忆写入、检索、失效与原始消息证据完整闭环；原文和记忆摘要区分 |
| C06 | `message_push`、`account_list`、`account_targets`、`account_send` / 4 | 已有渠道服务与工具；已有 Issue 的验收状态需核对 | 自己桌面推送、角色账号/目标发现、外部发送及真实投递状态；无旧 push 外发捷径 |
| C07 | `spawn`、`spawn_manage` / 2 | 无独立 TaskRuntime | research/scripting/general 子任务、同步/后台、列表/取消、并发和预算、原上下文结果回灌 |
| C08 | `schedule`、`list_schedules`、`cancel_schedule` / 3 | 已有 Todo 提醒，不等于通用调度 | instant/soft、at/after/every/cron、时区、消息接收时间补偿、重启、查看和取消 |
| C09 | `set_group_listening`、`lookup_member`、`lookup_group_context`、`update_group_context` / 4 | 已有旁听/群上下文；Meido 成员工具名为 `lookup_group_member`，当前实现只查当前群 | 保留群读写授权并补主人上下文按姓名/ID、渠道、历史昵称查询；分页、归属、字段省略/清空语义正确 |
| C10 | `mcp_add`、`mcp_list`、`mcp_remove` / 3 | 仅浏览器专用 MCP；无通用管理 | 授权 stdio 服务连接、分页发现、命名空间、即时可见性更新、调用、持久化和断连移除 |
| P01 | 浏览器 28 工具 | 两份 tools.json 同源，main 已有角色授权、独立持久 profile | 核对全部 28 schema/执行映射，并用真实浏览器覆盖观察、交互、等待、导航、tab、截图和持久身份 |
| P02 | `computer_list_windows`、`computer_get_window_state`、`computer_click`、`computer_type_text`、`computer_press_key`、`computer_hotkey`、`computer_scroll`、`computer_release` / 8 | 无通用桌面插件 | 固定 Windows 原生驱动、窗口四元组、失效引用、主屏限制、独占 lease、前台/后台投递和资源释放 |
| P03 | `herdr_start_task`、`herdr_list_tasks`、`herdr_get_task`、`herdr_continue_task`、`herdr_cancel_task` / 5 | 无 Coding Agent 任务插件 | 真正启动、查询、继续、取消 Herdr/Codex/Claude 任务，并核对 worktree、交付、验证、commit/PR head |
| P04 | `observe_screen` / 1 | 无屏幕感知插件或独立角色视觉连接 | 按需截主屏，经独立 vision 模型只读分析，只回摘要；不把原图交给聊天模型或持久化 |
| P05 | `pet_action` / 1 | 无原生桌宠 surface/包/桥 | 真实透明置顶桌面窗口中 move/play；动作包动态枚举、冷却、当前角色/桌面限制及 accepted 语义 |
| P06 | `generate_image` / 1 | 无对应插件 | NovelAI txt2img/img2img、尺寸/seed/模型、输入媒体归属、结果展示、消费去重；场景 CG/历史/重生成具备对应服务流程 |
| P07 | `read_attachment` / 1 | 已有附件 ID allowlist、角色/session/TXT/MD 范围 | 当前及引用 QQ 附件分页、长行字符分页、大小/类型/身份限制；保持 ID 接口，不退回任意路径 |

核心小计 31；插件小计 45。下面四组浏览器名称合计 28，不能只验收常用六项：

- 观察：`agent_browser_read`、`agent_browser_snapshot`、`agent_browser_screenshot`、`agent_browser_get_text`、`agent_browser_get_url`、`agent_browser_get_title`。
- 交互：`agent_browser_open`、`agent_browser_click`、`agent_browser_fill`、`agent_browser_type`、`agent_browser_press`、`agent_browser_check`、`agent_browser_uncheck`、`agent_browser_select`、`agent_browser_scroll`、`agent_browser_eval`。
- 等待：`agent_browser_wait_ms`、`agent_browser_wait_for_selector`、`agent_browser_wait_for_text`、`agent_browser_wait_for_load`。
- 会话和导航：`agent_browser_close`、`agent_browser_back`、`agent_browser_forward`、`agent_browser_reload`、`agent_browser_tab_new`、`agent_browser_tab_list`、`agent_browser_tab_switch`、`agent_browser_tab_close`。

| 标识 | Skill | 目标与依赖 |
| --- | --- | --- |
| S01 | `weather` | 保留 curl → wttr.in/Open-Meteo 流程，检查本机命令可用性和角色 Shell 白名单 |
| S02 | `summarize` | 使用 summarize CLI 处理 URL/文件/YouTube；明确 CLI、所选服务及凭据准备方式；缺依赖要说明 |
| S03 | `feed-manage` | 可使用实际配置的 feed MCP 进行 list/subscribe/unsubscribe/latest/summary/search/catalog |
| S04 | `meme-manage` | 读取/维护角色获准目录的 memes 分类及 manifest；引用真实媒体 |
| S05 | `skill-creater` | 创建合法 Meido SKILL.md 及支持文件；新技能可被下一次运行发现并实际激活 |
| S06 | `create-drift-skill` | 创建当前角色 Drift 目录中的长期小任务技能，后续 Drift 真正执行 |
| S07 | `codex-delegate` | 具备 CLI 检测、后台 spawn、受控 codex exec、结果文件/session ID 回读和原上下文结果回灌 |
| S08 | `create-proactive-source` | 创建 alert/content/context MCP 来源及配置；经过信任和授权后可消费事件、取正文和持久 ACK |

不能原样复制 Skill frontmatter：Meido 现有格式要求 `id/version/description/tools/activation`，并校验 hash、信任和支持文件。需要转换这八份指令并保留来源署名，补 `requires.bins/env` 的可用性表达和必要脚本资源校验。技能正文不授予工具权限；“依赖缺失”不能作为全部 Skill 永远不可运行的完成证据。

| 标识 | 运行类型 | 完整范围和专用语义 |
| --- | --- | --- |
| A01 | 常规主动决策 | 12 个 schema：`get_alert_events`、`get_content_events`、`get_context_data`、`recall_memory`、`get_content`、`web_fetch`、`web_search`、`get_recent_chat`、`message_push`、`mark_interesting`、`mark_not_interesting`、`finish_turn`。需要真正的数据源抓取、候选、上下文、关联正文、兴趣/去重和提交管线 |
| A02 | Drift | 独立目录、`read_file/write_file/edit_file`、受限复用 recall/web/history/shell、专用 `message_push/finish_drift`，及 MCP `mount_server`。需要空闲触发、角色小任务、预算、终止、run 记录和发送收尾 |

A01 的同名 `message_push` 只暂存一次草稿，`finish_turn(reply)` 才提交；skip 要说明且不能已有草稿。A02 的推送每 run 最多尝试一次，结束必须检查实际发送状态；成功发送后仅保留写文件和 finish 工具。普通对话、主动决策和 Drift 的同名工具不能共用一个忽略运行类型的 dispatch。

## 4. 沿用的边界和适配差异

1. 身份只能来自 ToolContext 和宿主绑定的应用服务。模型参数、pre hook、子任务交接或 MCP 返回都不能改变 role/session/run、发言者、附件范围和投递账号。接收额外身份字段时拒绝，不能模仿上游 kwargs 覆盖顺序。
2. SQLite 继续作为消息、运行、任务、调度和投递事实来源。任务内部 transcript 不构成角色的第二个用户会话；结果回灌写入原会话中的明确消息类型。
3. Shell 保留 argv、白名单、固定 workspace、最小环境和宿主预算；不开放模型选择 shell、cwd、任意 env 或 stdin。后台生命周期可以扩展，任意脚本权限不会随之获得。CLI 需要的凭据由用户配置该 CLI 或专用受控服务，不能继承 Meido 模型密钥。
4. 文件工具默认绑定角色 workspace，授权额外读根须明确配置；所有文件操作在解析真实路径后校验范围。受控技能资源可单独授予只读范围，不能为读取 SKILL.md 放开整个宿主文件系统。
5. 记忆写入和失效经 MemoryEngine，消息只读工具经现有 SessionStore/渠道存储授权查询，不直接暴露全库。
6. `message_push` 只投当前角色自己的桌面目标；对外经角色账号投递服务。时间任务、后台完成、主动与 Drift 都复用同一目标校验和去重入口，不保留 Shiori 已失效的 schedule 外发路径。
7. TodoPlugin 和 ADR-0005 的当天提醒者、当天暂停、次日生效规则继续存在；通用 schedule 是另一个用户概念。
8. 新工具复用 AgentTool/Plugin/Skill/Hook seams；AI 核心只接收模型配置和消息，不读取角色存储、系统桌面或插件配置。
9. 外部服务操作继续需要相应明确授权；模型可见、risk 过滤、插件启用和 MCP 握手都不是授权。保留上游功能时要实现具体许可路径，不能通过永久隐藏完成迁移。

## 5. 架构决定和待审选项

以下决定会影响多项功能。维护者于 2026-10-15 在本次对话明确回答“批准上述方案，继续实施”，批准 D01–D03 的具体方案，分别记录到 ADR-0011、ADR-0012 和 ADR-0013；后续实施不重复请求同一批准。D04 的 Electron 独立宿主路线已于 2026-10-17 获主人明确选择，见 [ADR-0020](../adr/0020-owned-electron-desktop-pet.md)。D05 的外部操作许可由具体任务的用户配置和明确授权提供，不随工具启用自动取得。基础配置修复、文件、网络、消息和 MemoryEngine 工具不依赖尚未决定的原生宿主。

### D01. 发现、冻结授权候选和单调 schema 激活

推荐方案：运行开始冻结角色授权、可信插件、工具定义、作用域和依赖状态组成的候选目录；每次 provider 请求使用一个不可变可见性版本。`tool_search` 只在目录中搜索已授权工具，产生追加的激活事实，下一次请求的可见集合单调增加；先持久化激活结果再发下一次请求。旧版本不会被原地改写，审计关联实际版本。

替代方案是只在下一次独立对话激活，能维持现有单快照实现但无法复刻 Shiori 同一推理回合发现后调用，因此不推荐作为最终状态。

MCP 动态连接需要显式规则：已由主人预授权的 server 配置可在 `mcp_add` 成功后追加带父版本的能力修订，新工具只能继承原先的 server grant/风险上限/角色范围，不能引入新宿主许可。尚未受信任的任意 command/env 先返回需配置/授权的原因。新 server 的工具需在当前运行中可发现并调用，不能悄悄延迟到下一回合然后声称即时挂载完成。若要求任意命令自动获准，属于另一次权限决定。

执行时检查当前激活版本并重验最终参数。许可撤销或插件停用可中止执行；单调可见不意味着撤销后仍有权限。重启和 transcript 回放只恢复仍有效且与当前定义匹配的激活，不把旧授权复活。

### D02. 独立 TaskRuntime

推荐方案：增加独立应用级任务服务，SQLite 保存不可变 owner/origin、执行类型、parent run、能力版本、状态、预算、进度、日志引用和交付；AgentLoop 仅通过 submit/query/cancel 等深接口与其交互。子 agent 可复用 portable loop，但有独立运行/工具子集/预算，内部工作集不会伪装为主人回合。

替代方案是把 asyncio 后台任务塞进聊天 Runtime 或依赖客户端连接；会破坏 ADR-0003 的运行边界，并使聊天结束后任务失去归属和结果恢复，因此不推荐。

需审阅的具体契约：并发上限和子任务预算；显式后台与自动转后台的硬截止；应用退出是否终止受管本机进程；重启时将丢失的执行句柄判为 interrupted 而非假恢复；原角色/目标失效不改投其他会话。Shell 完成默认由 `task_output` 拉取；spawn 完成自动回灌，保留两者区别。只有后台受管任务可在父对话结束后继续，前台普通工具仍遵守取消穿透。

### D03. 通用调度与主动执行

推荐方案：独立 Schedule 应用服务共享宿主时钟、SQLite 和投递服务；instant 只投固定内容，soft 交 TaskRuntime 执行。存储绝对计划时刻、IANA 时区、原始触发条件、来源接收时间、运行次数和幂等键。客户端/模型不决定宿主身份。失败/离线/重启时采用的补执行策略须显式并可测，不改变现有 Todo 提醒规则。

替代方案是把 schedule 写成待办并由当天提醒者执行，会改变角色归属、固定文本、任意 prompt、cron 和取消语义，不推荐。

主动来源与 Drift 的执行调度共享任务生命周期，但事件 ACK、内容候选、聊天活跃判定、草稿提交和 Drift 结束状态由各自深模块拥有；不混入 TodoPlugin。

### D04. 桌面宿主与桌宠原生窗口

Meido 当前是 React 网页 + Python 后端，网页中的动画无法等同桌面透明置顶 surface。推荐增加**可选的轻量原生 desktop host**，只负责透明窗口、桌宠包渲染、位置和 IPC，后端继续拥有角色授权与动作规则。具体可选：

- Electron sidecar：可适配 Shiori 的 Web surface/player 和动作桥，保留现有 React/Web 主入口；需要新增 Node 原生发行、签名、IPC 和窗口生命周期。
- Python/PySide6 sidecar：沿用 Python 发行，但必须重写或嵌入相应包渲染器，并核验许可证和安装体积。

主人已在本次对话明确选择 Electron 独立宿主并要求继续实施，见[具体规格](desktop-pet-spec.md)和[ADR-0020](../adr/0020-owned-electron-desktop-pet.md)。上述 PySide6 仅保留为比较方案。选定路线须实现真实桌面窗口、角色包绑定、move/play、冷却/单回合限制和可关闭/重启生命周期。不能把 HTML 面板或只返回 accepted 的假桥作为完整交付。语音、Bilibili 直播等不是 pet_action 的必要范围，不因同处上游插件就自动引入。

Computer Use 独立采用固定原生驱动适配，跨角色/进程独占桌面 lease；不依赖桌宠窗口。Screen Perception 独立截屏并使用角色 vision 连接；不能为少做配置而把屏幕原图送主聊天模型。

### D05. Coding 委托与外部服务许可

推荐方案：Herdr 与 codex-delegate 共享 TaskRuntime 的归属、日志、取消和回灌，分别保留各自 CLI/会话协议。用户明确选择仓库、执行器权限配置和允许的外部操作；不复制强制 bypass/skip-permissions 标志作为默认。需要真实支持修改、验证、提交和经授权 Draft PR 的完整工作流；仅健康检查或只读状态是中间阶段。

NovelAI 使用单独服务凭据和媒体归属，不透传角色模型密钥；自动场景 CG 与手动请求有不同触发/额度控制，但都须有完整实现和验收。角色 vision 连接与主聊天连接分别绑定稳定模型配置 ID。

## 6. 数据流

### 6.1 普通对话和真实工具目录

```text
角色配置 + 主人/外部发言者上下文 + 插件 grant + 依赖检查
  → 共享能力解析服务
  → 目录记录（已实现/启用/授权/可见/健康 + 原因 + 版本）
  ├→ 角色设置中的工具面板（只展示，不执行工具或启动付费服务）
  └→ 运行候选目录 → 当前激活 schema → AI 核心 → 模型调用
      → 名称/激活/身份/作用域验证 → pre hook → 最终参数重新验证
      → AgentTool.execute → 应用服务 / 受控执行器
      → 结构化结果 + 可选图像/媒体 + 审计/SSE
      → SQLite message_end → 下一次模型请求
```

工具面板与对话必须共享解析逻辑，避免一份写死的清单和一份真正的候选。面板可显示完整实现目录；“当前可用”必须注明角色、运行类型、上下文和快照时间。健康检查不能偷偷产生外发、创建任务或消耗生图额度。

### 6.2 文件、网络与记忆证据

```text
文件参数 → 角色授权根 + 真实路径校验 → 有界读/原子写/同文件锁 → 来源+正文/图片/diff
URL参数 → 协议/每跳目标校验 → 有界HTTP客户端 → 类型/大小检查 → URL+正文/明确错误
消息关键词 → 宿主可读上下文 → 当前存储搜索 → 稳定source_ref
source_ref → 再次校验身份/上下文 → 原文+邻接消息+顺序/时间
记忆工具 → 角色MemoryEngine服务 → source_ref/失效状态 → 召回后可用消息工具核原文
```

web_fetch 应覆盖重定向、DNS 解析和私有网络边界；不能把仅屏蔽字符串字面内网地址当作完整请求目标验证。文件要测试 Windows 盘符、UNC、junction/symlink 和不存在父目录，拒绝校验后逃逸。授权读取的 Skill 资源与普通角色工作文件分别表达来源。

### 6.3 后台、定时、主动和 Drift

```text
工具/调度/主动触发 → 宿主绑定origin与许可 → TaskRuntime持久创建任务
  → 独立进程/子agent/管线 → 日志、预算、取消、终态
  ├→ Shell：task_output拉取 / task_stop停止
  ├→ spawn/Herdr：原角色原上下文的完成事件 → 统一投递与去重 → 会话记录
  ├→ schedule：instant投固定内容；soft执行prompt后投递
  ├→ proactive：来源候选与正文 → 暂存草稿 → finish_turn提交/跳过 → ACK
  └→ Drift：技能与工作目录 → 单次推送尝试 → finish_drift核对发送并收尾
```

完成回灌、定时和主动消息要有明确来源类型，不能触发普通主人消息的待办整理或记忆维护。投递 accepted/queued、sent/confirmed、failed/unknown 分开；平台无法确认时不伪称送达。任务恢复先检查活执行句柄/进程身份，不能因为 observation timeout 自动重启。

## 7. 行为契约和全功能验收矩阵

每行需要公开行为证据；不得仅以内部类存在、工具名称计数、manifest、mock 调用或绿色总测试替代完整验收。

| 验收标识 | 可验证行为 | 最小必要反例 / 失败路径 | 证据 seam |
| --- | --- | --- | --- |
| G01 配置与目录 | 修改角色描述、名称、头像后，Shell 全配置及其他能力不丢；重载一致；目录与实际 schema 对齐 | 关闭、空白名单、缺 grant、deferred、缺二进制、加载失败各有不同原因；不能把注册数当可用数；手工把已关闭工具名写入enabledTools也不能绕过专用开关 | React 用户操作 + 角色 HTTP API + 捕获的模型请求 |
| G02 调用闭环 | 真实 HTTP 对话中模型 tool_call 执行，结果持久化后进入下一次请求；图像可到视觉 provider | 禁止工具/额外身份字段/pre hook 改参/取消/超时/异常均正确错误，不伪 success | `AgentTool.execute` + HTTP/SSE + SessionStore 公开读取 |
| C01 | 关键词/精确选择和已加载反馈；同 run 下一请求出现完整 schema | 搜索/偷调未授权工具仍拒绝；重启不恢复失效授权；MCP 修订不改写旧快照 | HTTP 对话 + provider 请求序列 + 公开能力版本 |
| C02 | 文本 0-based offset、1-based 行号、400 行/10000 UTF-8 字节预算、图片、有序一级目录；写入建父目录；唯一/全量替换和 diff | 非文本、未知图片、越界根/junction、0/多次匹配、BOM/CRLF、并发冲突、取消后的完整文件状态 | AgentTool + 临时真实文件；角色删除后权限回收 |
| C03 | 白名单真实命令前台和后台执行；输出大于预算仍持续消费；转后台硬截止；轮询尾部与完整日志；停止整树 | 跨角色 task ID、未知任务、子孙进程、非零、超时、取消、关闭/重启 interrupted；父回合结束后的行为 | AgentTool + 本机受控子进程 + HTTP 对话 |
| C04 | 查询参数影响检索请求；SSE/JSON 响应能解析；抓取 markdown/text/html 和最终 URL | 无凭据/服务失败/空结果、重定向内网、DNS 目标、二进制、超5MiB/50000字符、超时/取消 | AgentTool + 受控 HTTP 服务；真实公开网页 smoke |
| C05 | memorize → recall → fetch 原文 → forget → 不再有效；search 分页 source_ref 可定位并返回邻接原文 | 其他角色、私聊/群上下文扩权、伪 session_key、内部 metadata、失败回复、记忆无来源/无匹配 | AgentTool + 真实 MemoryEngine/SQLite + HTTP 对话 |
| C06/C09/P07 | 角色账号/目标目录、成员姓名/ID/历史昵称、群笔记省略/清空、旁听和附件完整流程 | 错角色/账号、过期目标、非主人、外部群/陌生人、跨绑定、附件路径伪造；外发失败不能显示成功 | 现有渠道公开服务 + 工具；显式授权测试账号实投 |
| C07 | 同步有最终结果，后台立即返回并在完成后自动回原上下文；list/cancel 生效 | 资源上限、未知profile约定、预算终止、重复完成、原目标失效、取消竞态、越权子工具 | AgentTool + fake provider 驱动真实 TaskRuntime + HTTP |
| C08 | at/after/every/cron、instant/soft、上海默认及其他时区；after 按接收时刻补偿；重启持久；角色范围列表/取消 | 无效时间、到期/离线、重复触发、取消并发、id前缀最短8字符、重名匹配策略、外发许可撤销 | 可注入时钟 + 真实持久调度/投递 + HTTP 对话 |
| C10 | stdio initialize/list分页/call，文本/图像/structured content，动态名稳定；连接后发现并调用；移除注销且清进程 | 重名工具、非法命令/环境、未授权server、握手/请求超时、断连、部分tools/list、重启恢复、外部发言者拒绝 | 本地测试 MCP server + AgentTool + HTTP/模型请求 |
| P01 | 全28工具 schema 与适配可验证；真实页面涵盖导航、表单、tab、等待、截图；同角色登录状态持久 | 缺原生依赖、不同角色profile、过期引用、退出/取消/删除角色、异常清理 | 既有浏览器 seam + 本地页面/真实二进制 + 对话 |
| P02 | list → state → 有四元组的动作 → 再观察确认；按键和release；固定驱动校验 | 旧窗口/重建/移动/DPI、多屏、最小化、错token、并发角色/进程、后台组合键、unverifiable、取消清lease | 本机受控测试窗口 + 原生驱动；lease故障注入 |
| P04 | 仅按需截主屏；独立 vision 连接被调用且 tools=[]；只返经过校验的摘要 | 缺桌面/视觉权限、错误JSON、工具/动作输出、停用和取消；持久记录/主聊天请求中无原图 | 截屏 port + AI公开seam + 本机smoke |
| P05 | 导入包绑定角色，真实桌面透明surface中 move/play；枚举包动作；重新启动与停用一致 | 错角色/非桌面、未绑定、非法动作、3秒冷却、同回合二次、桥断连；accepted不冒充播放完毕 | 原生host IPC + renderer事件 + 人工视觉验收 |
| P06 | txt2img/img2img、seed/尺寸/模型、保存/显示、历史/重生成、生成媒体随回复且不重复；场景CG流程可触发 | 无token、未解析env、非法参数/跨角色底图、额度/网络/取消、重复消费、scene_cg缺scene_key | 本地模拟provider HTTP + 媒体/对话；获准真实生图 |
| P03/S07 | 真正独立worktree任务，查询/继续/取消，同agent/session；结果回原上下文；验证交付/commit/PR head | CLI缺失/未登录、并发继续、取消只能确认到unknown、宿主重启、旧head、伪交付、未经授权外部写入 | Herdr/Codex公开CLI adapter + 测试仓库；获准真实任务 |
| S01–S08 | 八份技能可发现、显式激活、依赖可解释；各自完整工作流实际演示；新建技能/主动源/Drift技能被后续运行消费 | 不支持frontmatter、hash改变、脚本/支持文件越界、同名优先级、缺bin/env/MCP、工具未授权 | SkillRegistry公开seam + HTTP $skill调用 + 依赖fixture/真实CLI |
| A01 | alert/content/context → 复合键正文 → 兴趣标记 → 一次草稿 → reply提交/skip；近期聊天过滤主动/内部帧；ACK持久去重 | get_context重复、无草稿reply、有草稿skip、失败抓取误判不感兴趣、过期来源、投递失败/重启重复 | 测试MCP来源 + 注入时钟 + 真实管线/SQLite/投递 |
| A02 | 空闲触发 → Drift技能/目录 → 授权工具/MCP挂载 → 最多一次发送尝试 → finish核对状态；成功后收窄工具 | 主人新消息/取消、预算、跨角色目录、第二次推送、未送达却finish成功、停用后资源残留 | 可控空闲/时钟 + 真实管线 + AgentTool/投递公开seams |

文件预算、参数默认值和 CLI 协议以调研固定上游为行为参考；如 Meido 要改变可见参数或范围，要在对应 Ticket 的兼容差异和验收中具体列明。工具意图 `description` 可沿用上游 5–12 字提示，但不能把模型是否遵守提示当作授权或安全校验。

## 8. 测试决定和公开 seam

- 默认红—绿—重构。先通过公开 `AgentTool.execute(arguments, ToolContext, on_update)` 写用户行为失败测试，再实现。只在必要边界注入 HTTP transport、时钟、进程/桌面驱动或 provider；不测试私有函数名、内部容器或复制实现的分支。
- 应用级使用 `POST /api/roles/{role_id}/messages` 的 HTTP/SSE 驱动对话，用可控 provider 发出真实 tool_call 并检查第二次 provider 请求、角色 session 读取和持久审计。不能直接调用私有装配函数代替整条入口。
- 前端使用 React Testing Library 和 user-event 按标签操作角色设置、保存、重载、启停和查看错误；输入后的真实角色 API 配置和实际 schema 是断言目标。
- 外部 HTTP/MCP/CLI 用受控服务和临时测试仓库验证协议与失败；涉及本机进程树、浏览器、截图和原生窗口时必须有真实依赖 smoke。mock 通过仅证明隔离层，不声称真实服务/桌面已验收。
- 每个工具切片运行相关 `python -m pytest <相关测试>`；前端相关切片运行 `npm run test:web -- <相关测试>` 和 `npm run build`（包含 TypeScript 检查）。整体验收运行 `python -m pytest`、`npm run test:web`、`npm run build`；若仓库新增正式命令，以当前脚本为准并登记。
- 完成后分别按仓库规范和本规格做独立 Code Review。CI、未解决评论、真实依赖验收缺失或工具处于占位状态时不得宣告全范围完成。

## 9. 垂直 Ticket 划分和依赖

以下为票的候选目标和真实行为依赖。创建前核对开放 Issue/PR，能扩展或验收现有工作的应优先复用。每个最终 Ticket 用 `Parent / What to build / Acceptance criteria / Blocked by` 四段结构，写原生 GitHub 依赖；本表不能替代正式 Ticket。

| 票代号 | 从用户角度完整可演示的结果 | 覆盖 | 真正的前置依赖 |
| --- | --- | --- | --- |
| T01 | 主人可安全保存角色全部工具配置，配置受控Shell并查看真实工具状态与禁用原因；关闭开关后enabledTools不能重新开启该能力 | G01、G02现有能力 | None — can start immediately；如已有UI工作覆盖先协调 |
| T02 | 角色在获准目录读取、创建和精确修改文件，主人能验证真实文件和diff | C02、G02 | None — can start immediately；沿用现有AgentTool与角色目录边界 |
| T03 | 角色能联网搜索并抓取来源网页，失败和引用可追溯 | C04、G02 | None — can start immediately |
| T04 | 角色能搜索允许的历史并取消息原文作为回答证据 | C05消息部分、G02 | None — can start immediately；现有渠道上下文边界 |
| T05 | 主人可以在对话中让当前角色记住和撤销记忆，之后召回结果正确 | C05记忆部分、G02 | None — can start immediately；复用MemoryEngine |
| T06 | 角色可在同一次对话发现并调用延迟工具且不扩大权限 | C01、D01 | T01能力目录；D01具体方案审阅 |
| T07 | 角色长命令可后台继续、查询完整结果和停止，并正确处理关闭/重启 | C03、D02 | D02具体方案审阅；T01工具配置保留（避免后续配置覆盖） |
| T08 | 角色可同步或后台委派子agent，管理任务并自动收到完成结果 | C07 | T07独立TaskRuntime合入 |
| T09 | 角色建立、查看、取消通用定时文本或到时AI任务 | C08、D03 | T07；D03具体方案审阅 |
| T10 | 已有角色账号/群/附件实现达到Shiori范围，错误和真实投递可验证 | C06、C09、P07 | 协调现有#116–#121；以最新原生依赖关系为准，不重复实现渠道 |
| T11 | 已有浏览器28工具在授权角色上真实可用、profile隔离并显示准确依赖错误 | P01 | None — can start immediately；已有PR覆盖时改为协调验收 |
| T12 | 主人配置并授权本地MCP，角色在当前对话连接、发现、调用和移除 | C10 | T06能力修订/发现；D01通用MCP权限部分审阅 |
| T13 | 主人能使用天气、摘要、表情包维护和技能创建四个内置Skill | S01、S02、S04、S05 | T02；必要CLI的独立用户配置和现有Shell许可；不要求先做MCP/TaskRuntime |
| T14 | 主人通过feed Skill管理真实MCP订阅源 | S03 | T12、T13的技能适配 |
| T15 | 角色通过codex-delegate后台完成CLI任务并回读结果/session | S07 | T08、T02、T13；D05对应许可审阅 |
| T16 | 角色按需观察当前屏幕并只获得独立视觉模型摘要 | P04 | 独立vision连接的具体配置方案审阅；不依赖Computer Use或桌宠 |
| T17 | 角色在获准Windows桌面中安全定位并操作真实窗口、释放控制权 | P02 | 原生驱动/桌面授权/lease具体方案审阅；不依赖T16 |
| T18 | 主人在真实桌面使用角色桌宠，角色能移动和播放包动作 | P05、D04 | Electron路线已批准，按#195实施；不以网页预览替代 |
| T19 | 角色能按请求生成/重生成图片，维护历史和场景CG并正确附到回复 | P06 | NovelAI凭据/媒体许可方案审阅；独立于TaskRuntime与桌面 |
| T20 | 角色可管理完整Herdr Coding任务并验证真实交付 | P03 | T07、D05审阅；测试仓库/CLI/外部操作授权 |
| T21 | 角色消费主动来源并经草稿/finish提交或跳过，避免重复投递 | A01、S08 | T07、T12、T03、T04；与T09共享行为的变更先串行 |
| T22 | 角色空闲时执行Drift小任务，挂载MCP并按单次发送/finish收尾 | A02、S06 | T21共享主动/来源管线先合入；T02、T13；按需求复用T09时钟服务 |
| T23 | 全部对应工具、Skill、主动/Drift与真实依赖形成完整验收证据 | 全矩阵 | 各相关功能票已合入和人工批准的服务/桌面验收环境 |

T01–T05、T11 可以从同一最新 main 的独立分支并行，但每票在修改前登记涉及文件；若均需改 `main.py` 的同一装配区、角色配置或capability行为，改为顺序或复用同一已合入seam，不在多个分支重复改写。首批约定：T01拥有角色配置/RoleEditor/能力目录与专用开关hard-deny；T02/T03不修改models.py、RoleEditor或capabilities等共享行为，使用现有enabledTools和Role API配置，根协调者统一安排main.py各自局部装配。T02/T03可使用既有AgentTool公开接口，不能为了等待T06才做纯模块而漏掉完整对话接入。没有真正依赖时不人为设置全部串行。

所有实现分支每 Issue 从最新 main 创建，采用 `feat/<issue>-...`、`fix/<issue>-...` 或 `chore/<issue>-...`。依赖票未合入则等待；不得从前置功能分支继续分支或提交堆叠PR。PR 目标 main，中文 Conventional Commits 标题，正文 `Refs #...`，未经人工明确批准不合并、不关闭Issue、不执行外部事务。因此本轮可以完成独立票并提交可审查PR，不能把“PR已创建”记成依赖已解除或全功能已交付。

## 10. 风险、限制和证据账本

- 上游MIT复制范围需保留 `Copyright (c) 2026 YinFengWindy` 和许可；Browser/Computer原生文件、NovelAI服务/模型和CLI分别核对许可，不能用主仓库MIT替代。
- 工具目录的只读检查与运行期健康不相同；缺依赖有准确诊断是必要失败行为，仍需为最终可用路径补真实部署/运行证据。
- 宿主Shell白名单不是OS沙箱；MCP、Herdr、桌面和付费API分别获得许可，不能随工具搜索自动升级。
- 新数据库记录、状态机和能力修订关系必须有旧数据兼容与失败恢复测试；SQLite会话事实和后台/投递事实不能互相覆盖。
- 浏览器/桌面引用会失效，外部账号目标和所有权也会变化；执行前再次校验，失败不换目标或自动重试外部写入。
- screenshot/模型密钥、MCP env和用户附件不能进入普通审计/日志预览。完整Shell日志和生成媒体需要角色归属、预算、保留/清理规则。
- `observe_screen` 不包括24小时持续录屏；语音、Bilibili直播和未注册的其他插件子系统不属于本次76工具和指定运行管线范围。自动场景CG、媒体历史等作为指定工具完整工作流已在P06明确列入，不能以“不是额外工具名”为由丢弃。
- 开放Issue与main已有实现可以同时存在，main仍需按行为验收；具体Issue标签、PR目标/进度、原生依赖关系以实施时GitHub为准。

实施期间在各Ticket/PR记录：`验收标识 → main提交/PR → 公开测试命令与结果 → 真实依赖/运行证据 → 未完成项`。总目标完成前逐项检查C01–C10、P01–P07、S01–S08、A01–A02和G01–G02；任何缺失、间接证据、失败、未经验证或仅占位项都保持未完成。不能从测试通过率、工具数量或计划存在推断完整目标已经实现。
