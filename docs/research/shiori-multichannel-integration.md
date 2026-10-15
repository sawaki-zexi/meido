---
title: Shiori-Agent 多端接入实现研究
kind: 源码研究
status: 当前基线研究
last_verified: 2026-10-13
baseline_commit: 1c241a2627bbcc99124aef71abaee237ac057499
sources:
  - https://github.com/YinFengWindy/Shiori-Agent/tree/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend
  - https://github.com/YinFengWindy/Shiori-Agent/tree/1c241a2627bbcc99124aef71abaee237ac057499/packages/sdk/python/shiori_sdk
  - https://github.com/YinFengWindy/Shiori-Agent/tree/1c241a2627bbcc99124aef71abaee237ac057499/plugins
  - https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/docs/_handbook/channel-plugins.md
---

# 研究范围与证据约定

本文只记录 Shiori-Agent 当前 `main`（提交 `1c241a2627bbcc99124aef71abaee237ac057499`，2026-10-10）源码、测试和仓库手册中可以核验的行为。源码链接固定到该提交；手册中标为 `bdfdae59` 的领域说明只作为导航，行为判断以同一基线源码和测试为准。

实现改编所需的上游版权与许可声明保存在[第三方引用声明](../reference/third-party-notices.md)。

- **已提取**：源码或测试直接证明的行为。
- **推断**：由多个已提取事实组合出的架构含义，未把推断写成 Shiori 的明确承诺。
- **不确定性**：源码无法单独证明，或依赖真实平台/部署配置，需实测或产品决定。

## 一、总体边界：插件负责平台，宿主负责路由

**已提取。** 外部聊天渠道全是插件，宿主内置的传输只有 `desktop`。渠道插件在 `manifest.yaml` 静态声明渠道、会话类型和能力，`setup()` 通过 `ctx.channels.add()` 贡献连接；账号身份、凭据、所属角色和响应规则由插件自己的存储持有，宿主只在运行时内存中索引已加载账号。[手册 §1-2](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/docs/_handbook/channel-plugins.md#1-包布局与-manifest)、[`plugins/telegram/backend/plugin.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/telegram/backend/plugin.py)、[`apps/backend/core/accounts/registry.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/accounts/registry.py)

宿主的 `ChannelHub` 统一执行账号存活、角色归属、响应规则、黑名单、身份识别、线程创建和出站投递标记；平台插件负责解析事件、平台去重、媒体处理、回复格式、连接生命周期和平台 API。[`apps/backend/core/channels/hub.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/channels/hub.py)、[`apps/backend/core/channels/role_routing.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/channels/role_routing.py)

**推断。** 这个边界允许增加飞书、QQ、Telegram 而不复制 Agent 回合逻辑；角色、会话和权限必须由宿主统一解释，平台差异只能留在插件适配层。

## 二、平台覆盖和当前限制

| 插件 | manifest 声明 | 入站实现 | 配对作用域 | 群聊/旁听 | 出站/流式 |
| --- | --- | --- | --- | --- | --- |
| `telegram` | 私聊、群聊；`instance_prefix: telegram_` | `python-telegram-bot` 轮询；消息/图片/文件；Telegram 消息 ID 去重 | `platform`，Telegram 用户 ID 跨 Bot 唯一 | 群聊可触发；manifest 未声明 `group_listening`，所以当前不能旁听所有群消息；Telegram 只上报 @，不把 reply 作为宿主触发条件 | 私聊和群聊文本/图片/文件；私聊支持消息编辑式流式，群聊不保证流式；Bot 隐私模式可能限制普通群消息 |
| `qq`（NapCat） | 私聊、群聊；`group_listening: true`；群 chat ID 为 `gqq:<群号>` | 每个账号独立 OneBot/NapCat socket；解析 CQ @、CQ reply、媒体和附件 | `platform`，QQ 号跨账号唯一 | @ 或回复账号触发；未点名消息可进入旁听记录 | QQ 文本和图片可合并；群回复 @触发者并可附加 @；QQ 自有 `read_attachment` 工具 |
| `qqbot`（官方 QQBot） | 仅私聊；C2C ID 为 `c2c:<app_id>:<openid>` | Gateway C2C 事件；Gateway/REST 连接按应用管理 | `account`，OpenID 只对一个 App 生效 | 群事件明确忽略 | C2C 文本/图片；私聊流式接口 |
| `feishu` | 仅私聊，`chat_id` 形如 `oc_<...>` | `lark-oapi` 长连接运行在独立线程/事件循环；回调回到宿主循环；事件去重；文本/富文本/图片/文件/引用 | `account`，Feishu `open_id` 只对一个应用生效 | 群事件当前直接忽略 | 私聊文本/图片；CardKit 流式卡片，权限不足时普通卡片回退 |

来源：[`plugins/telegram/manifest.yaml`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/telegram/manifest.yaml)、[`plugins/qq/manifest.yaml`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/qq/manifest.yaml)、[`plugins/qqbot/manifest.yaml`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/qqbot/manifest.yaml)、[`plugins/feishu/manifest.yaml`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/feishu/manifest.yaml)、[`plugins/qq/backend/accounts_inbound_adapter.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/qq/backend/accounts_inbound_adapter.py)、[`plugins/qqbot/backend/inbound.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/qqbot/backend/inbound.py)、[`plugins/feishu/backend/channel.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/feishu/backend/channel.py)、[`plugins/telegram/backend/channel/lifecycle.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/telegram/backend/channel/lifecycle.py)。

**不确定性。** 本文能确认的是 Shiori 当前代码，而不是平台服务长期保证；飞书群聊和官方 QQBot 群聊是否可用应当按当前 SDK/平台权限重新实测。若 Meido 的产品目标是“三个平台都支持群聊”，不能把 Shiori 的 `feishu` / `qqbot` 当前能力直接当作已实现能力。

## 三、账号模型、归属和账号级响应规则

**已提取。** 宿主生成确定的账号 ID：`<plugin_id>:<platform_account_id>`。同一平台账号不能属于不同角色；一个角色每个插件最多一个账号；同一角色不能重复添加同一插件的第二账号。`AccountRegistry._ownership_refusal()` 和 `tests/backend/core/accounts/test_registry.py::test_index_refuses_cross_role_and_second_account_per_plugin` 直接覆盖这些约束。[`packages/sdk/python/shiori_sdk/accounts/models.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/packages/sdk/python/shiori_sdk/accounts/models.py)、[`apps/backend/core/accounts/registry.py#L244-L289`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/accounts/registry.py#L244-L289)

每个账号保存：平台身份、插件配置引用、所属角色、显示名、头像、连接状态和 `AccountResponseRules`。规则只有三个字段：`private_enabled`、`group_enabled`、`blocked_sender_ids`；群开关是账号级的，所有群共享同一开关，点名/回复条件由宿主统一加在其上。[`packages/sdk/python/shiori_sdk/accounts/models.py#L22-L34`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/packages/sdk/python/shiori_sdk/accounts/models.py#L22-L34)

**已提取：入站准入顺序。** 插件解析事件并构造 `InboundMessage` 后先做平台消息去重；`ChannelIntake` 在运行时换代期间有界缓冲输入，溢出或关闭时回原聊天发送“渠道配置正在切换”提示。接收时先检查账号已登记、在线、属于存在的角色、且账号确实承载当前 transport；再检查私聊/群聊开关和发送者黑名单；群消息必须 `@` 账号或回复账号（回复条件要求插件上报 `reply_to_sender_id`）；未点名的群消息只有在该群已开启旁听时才进入旁听存储，否则丢弃。最后检查会话外部消息 ID 去重，才发布到宿主消息总线。[`apps/backend/core/channels/hub.py#L127-L202`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/channels/hub.py#L127-L202)、[`apps/backend/infra/channels/intake.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/infra/channels/intake.py)、[手册 §4](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/docs/_handbook/channel-plugins.md#4-入站会话键与-chat_id-约定)

黑名单同时支持平台用户 ID 精确匹配和渠道别名（例如 Telegram 用户名）忽略大小写匹配。`/stop` 使用同一准入；`/chatid` / `/myid` 由插件在 Hub 准入前识别，是唯一在未绑定会话上回复的控制命令，且黑名单发送者不会收到回复。[`apps/backend/core/channels/hub.py#L223-L299`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/channels/hub.py#L223-L299)、[`packages/sdk/python/shiori_sdk/channels/chat_types.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/packages/sdk/python/shiori_sdk/channels/chat_types.py)、[`plugins/telegram/backend/channel/commands.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/telegram/backend/channel/commands.py)。

**推断。** “允许所有人私聊”在 Shiori 不是“所有未经配置的渠道都可聊天”，而是账号存在且 `private_enabled=true` 后，对该账号没有黑名单的发送者放行；账号不在线、未分配角色、私聊开关关闭或黑名单命中都会静默丢弃。

## 四、入站数据契约、回复和来源快照

**已提取。** 标准入站消息至少包含：`channel`、平台 `sender`、稳定 `chat_id`、正文、媒体本地路径和 `metadata.account_id`。元数据中还应尽量保留 `external_message_id`、`chat_type`、结构化 `mentioned` / `mentioned_ids`、`reply_to_sender_id`、`sender_name`、`group_name`、`via_account`。插件字段先经过 `plugin_metadata()`，宿主自己断言的 `sender_is_user` / `reply_to_sender_is_user` 会被删除，插件不能伪造主人身份。[`packages/sdk/python/shiori_sdk/channels/projection.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/packages/sdk/python/shiori_sdk/channels/projection.py)、[`packages/sdk/python/shiori_sdk/channels/message_source.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/packages/sdk/python/shiori_sdk/channels/message_source.py)

`ViaAccount` 是消息经过的机器人/应用账号快照，包含 `platform`、`platform_account_id`、当时显示名和插件格式化的来源前缀。入站消息、回复成功后的 `mark_delivery`、`account.send` 回执三条路径都应带快照；宿主验证平台和账号 ID 一致，入站不合格则拒收，发送已成功但快照不合格则只记录错误、不回滚发送。快照保存在消息里，所以账号删除后历史仍能显示原来源，旧消息没有快照也不回填。[手册 §4 “经由账号快照”](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/docs/_handbook/channel-plugins.md#经由账号快照)、[`packages/sdk/python/shiori_sdk/accounts/models.py#L39-L109`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/packages/sdk/python/shiori_sdk/accounts/models.py#L39-L109)

**已提取：回复上下文。** QQ 当前在入站准入后才下载被引用消息的图片/文件并调用 `with_quote`；本回合正文会看到引用正文和附件，持久化消息仍只保存当前消息自己的正文/附件，引用内容放在 `reply_to_*` 元数据供手机端渲染。旁听消息不走引用下载。Telegram 和飞书也解析平台 reply/parent，但能力以平台插件实现为准。[`plugins/qq/backend/accounts_inbound_adapter.py#L121-L181`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/qq/backend/accounts_inbound_adapter.py#L121-L181)、[`packages/sdk/python/shiori_sdk/channels/reply_context.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/packages/sdk/python/shiori_sdk/channels/reply_context.py)

## 五、会话、线程与上下文隔离

**已提取。** 角色拥有一个共享运行会话 `role:<role_id>`，每个桌面/渠道聊天拥有独立线程：

```text
thread:<role_id>:desktop
thread:<role_id>:<channel>:<chat_id>
```

`RoleTurnRouter` 为网络聊天创建 `network_thread_id()`，将消息投影到角色共享 session，并写入 `role_id`、`thread_id`、`session_key_override=role:<role_id>`、`context_channel` / `context_chat_id`、`transport_*` 等元数据。外部渠道与桌面不会各自拥有独立 Agent session。[`packages/sdk/python/shiori_sdk/channels/threads.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/packages/sdk/python/shiori_sdk/channels/threads.py)、[`apps/backend/core/channels/role_routing.py#L70-L160`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/channels/role_routing.py#L70-L160)

**已提取：上下文分类。** 共享角色 session 的历史按线程实时分为：

- 用户上下文：桌面、已配对用户的已知私聊、计划任务和统一会话前的旧消息。
- 外部上下文：陌生私聊和全部群聊。

用户上下文回合可见所有当前用户上下文线程；外部回合只能看到自己的网络线程原文，其他外部线程不会泄露原文。已配对用户在群里的消息仍属于外部上下文；只有消息来源的 `sender_is_user` 标记表示“这一条消息确实由用户本人发出”。[`apps/backend/conversation/context_scope.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/conversation/context_scope.py)、[`tests/backend/conversation/test_context_scope.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/tests/backend/conversation/test_context_scope.py)

新配对私聊只从配对时间点起加入用户上下文；旧消息保持原记录但不自动变成用户上下文。解绑后按当前身份绑定计算，私聊重新回到外部上下文，但历史的 `sender_is_user` 快照仍表示当时发送者身份。[`apps/backend/core/identity/store.py#L137-L161`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/identity/store.py#L137-L161)、[`apps/backend/conversation/context_scope.py#L63-L85`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/conversation/context_scope.py#L63-L85)

**关键产品含义。** 网页桌面聊天只显示 `desktop` 线程；外部渠道消息不会自动混入桌面时间线。手机面板读取网络线程，因而同时能查看飞书/QQ/Telegram 的渠道聊天。[`apps/backend/conversation/context_scope.py#L133-L151`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/conversation/context_scope.py#L133-L151)、[`apps/backend/desktop_bridge/session_presenter.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/desktop_bridge/session_presenter.py)

## 六、身份配对、主人识别和删除语义

**已提取。** 桌面端生成一次性 8 位配对码，有效期 10 分钟，只存内存，重启失效；用户在任一角色账号的私聊中发送配对码。插件必须在路由前、仅私聊调用 `answer_pairing_code()`，成功后通过原渠道回复“已绑定”并丢弃该消息，不触发角色回合。Telegram/QQ 使用 `scope="platform"`；飞书/QQBot 使用 `scope="account"`，因为 Feishu `open_id` / QQBot `openid` 是应用内作用域。[`apps/backend/core/identity/pairing.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/identity/pairing.py)、[`packages/sdk/python/shiori_sdk/channels/pairing_command.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/packages/sdk/python/shiori_sdk/channels/pairing_command.py)、[`plugins/telegram/backend/channel/inbound.py#L196-L208`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/telegram/backend/channel/inbound.py#L196-L208)、[`plugins/feishu/backend/channel.py#L517-L536`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/feishu/backend/channel.py#L517-L536)

`user_identities.json` 持久化平台身份、配对作用域、所属账号（account scope）和已知私聊列表。每个身份可记录多个账号的私聊，但每个账号以最后登记的私聊为主动投递目标；用户后续在新的私聊中发言会刷新该账号的目标，同时保留每个聊天自己的 `context_since` 边界。删除账号会删掉该账号作用域的身份，并从平台作用域身份移除该账号私聊；重新添加同一账号需要重新配对。[`apps/backend/core/identity/models.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/identity/models.py)、[`apps/backend/core/identity/store.py#L137-L186`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/identity/store.py#L137-L186)

**推断。** Shiori 的“主人”不是渠道账号本身，而是可在多个渠道绑定的外部身份；主人识别和渠道账号归属是两个独立维度。这个分离正是群中区分“主人发言”和“角色账号发言”的基础。

## 七、外部工具权限与群聊控制

**已提取。** 外部上下文（陌生私聊、群聊）中，发送者不是已配对用户时，只能调用注册时声明 `external_allowed=True` 的工具；模型收到的工具 schema 和执行拦截两层都受限。已配对用户本人在群里触发的回合不受该外部白名单限制。限制不由平台插件自己决定，而由宿主当前回合的 `ContextView` 与 `MessageSource.sender_is_user` 判定。[`apps/backend/agent/tools/external_access.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/agent/tools/external_access.py)、[`apps/backend/bootstrap/toolsets/groups.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/bootstrap/toolsets/groups.py)、[`apps/backend/agent/tools/registry.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/agent/tools/registry.py)

当前注册为外部可用的群工具包括成员档案查询和 QQ `read_attachment`；群旁听开关、群笔记和群摘要读写不在外部白名单，因此陌生群友不能使用。已配对用户在群里的回合可以使用全部主人级工具，但 `set_group_listening` 仍限制在当前群；跨群 `lookup_group_context` / `update_group_context` 只校验角色拥有该群，不返回聊天原文。[`apps/backend/bootstrap/toolsets/groups.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/bootstrap/toolsets/groups.py)、[`apps/backend/agent/tools/group_listening.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/agent/tools/group_listening.py)、[`apps/backend/agent/tools/group_context.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/agent/tools/group_context.py)

`account_send` 在外部受限回合中通过 `ExternalArgumentLimit` 进一步收窄为只能 `target_kind="user"`，也就是只能发给已配对用户；陌生人不能借角色账号向任意群/私聊主动发消息。[`apps/backend/agent/tools/account_delivery.py#L32-L77`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/agent/tools/account_delivery.py#L32-L77)

**推断。** “群中识别为主人后允许主人级工具”在 Shiori 中并非简单把群线程变成用户上下文，而是保留群的外部历史隔离，仅在该回合通过可信 `sender_is_user` 放开工具。

## 八、出站、主动消息和一致性

**已提取：角色回复。** 渠道插件订阅 `OutboundMessage`，按平台发送，随后调用 `ChannelHub.mark_delivery()` 写入发送状态和平台消息 ID。群回复按平台约定点名触发者：QQ 在消息中 @触发者，Telegram 回复触发消息；模型额外选择的 `mention_ids` 仅在群回复带入，平台不支持的 ID 被跳过并记录 warning，回复照常发出。`ViaAccount` 只在成功投递时写入。[手册 §5](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/docs/_handbook/channel-plugins.md#5-出站与-message_push)、[`plugins/qq/backend/accounts_outbound_adapter.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/qq/backend/accounts_outbound_adapter.py)、[`plugins/telegram/backend/channel/outbound.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/telegram/backend/channel/outbound.py)

**已提取：`message_push`。** 插件通过 `ctx.push_tool.register_channel()` 提供主动文本、图片和文件发送；工具描述动态列出当前已注册渠道和 chat ID 格式。宿主要求角色绑定渠道目标，换代/停用时用 transport lock 和 retired transport 保证已接受的发送继续使用原连接，不把消息发到新账号。发送后通过 `ExternalPushSyncService` 把主动消息写入角色共享 session 的目标网络线程，并广播 `ProactiveMessageCommitted` / 手机更新事件；回合内发送的消息跟随回合提交，回合失败仍会持久化已经发出的主动消息。[`apps/backend/agent/tools/message_push.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/agent/tools/message_push.py)、[`apps/backend/conversation/push_sync.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/conversation/push_sync.py)、[`apps/backend/bootstrap/channel_host.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/bootstrap/channel_host.py)

**已提取：账号目标发送。** `account_list`/`account_targets`/`account_send` 是面向模型的显式目标 API；模型只选择渠道，不直接传账号 ID，宿主按“每角色每插件一个账号”解析当前账号，再调用插件 RPC。目标有 `private`、`group`、`group_member`；`user` 是模型专用目标，由宿主按身份绑定解析成用户私聊，插件不会收到 `user`。宿主只检查 payload 形状，平台不支持的群聊、话题、临时会话或提及由插件明确拒绝。发送尝试先写 `account_delivery_attempts` 为 `pending`，平台有真实回执才标记 `sent`；不确定的网络结果保持 pending，不误报失败。[`apps/backend/agent/tools/account_delivery.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/agent/tools/account_delivery.py)、[`packages/sdk/python/shiori_sdk/accounts/targets.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/packages/sdk/python/shiori_sdk/accounts/targets.py)、[`apps/backend/core/accounts/delivery_ledger.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/accounts/delivery_ledger.py)

**不确定性。** 是否应给 Meido 复刻 `account_send` 的跨目标能力，取决于产品是否允许角色/主人从手机聊天中主动联系第三方。若当前只要求“手机和网页聊天同步”，可先实现被动回复与用户私聊主动提醒，把第三方目标发送留在后续权限设计。

## 九、手机端同步实现

**已提取。** 手机端是网络聊天的独立读取模型，不是桌面时间线的别名：

1. `phone.conversations.list(role_id)` 列出角色当前未归档的网络线程，按最新消息倒序；每行包含 `thread_id`、当前承载账号、channel、群/私聊类型、联系人名称、缓存头像、是否为主人私聊、是否支持旁听和最后消息预览。
2. `phone.conversation.messages(role_id, thread_id, before_seq, limit)` 只允许读取该角色当前网络线程，返回分页消息，最旧优先，带 `has_more` / `next_before_seq` 游标。
3. 手机消息把角色消息标成 `role`，其它消息标成 `other`，并重新按当前绑定判断 `sender_is_user`；同时返回发送者 ID/名称/头像、结构化 @ 成员、引用块和媒体路径。账号删除后线程仍可读，但 `account_id` 变成 `null`。
4. 会话提交产生 `phone.conversation.updated`，payload 包含角色、线程、刷新后的列表行和本次新写入消息。前端监听事件后就地更新列表和当前聊天，不需轮询/整页 reload。旁听消息另发 `phone.listening.heard`。

来源：[`apps/backend/desktop_bridge/phone_requests.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/desktop_bridge/phone_requests.py)、[`apps/desktop/renderer/src/phone/phoneClient.ts`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/desktop/renderer/src/phone/phoneClient.ts)、[`apps/desktop/renderer/src/phone/usePhoneConversations.ts`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/desktop/renderer/src/phone/usePhoneConversations.ts)、[`apps/desktop/renderer/src/phone/usePhoneLiveUpdates.ts`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/desktop/renderer/src/phone/usePhoneLiveUpdates.ts)、[`apps/desktop/renderer/src/phone/PhoneConversationList.tsx`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/desktop/renderer/src/phone/PhoneConversationList.tsx)。

**推断。** 对 Meido 的“手机端聊天，网页端同步”，最重要的不是把外部消息复制进桌面聊天，而是建立权威的角色共享消息存储、按来源线程的手机读取模型和实时事件。网页端可查看全部渠道网络线程；桌面角色聊天仍可以只显示桌面线程。

## 十、运行时换代、去重和失败边界

**已提取。** `ChannelHost` 负责渠道启停、配置换代、入站暂停、候选连接启动、失败回滚和已移除 transport 的延迟退役。`configuration_key` 不变时可复用旧连接；新实例构造函数不能产生网络流量；`start`/`stop` 必须可重复调用。入站通过 `ChannelIntake` 有界缓存，连接切换期间不让消息静默丢失；发送使用 transport lock，已接受但尚未完成的旧代发送可以继续走 retired transport。[`apps/backend/bootstrap/channel_host.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/bootstrap/channel_host.py)、[`apps/backend/infra/channels/intake.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/infra/channels/intake.py)、[`tests/backend/bootstrap/test_channel_handover.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/tests/backend/bootstrap/test_channel_handover.py)

**已提取：入站调度。** AgentLoop 从单个进程内 `asyncio.Queue` 读取入站项，并在读取下一项前等待当前回合结束；队列没有持久化。慢回合会阻塞其他角色的入站处理，进程重启时尚未处理的队列项不会恢复。[`apps/backend/bus/queue.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/bus/queue.py)、[`apps/backend/agent/looping/core/processing.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/agent/looping/core/processing.py)

**已提取。** 去重有两层：平台插件按平台消息/事件 ID 做短期窗口去重，宿主 `ConversationService.has_external_message()` 再按网络线程和 `external_message_id` 做持久去重。两个层都保留是因为平台可能重投，且插件短期窗口重启后会丢失。[`apps/backend/core/channels/role_routing.py#L120-L141`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/channels/role_routing.py#L120-L141)、[`plugins/telegram/backend/channel/dedupe.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/telegram/backend/channel/dedupe.py)、[`plugins/feishu/backend/dedupe.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/plugins/feishu/backend/dedupe.py)

**推断。** 多端接入的可靠性不能只靠 WebSocket/轮询连接状态；必须把平台去重、宿主持久去重、账号在线栅栏、出站投递状态和换代排空作为一条完整链路。

## 十一、直接可借鉴与需要谨慎复刻的部分

### 适合直接借鉴

1. **插件/宿主分工**：平台 SDK 和消息格式放插件，准入、身份、角色路由、线程和工具权限放宿主。
2. **角色共享 session + 来源线程**：`role:<id>` 作为统一运行上下文，`thread:<role>:<channel>:<chat>` 作为历史、权限和手机展示边界。
3. **账号级响应规则**：私聊/群聊开关和黑名单作为账号配置；群聊统一由 `@/回复` 触发，旁听作为显式、按群的独立开关。
4. **一次性配对和身份作用域**：Telegram/QQ 使用平台作用域，飞书/官方 QQBot 使用账号作用域；配对消息不进入角色历史。
5. **工具双层限制**：外部回合只展示并执行 `external_allowed` 工具；敏感工具用参数限制收窄到 `user` 目标。
6. **来源快照、平台 ID 去重、出站回执**：避免账号改名/删除、平台重投和回复状态丢失导致历史不可解释。
7. **手机独立读取模型 + 实时事件**：列表/分页读取网络线程，消息提交后发增量事件，桌面线程与网络线程不混 UI。

### 不应无条件照搬

1. **“一个角色每插件只能一个账号”**：这是 Shiori 当前宿主和模型工具简化目标选择的结果，不是平台必然约束；Meido 若需要同一角色同时绑定多个 Telegram Bot 或多个 QQ 账号，账号目标、来源线程和主动投递都要重新设计。
2. **“主人在群里可用全部主人级工具”**：Shiori 依赖一次性配对和 `sender_is_user` 可信标记；若 Meido 的工具包含高风险外部副作用，应额外按群/工具设置 allowlist 或二次确认。
3. **“配对即把私聊纳入用户上下文”**：Shiori 通过 `context_since` 只共享配对后的私聊，历史边界复杂但安全；直接把整个私聊历史提升为主人上下文会泄露陌生人旧内容或改变记忆分类。
4. **群旁听**：Shiori 只有 NapCat QQ 声明 `group_listening`；Telegram Bot 隐私模式、官方 QQBot 和飞书当前代码都不提供同样的全量旁听。Meido 不能只做一个平台无关开关，应让插件声明能力并明确平台限制。
5. **主动消息“最近活跃渠道”推断**：Shiori 的可靠目标是已配对身份记录的私聊；不要仅按最后一条消息的渠道向未绑定/不确定目标发送。
6. **将第三方目标发送和用户聊天混为一体**：`account_send` 是完整的目标目录、群成员、话题、媒体和不确定投递状态系统。若 Meido 当前只要多端聊天同步，可以先实现来源回复和主人私聊投递，再单独评审第三方主动发送。
7. **Shiori 当前平台矩阵**：飞书和官方 QQBot 在基线中仅私聊；如果产品验收要求飞书/QQ 群聊，必须重新验证平台事件、权限、@/回复和历史隔离，不能宣称“复刻即支持”。

## 十二、调研阶段的待决策记录

以下是调研阶段提出的问题，不是当前开放决策。用户随后明确要求整体复刻 Shiori，QQ 同时接 NapCat 和官方 QQBot；当前行为见[多端会话规格](../specs/multichannel-conversations-spec.md)，逐项源码与测试对照见[复刻核验矩阵](shiori-multichannel-completion-audit.md)。

- 是否坚持“一角色一渠道账号”，还是允许一角色多个同平台账号；若允许，网页/手机列表是否按账号实例分组。
- 外部渠道消息是否只在手机端显示，还是网页端也新增“网络会话”视图；桌面主聊天是否永远只显示 desktop 线程。
- 飞书和官方 QQBot 是否第一期只做私聊；QQ 是否明确采用 NapCat/OneBot，而不是官方 QQBot。
- 群里配对主人可用哪些主人级工具；哪些工具即使主人触发也必须二次确认或按群禁用。
- 是否第一期支持旁听；若支持，按插件能力声明，且旁听消息是否计入记忆、每日上限如何设置。
- 是否第一期实现 `account_targets` / `account_send` 的第三方目标发送，还是只实现原消息来源回复和已绑定主人私聊。
- 主动消息如何选择目标：只发给显式绑定用户，还是允许按角色配置固定群/私聊目标；失败/不确定回执是否需要重试队列。
- 是否复刻 Shiori 的 `ViaAccount`、引用块、结构化 @ 和头像缓存；这些会直接决定消息 schema 和手机 UI。

## 来源索引

- 渠道插件手册：[channel-plugins.md](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/docs/_handbook/channel-plugins.md)
- 渠道领域说明：[channels.md](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/docs/knowledge/modules/channels.md)
- 会话领域说明：[conversations-and-sessions.md](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/docs/knowledge/modules/conversations-and-sessions.md)
- 宿主路由：[`ChannelHub`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/channels/hub.py)、[`RoleTurnRouter`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/channels/role_routing.py)
- 身份：[`core/identity`](https://github.com/YinFengWindy/Shiori-Agent/tree/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/identity)
- 手机：[`phone_requests.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/desktop_bridge/phone_requests.py)、[`phoneClient.ts`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/desktop/renderer/src/phone/phoneClient.ts)
- 端到端测试：[`tests/backend/core/channels/test_hub.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/tests/backend/core/channels/test_hub.py)、[`tests/backend/conversation/test_context_scope.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/tests/backend/conversation/test_context_scope.py)、[`tests/backend/desktop_bridge/test_phone_requests.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/tests/backend/desktop_bridge/test_phone_requests.py)
