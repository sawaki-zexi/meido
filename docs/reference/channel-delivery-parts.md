# 渠道分段投递契约

状态：当前

最后核验：2026-10-26

事实来源：[宿主](../../backend/app/channels/host.py)、[投递](../../backend/app/channels/delivery.py)、[账本](../../backend/app/multichannel.py)、[历史投影](../../backend/app/channels/delivery_projection.py)、[手机展示](../../web/src/features/channels/DeliveryDetails.tsx)。架构取舍见 [ADR-0029](../adr/0029-channel-delivery-parts-and-deadlines.md)。

## 外部结果

既有 delivery 查询继续返回四种状态。新增字段也通过消息 metadata 和 `account_send` 工具返回；旧记录可以缺少它们。

| 字段 | 含义 |
| --- | --- |
| `parts` | 固定顺序的物理段；普通文本/媒体或流式最终段 |
| `partialDelivery` | 已有确认回执，但本条尚未全部确认 |
| `deliveredContent` | 目前确认的正文，不含未确认尾部或临时占位 |
| `receiptKind` | 普通平台为 `platform_message`，必须有真实消息 ID；仅 Grok 可用 `transport_acceptance`，以 HTTP 200 表示任务接收 |
| `deliveryDeadlineAt` | 本次有限期限对应的墙钟秒数，仅用于展示；执行用单调时钟 |
| `requestedReplyTo` | 原请求引用；后备不会擦掉该关系 |
| `messageThreadId` | Telegram 原话题；去引用不删除它 |
| `streaming` | 当前保存的是尚未最终定稿的流式状态 |

每段有 `index/kind/content/status/attempts/platformMessageId/error/withoutReply/plainText`。媒体还有 `attachmentIndex/fileName`；QQBot 有 `msgId/msgSeq`。对已有流式消息的编辑可以附 `confirmedContent`，表示更新失败或未知时仍可证明曾经显示的内容。

Grok 接收段保存 `httpStatus=200`，不填写平台消息 ID；手机和模型历史以此作为当前线程的已提交证据，显示“已提交 Bot”。这不是 Bot 的回复或任务完成证据。配置及回程见 [Grok 手册](../handbook/grok-bot.md)。

段状态 `pending` 表示尚未派发；终态逻辑消息中的 pending 是“未发送”，不是待自动补发。`sending` 是请求已进入发送边界且等待结果；重启将它转为 `uncertain`。`sent` 必须有回执。明确拒绝为 `failed`，未知为 `uncertain`。存在确认段时，失败与未知都不抹除前段。

## 调度与恢复

- 宿主首次进入发送时冻结最多 120 秒，合并来源更早期限。适配器每次等待后以及获取令牌/连接后的最后发送边界复验许可。
- Telegram 默认聊天发送 2 秒、编辑 5 秒、typing 8 秒，账号 0.25 秒；每次物理操作最多 5 次。飞书最多 4 次，0.5 秒起指数退避。每账号最多容纳 64 个正在发送/等待的操作，普通消息最多 128 段。
- 限流等待取平台等待值和指数退避的较大值；超过剩余期限即停止。引用撤回和明确格式拒绝每段最多各一次，与限流共用尝试计数。
- Telegram 正文实体按 UTF-16 分段，媒体 caption 使用相同实体转换。飞书普通/主动文本发卡片，代码块跨段补闭合和重开，账本仍保留原段正文。
- QQBot 普通正文使用 Markdown，文本与图片保留同一被动来源。C2C 来源最多预留 5 个序号；已知过期来源不发送，平台拒绝时不改主动发送。适配器重建从原账本继续配额。
- 群禁回、账号代次、启停修订和来源许可变更会使旧任务停止；再开启不会复活旧尾部。实际返回的回执仍保存。

## 历史、界面和重启

原始消息保存完整生成内容及分段明细。运行时和消息查询仅投影确认正文及确认媒体；不会将未发尾部放入后续模型上下文。手机和普通会话视图提供“分段送达详情”，并区分部分送达、未知段、未发送尾部。

重启保留已确认 ID，遗留在途段转为未知；未派发段保持未发送。流式未定稿不被视为完整发送。只有所有最终段已经确认的遗留记录才可恢复成 sent；任何恢复都不发网络请求。

`transport_acceptance` 为后续 Grok 接入保留，界面措辞是“已提交 Bot”，不代表对端完成。此版本没有为其他平台提供免消息 ID 的成功路径。

## 验证边界

上述行为由隔离数据库、受控 HTTP/WebSocket 替身、历史查询和界面组件验证。没有真实账号验收，不保证平台对任意 Markdown 语法的显示效果；未知错误保持保守终态。
