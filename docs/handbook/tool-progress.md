# 临时工具进度

## 数据与生命周期

既有工具 `on_update(AgentToolResult)` 是可丢弃的**快照**，不是日志或追加文本。
现有生产调用方只新增 MCP 连接准备的已进入阶段；不发送 stdout、参数、凭据、百分比
或协议通知。Runtime 保持顺序调用，单次 execute 在有界缓冲器旁运行，原有权限轮询、
固定 timeout 与资源 join 不变，不引入事件总线或并行工具。

单个 invocation 的 callback 窗口在 execute settle 时关闭（包括 `call_soon` 的迟到
回调），取消、撤权和 timeout 的 local token 在清理之前拒绝新更新。消费者退出时
关闭缓冲并等待执行清理；旧调用回调不能污染后一个同名调用。

已经 settle 的真实结果优先于同时观察到的取消/撤权；这不授予继续执行或投递权限。
宿主取消后不再开始批次内后续调用或下一模型请求。未完成调用仍按既有取消/timeout/
拒绝合同结束，不把已派发的外部效果宣称为未执行或回滚。

## 固定预算

- 每 call pending 最多 **32** 个 snapshot、序列化 envelope 合计最多 **256 KiB**。
- 单个 envelope 最多 **32 KiB**，正文 UTF-8 最多 **16 KiB**，details 最多 **8 KiB**。
- 更小的 `ToolDefinition.output_limit` 优先；进度按字节收紧，final 原有限制不变。
- JSON 转义与身份 envelope 计入预算。异常大的身份无法容纳时丢弃进度，final 不丢弃。
- details 只检查有限节点/深度/字节，不先无限 deepcopy/序列化；非 JSON、循环、过深、
  超额或无效 Unicode 详情降级为省略标记。图片不复制、不传输，仅记录省略数量。
- 队列满先丢最旧中间快照，尽量保留最新快照；同步 callback 不创建异步 task。
- final result 从独立执行 task 直接取回，永不进入可丢弃队列；SQLite `message_end`
  持久化仍先于下一 provider request。执行 settle 后通过宿主持有的结果回调提交既有
  审计与消息处理器，不等待可丢弃进度的订阅者继续消费；关闭/取消会 join 这次持久化，
  外层事件消费不重复保存。存储失败结束当前运行，不自动重放已执行动作。
  重开历史不重放临时进度。

## SSE

沿用 `tool_execution_start/update/end`。三者都有 `roleId`、`sessionKey`、`runId`、
`sequence`、`runtimeSequence`、`toolCallId`、`toolName`，start 不再发送原始 arguments。
update/end 增加：

```json
{"result":"安全文本摘要","isError":false,"status":"running","droppedUpdates":0}
```

`result` 是字符串。update 固定 `running`；end 保留既有结果类别（例如 `succeeded`、
`failed`、`denied`、`timed_out`、`cancelled`、`unknown`）。`droppedUpdates` 为本 call
累计合并/丢弃数；不声称这些快照是完整历史。前端只保留当前调用最新状态，已终态
调用与旧角色/会话/run 的更新不再接受。

## 离线验收记录（不是生产性能结论）

实际 listening HTTP 测试证明：工具发出一次阶段后等待 barrier，SSE 客户端已经收到
该 update 时，工具尚未完成。取消、timeout、撤权、错误、消费者 close 及后续同名
调用覆盖 callback 关闭；final 与既有结果持久化测试保持通过。

2026-10-26 固定假工具压力测试：

| 输入 | 峰值 pending 数 | 峰值保留 envelope 字节 | 合并/丢弃 |
| --- | ---: | ---: | ---: |
| 100,000 次小快照 | 32 | 69,792 | 99,968 |
| 1,000 次大中文正文 + details | 10 | 245,510 | 990 |

关闭后接受数为 0。数值约束宿主的额外快照持有，不约束可信工具自身分配的内存。

对照 Pi `d5b3de22` 的 per-invocation `acceptingUpdates` 和两个 late-update 测试；
不复制 Pi 无界 `updateEvents` 数组、默认并行或可变 hooks。缓冲预算为本次规格的
保守起点，不宣称生产吞吐/延迟改善比例。
