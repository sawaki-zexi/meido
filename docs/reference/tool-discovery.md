# 请求固定的工具发现与轻量恢复

状态：当前

最后核验：2026-10-26

事实来源：[发现协议](../../backend/app/agent_runtime/discovery.py)、[能力解析](../../backend/app/agent_runtime/capabilities.py)、[运行循环](../../backend/app/agent_runtime/loop.py)、[持久化宿主](../../backend/app/agent_runtime/runtime.py)、[公开工具/版本测试](../../tests/python/test_tool_discovery.py)、[搜索行为测试](../../tests/python/test_tool_search_relevance.py)、[HTTP 行为测试](../../tests/python/test_tool_discovery_api.py)。对应 [Issue #148](https://github.com/sawaki-zexi/meido/issues/148)、[ADR-0011](../adr/0011-versioned-tool-activation.md)。

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
{"query":"查群笔记","top_k":5,"allowed_risk":["read-only"]}
```

```json
{"query":"select:account_list,agent_browser_get_title"}
```

`query` 最长 2000 字符；空查询或无匹配返回空列表及提示。关键词保留小写整体查询，并按空白、中英文边界、中文单字和相邻双字召回；不依赖外部分词库或远端模型。名称按下划线拆分，精确名称片段、部分片段、全名包含的权重依次为 10、5、3，MCP 工具为 12、6、3；搜索提示额外加 4，说明额外加 2；新增参数命中加 3，namespace 共用信息对每个候选最多加 2。先按分数降序，再按名称升序稳定排序。普通查询与一个未加载候选的完整名称精确相同时只返回该工具。`top_k` 为 1–10，默认 5，仅用于关键词检索。`select:` 不区分前缀大小写，逗号分隔精确名称，去重后最多 50 项。

`allowed_risk` 可选，取值沿用 Shiori：`read-only`、`write`、`external-side-effect`，分别对应 Meido 的 `read_only`、`mutating`、`external`。空数组不额外收紧风险。风险过滤不能扩大宿主授权。

结果包含 `matched`、`unlocked`、`already_loaded`、`skipped_due_to_budget`、`budget` 和 `tip`。每个匹配保留 `name`、`description`、`risk`，并提供上游兼容的 `summary`（最多 120 字符）、`why_matched`（命中字段及查询词）、`always_on`（当前声明是否直接暴露）。解释最多 8 项，每项最多 160 字符，省略时返回 `explanation_truncated: true`；排名使用完整查询。完整 JSON 最多 16000 字符；必要时先缩短说明、再减少结果，并返回 `truncated: true`，只暂存实际返回的激活名称。关键词只返回尚未加载的匹配工具；精确选择可以报告已经加载。未授权、隐藏、审批拒绝、外部受限或已撤权工具在排名前被排除，不会进入匹配结果。模型不能传入角色、session、可见集合、授权标志或版本。

`ToolDefinition.search_hint` 是宿主搜索元信息，不进入 provider 的函数参数 schema。生产定义沿用固定上游的群资料、浏览器、电脑控制、Coding Agent、后台任务、文件目录、网络、消息、记忆、调度、生图、附件和屏幕提示；没有上游提示的工具保持空值。已加载的核心工具仍可直接调用，不因新增提示重新进入关键词结果。桌面 `message_push` 仅向真实主人上下文暴露；访客回合不会收到其 schema，也不能通过搜索解锁。

### 参数与 namespace 元数据

搜索文档只派生允许字段：工具名称、`search_hint`、描述、JSON Schema 的 property 名称和 `description`，以及 namespace 身份/短简介。schema 仅遍历 `properties`、`items`、`anyOf`、`oneOf`、`allOf`，不跟随 `$ref`，不索引 default/example/enum 值，不连接服务或执行 instructions。每工具派生文本最多 8192 UTF-8 bytes；遍历最多深度 8、256 节点，循环引用安全停止。命中行的 `metadata_diagnostics` 可包含 `search_text_truncated`、`schema_depth_limit`、`schema_node_limit`、`schema_cycle`。`why_matched` 使用 `名称`、`提示`、`描述`、`参数`、`namespace` 区分证据，不回显整段 schema。

`ToolDefinition.namespace_summary` 是宿主提供的短元信息，不进入 provider 函数 schema；非空值参与定义身份/恢复比较。namespace 直接使用已核验来源 `mcp:<server_id>` 或 `plugin:<id>`，不会因显示别名相同而合并。缺少简介时保留来源身份及空摘要，不采用远端 instructions 作为系统政策。

小地图先按当前许可过滤，再按 namespace 排序输出 `{namespace, summary, discovery_route}`。摘要最多 256 UTF-8 bytes，完整渲染最多 4096 UTF-8 bytes（包含标题、JSON 分隔符、路由和遗漏提示），超限整项省略。遗漏数仅计算已授权来源。读取地图不连接服务、不加载 schema。索引每次从当前不可变定义重新生成，无持久索引或第二套授权目录，也没有 provider prefix/cache 收益承诺。

### 完整声明预算

计量对象是每个完整 provider-neutral 声明 `{name, description, parameters}` 的稳定 JSON：key 排序、无多余空格、非 ASCII 原文编码成 UTF-8；取声明字节数之和。它不是输出文字上限，不是 token 数，也不是任意 provider wire 的完整请求大小。

- 单次搜索新增最多 **32768 bytes（32 KiB）**；累计当前可见声明目标上限 **131072 bytes（128 KiB）**。
- 必需直接声明的大小为 B，实际上限为 `max(B, 131072)`。B 达到或超过目标时保留全部基础工具，`baseline_over_budget=true`，拒绝新加载。
- 按排名/`select:` 的确定顺序，整工具放不下就跳过；继续尝试后续更小的候选。关键词的 `top_k` 限制预算筛选后的可加载匹配数，超大候选不占名额；找到足够结果后停止扫描。`select:` 不绕过预算，不裁剪 schema JSON，不自动卸载已有工具。
- `budget` 报告 `baseline_bytes`、`loaded_bytes`、`pending_bytes`、`new_bytes`、`total_bytes`、`new_limit_bytes`、`total_limit_bytes`、`effective_limit_bytes`、`remaining_bytes`、`remaining_new_bytes`、`baseline_over_budget`、`rejected_count`。没有伪装为实测的 token 估算。
- `skipped_due_to_budget` 只含本次已授权命中；结果过长时先缩短这份名称列表并标记 `skipped_names_truncated`，保留可加载匹配和拒绝计数。若仍需裁剪 `unlocked`，被裁掉的工具不激活、不占新增预算。
- 同请求暂存选择共享预留额度，同名定义只计一次；失败/取消释放预留，仅成功持久化的选择影响后续请求。已发给同一来源的声明即使随后撤权，仍占 provider 上下文字节直到移除，但不会再作为搜索结果/解释返回，执行仍重验当前许可。
- 恢复按历史选择顺序重新计量完整当前定义，仅使用累计限额（不是把恢复误算成一次 32 KiB 搜索）；超限条目不恢复并诊断。普通重复搜索、重启及历史压缩都不重置已可见 schema 字节。

固定中英评测、字段消融、体积与增长实测见[检索元数据与预算评测](../research/tool-search-metadata-and-budgets.md)。

`unlocked` 是待持久化的激活结果。同一 provider 回复中的其他调用继续使用该回复原有的版本；即使排在搜索后面，也不能抢先调用新工具。搜索结果的普通消息持久化完成后发布内存选择，下一次 provider 请求携带完整 schema；不再额外提交整份目录版本。

## 请求绑定、调用事实与恢复

运行开始冻结候选定义、来源、风险、审批、角色、session 与宿主准入上界。每次请求持有不可变 CapabilityResolution、定义及捕获的执行方法；同名注册改变不能替换该请求的实现，原对象声明改变则拒绝 `stale_definition`。`capabilities.current()` 只给下一请求提供新值；DiscoveryState 不保留历史 resolution 列表。

普通结果/消息保存成功后发布内存选择；同回复的其他调用仍使用原 request binding。保存失败、取消和失败工具释放待提交选择，不提前激活。未提供自定义消息处理器时，启用发现的 Runtime 仍保存普通结构化调用与工具结果。

正常完成时在该 run 的 `modelSnapshot.toolSelection` 保存一次 `tool-selection-v1`：roleId/sessionKey/sourceScope 与有序 selected（name/source/fingerprint）。指纹包括完整声明、来源、风险、审批、版本、暴露方式、超时、输出限制、外部许可及非空 searchHint/namespaceSummary，不仅是 provider schema。缓存没有候选目录和 schema，不授予权限；失败/取消不替换原有效选择。恢复必须存在原 run 的用户消息，清空历史不能从 detached cache 复活选择。

渠道 sourceScope 是实际 FrozenChannelSource（去除单条消息时间）的摘要，包含账号/配对代际、真实 sender 和线程。后续同来源可恢复，其他 sender、访客、重建账号或重新配对不命中。恢复逐项重新验证注册、授权、完整定义与当前累计 schema 预算，保留选择顺序；缺失、变化、撤权、超预算分别产生有界 absent/definition_changed/revoked/budget 诊断，只存当前来源 run。缺失项本次丢弃，不无限等待晚注册自动激活。

既有 tool_audits 的 capability_binding_json 保存 `tool-call-v1`：一个实际所用定义与 definitionFingerprint、是否已激活、request/run/role/session/source 标识和必要 builtin 作者摘要。未使用候选不会扩大每次调用记录。`SessionStore.call_binding(run_id, host_call_id)` 读新事实，旧链只读 fallback；旧 `capability_revision_for_call` 名称兼容，但新返回值不再是全目录。snapshotId/visibilityVersion 是请求诊断标识，不是授权凭证。

Runtime 为重复 provider call ID 分配唯一宿主 ID。消息、审计、TaskOrigin 使用宿主 ID；assistant metadata 保留 provider_tool_call_ids 以正确回放远端 ID/签名。重复宿主 audit ID 拒绝，不能覆盖已确认或 uncertain 事实；没有自动工具重放。

普通 Drift 作者使用仍活动的 write/edit audit 与真实用户、当前 author scope、builtin 内容 hash；自主作者使用现有 turn/call/bundle。authored_files 的新 writer_snapshot 内容是有限 drift-author-v1 proof，旧行保留。spawn 持有有限已选绑定，schedule 保留自己的有限跨重启定义，不依赖普通聊天目录链。meme provenance 的 capabilitySnapshotId 只关联原 run/call，不作为独立权限。

旧 capabilityRevisions/capabilityCalls、autonomy_capability_revisions 不再由生产运行新增；旧历史不删除、不全量改写。旧桌面激活仍按完整定义和当前权限恢复；旧渠道只有 session、缺少可信来源范围的缓存不会升级为当前 sender 权限。迁移/bridge 回滚边界见 [ADR-0026](../adr/0026-request-bound-lightweight-tool-loading.md)。

执行前与 pre hook 后重新检查当前许可并按请求固定 schema 验证最终参数。新增工具/风险/白名单不扩大旧运行，撤销及时收窄。

异步工具执行期间按 50ms 间隔复核授权及运行取消信号；撤权会取消尚未完成的工具任务、传递工具取消信号并等待清理，再记录拒绝结果；已经 settle 的真实回执优先保存，不因同时观察到撤权而改写。超时和父运行取消同样等待清理，局部工具拒绝不会取消父运行，也不会被记为成功。该机制依赖工具遵守异步取消或合作信号，不能撤回已经提交的外部事务，也不能抢占阻塞事件循环的同步代码。

停止与断开造成的连续父任务取消也不会中断已开始的工具清理；Runtime 保持等待实际工具终止，完成后再传播记住的取消。运行终态和角色运行锁的释放等待本次工具执行 task 及其结果持久化完成；这不等于所有 MCP 资源都已确认退出。未确认退出的 MCP owner 继续保留，独立显示 cleanup_failed 并允许重试，不改写已完成的业务终态，见 [MCP 服务契约](mcp-services.md)。

启用发现的渠道运行额外冻结真实来源：[FrozenChannelSource](../../backend/app/channels/source_authority.py) 从实际 run 的入站消息及线程读取账号、平台发言者和主人绑定代际，不接收模型自报身份。每次搜索与执行复核平台总开关及账号启用、私聊/群范围、屏蔽名单、线程归属及原 `identity_id`/`paired_at`/私聊授权起点。解绑后重新配对或删除后重建同 ID 账号不能复活旧运行。原外部回合即使随后配对也不提升权限。JSON 接口 `to_dict`/`from_dict` 供后续后台任务保存相同来源事实，日期使用带时区的 ISO 字符串。

工具审计由共享 `RuntimeManager` 写入，桌面及渠道入口均使用事件中的实际版本；HTTP 层只负责转发事件。

## 预授权服务发布接口

`ServiceGrant` 由宿主在运行开始提供，固定 grant/service/role 身份、风险上限及外部范围。`CapabilityResolution.stage_service_tools(context, grant_id, parent_snapshot_id, tools)` 供后续 MCP 连接宿主提交具体工具：

- grant 必须来自运行开始的授权，角色和父版本必须匹配；
- 工具来源必须是该服务的 `mcp:<service_id>`，风险和外部范围不能超出 grant；
- 不能覆盖原目录中的名称，也不能引入额外审批或宿主服务权限；
- 新定义先冻结为延迟候选，宿主普通调用结果持久化后才发布带父 request 标识的内存目录；之后可在当前运行搜索并调用。

该接口不接收模型 command/env。T12 的 MCP 宿主通过独立管理工具使用该接口，真实连接、调用、按来源恢复及整树清理见 [MCP 服务契约](mcp-services.md)。服务移除通过 `stage_service_removal(context, grant_id, parent_snapshot_id)` 暂存带父版本的注销修订；结果持久化后移除可见定义及候选，旧请求绑定保持不变但连接已失效。相同服务 grant 的重新添加可以恢复自己此前移除的名称，不能占用其他来源名称。

上游行为依据：[Shiori-Agent 固定版本 tool_search.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/tool_search.py)、[search_backend.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/search_backend.py)。归一化和评分规则改编自该 MIT 源码，协议接入 Meido 的宿主身份、不可变快照和持久化边界；来源许可见 [MIT License](../../third_party/shiori-agent/LICENSE)。

同 run 的 MCP 重连通过 host-only `rebind=True` 暂存新连接执行器，要求同服务 grant、完整同名声明集合完全相同；只在下一请求生效。旧 request 不能被重新连接到新 client。`activate=True` 的整服务加载/Drift mount 仍计累计声明预算，无法完整容纳则原子拒绝，不声称已加载部分未知工具。
