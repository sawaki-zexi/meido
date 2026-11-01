# 聊天卡顿与断线：Shiori 对照

状态：当前调研；基线问题记录于下文，#291 准备边界已合入，#292 摘要计量与反馈在本分支实现，#293 尚待实施。
事实来源：固定上游源码、隔离性能复现、[准备回归](../../tests/python/test_context_preparation.py)、[完整规格](../specs/chat-responsiveness.md)。
最后核验：2026-11-01。

## 基线

- Meido：运行版本 `90c9270033b9883f901e8103ad5fcac9c4803341`；最新 `origin/main` 为 `42020b63863c680825e7c887d4e6ba5a100a880d`，两者之间只有 README 变更。
- Shiori：最新 `origin/main` 为 `be6343d1deaa62c69f27d33654e77020b8f17c67`；本次引用的工具注册、摘要、聊天任务代码与已读取的 `71c8b0e460530683cd28aad6bb62e873c7167c89` 相同。最新变更为 presence 收口。
- 以下性能与行为差异描述核验时的基线；不表示 #291 改动后的所有路径仍有相同缺陷。

## 已复现的本地性能问题

无网络、合成角色、独立数据的 161 条消息／约 82,024 token／18 个可见工具：原路径准备 25.140 秒，事件循环阻塞 25.130 秒；在同一次固定输入准备中复用一次权限结果后为 0.503 秒，选出的摘要输入一致。

独立采样显示：2,261 次整套权限计算、31,654 次 SQLite 执行、20,349 次 SQLite 连接；53 次请求估算、52 次窗口工具选择。约 25.5 秒花在授权检查。

复现脚本和日志：`D:/meido-284-herdr-control/.data/checks/chat-perf-20261031/`。对照仅用于定位，不能把这个临时缓存原样用于正式执行。

调用链：`context_provider._compact` 的候选循环 → 窗口工具筛选与 `MeidoProvider.estimate_request` → namespace 提示重建 → 每个工具的 `authorize_tool` → 全套 `_runtime_capability_policy` → 配置、插件、数据库和文件读取。该段同步执行于服务事件循环。

## 上游事实与差异

1. [当前注册表](https://github.com/YinFengWindy/Shiori-Agent/blob/be6343d1deaa62c69f27d33654e77020b8f17c67/apps/backend/agent/tools/registry.py) 的 schema、常驻名称和外部许可筛选直接读取内存工具及元数据；[窗口选择](https://github.com/YinFengWindy/Shiori-Agent/blob/be6343d1deaa62c69f27d33654e77020b8f17c67/apps/backend/agent/core/passive_turn/tool_visibility.py) 保持常驻及历史顺序。实际调用仍查询当前实现和当前限制。
2. [压缩候选](https://github.com/YinFengWindy/Shiori-Agent/blob/be6343d1deaa62c69f27d33654e77020b8f17c67/apps/backend/core/compaction.py) 从配置的保留轮数向零递减；默认 2 轮最多检查 3 个不同候选。Meido 从最早历史逐步尝试截断，长历史会产生大量完整估算。候选估算和最终带摘要请求验证必须分开，后者仍是提交前事实。
3. [摘要请求](https://github.com/YinFengWindy/Shiori-Agent/blob/be6343d1deaa62c69f27d33654e77020b8f17c67/apps/backend/core/compaction_summary_request.py) 明示完整 JSON 模板；仅合法但过长的结果允许一次缩短，携带计量值、计量来源和硬上限。Meido 缩短时只给已有摘要及一句缩短要求。
4. [可见输出计量](https://github.com/YinFengWindy/Shiori-Agent/blob/be6343d1deaa62c69f27d33654e77020b8f17c67/apps/backend/agent/prompting/output_usage.py) 扣除明确有效的 reasoning 用量，遇到缺失／不一致则使用规范化正文的本地计量；[摘要校验](https://github.com/YinFengWindy/Shiori-Agent/blob/be6343d1deaa62c69f27d33654e77020b8f17c67/apps/backend/core/compaction_summary_validation.py) 记录脱敏原因。Meido 直接使用总 output_tokens。
5. 合成计量复现：总输出 2500、reasoning 1500、上限 2000 时，Meido 算作 2500；Shiori 的有效可见上界是 1000。真实旧失败的两次计量为 2381/62 和 2236/61，即扣除 reasoning 后仍超过 2000；不能把该旧失败全部归因于这一计量缺陷。
6. [聊天服务](https://github.com/YinFengWindy/Shiori-Agent/blob/be6343d1deaa62c69f27d33654e77020b8f17c67/apps/backend/desktop_bridge/chat_service.py) 持有后台 turn task；[请求接口](https://github.com/YinFengWindy/Shiori-Agent/blob/be6343d1deaa62c69f27d33654e77020b8f17c67/apps/backend/desktop_bridge/chat_requests.py) 将 chat.send、chat.cancel 分开。Meido 的 SSE 生成器持有整个执行，断线 watcher 主动取消 run，前端卸载又会 abort。Web 重连订阅是适配这一任务归属原则的 Meido 实现，不能声称 Shiori 已有相同的 HTTP/SSE 接口。

## 真实运行范围与限制

最近审计的一轮约 241.7 秒，查看角色图片 0.752 秒、生图 4.977 秒，图片结果之后仍等待约 193.5 秒。大量等待位于工具前后，不能把全部时间记在工具函数上。

摘要存在约 90–96 秒超时，或一次缩短后仍校验失败。辅助模型等待和本地同步阻塞是不同问题；前者需正确计量、格式约束、有限失败回退及可见状态，不能通过掩盖错误、无限重试或忽略硬预算来解决。

没有真实付费模型调用、外部消息、正式服务重启或数据修改。

## #291 实施证据

同机相同输入（161 条、82,024 初始 token、18 个可见工具、24 个候选），两次改后准备分别为 **0.149 秒、0.073 秒**，完整 policy 均为 **1 次**，最大事件循环延迟分别为 **0.101 秒、0.044 秒**；基线分别为 25.140 秒、2,261 次和 25.130 秒。后一次为最终代码复测。计时在摘要网络请求前停止，不代表外部模型速度。

隔离脚本适配了新的准备入口：先捕获本次输入，再投影窗口和估算；原有文本、模型容量 100,000、触发 75,000、目标 40,000 和真实宿主 policy 保持相同。按配置保留轮数的行为改变后，选出的摘要前缀允许不同。未使用诊断 policy 缓存。

本地证据路径（未跟踪）：`.data/checks/291/profile_probe.py`、`performance-after.txt`、`red.txt`。CI 不断言此机器相关耗时，而通过 [真实 HTTP barrier 与输入一致性测试](../../tests/python/test_context_preparation.py) 验证等待期间仍可读取页面、停止和正常收尾，并通过定向窗口、工具、来源与用量回归验证语义。

`CapabilityPreparation` 只描述工具；原文窗口选择与搜索共用它。`MeidoProvider` 明确捕获上下文，再渲染 `PreparedRequest`，估算不再修改提示状态。最终 rich messages、工具和配置复用后交给原有 adapter。实际 dispatch 仍检查当前注册表和权限。取消时只排空已启动的窗口写事务，独立只读工作不阻止任务取消。

## #292：协议用量核验与实现边界

固定 Shiori 的 `OutputTokenUsage`、`SummaryRequest`、`normalize_summary` 分别负责严格计数、唯一缩短请求和合法状态投影。Meido 现在在适配器将非法计数转为显示用零值之前，保留独立的严格输出计量；摘要通过统一校验入口使用它，普通聊天原计数含义不变。完整 JSON 模板、规范化状态与计量反馈随同一次缩短请求发送，安全诊断通过持久化查询保留。

- OpenAI Completions／Responses 的输出总量包含 reasoning，已有各原生协议适配器分别读取 `completion_tokens_details.reasoning_tokens` 和 `output_tokens_details.reasoning_tokens`；Azure／Codex 复用 Responses。
- [Anthropic 官方说明](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost)：`output_tokens` 包含全部思考，`output_tokens_details.thinking_tokens` 给出其中思考用量；流式在最终 `message_delta` 提供。适配器合并终态时保留此严格细分；缺失且观察到 thinking 时采用本地正文估算。
- [Google UsageMetadata](https://ai.google.dev/api/generate-content#UsageMetadata)：`candidatesTokenCount` 与 `thoughtsTokenCount` 分开列出，`totalTokenCount` 还包含 prompt 和工具提示。Meido 原有显示输出会把 candidates 与 thoughts 相加；严格正文计量直接取 candidates，避免重复扣除。
- [Pi 固定类型合同](https://github.com/earendil-works/pi/blob/7d75a0e3eb273dff753d49c066ea29fbe7b3b6f6/packages/ai/src/types.ts#L446-L469)：reasoning 是 output 的子集，可明确为零，未报告时为 undefined。不能把缺失计数先归零再当作有依据的零值。
- [Bedrock TokenUsage](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_TokenUsage.html) 没有独立 reasoning 字段。[Mistral reasoning](https://docs.mistral.ai/studio/conversations/reasoning) 确认 thinking 块会增加 token，但未核验到必有的独立细分字段。两者观察到 thinking 却没有有效细分时回到本地正文估算，不臆造用量。

协议回归使用真实适配器消费可控 HTTP／事件流，验证总量、细分、缺失、非法值及 Google 独立候选计数。摘要行为回归验证 2500／1500 被接受为 1000 且没有第二次请求；这修复错误重试，不能保证外部模型的生成速度。

## #293：聊天任务归属实施

固定 `be6343d` 的 `DesktopChatService.start_chat_turn` 同时拥有前驱等待与实际运行，取消排队请求通过 shield 保护前驱；其 terminal 延后到 `_finish_turn`。Meido 在 `chat_runs.py` 适配此顺序，仍使用既有 RuntimeManager、SQLite 与 SSE。`queued` 是已接受但尚未进入锁的持久状态，服务重启只失败收尾。

新增接受标识和 run 游标是 Meido 网页恢复协议，不声称是上游相同传输。当前工具投影、事件环和完成缓存均有上限；缺口从数据库恢复，慢订阅不阻塞生产。后端实际 HTTP 与前端集成测试覆盖断开、重订阅、响应丢失、queued Stop、慢清理及迟到回调；当前事实见[生命周期合同](../reference/chat-run-lifecycle.md)。没有改动正式服务或调用付费模型。
