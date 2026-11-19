# 新版 Meido 多端会话开发方案

状态为开发设计草案，更新于 2026-11-19。已确认方向为 Koishi/Satori 接入、Shiori 主人配对、N.E.K.O. 式会话权限，以及每回合独立的 Pi 内存会话。下文的接口、表结构和里程碑是建议实现；上游提供协议或 SDK 的依据，各渠道实际能力在对应阶段验收。[重构决策](pi-refactor-decisions.md) · [Pi 执行底层](pi-runtime-integration-design.md)

## 首版交付

主人给角色连接一个 QQ 账号，从 Meido 生成配对码，在 QQ 私聊完成绑定。主人在桌面告诉角色自己的称呼偏好，换到已配对的 QQ 私聊，角色仍能按记住的偏好称呼主人。桌面与 QQ 各自保存聊天线程；角色也能在获准的 QQ 群里应答。群友、受委托管理员和主人在同一群里连续发言，每个人只能使用自己的授权。主人在网页按角色查阅各会话、调整权限，能看到每条消息发到了哪个聊天、是否发送成功。

先用 Meido 主聊天、一个 NapCat 账号和一个测试群走完整流程。Telegram、飞书、Discord 后续逐个平台验收。官方 QQBot 与 NapCat 分开登记能力。飞书聊天接入使用独立的渠道授权。

## 技术选型与进程

| 部件 | 建议技术与职责 |
| --- | --- |
| 渠道桥 | 本机独立 Koishi 进程，加载平台适配器和 Satori 服务。QQ 首条链路为 NapCat → OneBot 11 → Koishi → Satori v1。Meido 经带令牌的 WebSocket 收事件，经 HTTP 调用消息 API。断线只影响外部聊天，桌面仍可使用。适配器凭据由接入端管理，宿主保存连接引用。 |
| 会话宿主 | Node.js ≥22.19、TypeScript，复用新版 Meido 服务端及角色 ID。桌面从已认证的本机入口进入，渠道从 Satori 进入，两者调用同一个回合入口。账号归属、配对、准入、历史筛选、工具范围和发送目标由宿主管理。 |
| 执行 | 依照执行模块固定 `@earendil-works/pi-coding-agent@1.5.2`。每次使用 `SessionManager.inMemory()` 创建独立会话，同一回合内使用 Pi 原生工具循环、取消和结束事件。宿主按角色提供模型连接、可信资源加载器以及本轮工具。 |
| 本地持久化 | SQLite 保存账号、绑定、会话、消息、运行和投递。事务与唯一约束负责去重和状态转换；网络请求在事务外进行。附件使用素材服务的引用和本地原件。长期记忆沿用角色 Markdown 和结构化记忆引擎。 |
| 管理界面 | 复用 Meido 前端和本机认证。通过 HTTP 修改账号、配对与策略，用游标分页读取聊天；建议用 SSE 推送已提交的消息、授权变更和投递状态。重连从持久记录补读。 |

交付首个纵向切片时锁定实际通过测试的 Koishi、Satori 服务插件、OneBot 适配器和 NapCat 版本，并记录 Node 版本、协议路径及所需配置。源项目的 Satori 集成提供桥接参考；Pi 相关依赖沿用现有执行模块。[AIRI 接入实例](https://github.com/moeru-ai/airi/blob/b3a6d60718e52785eedcc1f96c6f1ab398741286/integrations/satori-bot/README.md) · [Satori 事件协议](https://satori.chat/en-US/protocol/events.html) · [Satori 消息 API](https://satori.chat/en-US/resources/message.html) · [固定版本 Pi SDK](https://github.com/earendil-works/pi/blob/v1.5.2/packages/coding-agent/docs/sdk.md)

## 主要模块和接口

| 入口或模块 | 输入 | 输出与边界 |
| --- | --- | --- |
| 渠道桥接 | Satori login、channel、guild、user、message、原生话题，以及结构化引用、提及、附件 | 统一消息，带本地账号实例、平台身份范围、会话类型和来源 ID。缺失的引用作者或时间保留未知。自己的回显仅用于核对投递。 |
| 身份与准入 | 可信来源、接收时绑定、人物名单、群模式和平台能力 | 拒收、仅旁听、进入回合三种结果。平台管理员头衔可作显示资料。主人身份只来自宿主配对。黑名单在记忆、模型和任务前拦截。 |
| 会话与上下文 | 角色、会话、入站消息、绑定生效起点、当前仍有效的授权 | 当前输入、可读历史和附件、记忆主体、可用工具和目标范围。记录来源和授权版本以便撤权。摘要、检索、工具续接均使用相同范围。 |
| Pi 运行 | 本轮上下文、可信工具定义、取消信号 | 结构化回复、应答对象、工具进度。使用 Pi 的工具声明和执行拦截。工具执行前校验实际参数、调用人和当前授权，外部写入提交前再核验。 |
| 投递 | 运行 ID、目标账号及会话、要发送的消息和素材引用 | 按实际发出的平台消息记录发送结果和平台消息 ID。普通回复与后台任务回报共用发送入口。 |
| 读模型 | 角色、会话、分页游标、事件游标 | 来源账号和发言人快照、原始引用、实际应答关系、附件及消息发送结果。网页打开群历史只产生读取操作。 |

建议管理 API 以角色为入口，例如 `GET /api/roles/{id}/conversations`、`GET /api/conversations/{id}/messages?before=…`、`PATCH /api/conversations/{id}/policy`。一次性配对由本机主人入口 `POST /api/owner/pairing-codes` 发起；解绑使用绑定记录 ID。策略写入携带前次读取的版本，过期时返回冲突并让页面重新加载。连接令牌、原始配对码和模型凭据均与可浏览的消息记录分开。

## SQLite 记录与不变量

| 逻辑记录 | 关键字段、约束与读取方式 |
| --- | --- |
| 渠道账号、会话 | 账号保存角色、桥实例、平台、机器人身份、连接代次和已验证能力；同一活跃登录只归一个角色。会话按角色、账号实例、类型、平台聊天和话题唯一。无话题使用固定值参与唯一约束。账号移除后保留来源快照；重绑新实例不会继承旧账号授权。 |
| 主人绑定、策略 | 绑定保存平台或应用有效范围、外部用户 ID、起点、撤销时间和代次。一个配对码只允许一个私聊成功消费，绑定和消费同事务提交。人员名单和群策略按角色保存。授权发生变化时更新版本。 |
| 消息、附件、应答 | 保存会话内稳定序号、平台消息 ID、实际作者、身份快照、时间和消息段；引用与应答分别指向来源消息。以会话和平台消息 ID 去重；跨群相同数字 ID 各自有效。创建、编辑、撤回分别处理，更新既有记录不会再次触发新消息回合。 |
| 运行、发送记录 | 运行关联实际输入、触发人、入站授权上限和状态。普通回复按输入幂等建回合。每个发送动作记录目标、平台返回的消息 ID 或错误。读取历史时仅将平台已确认发送成功的内容视为角色已说过。 |
| 记忆及素材引用 | 记忆保留主体、来源和证据消息。聊天附件通过本线程的消息和附件序号读取。已收藏的图片和表情按角色素材 ID 查找，可在该角色其他聊天中使用。[素材服务](pi-media-library-design.md) |

## 身份、受众和工具的裁决

主人从本机界面取得十分钟有效的一次性配对码，在目标平台私聊角色。配对消息在普通聊天入库前消费。平台级身份可应用于同一平台的多个角色账号；仅在应用内稳定的标识限定到该应用。只有主人通过绑定跨端识别。普通成员和管理员的身份与记忆沿用所在渠道的范围。历史按角色与当前线程读取，主人记忆按角色、绑定和受众读取。解绑后重新配对产生新起点，排队中的请求不能借后来的绑定增加权限。[Shiori 配对](https://github.com/YinFengWindy/Shiori-Agent/blob/7ecceb3be3954aa3ba4b154b5e052411fe7ccb08/apps/backend/core/identity/pairing.py) · [历史范围](https://github.com/YinFengWindy/Shiori-Agent/blob/7ecceb3be3954aa3ba4b154b5e052411fe7ccb08/apps/backend/conversation/context_scope.py)

| 当前请求 | 可提供给 Pi 的材料 | 可执行动作 |
| --- | --- | --- |
| 桌面主人、已配对主人私聊 | 当前线程内获准的历史、引用和附件，加上该角色获准使用的主人记忆。桌面主聊天和 QQ 私聊各有自己的线程。 | 角色在该入口启用的主人工具。普通回复回原处；指定改投时验证目标账号、受众和素材。 |
| 成员私聊 | 当前私聊记录和开启的该成员记忆。普通成员与信任成员均按稳定 ID 识别。 | 名册成员可用显式开放的工具。陌生人的应答按私聊设置，默认没有业务工具。 |
| 获准群聊 | 当前群或话题、当前引用、本群资料和群内成员资料。群模式为关闭、点名应答、自然参与。自然参与需要能收普通群消息。 | 工具须同时符合角色配置、真实发言人和场合。主人在群里仍能被识别；受委托管理员按自己的授权使用工具。 |
| 群内参考私人记忆 | 默认关闭。按群开启且发言人本人已启用个人记忆后，只读该发言人获准在群内使用的记忆。引用作者、被提及者、角色主动插话均不能提供这项授权。 | 群资料写回原范围；普通群友无权指定群记忆或成员档案的变更。另按实际作者识别主人亲自交代的偏好，限定本群或通用的适用范围。私人读取权限与写入归属分别校验。[记忆开发方案](pi-memory-design.md) |

角色已启用的全部主人工具是身份上限。工具是否可在群里调用、结果可发给谁，分别校验。以日历为例，群用工具只向该群回合返回可公开的忙闲信息；只准私聊的会议明细由宿主在已授权的私人目标中处理，避免将明细放入群模型再靠措辞隐藏。文件、素材和后台工作结果同理。新工具默认仅主人可用；可间接调用其他工具的扩展沿用 Pi 执行模块的限制。[N.E.K.O. 工具分档](https://github.com/zhaijiunknow/n.e.k.o_plugin_qq_auto_reply/blob/54acf36bf978edad4757e5fffc743c5618360700/plugin_tool_service.py) · [单向记忆读取](https://github.com/zhaijiunknow/n.e.k.o_plugin_qq_auto_reply/blob/54acf36bf978edad4757e5fffc743c5618360700/group_private_memory.py)

### 普通群友能用哪些工具

这里的“群友工具”指普通成员发言后，角色的 Pi 回合可以为其调用的业务工具。普通群回复、引用和发送文字由渠道投递完成。下表按固定版本的注册和授权代码核对；实际可用还取决于已加载的插件、平台能力和实例设置。

| 上游 | 普通群聊场景中实际有哪些能力 |
| --- | --- |
| Shiori 内置 | 受限外部回合可用 `web_search`、`web_fetch`、`search_messages`、`fetch_messages`、`tool_search`。工具发现仍按外部授权过滤。`lookup_member` 允许按当前渠道查成员；群笔记查询、修改和旁听开关留给获准回合。[基础工具注册](https://github.com/YinFengWindy/Shiori-Agent/blob/7ecceb3be3954aa3ba4b154b5e052411fe7ccb08/apps/backend/agent/tools/meta/register.py) · [群工具注册](https://github.com/YinFengWindy/Shiori-Agent/blob/7ecceb3be3954aa3ba4b154b5e052411fe7ccb08/apps/backend/bootstrap/toolsets/groups.py) · [成员查询范围](https://github.com/YinFengWindy/Shiori-Agent/blob/7ecceb3be3954aa3ba4b154b5e052411fe7ccb08/apps/backend/agent/tools/member_lookup.py) |
| Shiori 渠道和插件 | 渠道工具另有 `account_list`、`account_targets`、`account_send`。外部受限回合的发送目标只允许已绑定用户。加载对应插件后还有 QQ 的 `read_attachment`、NovelAI 的 `generate_image`、计划插件的 `list_schedules`；计划的创建和取消没有这项外部授权。[渠道注册](https://github.com/YinFengWindy/Shiori-Agent/blob/7ecceb3be3954aa3ba4b154b5e052411fe7ccb08/apps/backend/bootstrap/tools.py) · [发送目标限制](https://github.com/YinFengWindy/Shiori-Agent/blob/7ecceb3be3954aa3ba4b154b5e052411fe7ccb08/apps/backend/agent/tools/account_delivery.py) · [QQ](https://github.com/YinFengWindy/Shiori-Agent/blob/7ecceb3be3954aa3ba4b154b5e052411fe7ccb08/plugins/qq/backend/plugin.py) · [NovelAI](https://github.com/YinFengWindy/Shiori-Agent/blob/7ecceb3be3954aa3ba4b154b5e052411fe7ccb08/plugins/novelai/backend/plugin.py) · [计划](https://github.com/YinFengWindy/Shiori-Agent/blob/7ecceb3be3954aa3ba4b154b5e052411fe7ccb08/plugins/schedule/backend/plugin.py) |
| N.E.K.O. QQ 插件 | QQ 自带 `recall_memory`，模型给查询词和时间，宿主决定可读主体；关闭群记忆后不读取。外部插件映射 `qq_open_plugin_tools` 默认为空。搜索、点歌等已启动插件可以由主人配置；`all` 仅包含名册中的普通、信任成员和管理员，`admin` 仅管理员，未识别者没有外部插件工具。执行时复核具体入口。[记忆工具](https://github.com/zhaijiunknow/n.e.k.o_plugin_qq_auto_reply/blob/54acf36bf978edad4757e5fffc743c5618360700/memory_tool_service.py) · [默认配置](https://github.com/zhaijiunknow/n.e.k.o_plugin_qq_auto_reply/blob/54acf36bf978edad4757e5fffc743c5618360700/settings_schema.py) · [插件分档](https://github.com/zhaijiunknow/n.e.k.o_plugin_qq_auto_reply/blob/54acf36bf978edad4757e5fffc743c5618360700/plugin_tool_service.py) |
| N.E.K.O. QQ 表情 | 本群允许收藏且最近收到符合条件的图片时，回复运行才注册 `save_sticker`。这是带来源的条件工具；主人给群开放外部插件与收藏开关各自生效。Meido 已有素材模块定义收藏权限，普通群友首版只可获准检索和发送，群素材收藏仍由有写权限的入口负责。[实际回合装配](https://github.com/zhaijiunknow/n.e.k.o_plugin_qq_auto_reply/blob/54acf36bf978edad4757e5fffc743c5618360700/reply_generation_service.py) |
| AIRI / Koishi | AIRI 的 Satori 模块注册了 `read_unread_messages`、`send_message`、`list_channels` 和 `continue`、`break`、`sleep`。它们是 Agent 收发和调度动作，注册表本身不能证明普通群友获得跨频道读取或发送权。Koishi 的 `help`、`echo` 等是已安装插件提供的用户指令，指令和选项可单独设权限。在 Meido 中，渠道指令与 Pi 工具分别登记。[AIRI 动作注册](https://github.com/moeru-ai/airi/blob/b3a6d60718e52785eedcc1f96c6f1ab398741286/integrations/satori-bot/src/capabilities/registry.ts) · [Koishi 指令权限](https://koishi.chat/zh-CN/manual/usage/command) |

**Meido 群友默认清单采用 Shiori。** 以本节核对的 Shiori 提交 `7ecceb3` 中标记为 `external_allowed` 的九项内置工具及三项插件工具为基线。群与消息通过准入后，普通群友回合自动继承该清单，包括未单独列入名册的群友。角色已启用、服务已配置、渠道支持的工具才实际可用。主人显式配置的群或成员禁用项优先；已有自定义配置保留。陌生人私聊仍按上表的私聊规则处理。

| 开放方式 | 群友可以请求的工具 | 使用范围 |
| --- | --- | --- |
| 默认 · 网页 | `web_search`、`web_fetch` | 搜公开资料、读公开 HTTP(S) 网页。重定向复核目标；请求不携带主人登录态，也不访问本机或内网服务。 |
| 默认 · 消息与成员 | `search_messages`、`fetch_messages`、`lookup_member` | 前两项限当前线程或话题。成员查询按当前渠道解析标识，再按本群受众返回可见名字、名片等资料；本人记忆仍遵守既有的成员读取规则。 |
| 默认 · 工具发现 | `tool_search` | 只检索本回合有效且已注册的工具。发现、解锁及随后的实际调用使用同一授权范围。 |
| 默认 · 渠道 | `account_list`、`account_targets`、`account_send` | 查询本角色的渠道、能力和适配器可提供的目标目录。目录的查询权与发送权分别校验。受限发送的参数声明和执行入口均限定 `target_kind=user`，由宿主解析到已绑定主人的私聊；普通群回复仍回原群。 |
| 对应插件启用后默认 · 附件 | `read_attachment` | 从当前消息或获准引用、附件序号解析。QQ 首批验收文本附件，不接受模型传来的本地路径。 |
| 对应插件启用后默认 · 生图 | `generate_image` | 角色启用且已配置生图后，群友可请求生成。沿用次数额度、参考素材权限和任务幂等键；发图时另验素材与目标。 |
| 对应插件启用后默认 · 计划 | `list_schedules` | 列出本角色待执行计划，按群受众筛选可展示字段。此项只读，创建和取消计划分别授权。 |
| 按群开启 | `recall_memory` 查本群公共约定或本人可读记忆 | 复用已有记忆工具及可见来源校验。第三方提及不能选择他人的个人档案；群内参考本人私人条目仍需群与本人双开关。[记忆方案](pi-memory-design.md) |
| 按群开启 | `search_assets` 找表情、`send_asset` 发到当前群 | 从本角色素材库检索，按本次工具权限分别核验检索和投递；账号、群和话题由宿主固定。[素材工具](pi-media-library-design.md) |

以上三项按群开启的工具是 Meido 已有扩展。`tool_search` 使用宿主过滤后的注册表；Shell、任意文件读写、群记忆写入及其他未授权工具均排除在候选和可执行集合之外。主人在群设置中关闭某项后，列表、发现及执行同步收窄。

**开发与验收。** 在群权限、工具和素材联调票中使用真实 Pi SDK、受控搜索服务和两个模拟群。分别断言发给模型的工具名、工具实际执行次数，以及回复到达的目标和内容：

1. 对没有自定义覆盖的新群，以普通成员和未列名但已准入的群友各跑一轮。内置服务配置齐全时，有效工具集合等于表中九项内置工具；分别启用 QQ 附件、生图、计划插件时，仅相应工具加入默认集合。用实际模型声明及工具发现结果核对集合。显式关闭某项后，搜索工具名和直接调用都无法使用它。
2. 群 A 默认可回看本群引用，启用 QQ 附件能力后可解释相应文本附件。群 B 的引用、私聊附件、引用主人的命令和伪造工具名都不能扩大读取范围。`lookup_member` 返回获准的成员资料，不能借此取得他人私人档案。
3. 搜索到表情后撤销发送权限，已有候选不能触发外发。启用生图后重复提交同一任务只预占一次额度；实际发送成图时再检查素材与受众。
4. 群记忆召回与写入分别验证。普通群友可按授权查询本群已确认的活动安排；要求角色改群规、改他人称呼，或绕到后台整理，均不能写成设定。
5. 群友通过 `account_targets` 查到一个群后，试图向它发送消息，实际投递次数应为零；选择已绑定主人时，只向该主人的私聊投递一次。未绑定或发送前解绑时不改投。在计划插件启用的群回合能查到本角色可向群展示的计划，无法通过工具发现创建或取消计划。

### 工具由谁提供，开发时接在哪里

2026-11-19 补充。通用工具优先直接加载 Pi 内置能力或现成扩展。Meido 为角色、会话、素材和投递等业务提供相应工具，按实际发言者、目标和受众裁决本轮权限。Koishi/Satori 提供平台事件和收发 API。设置页区分“已授权”和“提供方可用”。

| Pi 工具 | 实现来源与接入点 | 首次可用的条件 |
| --- | --- | --- |
| `web_search` | Pi 社区扩展 `@laboon85/pi-web-access@0.9.1` 原生注册同名工具。扩展默认使用免密钥的 `exa-free`，返回标题、摘要和 URL。 | 加载扩展、允许该工具且网络可达。 |
| `web_fetch` | 同一扩展注册同名工具。默认由其 `local` 提供方读取公开网页、提取正文并标注来源。 | 与搜索分别授权，可直接读取群友给定的公开链接。[扩展源码与配置](https://github.com/Lineance/pi-web-access/tree/16ddefe27cfd54b882212c8f586602d7de5590dc) |
| `search_messages`、`fetch_messages` | Meido 会话服务查询本地 SQLite；先限定角色和当前线程，再按文本或稳定消息引用读取。 | 会话库就绪。空历史是合法的零结果。 |
| `lookup_member` | Meido 按渠道稳定 ID 查询可见成员资料；当平台支持时，通过 Satori 补全当前群的公开名片。 | 有可核验的渠道身份；同名返回候选，未知字段保留为空。 |
| `tool_search` | 复用 Pi SDK 的 `createToolSearchExtension()` 并显式启用。交给其发现的 deferred 候选只能来自本轮获准工具，执行前再验权。 | 宿主已限制本轮可发现集合；收回授权后同步剔除候选，不能只移出模型可见列表。[Pi v1.5.2 内置扩展](https://github.com/earendil-works/pi/blob/v1.5.2/packages/coding-agent/src/extensions/tool-search/index.ts) |
| `account_list`、`account_targets`、`account_send` | 前两项组合 Meido 角色账号映射、已知会话和适配器支持的目录；发送经投递模块调用 Satori `message.create`，记录回执。 | 桥接登录和相应平台能力可用。向主人发送时还需有效配对和可达私聊目标。[Satori 消息 API](https://satori.chat/en-US/resources/message.html) |
| `read_attachment` | QQ 接入层解析当前或引用消息的附件，宿主按来源、类型和大小取回并提取文本。 | 对应账号能取回原件，且支持该文件类型。过期或只有预览时返回明确状态。 |
| `generate_image` | 调用 Meido 素材服务的生图任务接口。模型连接、原件保存、次数额度和投递复用素材方案。 | 已配置兼容的生图后端并通过试生成；具体供应商及版本仍待该模块验收。[素材开发方案](pi-media-library-design.md) |
| `list_schedules` | 已启用的计划模块提供只读查询，宿主筛选当前角色可向本群展示的条目。 | 有提供该能力的模块才注册。首版聊天可先行验收；单列记录计划插件覆盖情况。 |

**网页工具直接随 Pi 加载。** 新版宿主依赖固定版本的扩展包，由 Pi 的 `DefaultResourceLoader` 加载包声明的入口。扩展负责搜索、网络请求、网页解析及返回格式，Meido 负责在每个会话授权相应工具。首版使用扩展的默认配置；后续更换提供方优先使用扩展已有配置。[npm 发行包](https://www.npmjs.com/package/@laboon85/pi-web-access/v/0.9.1) · [扩展注册入口](https://github.com/Lineance/pi-web-access/blob/16ddefe27cfd54b882212c8f586602d7de5590dc/src/index.ts)

**验证记录。** 2026-11-19 在独立临时目录使用 Node.js 24.15、Pi SDK 1.5.2 和扩展 0.9.1 建立内存会话。通过会话里的 `web_search` 无密钥搜到 Satori 官方 Events 文档，用 `web_fetch` 读到同一页的正文；移除搜索后，当前活动工具只剩 `web_fetch`。正式群聊联调仍要验证模型实际调用、禁用后无法通过工具发现重新取得权限，以及 QQ 最终回复中的来源链接。

## 回合、记忆与故障恢复

1. **接收。** 从账号与平台范围解析来源。在任何排队或附件下载前确定当时身份和授权上限。获准事件的消息、入站去重和待处理回合同事务落盘。Satori 事件序号用于连接恢复；消息身份使用来源范围内的平台消息 ID。重连重新同步在线账号及能力。
2. **排队。** 同一会话按稳定顺序启动回合，不把另一位发言人的插话送入当前 Pi 运行。其他会话可以在实例并发上限内独立处理。进入模型前读取当前授权与原上限的交集；排队期间早一轮已经送达的合法回复可以进入历史。
3. **装配。** 保留当前请求，从当前线程选取获准的历史、引用、聊天摘要和附件。跨端识别出的主人可以召回同一角色获准使用的主人记忆。“第二张”只按本线程的消息定位，找不到则请主人在当前线程指明或提供图片。聊天很长时，优先把当前问题、引用和相关的近期对话交给模型。较早的原话仍保存在该线程，可在聊天记录中查看。Markdown 常驻记忆、结构化召回、Pending 和 Dreaming 产物都须保留适用受众和来源；混有私人内容的整段文档只在适当范围读取。压缩事件衔接现有记忆方案，正式回复成功时结算本轮结构化记忆。
4. **执行和撤权。** 订阅 Pi 事件后运行，同回合的工具调用连续执行。授权收回时取消受影响运行，丢弃依赖失效材料的未发草稿，重开时重新装配。
5. **发送。** 发送接口成功时显示「已发送」，返回错误时直接展示失败原因。文字和图片分别调用时各记一个结果。重连后继续处理新消息，重复入站沿用原消息 ID 去重。后台回报使用该任务的原发送目标。

## 开发顺序与完成条件

| 阶段和依赖 | 用户可以验证的结果 | 实现及验收重点 |
| --- | --- | --- |
| 1．QQ 聊天闭环。依赖新版角色 ID 和 Pi 正式聊天入口 | QQ 测试私聊可以问答，网页可打开这段历史。重发入站只收到一次回复。 | 先跑真实 Koishi + Satori 和可控 OneBot 对端，接入实际 Pi SDK、测试模型、SQLite 和只读会话页；记录兼容版本与真实 Satori 事件和回执形状。 |
| 2．主人配对。依赖 1 | 主人可从 QQ 私聊配对并从网页解绑，重配有独立生效起点。 | 覆盖配对码并发消费、配对前排队消息、解绑与双角色。 |
| 3．群聊权限。依赖 2 | 主人在群中被认出。群友引用主人时依然使用自己的权限。 | 先打通点名应答，再按各自依赖接群聊工具、自然参与、表情和成员记忆。 |
| 4．按前置联调 | 桌面保存的称呼可在已配对私聊使用，两处各有历史。群聊能自然接话、使用获准的资料和素材。 | 主人记忆只等配对和基础记忆。群资料、动图与主动选图按[开发计划](../handbook/pi-development-plan.md)各自联调。 |
| 5．逐个增加渠道。依赖首版 QQ 验收 | 对每个新增平台，设置页准确显示支持的聊天、消息和参与方式。 | Telegram、飞书、Discord 各自复用以上场景，逐项验证私聊、群或话题、配对身份范围、引用、普通群事件、附件和主动发送。只收到点名事件的账号只提供点名应答。官方 QQBot 另作账号类型验收。 |

## 验证环境、风险与交付证据

开发使用独立数据目录、端口、桥令牌和测试账号。协议测试在隔离实例运行真实 Koishi 和 Pi SDK，替换平台对端与模型服务。断言用户能读到的记录、实际模型请求中的上下文和工具、对端实际收到的消息和重启后的数据库结果。每个阶段运行相关自动化测试、类型检查和构建，按需求和仓库规范审查。首版演示另需专用 NapCat 测试账号与测试群；模拟协议通过和平台实测在结果中分别列出。

首版按真实平台能力验证消息 ID、群事件和发送接口返回，发现能力缺口时在设置中准确展示支持的参与或发送方式。

已关闭的旧版 [#116 渠道接入](https://github.com/sawaki-zexi/meido/issues/116)、[#202 网页浏览](https://github.com/sawaki-zexi/meido/issues/202)、[#338 身份与应答](https://github.com/sawaki-zexi/meido/issues/338) 和 [#370 外部工具权限](https://github.com/sawaki-zexi/meido/issues/370) 提供回归场景。新方案采用 Koishi/Satori 和独立 Pi 回合；旧票的验收记录只证明旧版行为。
