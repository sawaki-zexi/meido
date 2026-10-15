# Shiori 多端会话复刻核验矩阵

状态：当前核验记录；最终全量检查由交付 PR 记录。

最后核验：2026-10-15。

事实来源：Shiori-Agent 基线 `1c241a2627bbcc99124aef71abaee237ac057499` 的 [渠道插件手册](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/docs/_handbook/channel-plugins.md)、[身份存储](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/core/identity/store.py)、[旁听存储](https://github.com/YinFengWindy/Shiori-Agent/blob/1c241a2627bbcc99124aef71abaee237ac057499/apps/backend/conversation/listening_store.py)，以及本表列出的 Meido 源码与行为测试。

## 核验范围

用户已明确选择整体复刻 Shiori 的账号、全局主人身份、群聊触发、工具权限、上下文隔离、目标目录、主动投递和手机/网页同步；QQ 同时接 NapCat 和官方 QQBot。本表不把此前草案写成“后续”的行为自动排除在交付范围外。配置真实平台账号后的权限、事件订阅和网络可达性是部署验证，模拟平台契约测试不能证明真实账号已经连通。

## 行为与证据

| 要求 | 当前实现与核验边界 | 直接证据 |
| --- | --- | --- |
| 四类账号与平台基线 | Telegram 私聊/群聊；NapCat 私聊/群聊；QQBot C2C、飞书私聊。受平台限制的目标明确拒绝 | [`channels/`](../../backend/app/channels/)、[`test_adapters.py`](../../tests/python/channels/test_adapters.py)、[`test_channel_api.py`](../../tests/python/test_channel_api.py) |
| 账号唯一归属 | 一角色每适配器一个账号；同平台身份唯一归属；已验证身份不能原地替换 | [`multichannel.py`](../../backend/app/multichannel.py)、[`test_channel_identity_parity.py`](../../tests/python/test_channel_identity_parity.py) |
| 配对码完整语义 | 全应用一个进程内待用码，新码替换旧码，10 分钟过期、重启失效；任意角色/渠道私聊可消费 | 同上；测试覆盖跨角色跨平台、重复使用、无效接收账号及重启 |
| 配对控制流 | 命令与确认不写会话 transcript，不启动模型；成功回执可单独入投递账本 | [`main.py`](../../backend/app/main.py)、[`test_multichannel.py`](../../tests/python/test_multichannel.py)、[`test_telegram_runtime.py`](../../tests/python/channels/test_telegram_runtime.py) |
| 身份作用域与解绑 | QQ/Telegram 平台全局、飞书/QQBot 应用范围；解绑撤销聊天授权、主人目标及提醒订阅，历史保留 | [`IdentityBindings.tsx`](../../web/src/features/channels/IdentityBindings.tsx)、[`test_channel_identity_parity.py`](../../tests/python/test_channel_identity_parity.py)、[`test_push.py`](../../tests/python/channels/test_push.py) |
| 角色/线程原文隔离 | 桌面与渠道独立 transcript；主人私聊从授权起点共享主人片段；访客和群只读当前线程 | [`multichannel.py`](../../backend/app/multichannel.py)、[`test_owner_memory_context.py`](../../tests/python/channels/test_owner_memory_context.py)、[`test_host.py`](../../tests/python/channels/test_host.py) |
| 主人群权限 | 群内主人可用已启用主人工具，初始 raw 历史仍限当前群；非主人仅 `external_allowed` | [`main.py`](../../backend/app/main.py)、[`agent_runtime/`](../../backend/app/agent_runtime/)、[`test_runtime_recovery.py`](../../tests/python/channels/test_runtime_recovery.py) |
| 当前触发消息边界 | 回合初始历史截止当前来源消息，排队的其他发送者不继承其权限；可信渠道/发送者/身份来源投影进模型输入而不改原正文 | [`session_store.py`](../../backend/app/session_store.py)、[`test_turn_history_boundary.py`](../../tests/python/channels/test_turn_history_boundary.py)、[`test_runtime_recovery.py`](../../tests/python/channels/test_runtime_recovery.py)、[`test_telegram_runtime.py`](../../tests/python/channels/test_telegram_runtime.py) |
| 目标参数双层限制 | 外部发送 schema 收窄到主人目标并隐藏账号/群提及/话题；执行时再次限制实际来源账号与配对目标 | [`tools.py`](../../backend/app/channels/tools.py)、[`push.py`](../../backend/app/channels/push.py)、[`test_tools.py`](../../tests/python/channels/test_tools.py)、[`test_push.py`](../../tests/python/channels/test_push.py) |
| 群触发与控制命令 | @ 或原生回复触发；`/stop` 只中断当前来源会话，`/chatid` 等控制命令不进入模型历史 | [`commands.py`](../../backend/app/channels/commands.py)、[`test_channel_commands.py`](../../tests/python/test_channel_commands.py)、[`test_telegram_runtime.py`](../../tests/python/channels/test_telegram_runtime.py) |
| 旁听配额与存储 | 独立旁听存储；全局默认每日 200 条、单群覆盖；超额内存最近 30 条，重启丢弃溢出窗口 | [`listening.py`](../../backend/app/channels/listening.py)、[`test_listening_parity.py`](../../tests/python/channels/test_listening_parity.py)、[`GroupListeningPanel.tsx`](../../web/src/features/channels/GroupListeningPanel.tsx) |
| 旁听整理 | 独立游标，每批 50 条，跨天余量每 30 分钟扫描；同群版本校验后才提交 | [`consolidation.py`](../../backend/app/channels/consolidation.py)、[`test_consolidation_parity.py`](../../tests/python/channels/test_consolidation_parity.py) |
| 记忆分类 | 既有 MemoryEngine 记录主人私有；主人事实与线程外部档案分开；群友/访客不写主人记忆 | 同上及 [`group_tools.py`](../../backend/app/channels/group_tools.py)、[`test_owner_memory_context.py`](../../tests/python/channels/test_owner_memory_context.py) |
| 主人多渠道 Markdown | 维护授权桌面/主人私聊共同视图，专用稳定顺序不改消息原 session/sequence；读取前刷新当前授权 raw 近期视图 | [`memory_maintenance.py`](../../backend/app/memory_maintenance.py)、[`test_owner_memory_context.py`](../../tests/python/channels/test_owner_memory_context.py)、[`test_memory.py`](../../tests/python/test_memory.py) |
| 成员档案与群笔记 | 模型只能更新当前输入中实际发送者；查询/自动注入限当前群的相关成员和笔记/摘要 | [`group_tools.py`](../../backend/app/channels/group_tools.py)、[`test_group_tools.py`](../../tests/python/channels/test_group_tools.py)、[`test_consolidation_parity.py`](../../tests/python/channels/test_consolidation_parity.py) |
| 私聊流式 | Telegram 编辑消息、QQBot C2C 流、飞书 CardKit；失败根据已发送情况回退或标不确定 | [`telegram.py`](../../backend/app/channels/telegram.py)、[`qqbot.py`](../../backend/app/channels/qqbot.py)、[`feishu.py`](../../backend/app/channels/feishu.py)、[`test_telegram_runtime.py`](../../tests/python/channels/test_telegram_runtime.py)、[`test_feishu_media_stream.py`](../../tests/python/channels/test_feishu_media_stream.py) |
| 引用和附件 | 当前回合看引用正文/媒体，保存原发言正文；旁听不下载附件；附件读取/发送按角色和当前线程授权 | [`test_media_ingress.py`](../../tests/python/channels/test_media_ingress.py)、[`test_attachment_reader.py`](../../tests/python/channels/test_attachment_reader.py)、[`test_target_options.py`](../../tests/python/channels/test_target_options.py) |
| 原生提及与话题 | NapCat 群回复 @ 触发者；目录成员提及校验；Telegram 主题 ID 验证；不支持的平台拒绝 | [`host.py`](../../backend/app/channels/host.py)、[`test_target_options.py`](../../tests/python/channels/test_target_options.py) |
| 头像和来源 | 七天刷新，后台获取、128px PNG、失败保留旧头像；应用账号隔离；来源快照删除账号后可读 | [`avatars.py`](../../backend/app/channels/avatars.py)、[`test_avatars.py`](../../tests/python/channels/test_avatars.py)、[`ChannelView.tsx`](../../web/src/features/channels/ChannelView.tsx) |
| `message_push` 与待办 | 主人绑定目标发送、当前线程同步；待办显式选择授权私聊、发送前重验、每账号每事件一次 | [`push.py`](../../backend/app/channels/push.py)、[`test_push.py`](../../tests/python/channels/test_push.py)、[`PushTargets.tsx`](../../web/src/features/channels/PushTargets.tsx) |
| 可达目录与投递状态 | 目录来源为已观察/发现联系人、群、成员；群成员私发写私聊线程；真实回执、确定失败、不确定分开 | [`host.py`](../../backend/app/channels/host.py)、[`test_host.py`](../../tests/python/channels/test_host.py)、[`test_transports.py`](../../tests/python/channels/test_transports.py) |
| 网页同步 | 来源线程列表、最近消息、稳定分页、引用/媒体、头像、配对解绑、旁听分页与推送配置；EventSource 更新 | [`ChannelView.test.tsx`](../../web/src/features/channels/ChannelView.test.tsx)、[`IdentityBindings.test.tsx`](../../web/src/features/channels/IdentityBindings.test.tsx)、[`GroupListeningPanel.tsx`](../../web/src/features/channels/GroupListeningPanel.tsx)、[`PushTargets.test.tsx`](../../web/src/features/channels/PushTargets.test.tsx) |
| 启停/删除生命周期 | 账号切换排空已接受事件和发送，失败回滚；角色删除排空后保存内存快照，后期故障恢复历史/凭据/旁听/档案/推送/头像与连接；删除期间未启动回合明确拒收，避免锁排空死锁；启动恢复清理渠道/凭据/角色资料理解/推送，失败保留删除标记并禁止孤儿账号连接；遗留投递标不确定 | [`main.py`](../../backend/app/main.py)、[`role_snapshot.py`](../../backend/app/channels/role_snapshot.py)、[`test_role_deletion_recovery.py`](../../tests/python/channels/test_role_deletion_recovery.py)、[`test_channel_api.py`](../../tests/python/test_channel_api.py)、[`test_consolidation_parity.py`](../../tests/python/channels/test_consolidation_parity.py) |
| 配置换代入站缓冲 | 每账号最多 64 条进程内缓冲；恢复通过当前连接重新准入，禁止继承旧来源租约；溢出/禁用/删除回复原来源重发提示 | [`intake.py`](../../backend/app/channels/intake.py)、[`test_intake.py`](../../tests/python/channels/test_intake.py) |

## 部署差异与验证边界

1. 部署差异：Shiori 的 QQ 插件提供 NapCat 托管安装、扫码和登录管理；本轮 Meido 以已运行 OneBot/NapCat WebSocket 为接入前置，不内嵌托管 QQ 客户端。账号收发、响应规则、群旁听、权限与生命周期的实现不能被写成已经包含扫码部署功能。
2. 配对、切换、引用媒体、授权记忆及投递已经过完整串行回归；自动测试使用传输契约模拟，不能证明真实账号权限和网络可达性。

## 本轮定向结果

- 身份、全局配对、既有渠道与 Telegram Runtime：36 通过。
- 头像与身份：17 通过；头像格式/配额/缓存隔离单独覆盖。
- 既有记忆与主人多渠道投影：72 通过；后续增加的读前解绑刷新用例所在文件 6 通过。
- 网页渠道与身份绑定：12 通过。
- 角色删除故障恢复、启动清理、孤儿账号门禁与既有删除 API 回归：29 通过（37 项未选）；包含部分凭据清理失败、其他角色新消息保留与真实来源排空无死锁。
- TypeScript、Ruff 和差异空白检查通过；后续并行修改需由最终交付检查再次确认。

## 完整回归

同步最新 `main` 的邮件和 CI 改动后，`python -m pytest -q tests/python` 为 783 通过、2 跳过。额外覆盖同批消息的回合来源边界、迟到回复排序、配对前来源的迟到回复、按回合维护主人 Markdown、失败/不确定投递不入完整历史、进程内换代缓冲和启动删除恢复。

`npm run test:web` 为 12 个文件、79 项通过；包括普通/旁听记忆来源、近期列表外旧线程、角色归属及刷新竞态。TypeScript 与生产构建、后端与改动测试 Ruff、Python 编译和差异空白检查通过。规范与需求分别审查，已修复阻塞项；凭据名映射的集中定义保留为维护建议。
