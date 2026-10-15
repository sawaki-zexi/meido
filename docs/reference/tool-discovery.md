# 同次运行的工具发现与可见性版本

状态：当前

最后核验：2026-10-16

事实来源：[发现协议](../../backend/app/agent_runtime/discovery.py)、[能力解析](../../backend/app/agent_runtime/capabilities.py)、[运行循环](../../backend/app/agent_runtime/loop.py)、[持久化宿主](../../backend/app/agent_runtime/runtime.py)、[公开工具/版本测试](../../tests/python/test_tool_discovery.py)、[HTTP 行为测试](../../tests/python/test_tool_discovery_api.py)。对应 [Issue #148](https://github.com/sawaki-zexi/meido/issues/148)、[ADR-0011](../adr/0011-versioned-tool-activation.md)。

## 角色配置与兼容

把 `tool_search` 加入角色现有的 `agentConfig.enabledTools` 可开启按需发现；更新时保留其他配置和授权。未开启的角色保留原有 schema 形态。这是 Meido 对已有角色的兼容选择，区别于 Shiori 新安装的默认发现开关。

发现开关只改变已授权工具的暴露时机。它不启用 Shell、浏览器、渠道或其他工具，不安装依赖，也不扩大访客/群回合的范围。当前明确延迟的工具是：

- `agent_browser_*` 的 28 个工具；
- `account_list`、`account_targets`、`account_send`；
- `lookup_group_context`、`update_group_context`、`set_group_listening`、`lookup_group_member`；
- `read_attachment`（仍必须存在宿主绑定的当前 QQ 附件）。

这份集合来自固定 Shiori 的 `always_on` 声明，按具体名称应用。Shell、网络、文件、消息历史、记忆和 `message_push` 保持核心工具行为；后续任务工具继续使用自己的暴露元信息。Meido 专有的待办和邮件保留既有声明，不根据“插件/宿主”来源推断是否延迟。实际启用和注册情况以当前角色目录为准。

工具目录中的 `authorized=true` 表示策略准入；`modelVisible=false` 可以表示尚未激活，目录说明会给出拒绝原因。目录预览仍是下一次桌面运行的初始视图，不代表活动运行已经加载的版本。

## tool_search

```json
{"query":"标题","top_k":5,"allowed_risk":["read-only"]}
```

```json
{"query":"select:account_list,agent_browser_get_title"}
```

`query` 最长 2000 字符；空查询或无匹配返回空列表及提示。关键词按空白/标点拆词，不区分大小写，匹配名称和说明，名称匹配优先；中文说明可以直接检索。`top_k` 为 1–10，默认 5，仅用于关键词检索。`select:` 不区分前缀大小写，逗号分隔精确名称，去重后最多 50 项。

`allowed_risk` 可选，取值沿用 Shiori：`read-only`、`write`、`external-side-effect`，分别对应 Meido 的 `read_only`、`mutating`、`external`。空数组不额外收紧风险。风险过滤不能扩大宿主授权。

结果包含 `matched`（名称、简短说明、风险）、`unlocked`、`already_loaded` 和 `tip`。完整 JSON 最多 16000 字符；必要时先缩短说明、再减少结果，并返回 `truncated: true`，只暂存实际返回的激活名称。关键词只返回尚未加载的匹配工具；精确选择可以报告已经加载。未授权、隐藏、审批拒绝、外部受限或已撤权工具不会进入匹配结果。模型不能传入角色、session、可见集合、授权标志或版本。

`unlocked` 是待持久化的激活结果。同一 provider 回复中的其他调用继续使用该回复原有的版本；即使排在搜索后面，也不能抢先调用新工具。搜索结果持久化并提交新版本后，下一次 provider 请求携带完整 schema，模型可直接调用。

## 快照、调用与恢复

运行开始冻结候选定义、来源、风险、审批、角色、session 和宿主准入结果。每次可见性变化产生新的快照，旧快照及其嵌套 schema 不被原地改写。`capabilities.snapshot` 始终表示初始版本，`capabilities.current()` 返回当前版本。

`AgentRun.modelSnapshot.capabilityRevisions` 是有序的持久版本列表，每项包含 `snapshotId`、`visibilityVersion`、`parentSnapshotId`、`candidateCatalogId`、`generation`、`tools`、`candidates`、策略及插件元信息。初始版本为 0；激活追加版本并保持候选目录不变。`capabilityCalls` 把 tool call ID 绑定到发起该调用的 provider 版本，审计 `snapshotId` 使用同一版本。

宿主可通过 `SessionStore.capability_revision_for_call(run_id, tool_call_id)` 读取调用实际使用的版本；工具收到的不可变 `ToolContext.capability_snapshot_id` 提供相同身份。任务来源应绑定调用版本，不能用“当前最新版本”代替同回复中的较早版本。

Runtime 为重复调用 ID 分配唯一宿主身份，供 `ToolContext`、持久消息、审计及 `capabilityCalls` 使用。assistant metadata 的 `provider_tool_call_ids` 保存宿主 ID 到原 provider ID 的映射；rich/structured 请求回放恢复原 ID，并保留原生内容块及思考签名。Google/Vertex 无远端 ID、各响应重复生成 `call-0` 时，仍可在同一运行发现并执行新工具。

`RuntimeManager` 在结果持久化后、下一次请求前写入修订。存储失败时不发布新版本；取消后不继续发请求。未提供自定义消息持久处理器时，启用发现的 Runtime 也会保存结构化调用和工具结果。

新运行只参考同角色、同 session 的最近一个已完成运行。只有仍准入且完整定义一致的历史激活可进入新运行初始版本；关闭权限、变化的 schema/版本/来源、失败和取消运行不被复活。执行前和 pre hook 后再次检查当前许可，最终参数仍按被冻结的 schema 验证。Shell 最终命令同时受初始与当前白名单约束，添加命令不扩大旧运行权限，移除命令可撤权。

异步工具执行期间按 50ms 间隔复核授权及运行取消信号；撤权会取消工具任务、传递工具取消信号并等待清理，再记录拒绝结果。超时和父运行取消同样等待清理，局部工具拒绝不会取消父运行，也不会被记为成功。该机制依赖工具遵守异步取消或合作信号，不能撤回已经提交的外部事务，也不能抢占阻塞事件循环的同步代码。

停止与断开造成的连续父任务取消也不会中断已开始的工具清理；Runtime 保持等待实际工具终止，完成后再传播记住的取消。运行终态和角色运行锁的释放发生在真实资源清理之后。

启用发现的渠道运行额外冻结真实来源：[FrozenChannelSource](../../backend/app/channels/source_authority.py) 从实际 run 的入站消息及线程读取账号、平台发言者和主人绑定代际，不接收模型自报身份。每次搜索与执行复核平台总开关及账号启用、私聊/群范围、屏蔽名单、线程归属及原 `identity_id`/`paired_at`/私聊授权起点。解绑后重新配对或删除后重建同 ID 账号不能复活旧运行。原外部回合即使随后配对也不提升权限。JSON 接口 `to_dict`/`from_dict` 供后续后台任务保存相同来源事实，日期使用带时区的 ISO 字符串。

工具审计由共享 `RuntimeManager` 写入，桌面及渠道入口均使用事件中的实际版本；HTTP 层只负责转发事件。

## 预授权服务修订接口

`ServiceGrant` 由宿主在运行开始提供，固定 grant/service/role 身份、风险上限及外部范围。`CapabilityResolution.stage_service_tools(context, grant_id, parent_snapshot_id, tools)` 供后续 MCP 连接宿主提交具体工具：

- grant 必须来自运行开始的授权，角色和父版本必须匹配；
- 工具来源必须是该服务的 `mcp:<service_id>`，风险和外部范围不能超出 grant；
- 不能覆盖原目录中的名称，也不能引入额外审批或宿主服务权限；
- 新定义先冻结为延迟候选，宿主调用结果持久化后才发布带父版本的目录修订；之后可在当前运行搜索并调用。

该接口不接收模型 command/env。T12 的 MCP 宿主通过独立管理工具使用该接口，真实连接、调用、按来源恢复及整树清理见 [MCP 服务契约](mcp-services.md)。服务移除通过 `stage_service_removal(context, grant_id, parent_snapshot_id)` 暂存带父版本的注销修订；结果持久化后移除可见定义及候选，旧版本保持不变。相同服务 grant 的重新添加可以恢复自己此前移除的名称，不能占用其他来源名称。

上游行为依据：[Shiori-Agent 固定版本 tool_search.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/tool_search.py)。协议经 Meido 的宿主身份、不可变快照和持久化边界重新实现；来源许可见 [MIT License](../../third_party/shiori-agent/LICENSE)。
