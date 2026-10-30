# 当前注册表执行与原文窗口工具恢复

状态：当前

最后核验：2026-10-30

事实来源：[发现协议](../../backend/app/agent_runtime/discovery.py)、[能力解析](../../backend/app/agent_runtime/capabilities.py)、[运行循环](../../backend/app/agent_runtime/loop.py)、[持久化宿主](../../backend/app/agent_runtime/runtime.py)、[公开工具/版本测试](../../tests/python/test_tool_discovery.py)、[搜索行为测试](../../tests/python/test_tool_search_relevance.py)、[HTTP 行为测试](../../tests/python/test_tool_discovery_api.py)。当前运行契约对应 [Issue #280](https://github.com/sawaki-zexi/meido/issues/280)、[ADR-0032](../adr/0032-current-tool-registry-and-raw-window.md)。

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

`ToolDefinition.namespace_summary` 是宿主提供的短元信息，不进入 provider 函数 schema；非空值随当前定义参与搜索；历史恢复只取原文中的名称。namespace 直接使用已核验来源 `mcp:<server_id>` 或 `plugin:<id>`，不会因显示别名相同而合并。缺少简介时保留来源身份及空摘要，不采用远端 instructions 作为系统政策。

小地图先按当前许可过滤，再按 namespace 排序输出 `{namespace, summary, discovery_route}`。摘要最多 256 UTF-8 bytes，完整渲染最多 4096 UTF-8 bytes（包含标题、JSON 分隔符、路由和遗漏提示），超限整项省略。遗漏数仅计算已授权来源。读取地图不连接服务、不加载 schema。索引每次从当前定义重新生成，无持久索引或第二套授权目录，也没有 provider prefix/cache 收益承诺。

### 完整声明预算

计量对象是每个完整 provider-neutral 声明 `{name, description, parameters}` 的稳定 JSON：key 排序、无多余空格、非 ASCII 原文编码成 UTF-8；取声明字节数之和。它不是输出文字上限，不是 token 数，也不是任意 provider wire 的完整请求大小。

- 单次搜索新增最多 **32768 bytes（32 KiB）**；累计当前可见声明目标上限 **131072 bytes（128 KiB）**。
- 必需直接声明的大小为 B，实际上限为 `max(B, 131072)`。B 达到或超过目标时保留全部基础工具，`baseline_over_budget=true`，拒绝新加载。
- 按排名/`select:` 的确定顺序，整工具放不下就跳过；继续尝试后续更小的候选。关键词的 `top_k` 限制预算筛选后的可加载匹配数，超大候选不占名额；找到足够结果后停止扫描。`select:` 不绕过预算，不裁剪 schema JSON，不自动卸载已有工具。
- `budget` 报告 `baseline_bytes`、`loaded_bytes`、`pending_bytes`、`new_bytes`、`total_bytes`、`new_limit_bytes`、`total_limit_bytes`、`effective_limit_bytes`、`remaining_bytes`、`remaining_new_bytes`、`baseline_over_budget`、`rejected_count`。没有伪装为实测的 token 估算。
- `skipped_due_to_budget` 只含本次已授权命中；结果过长时先缩短这份名称列表并标记 `skipped_names_truncated`，保留可加载匹配和拒绝计数。若仍需裁剪 `unlocked`，被裁掉的工具不激活、不占新增预算。
- 同请求暂存选择共享预留额度，同名定义只计一次；失败/取消释放预留，仅成功持久化的选择影响后续调用。已发给同一来源的声明即使随后撤权，仍占 provider 上下文字节直到移除，但不会再作为搜索结果/解释返回，执行仍重验当前许可。
- 恢复按当前原文窗口的首次出现顺序重新计量完整当前定义，仅使用累计限额（不是把恢复误算成一次 32 KiB 搜索）；超限条目不恢复。普通重复搜索不重置声明预算；压缩和重启按实际保留原文重新构造工具集合。

固定中英评测、字段消融、体积与增长实测见[检索元数据与预算评测](../research/tool-search-metadata-and-budgets.md)。

`unlocked` 是本次搜索实际解锁的名称。普通调用结果持久化后更新可见集合，同一 provider 回复按工具调用顺序执行：先成功搜索再调用可使用新工具；反向、失败或未解锁调用仍返回拒绝。下一模型请求携带对应的完整 schema。

## 当前执行、调用事实与原文恢复

每次调用按名称取得当前实现。执行钩子可以拒绝或改写参数；钩子结束后按当前工具、真实宿主来源和最终参数复验，再进入实现。同名注册替换、MCP 重连后下一调用采用当前定义；注销、禁用或来源不可用不能继续执行旧对象。已进入函数的调用自然结束，宿主停止/超时及工具自身的领域取消规则继续生效，不撤回或自动重放已经发生的动作。

常驻工具按注册顺序排列，再加入实际原文窗口中用过或解锁过的名称并稳定去重。`tool_window.window_tool_names` 消费同一份 Runtime transcript；`ContextWindowProvider` 应用真实 source view 与持久 cut 后重建声明，在压缩候选计量、最终请求和重启中使用相同规则。工作摘要不会授予工具可见性。自然回复保留宿主 `finish_reply`，格式纠正只提供该协议。

成功解锁写入原工具结果的 `metadata.unlockedTools`。旧搜索记录中的 `details.unlocked` 或原 JSON 仍可读取；其他工具结果不因自称 unlocked 而授予可见性。停止新增和依赖 `modelSnapshot.toolSelection`，旧 JSON 和历史列原样保留。已被压缩移出的原文、清空的历史或另一实际线程不会从选择缓存补回名称。

既有 tool_audits 的 `capability_binding_json` 保存 `tool-call-v1`：一个实际调用定义、指纹、可见事实、run/role/session/source 标识和必要 builtin 作者摘要。指纹仅供历史审计，不是当前执行门槛。`SessionStore.call_binding` 兼容读取旧链；snapshotId/visibilityVersion 是诊断与来源事实标识，不是独立授权凭证。重复 provider call ID 仍映射唯一宿主 ID；重复宿主审计拒绝，不覆盖 uncertain 事实，不重放工具。

spawn 保留选定名称范围与原宿主身份，执行查当前注册表；soft schedule 保存名称范围，旧完整定义只读取名称，执行仍受当前许可限制。Drift 作者使用实际活动 write/edit 调用、真实用户/来源、当前作者许可与 builtin 内容证明，保持原产物和历史事实，不以旧执行器或选择指纹授予新调用。

停止与连续取消仍等待本次工具 task 及结果保存、资源清理后释放运行锁。MCP 未确认退出的 owner 保留原 cleanup 状态并支持重试；业务终态与资源退出分开，见 [MCP 服务契约](mcp-services.md)。

启用发现的渠道运行额外冻结真实来源：[FrozenChannelSource](../../backend/app/channels/source_authority.py) 从实际 run 的入站消息及线程读取账号、平台发言者和主人绑定代际，不接收模型自报身份。每次搜索与执行复核平台总开关及账号启用、私聊/群范围、屏蔽名单、线程归属及原 `identity_id`/`paired_at`/私聊授权起点。解绑后重新配对或删除后重建同 ID 账号不能复活旧运行。原外部回合即使随后配对也不提升权限。JSON 接口 `to_dict`/`from_dict` 供后续后台任务保存相同来源事实，日期使用带时区的 ISO 字符串。

工具审计由共享 `RuntimeManager` 写入，桌面及渠道入口均使用事件中的实际版本；HTTP 层只负责转发事件。

## 预授权服务发布接口

`ServiceGrant` 由宿主在运行开始提供，固定 grant/service/role 身份、风险上限及外部范围。`CapabilityResolution.stage_service_tools(context, grant_id, parent_snapshot_id, tools)` 供后续 MCP 连接宿主提交具体工具：

- grant 必须来自运行开始的授权，角色和父版本必须匹配；
- 工具来源必须是该服务的 `mcp:<service_id>`，风险和外部范围不能超出 grant；
- 不能占用其他来源的名称，也不能引入额外审批或宿主服务权限；
- 新定义作为当前延迟候选，宿主普通调用结果持久化后才发布带父 request 标识的内存目录；之后可在当前运行搜索并调用。

该接口不接收模型 command/env。T12 的 MCP 宿主通过独立管理工具使用该接口，真实连接、调用、按来源恢复及整树清理见 [MCP 服务契约](mcp-services.md)。服务移除通过 `stage_service_removal(context, grant_id, parent_snapshot_id)` 暂存带父版本的注销修订；结果持久化后移除可见定义及候选，后续调用无法查到已移除实现。相同服务 grant 的重新添加可以恢复自己此前移除的名称，不能占用其他来源名称。

上游行为依据：[Shiori-Agent 固定版本 tool_search.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/tool_search.py)、[search_backend.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/search_backend.py)。归一化和评分规则改编自该 MIT 源码，协议接入 Meido 的宿主身份、原调用回执和持久化边界；来源许可见 [MIT License](../../third_party/shiori-agent/LICENSE)。

同 run 的 MCP 重连通过 host-only `rebind=True` 更新同一服务 grant 的当前目录；允许远端 schema 更新，下一调用按新定义验证，不要求等下次模型请求。旧 peer 已进入的调用不能关闭替代 peer，也不自动重放。`activate=True` 的整服务加载/Drift mount 仍计累计声明预算，无法完整容纳时原子拒绝。
