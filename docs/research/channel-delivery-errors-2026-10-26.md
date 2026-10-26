# 渠道投递恢复与格式证据

状态：当前；调研证据，实现以 Issue #239 及通过的测试为准

最后核验：2026-10-26

事实来源：Shiori `d149de7f052f77c5bb88d4d1393d48dce1870875`、下列官方文档；仅使用本机替身，不代表真实账号验收。

## 飞书错误边界

官方网页正文未能从普通页面读取，但其 `.md` 地址返回完整 Markdown，已实际读取[回复消息](https://open.feishu.cn/document/server-docs/im-v1/message/reply.md)、[发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create.md)和[频控策略](https://open.feishu.cn/document/server-docs/api-call-guide/frequency-control.md)。

| 官方错误 | 可证明的事实 | 本规格采用的恢复范围 |
| --- | --- | --- |
| `230020` | 当前操作触发频控 | 原段有限退避；不重发前段 |
| `99991400`，HTTP 429（部分旧接口 400） | 网关频控；响应头 `x-ogw-ratelimit-reset` 给出秒数 | 遵守等待时间与原投递期限，二者冲突即停止 |
| `230011` | 被回复消息已撤回 | 原账号、聊天、话题内移除引用一次 |
| `230099` + 子码 `200621` | 卡片 JSON 解析失败 | 当前卡片段可降普通文本一次 |
| `230099` + 子码 `200861` | 卡片 schema 不支持所用组件 | 当前卡片段可降普通文本一次 |
| `230099` + `200381` / `11310` | 可能是模板权限或其他非格式错误 | 不作为通用格式后备条件 |
| `230002/230013/230018/230027` | 群成员、可用范围、群策略或可见权限拒绝 | 不改成无引用或其他目标重发 |
| `230019/230071/230072` | 话题不存在或不支持该话题回复 | 不丢弃话题改投默认聊天 |

不把未知 5xx、连接中断、超时或缺消息 ID 当作已确定拒绝。白名单仅说明可以再次尝试，不保证最终成功；所有尝试共享段级计数和逻辑消息期限。

Shiori 的 [Feishu `_send_chunk/_deliver`](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/feishu/backend/channel.py#L697)已有卡片、分段及回执回调，但后备覆盖较宽的异常。Meido 按已批准 F11/F12 收紧为可证实未接受的白名单，不照搬宽泛异常后备。

## Telegram 与 QQBot

Shiori [发送实现](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/telegram/backend/utils/sending.py)用 `telegramify-markdown` 转成正文与实体；每段回调立即交付回执。[库的一手说明](https://pypi.org/project/telegramify-markdown/)确认实体偏移使用 UTF-16；本次核验发行版为 1.4.1。仅采用无网络的转换功能，不启用 Mermaid 远程渲染或库内自动发送。

[Telegram Bot API](https://core.telegram.org/bots/api#messageentity)定义实体偏移及长度的 UTF-16 单位，回复参数提供同聊天的无引用后备，频控通过 `retry_after` 返回秒数。保留明确选择的话题，拒绝因后备改投其他话题。

QQBot 的 [腾讯官方源码文档](https://github.com/tencent-connect/bot-docs/blob/main/docs/develop/api-v2/server-inter/message/send-receive/send.md)（本次直接读取原始文件）规定普通 Markdown 使用 `msg_type=2` 与 `markdown.content`，`msg_id + msg_seq` 共同去重。当前 C2C 说明为 60 分钟内至多回复 5 次，群聊为 5 分钟；本次仅实现 C2C，并把已知过期和配额预留保存在原消息关联下。该文档也声明主动推送不再提供，因此任何格式/权限错误都不能被解释为改发主动消息的许可。媒体请求保留 `msg_id`，使用同一来源下新预留的 `msg_seq`；按官方说明保留媒体 `content` 空格。HTTP 替身验证真实请求负载，重建适配器后仍不重用序号；没有宣称真实账号验证。

## 验证边界

120 秒逻辑消息期限、Telegram 账号 0.25 秒 / 聊天发送 2 秒 / 编辑 5 秒 / typing 8 秒、最多 5 次，以及飞书最多 4 次、0.5 秒起退避，是批准 Spec 的产品初值。它们不是官方保证的吞吐量，也不是本次实测平台结果。

原始线程和四种顶层状态保留。新增逐段事实记录在现有投递账本，重启只恢复可查询状态，不自动重放未确认段；Grok 的传输接受回执由独立后续 Ticket 接入，不能放宽普通平台必须返回消息 ID 的要求。
