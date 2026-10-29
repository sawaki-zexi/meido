# 聊天表达与查证提示

状态：当前实现；最后核验：2026-10-29；Refs #275。
事实来源：[共同提示](../../backend/app/agent_runtime/conversation_prompt.py)、[Provider](../../backend/app/agent_runtime/meido_provider.py)、[分条协议](../../backend/app/agent_runtime/natural_reply.py)、[模型请求验证](../../tests/python/test_natural_reply_api.py)。

## 表达与发送协议分开

普通角色请求固定带有 `[对话方式]`，涵盖意图与语境优先、专业任务保持角色声音、按内容控制详略、不机械安抚/反问/表演动作、未知词不扩写成用户需求，以及明确反馈即时生效。角色设定与持久数据不被重写。

`naturalChatEnabled` 仅控制普通回复的 `finish_reply(messages)` 分条提交方式，不再决定基础聊天规则是否生效。自然群聊通过同一角色 Provider 使用共同原则，但仍只有 reply/wait/silence 和原 JSON 契约，不因此获得工具或主人记忆权限。

提供独立 system 的摘要等专用请求不强行加入聊天风格。旧式纯文本适配器收到同样的表达原则，并明确其实际没有可调用业务工具；不会把宿主工具目录说成已发送给这个适配器的 schema。

## 事实与工具使用

`[查证与工具使用]` 按本次请求真正提供的工具生成。稳定知识、给定文本和日常交流可直接回应；依赖当前外部事实的结论需要适用的当前证据。Provider 创建时冻结带 UTC offset 的本轮时间，压缩回退继续使用同一锚点。

可见工具可直接使用；提供 `tool_search` 时才给按需发现指引，下一次模型请求实际出现工具后才能使用。提供网页工具时才给相应搜索/正文读取规则。无工具时保留证据要求，但明确只能使用已提供材料，不能假称查过。

提示要求区分空结果、失败与成功；实际执行、工具授权和输出保存仍由原 Runtime 负责。网页内容和搜索材料不被当成可以改角色或权限的指令。基础表达和事实规则随最小回退保留；原有硬预算回退仍可移除业务工具和可选记忆，不能宣称本次已重做压缩或持久状态系统。

## 条件化技能

| 技能 | 加载线索 | 主要行为 |
| --- | --- | --- |
| `conversation-recall` | 之前、上次、还记得、记错等，或显式选择 | 先用已有背景；真有缺口才定向查询；优先最多三条证据，查无结果不盲目扩散；纠正区分当前理解与实际持久修改 |
| `conversation-continuity` | 道歉、称呼、别这样、不喜欢等，或显式选择 | 从真实互动判断反应；明确边界当轮生效；不过度推断亲疏，不虚构情绪状态或承诺 |

技能由现有 SkillRegistry 发现和校验，使用现有关键词匹配，不新增模型调用或自动执行器。两者不声明工具、Shell 或文件依赖；当前授权材料不足时允许澄清。未点名自然群聊不加载普通 SkillRegistry，使用上面的共同核心规则。

参考材料中的计分、冷场主动 @、自然月事件删除及 Tulpa 专用收发流程不在本次实现范围。原文件中的固定身份、账号和本地路径未导入。现有角色若有显式要求放弃角色语气等相反规则，仍应由主人编辑角色卡解决；本次不静默改写用户设定。

## 来源与验证边界

- Shiori-Agent `d3acaed186b172a78a5892e0307e9affd89759f0`：[行为规范](https://github.com/YinFengWindy/Shiori-Agent/blob/d3acaed186b172a78a5892e0307e9affd89759f0/apps/backend/prompts/agent.py)、[即时聊天](https://github.com/YinFengWindy/Shiori-Agent/blob/d3acaed186b172a78a5892e0307e9affd89759f0/apps/backend/prompts/natural_chat.py)。
- Tulpa `6753a28a9757c93c0ad34ed54064965df2cb081c`：[群聊行为](https://github.com/fumingyang2004/Tulpa/blob/6753a28a9757c93c0ad34ed54064965df2cb081c/chatlocal/prompts/mcp_chat/behavior.md)。MIT 声明见 [Tulpa LICENSE](../../third_party/tulpa/LICENSE)。
- 维护者于 2026-10-29 提供的 `active.md`、聊天 AGENTS 与八份技能，只作为参考材料。迁移的是语境判断和表达原则，不是原项目的执行指令。

HTTP/模型协议夹具验证规则实际到达普通、分条、旧适配器和自然群聊请求，且在工具后保持；技能用例验证选择、无关输入跳过、工具权限不增加和运行元数据。夹具生成的正文不是自然度实验。本轮未调用真实付费模型，不以测试通过宣称已经消除所有模型味。
