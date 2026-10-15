# Meido 当前架构与产品边界

状态：当前

最后核验：2026-10-15

事实来源：

- [`CONTEXT.md`](../../CONTEXT.md)
- [`backend/app/models.py`](../../backend/app/models.py)
- [`backend/app/session_store.py`](../../backend/app/session_store.py)
- [`backend/app/agent_runtime/`](../../backend/app/agent_runtime/)
- [`backend/app/multichannel.py`](../../backend/app/multichannel.py)
- [`backend/app/channels/`](../../backend/app/channels/)
- [`tests/python/test_multichannel.py`](../../tests/python/test_multichannel.py)
- [`tests/python/test_channel_api.py`](../../tests/python/test_channel_api.py)
- [`tests/python/channels/`](../../tests/python/channels/)
- [`docs/specs/multichannel-conversations-spec.md`](multichannel-conversations-spec.md)
- [`docs/handbook/multichannel-setup.md`](../handbook/multichannel-setup.md)
- [`docs/adr/0009-multichannel-conversation-threads.md`](../adr/0009-multichannel-conversation-threads.md)
- [`docs/specs/agent-runtime-tau-integration-spec.md`](agent-runtime-tau-integration-spec.md)
- [`docs/specs/memory-system-spec.md`](memory-system-spec.md)
- [`docs/specs/agent-capability-extension-spec.md`](agent-capability-extension-spec.md)
- [`docs/specs/mail-integration-spec.md`](mail-integration-spec.md)
- [`docs/adr/0008-qq-mail-draft-boundary.md`](../adr/0008-qq-mail-draft-boundary.md)

本文记录当前仍稳定的产品行为和架构边界。实现细节以源码和测试为准；需要重新取舍的未来能力应另建规格或 ADR。

## 1. 产品形态

Meido 是面向单一主人的本地网页应用。浏览器网页连接本地 FastAPI 后端，后端管理角色、桌面唯一会话、渠道线程、消息、模型连接、记忆和应用级插件。手机渠道与网页同步同一应用的会话，当前不提供多用户账户、云端共享或跨实例同步。

网页当前提供：

- 角色酒馆、角色创建、编辑、头像/角色卡和删除；
- 角色唯一会话、历史消息和流式回复；
- 模型连接设置与角色模型绑定；
- 记忆总开关、共享向量模型开关与配置，以及角色记忆查看、搜索和维护；
- 主人资料插件设置、飞书只读同步和角色理解；
- 待办管理、提醒角色和角色主动提醒；
- QQ 邮箱连接、同步、草稿审核状态、角色邮件工具和批次提醒；邮件最终由主人在 QQ 邮箱审核发送。
- Telegram、QQ NapCat、官方 QQBot 和飞书渠道账号管理，以及渠道线程查看、分页历史和实时更新。

项目介绍落地页是独立的静态体验，不调用本地后端，也不等同于实际聊天应用。

## 2. 角色与会话

- 角色拥有稳定且不可变的内部 ID；名称可以修改，也可以重复。
- 一个角色对应一个桌面持续会话；外部渠道私聊和群聊各自对应独立渠道线程。
- 角色设定包括简介、身份背景、性格、行为规则、回复约束和称呼。
- 角色删除会清理其桌面会话、渠道账号和线程、消息、记忆、索引和未完成的角色级维护工作；活动运行期间删除会被拒绝。
- 消息按稳定顺序持久化。用户消息、完整助手消息、结构化工具调用和工具结果具有明确类型；主动提醒不会被当作新的用户回合。

## 3. 模型与运行时

模型连接由本地后端维护。网页提交的表单可以先测试再显式保存；角色保存连接 ID，不直接持有密钥。模型配置和角色绑定在每次运行开始时快照化，运行期间的配置变更只影响后续运行。

每次用户消息创建一个 Agent Runtime 运行。Runtime 负责：

- 角色级并发锁、取消、超时和终态；
- provider-neutral 的文本/工具循环；
- 能力快照、工具策略和工具审计；
- SQLite transcript 持久化和兼容的 SSE 事件。

SQLite 是会话和运行状态的权威来源。服务启动时未完成的运行收束为失败；任何可恢复的消息结束必须在下一次 provider 请求前持久化。Agent Runtime 不负责长期任务、远程执行器或外部事务调度。

渠道宿主负责外部账号的准入、事件去重、线程路由、目标目录和投递状态；平台适配器负责连接、事件解析、媒体和平台收发。渠道回合复用角色级 Runtime，桌面回合可读取已配对主人私聊的授权片段，角色已配置渠道账号时才注入渠道工具；实际发送要求可用账号。访客回合只读取当前线程，群聊原文仍限当前群。

## 4. 记忆与应用插件

主人记忆按角色隔离，由 `MemoryEngine` 提供检索、写入、维护和管理接口，既有记录保持主人私有分类。结构化 SQLite 记录是权威来源，Markdown 文档是可读视图；桌面及已配对主人渠道回合维护主人事实，桌面与已授权主人私聊共同维护近期/历史视图。访客、群与旁听使用线程范围外部档案和独立整理游标，群友资料不进入主人 MemoryEngine。已配对主人私聊可注入角色记忆，访客和群回合只自动注入当前外部档案；主人群回合仍可主动使用记忆检索等已启用的主人工具。

当前应用级能力包括：

- **主人资料**：从飞书知识库只读同步普通文档，建立本地索引，并为每个角色生成独立的角色理解；不修改飞书原文，不写入角色记忆。
- **待办**：通过网页或角色工具共用应用服务管理主人待办；提醒以指定角色的主动消息写入会话，不触发普通回合或记忆维护。
- **能力扩展**：Tool、Skill 和 Plugin 在运行开始时解析为冻结的 `CapabilitySnapshot`。角色范围工具由后端注入作用域，插件不能直接访问核心存储。
- **渠道会话**：角色可绑定 Telegram、NapCat QQ、官方 QQBot 和飞书账号，同一平台账号唯一归属角色；主人身份按平台/应用作用域全局识别，可解绑。宿主校验线程、工具与目录目标，支持 `account_send`、`message_push` 和显式主人待办提醒投递。NapCat 旁听使用独立存储、每日配额、最近窗口、清理和后台整理；网页显示最近消息、来源/账号移除标记、头像、引用/提及和投递回执。陌生人回合只允许显式 `external_allowed` 的工具。

## 5. 当前边界与后续能力

当前不承诺以下能力：

- 多用户、云端同步和跨实例记忆复制；
- 教务系统等尚未接入的外部事务；QQ 邮件已按邮件集成规格接入，但仍不代表主人发送、删除、归档或移动外部内容；
- 飞书和官方 QQBot 群聊、Telegram 群旁听，以及网页直接向渠道发送消息；
- 长期任务、远程 Executor、Subagent、审批工作流和动态插件市场；
- 将 Skill 当作可执行代码，或把 Plugin 当作操作系统 sandbox；
- 已配置渠道消息投递之外的外部写操作，包括发送、删除、归档或移动邮件。

邮件能力已按 [`邮件集成规格`](mail-integration-spec.md) 落地，覆盖 #92–#98。QQ 连接、UID checkpoint、线程与任务幂等、草稿服务端核验、人工修改保护、网页入口、角色工具和批次提醒均属于当前实现；#95 的重新生成策略为创建新草稿并保留原草稿。附件、发送、删除、归档和移动仍明确不在范围内。

后续建议仍不得直接扩展普通 Agent Loop 的职责；需要长期运行、外部副作用或强隔离时，应新增对应的 TaskRuntime、Executor 或 ADR。多渠道规格中的验收标准用于检查完成程度，“后续建议”不应视为已经实现的产品承诺。四个平台的现有自动化验证以模拟传输和接口契约为主，实际账号权限与部署仍需平台实测。

## 6. 验证基线

提交前至少运行：

```text
python -m pytest
npm run test:web
npm run build
git diff --check
```

涉及 Runtime 或能力扩展时，还应运行仓库约定的类型检查、Ruff 和 compileall。
