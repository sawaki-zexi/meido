# ADR-0009：角色会话承载多端独立线程

状态：已接受

最后核验：2026-10-15

事实来源：

- 多端会话规格 [Issue #116](https://github.com/sawaki-zexi/meido/issues/116) 和[多端会话规格](../specs/multichannel-conversations-spec.md)
- [Shiori-Agent 多端接入实现](../research/shiori-multichannel-integration.md)，基线 `1c241a2627bbcc99124aef71abaee237ac057499`
- [`backend/app/multichannel.py`](../../backend/app/multichannel.py)、[`backend/app/channels/`](../../backend/app/channels/)、[`backend/app/main.py`](../../backend/app/main.py)
- [`backend/app/session_store.py`](../../backend/app/session_store.py)、[`backend/app/agent_runtime/`](../../backend/app/agent_runtime/) 和 [`tests/python/test_multichannel.py`](../../tests/python/test_multichannel.py)

## 背景

接入渠道前，Meido 的一个角色只有桌面唯一会话。Telegram、QQ NapCat、官方 QQBot 和飞书要求一个角色同时参与多个私聊或群聊；如果直接复用桌面 transcript，访客和群聊就可能读到主人历史。如果为每个平台另建角色 Runtime，又会重复模型配置、角色设置和并发控制。

[ADR-0002](0002-shiori-memory-engine-boundary.md) 中不引入 channel/chat scope 的范围以当时只有桌面会话为前提；角色记忆的 `role_id` 硬隔离仍应保留。

## 决定

- 共享角色级 Runtime 管理、配置和并发锁，桌面及各渠道聊天使用独立 transcript。桌面线程映射已有 `role:<role_id>` session，外部线程各用 `thread:<thread_id>` session，不为每个平台复制 Runtime。
- 由宿主持久保存角色、账号、线程与平台聊天的归属关系。消息与运行通过 session 归属线程，渠道 metadata/snapshot 补充来源；权限不能只依赖消息自报字段。
- 访客私聊和群聊的初始原始历史限当前线程。已配对主人私聊从授权起点读取当前聊天，并可获得桌面及其他授权主人私聊的近期片段；桌面当前仍沿用自己的历史流程。
- 主人身份是单一桌面用户的全局绑定，平台作用域适用于同平台所有角色账号，应用作用域仅适用于绑定账号；查询先验证入站账号的角色归属。身份共享不跨角色共享历史或记忆，适配器提供的身份声明不能直接授予主人权限。
- 访客使用 `external_allowed` 工具；主人群回合可使用角色已启用的主人工具，但不自动并入主人私有历史。群笔记/摘要与原始聊天分开，不能通过跨群笔记查询获取原文。
- 渠道宿主负责准入、身份、线程路由、工具目标授权和投递状态；适配器负责平台连接、解析、媒体和收发。账号归属角色，每种适配器最多一个账号，能力按规格明确声明。
- 已有桌面历史只登记为 `desktop` 线程，不搬移或重写消息、运行和工具记录。账号删除保留历史，角色删除按角色清理渠道数据。

## 原因

共享 Runtime 保留角色一致的行为、配置与并发语义；独立 session 则让 transcript 隔离直接落在现有持久化查询边界。结构化账号/线程归属和宿主身份查询提供可测试的授权事实，避免插件 metadata 成为权限来源。桌面映射保留既有历史格式，降低接入渠道时的数据兼容风险。

主人私聊的授权片段让同一主人在不同渠道继续对话；群聊仍限当前群原文，避免仅因主人在群中发言就自动暴露其他聊天。保留 Shiori 主人群工具权限意味着主人主动调用的工具仍可能返回私有信息或产生外部操作，进一步收紧需另行权限决策。

## 后果与实施边界

- 配对时间、账号归属、线程和目标目录成为持久授权契约；访客请求不能借用桌面默认历史，模型不能用任意平台 ID 绕过目录发送。
- 角色主人记忆保持 `role_id` 隔离与既有主人私有分类。主人渠道完成回合维护主人长期事实，授权主人私聊与桌面共同维护 Markdown 视图；访客私聊、群档案及旁听分别按当前线程整理，不写入主人 MemoryEngine。外部回合不自动注入主人记忆，可信主人在群中发言仍可形成主人事实。
- 旁听采用独立存储、每日配额、近期内存窗口和整理游标，不能用旁听批次推动正常会话维护游标。外部成员资料和群笔记与原始消息分开，模型返回的成员必须属于当前输入；账号删除保留历史，角色删除先停止维护再按角色清理。
- 消息/运行来源仍结合独立线程归属与结构化 metadata；统一的 `ConversationContextBuilder` 可进一步深化接口。主人 Markdown 使用稳定的跨 transcript 维护顺序，不改写桌面 API、原始消息序号或 Runtime transcript；读取近期上下文前重验当前配对授权，解绑后不读取旧私聊原文。
- 配置替换关闭准入并等待在途事件/发送结束，再停止旧连接、保存和启动新配置；失败回滚。该方案有切换空窗，不采用新旧连接并行启动或持久账号代次。
- 本决定替代 ADR-0002 中“不引入 channel/chat scope”的会话范围；其 MemoryEngine 契约、SQLite 权威数据源、Markdown 视图和角色记忆硬隔离继续有效。
