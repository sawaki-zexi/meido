---
title: Shiori 多端配置的信息组织与配对流程
kind: 源码研究
status: 当前
last_verified: 2026-10-15
baseline_commit: 4f266aeaf42fcd611362e2456417b91f659341cf
---

# Shiori 多端配置的信息组织与配对流程

状态：当前。最后核验：2026-10-15。事实来源：Shiori 当前 `main` 的 `4f266aeaf42fcd611362e2456417b91f659341cf`；GitHub API 确认其提交时间为上海时间 15:39:09。已比较旧基线 `1c241a2` 至当前提交，本文引用的账号、身份、四渠道插件和路由文件均未改变，读取本地同源副本核验。本文补充[已有实现研究](shiori-multichannel-integration.md)，不改变其结论。

## 看起来简单，是因为拆开了入口

**已提取。** 当前源码中的“我的身份”和“插件”是独立设置入口，没有把全部多端配置压成一个总开关加配对码的页面。插件页按功能、渠道、系统分组，每个渠道插件分别启停；账号凭据放在角色详情的“账号”页，每个平台一行，点开详情才配置。官方 README 同样明确这两个步骤。[设置注册](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/renderer/src/settings/registerBuiltinSettingsSections.tsx)、[插件列表](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/renderer/src/plugins/PluginManagementSection.tsx)、[角色账号页](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/renderer/src/accounts/RoleAccountsPanel.tsx)、[README](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/README.md)。

**已提取。** 账号详情仍有凭据：Telegram 填 Bot Token，QQBot 填 App ID/Secret，飞书另选区域。连接/断开在详情操作；账号在线后才显示响应规则，支持群聊才出现群开关。宿主限制一个角色每插件最多一个账号。[账号详情](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/renderer/src/accounts/AccountDetailDialog.tsx)、[Telegram](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/telegram/ui/TelegramAccountDetail.tsx)、[QQBot](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/qqbot/ui/QQBotAccountDetail.tsx)、[飞书](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/feishu/ui/FeishuAccountDetail.tsx)、[归属约束](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/core/accounts/registry.py#L244-L289)。

**已提取。** QQ 的简化还来自托管 NapCat：Windows x64 下新增账号自动准备并展示二维码，插件自己分配本机端口、生成令牌、写 OneBot 配置。用户因此不用填 WS 地址和 Access Token。这需要后端托管能力，不能只删输入框。[扫码表单](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/qq/ui/QQAccountForm.tsx)、[端口与令牌生成](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/qq/backend/napcat_account_files.py)。

## 配对码回答“谁是主人”

**已提取。** “我的身份”显示配对码与已绑定身份，支持解绑。配对码是全工作区唯一待用的 8 位一次性码，10 分钟有效，新码替换旧码，重启失效。主人从自己的平台账号私聊任意已连接的角色账号，发送代码；成功回复“已绑定”，该消息不触发模型。账号预先归属角色，配对不会选择角色或创建机器人。[身份界面](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/renderer/src/identities/IdentitySettingsSection.tsx)、[码生命周期](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/core/identity/pairing.py)、[消费确认](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/packages/sdk/python/shiori_sdk/channels/pairing_command.py)。

**已提取。** QQ/Telegram 按平台身份识别，飞书/QQBot 按应用账号作用域识别。绑定保存到 `user_identities.json`，还记录私聊投递目标。未配对用户在私聊启用且未被屏蔽时也可聊天，但属于隔离的外部上下文；配对主人在群里的发言仍属于群线程。关闭私聊回复不阻止配对，黑名单则阻止配对。[身份模型](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/core/identity/models.py)、[存储](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/core/identity/store.py)、[准入与配对](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/core/channels/hub.py#L87-L202)、[上下文分类](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/conversation/context_scope.py)。

**已提取。** 群聊开关是账号级，全部群共用；开启后仍需 @ 或回复本账号才触发回合，且依赖插件上报字段。未点名消息只在该群开启旁听时记入旁听，不触发回复。QQ 声明旁听；飞书和 QQBot 当前仅私聊，平台限制见已有研究。[规则界面](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/renderer/src/accounts/AccountResponseRulesEditor.tsx)、[群准入](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/core/channels/hub.py#L127-L202)。

## 对 Meido 的含义

**已提取。** Meido 当前主线 `bdb4ea1d472e4205aad6e89a60f66f4266efb7ab` 把日常会话、账号凭据、规则、黑名单、全局配对码、身份绑定和推送目标同时放在一个视图。全局语义的配对码嵌在角色账号侧栏，并受当前账号启用状态限制。[ChannelView](https://github.com/sawaki-zexi/meido/blob/bdb4ea1d472e4205aad6e89a60f66f4266efb7ab/web/src/features/channels/ChannelView.tsx#L581-L653)。

**推断。** 可借鉴的是把“账号接入”“主人身份”“日常消息”分开，让高级规则按需出现。QQ 扫码属于额外托管能力。没有截图，无法判定用户看到的 Shiori 版本和具体入口；以上为当前源码结论与设计建议，尚非实施规格。
