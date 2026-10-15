# 渠道连接事实与分类目录

状态：当前。最后核验：2026-10-16。对应 #170、完整规格 C06。

事实来源：[查询与分页](../../backend/app/channels/directory.py)、[宿主管理](../../backend/app/channels/host.py)、[模型工具](../../backend/app/channels/tools.py)、[实际传输](../../backend/app/channels/transports.py)、[Telegram API 与目录存储](../../backend/app/multichannel.py)、[公开 HTTP/模型往返](../../tests/python/test_channel_directory_api.py)、[本地协议边界](../../tests/python/channels/test_directory_transport.py)。

## 连接事实

`account_list` 保留 `accountId/channel/displayName/enabled/platformEnabled/effectiveEnabled/capabilities`，新增 `connection/online/observedAt/reason/supportedKinds`。网页账号接口保留 `connectionStatus`，并使用相同的连接事实。

`enabled` 是账号开关，`platformEnabled` 是平台开关；两者不代表在线。`online` 为布尔值或 `null`，`observedAt` 是该证据的 UTC 时间或 `null`，`reason` 解释证据。未提供观测接口的 transport 返回 `unknown/null`；已注册 handler 或尚在运行的任务不能证明在线。

| 平台 | 可证明在线的当前连接观测 |
| --- | --- |
| QQ / NapCat | 当前 WebSocket 上成功 `get_status` 返回严格布尔 `online`。WebSocket 连接成功本身仅证明 `connection=connected`，登录状态仍可为 `null`。重连与停止清除旧登录观测。 |
| Telegram | 当前账号代际的一次成功 polling；失败或停止后为 `false`，尚无成功观测时为 `null`。 |
| QQBot | 当前网关的 `READY/RESUMED`，握手本身不证明在线。 |
| 飞书 | 实际长连接成功；只启用 webhook、尚无长连接证据时为 `null`。 |

账号或平台关闭显示 `disconnected/false`；连接配置切换中显示 `connecting/null`。初始化失败且没有 adapter 时显示 `error/null`，不会回显底层错误中的敏感数据。

## 模型查询

`account_targets` 保留必填 `account_id`；可选 `kind/group_id/member_id/limit/cursor`。省略 `kind` 使用 `known`，`limit` 默认 25、范围 1–50。`members/member` 必须指定 `group_id`，只有 `member` 指定 `member_id`。账号参数不改为平台名称。

| 平台 | `supportedKinds` | 平台读取 |
| --- | --- | --- |
| QQ / NapCat | `known/friends/groups/members` | `friends/groups/members` 分别只调用一次 `get_friend_list/get_group_list/get_group_member_list`。`members` 必须是已知群且账号允许群聊。 |
| Telegram | `known/member` | `member` 只对已知且获准的群调用一次 `getChatMember`；不提供全群成员目录。 |
| QQBot、飞书 | `known` | 只读本地已观察目录，其他种类明确 `unsupported`。 |

`known` 是当前角色、当前账号的持久观察缓存，不声称覆盖平台全部联系人。平台查询成功后只持久化真实可达 `Target`。Telegram 单成员结果是资料：`kind=member`，包含 `memberId/groupId/displayName/username/status/isMember`，并固定 `canSend=false/sendTargetId=null`；它不创建私聊或 `GROUP_MEMBER` 发送目标。既有可发送目标仍用不透明 `id` 交给 `account_send`，发送的授权与交付账本没有改变。

成功结果包含 `targets/nextCursor/coverage/complete/observedAt/total`。`coverage=known` 表示观察缓存，`platform` 表示本次指定平台查询；`complete=true` 表示完整取得了这个有界快照，不代表 `known` 已覆盖整个平台。空目录返回 `targets=[]/total=0`；不是失败的替代值。

下一页重复原查询参数与 `limit`，附带 `nextCursor`。快照顺序、总数与观测时间固定；重复同一 cursor 返回同一页，新观察不会插入正在翻页的快照。cursor 绑定角色、账号创建与配置代际、平台开关代际、adapter 实例和真实来源范围；同来源后续主人回合可继续，换来源、撤权后恢复、删号重建或替换连接均不能复活旧 cursor。

## 硬边界与失败

每次平台查询最多一个 RPC、10 秒；完整观测最多 5,000 条、1 MiB UTF-8 JSON。NapCat 在真实 WebSocket 接收与 JSON 解析之前限制 1 MiB，Telegram 新查询在接收 raw body 时限制 1 MiB，并在解码之前拒绝非 identity 压缩响应。本地 `known` 先在 SQLite 内核算存储字段字节预算，再读取和解析记录。超限返回 `directory_too_large/complete=false`，不会截断后声称完整。

快照 60 秒失效，同时最多 8 个、每角色最多 2 个；平台查询同时最多 4 个、每角色 2 个、同账号 1 个。无容量时在请求平台前返回 `directory_busy`。取消或超时会取消并等待本次 RPC 退出；查询跨等待后、目录写入事务提交前与返回前都复核真实来源、角色、账号、配置、adapter 和取消状态，失效数据不得写入目录。

失败返回安全的 `error`、`complete=false` 和该角色账号的 `supportedKinds`，不包含 token、平台 URL、原始响应正文或异常详情。常见原因：

| 原因 | 含义 |
| --- | --- |
| `unsupported` | 此 adapter 不支持该种类。 |
| `group_not_authorized` | 群未被观察或账号未允许群聊。 |
| `permission_denied` | 平台 HTTP 401/403 的明确权限拒绝。 |
| `platform_rejected` | 平台明确拒绝；QQ retcode 不猜测为权限不足或空目录。 |
| `disconnected/platform_error/timeout` | 没有连接、连接/协议失败或查询超时。 |
| `permission_revoked/account_not_found` | 当前来源或账号已失效。 |
| `invalid_or_expired_cursor` | cursor 过期、查询不匹配或所属代际改变。 |
| `directory_busy/directory_too_large` | 已达资源或完整响应预算。 |
| `invalid_query/invalid_query_or_response/invalid_platform_response/unsupported_response_encoding` | 查询或平台响应不能按此契约安全处理。 |

## 兼容与入站观察

旧内部 `ChannelHost.account_targets()` 和网页列表仍返回列表；模型专用分页接口独立存在。入站只保存该条消息的会话与发送者观察，不发起联系人/群/全群成员查询，也不把 adapter 缓存中的其他成员重新导入。显式目录查询后保存的昵称继续供 [历史成员查询](member-lookup.md) 使用；离线历史查询的范围和可信来源规则不变。
