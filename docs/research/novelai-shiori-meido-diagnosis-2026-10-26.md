# NovelAI 生图可用性与 Shiori 对比诊断

状态：当前（调研结论，不代表线上付费生图验收）

最后核验：2026-10-26，Asia/Shanghai。

事实来源：Shiori-Agent 固定提交 [`d149de7f052f77c5bb88d4d1393d48dce1870875`](https://github.com/YinFengWindy/Shiori-Agent/commit/d149de7f052f77c5bb88d4d1393d48dce1870875)、NovelAI 官方文档、Meido 正式版本 [`048c8ee7001e3557d4f8f6075fb74c5de5a6dafd`](https://github.com/sawaki-zexi/meido/commit/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd) 的源码、只读接口和会话审计，以及独立临时数据上的测试。

**结论：Meido 已实现 NovelAI 生图，但本机 Hiyori 当前无法使用，直接原因是生图消费许可未对当前设置生效。** 服务开关和凭据状态正常，服务配置版本为 5，消费确认版本为 0，宿主因此在两次相关回合中都排除了 `generate_image`。随后出现的「模型生成失败」发生在工具搜索后的聊天模型阶段，两次对话均未调用 NovelAI；现有审计没有留下这次聊天失败的原始原因。

隔离检查有 11 项通过，覆盖配置、工具调用、文生图、图生图和原回复附图。只改变合成配置的消费确认，准入就从阻断变为可用。本次没有真实付费生成，因此尚不能判断已保存 Token 的有效性、账户额度或上游出图是否成功。

| 对比点 | Shiori 当前源码 | Meido 正式版本与本次状态 |
| --- | --- | --- |
| 生图能力 | 插件已实现聊天工具、工作台、历史与自动 CG | 同类入口均已实现；本次卡在聊天与手动生图的公共准入 |
| 工具可见性 | 启用后 `always_on=True`，另受回合策略筛选 | `exposure=direct`，但当前消费许可未生效，所以不向模型提供 |
| 消费许可 | NovelAI 插件没有 Meido 的消费确认版本字段 | 消费确认绑定服务版本；改参数或 Token 会使确认失效 |
| 用户看到的状态 | 工作台显示本地 readiness，并区分若干生图错误 | 设置仍显示「已启用」「独立凭据已配置」，缺少显著的最终可用状态 |
| 本次失败位置 | 不适用 | `tool_search` 成功后第二轮聊天失败；NovelAI 生成记录为零 |

表中事实的固定源码及实测证据见下文。角色回复中的能力自述不能代替工具目录和调用记录。

## Shiori 的生图入口

**已提取：Shiori 已有完整的 NovelAI 文生图、图生图、生成历史、图片重生成和自动场景 CG 实现。** 当前代码归属 `plugins/novelai/`；插件默认关闭，显式启用后装配服务、工具、RPC 和自动 CG 订阅。宿主读取 `[plugins.novelai].enabled`，缺省时使用 manifest 的 `default_enabled: false`。Token 是独立配置字段；插件开关不等于 Token 已有效。[插件声明](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/manifest.yaml#L1-L22)、[启用判定](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/plugin_host/kernel.py#L102-L109)、[装配](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/plugin.py#L107-L199)。

| 入口 | 发起过程 | 聊天模型依赖 |
| --- | --- | --- |
| 聊天中请求图片 | 聊天模型调用 `generate_image`，工具调用同一 NovelAI 服务 | 需要聊天模型正确产生工具调用；生图本身由 NovelAI 执行 |
| 生图工作台 | 表单调用 `plugin.novelai.generate`，直接进入 NovelAI 服务 | 手动填写 tags 后不需要聊天模型先批准或翻译 |
| 自动场景 CG | 场景观察提交事件，角色开启自动 CG 后由 light 模型转换 tags，再调用生图 | 除场景判断外，还需要 light 模型完成提示词转换 |

来源：[聊天工具](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/tool.py#L13-L159)、[工作台 RPC](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/rpc.py#L44-L84)、[自动 CG 准入](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/auto_cg_controller.py#L86-L125)、[提示词转换](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/auto_cg_controller.py#L139-L184)。

**已提取：角色自动 CG 开关只控制自动触发，不负责注册手动生图工具。** 插件 setup 无条件注册 `generate_image`；自动 CG 控制器另查角色偏好。因此，不能把「关闭自动 CG」解释为「不能按用户请求生图」。上述三条入口也不能合并验收：工作台成功只能证明工作台路径，不证明聊天模型会调用工具。

## Shiori 如何让模型知道能生图

`generate_image` 注册时明确设置：

```python
risk="external-side-effect"
always_on=True
search_hint="生图 生成图片 NovelAI 立绘 场景图"
external_allowed=True
```

**已提取：在插件启用且当前回合策略允许时，Shiori 会从首轮就向聊天模型提供生图工具。** 即使启用了工具搜索，初始可见集合仍包含允许的 `always_on` 工具；无需先搜到 `generate_image` 才能发现它。`external_allowed=True` 是 Shiori 对外部群聊的自身产品决定，不是 Meido 的外部账号或消费授权。[注册事实](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/plugin.py#L149-L156)、[初始可见集合](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/tool_visibility.py#L7-L31)、[每次请求筛选](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/reasoning_loop.py#L217-L246)。

Shiori 的运行提示还要求：工具已可见则直接调用，已知名称但不可见则用 `tool_search(query="select:工具名")` 加载，未搜索前不得断言没有能力。若模型直接调用尚未加载的 deferred 工具，运行时会返回明确的加载指引。**推断：这两层设计减少了把「当前未加载」误说成「系统完全没有」的机会；它们不保证所有模型都遵守。** [运行提示](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/prompts/agent.py#L71-L73)、[未加载纠正](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/reasoning_loop.py#L594-L600)。

## NovelAI 请求与结果

**已提取：Shiori 默认地址是 `https://image.novelai.net`，请求 `POST /ai/generate-image`，使用 `Authorization: Bearer <token>`。** JSON 包含 `input`、`model`、`action`、`parameters`；文生图 `action=generate`，图生图 `action=img2img`。默认模型为 `nai-diffusion-4-5-curated`，默认限制为单张 1,048,576 像素、28 步、1 张。V4.5 参数另包含 `params_version=3`、`v4_prompt`、`v4_negative_prompt` 等。[HTTP 客户端](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/client.py#L18-L42)、[默认配置](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/models.py#L10-L26)、[模式转换](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L320-L329)、[参数构造](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L392-L445)。

工具要求模型把中文需求转换成逗号分隔的英文 tags，服务在外发前校验提示词、模式、尺寸和步数。这是 Shiori 当前的应用约束；不能把它说成 NovelAI 所有模型一律不支持自然语言。官方模型文档描述了 V4.5 的自然语言理解能力，当前网页也列出 V5；Shiori 本快照的专用参数构造仍只匹配 `nai-diffusion-4-5`，不能因官网已有 V5 就断言本集成已兼容 V5。[工具说明](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/tool.py#L16-L45)、[服务校验](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L75-L119)、[模型判定](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L513-L514)、[NovelAI 官方模型文档](https://docs.novelai.net/en/image/models/)。

成功响应支持 ZIP、JSON base64 图片和直接图片字节；服务提取并验证图片，保存图片、原始请求与元数据。聊天插件在工具成功时记录图片路径，随后将图片附加到该会话的回复；如果已由 `message_push` 发送，则移除待附加项，避免重复。[响应解析](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L460-L510)、[持久化](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L214-L307)、[聊天图片关联](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/plugin.py#L41-L104)。

## 配置状态和错误解释

Shiori 工作台的 `plugin.novelai.status` 是无网络的本地配置检查：空 Token 或未展开的 `${ENV}` 返回未配置，其余非空字符串返回已配置。**「已配置」不代表 Token 通过服务器认证，也不代表账户有额度。** NovelAI 官方说明 Persistent API Token 来自账户设置，重新生成会使旧 Token 失效。[本地状态](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/rpc.py#L81-L84)、[Token 判定](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/failures.py#L34-L69)、[官方 Token 说明](https://docs.novelai.net/en/text/usersettings/account/)。

工作台先显示本地 readiness；提交失败时通过稳定错误码展示具体原因，并对凭据问题提供设置入口。

| 情况 | Shiori 错误分类 | 工作台提示 |
| --- | --- | --- |
| Token 为空或环境变量未展开 | `novelai_not_configured` | NovelAI 未配置 |
| 上游 HTTP 401 或 403 | `novelai_unauthorized` | NovelAI token 无效 |
| 上游 HTTP 402 | `novelai_quota` | NovelAI 订阅或额度不足 |
| 超时或网络连接错误 | `novelai_network` | 连不上 NovelAI |
| 其他上游错误或不可读图片 | `novelai_upstream` | NovelAI 返回了错误 |

来源：[后端错误映射](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/failures.py#L97-L137)、[前端文案与设置入口](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/generationFailure.ts#L34-L91)、[生成与 readiness](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/novelAiGeneration.ts#L33-L89)。这些是工作台 RPC 路径的保证，不能直接套作所有聊天失败的显示行为。

自动 CG 的后台失败会被独立记录，不把已完成的主聊天回复改成失败。控制器对生图保留一次重试；这只是 Shiori 的现状，移植到有消费成本的系统时仍需遵守 Meido 自身的幂等与不确定结果约束。[后台调度](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/auto_cg_controller.py#L108-L125)、[生图重试和失败收尾](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/auto_cg_controller.py#L219-L268)。

## Meido 正式实例实测

### 版本与有效配置

`D:\meido\Meido.ps1 -Action Status` 返回正式发布版本 `048c8ee7001e3557d4f8f6075fb74c5de5a6dafd`，代码目录为 `D:\meido\.runtime\releases\048c8ee7001e-20261026-153206-99fc25`，API 为 `http://127.0.0.1:4288`。本次核查使用这个版本；主工作区的旧功能分支与未提交内容不是运行事实来源。

只读 `GET /api/services/novelai` 和 Hiyori 工具目录得到：

| 检查项 | 实测值 | 意义 |
| --- | --- | --- |
| `enabled` / `manualEnabled` | `true` / `true` | 服务及聊天、手动用途已打开 |
| `tokenConfigured` / `credentialCleanupPending` | `true` / `false` | 凭据已保存，无待清理状态；不是远端认证成功证明 |
| `revision` / `manualConsentRevision` | `5` / `0` | 当前配置没有有效的生图消费确认 |
| 每角色 / 账户每日尝试上限 | `5` / `20` | 当前有限次数配置 |
| Hiyori `generate_image.registered` / `exposure` | `true` / `direct` | 工具已经实现并注册，正常准入后首轮就可见 |
| `enabled` / `authorized` / `modelVisible` | 均为 `false` | 宿主在提供模型工具列表前排除该工具 |
| 工具不可用原因 | `请主人确认当前服务设置的有限生图消费许可` | 当前真正的阻断条件 |
| 生图历史及当天使用量 | 历史空；账户、手动、CG 尝试均为 `0` | 没有进入生成尝试账本 |

这里不只是用当前配置推测过去：15:39 和 15:40 两次会话各自保存的能力快照均记录 `generate_image: denied:disabled`，初始 25 个工具及候选集合均没有 `generate_image`。这是截图当时模型确实没收到生图工具的直接证据。

源码对应：[`unavailable_reason`](https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/novelai_generation/application.py#L130-L149) 检查服务级许可与额度；[`_enabled_runtime_tool_names`](https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/main.py#L4905-L4915) 只在检查通过后添加生图工具；[工具定义](https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/novelai_generation/tools.py#L26-L36) 明确为 `direct`。

### 消费确认为什么容易丢失

**已提取：** 修改生成参数会使旧确认失效；保存新 Token 也会使确认失效。前端所有参数编辑都会先将当前确认框取消，然后保存时明确发送 `confirmManualConsumption: false`，除非用户在最后一次修改后重新勾选。后端没有配置变更审计，不能从版本 5 和确认 0 还原究竟是从未确认，还是后续修改清除了确认。[后端保存规则](https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/novelai_generation/config.py#L92-L144)、[前端保存与确认重置](https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/web/src/features/novelai/NovelaiSettings.tsx#L22-L52)。

**已提取：** 设置页标题徽标只判断 `enabled`，凭据徽标只判断 `tokenConfigured` 和清理状态；它们没有表示消费确认是否有效。即使仍然不能生图，页面也可以同时显示「已启用」「独立凭据已配置」和「NovelAI 生图设置已保存」。**推断：这是本次“配置了却说不能用”的主要交互缺口。** [状态展示](https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/web/src/features/novelai/NovelaiSettings.tsx#L51-L77)、[插件列表只读取启用值](https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/web/src/features/settings/PluginSettings.tsx#L41-L55)。

Hiyori 旧角色字段 `novelai.manualEnabled=false` 不是当前聊天生图的阻断原因：这一版本的手动与聊天准入读取服务级 `manualEnabled` / `manualConsentRevision`；角色级 `sceneCgEnabled`、辅助模型和 `consentRevision` 用于自动 CG。不能建议用户到角色面板反复修改旧的手动开关来解决本次问题。[当前准入分支](https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/novelai_generation/application.py#L137-L157)。

### 截图中第二次失败的实际调用链

时间均为 2026-10-26 Asia/Shanghai，证据来自会话数据库以 SQLite `mode=ro` 读取的 `agent_runs`、`messages`、`tool_audits` 和各回合的能力快照，未复制完整对话到文档。

1. **15:39:43**：询问「你会生图吗」的回合正常完成；快照中生图工具被禁用。
2. **15:40:41**：用户追问已配置的 NovelAI。该回合仍从不含生图工具的 25 个工具开始，使用 `deepseek-flash`。
3. **15:41:00–15:41:01**：模型调用 `tool_search`，查询 `生图 图像生成 画图 image generation noval nova`，`top_k=10`。工具审计为成功。
4. 搜索返回并加载了 10 个 `agent_browser_*` 工具，第一项截图因 `image` 与「图」匹配，其余多项仅因「图」匹配。结果没有 `generate_image`，因为它已在准入阶段被排除。
5. **15:41:07**：第二轮聊天模型阶段结束为 `failed`，最后回复无内容。`agent_runs.error` 为 `NULL`，最后消息只保留 `runtimeStopReason=error`、`runtimeReason=failed`，没有原始错误类型、HTTP 状态或上游 request ID。该回合唯一工具审计是成功的 `tool_search`。

**已提取：** 本次报错不来自 NovelAI 的生图 HTTP 请求。**尚不能确定：** 聊天模型阶段具体是网络、协议、服务端还是本地适配错误；也不能只根据发生顺序，就断言加载浏览器工具导致了模型失败。运行日志仅有正常接口访问，没有可用的该次异常详情。

源码存在诊断信息丢失的路径：模型适配器对普通异常只取 `str(error)`，运行时也把字符串继续向上传；空字符串异常因此可能到达通用「模型生成失败」兜底。它解释了这种信息不足如何发生，但不证明本次一定抛出了哪种异常。[适配器错误转换](https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/ai/pi_openai_adapter.py#L601-L623)、[运行时错误处理](https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/agent_runtime/loop.py#L142-L154)、[界面错误事件兜底](https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/main.py#L6862-L6878)。

### 复现与因果验证

最小只读检查可以重复发现当前症状，消费许可生效后会转绿；不读取凭据，不调用聊天模型或 NovelAI：

```powershell
$roles = Invoke-RestMethod 'http://127.0.0.1:4288/api/roles'
$role = $roles.roles | Where-Object name -eq 'hiyori'
$catalog = Invoke-RestMethod "http://127.0.0.1:4288/api/roles/$($role.id)/tools"
$generation = $catalog.tools | Where-Object name -eq 'generate_image'
if (-not $generation.modelVisible) { throw $generation.reason }
```

已执行两次同等接口检查，均返回 `modelVisible=false` 和 `请主人确认当前服务设置的有限生图消费许可`；带断言检查退出码为 1。

另在独立临时 SQLite 和内存假凭据上运行正式版本的 `NovelaiSettingsStore` 与 `NovelaiApplication.unavailable_reason`，角色旧手动开关始终保持 `false`：

```text
BEFORE: blocked; role manual flag remains false
AFTER: admitted; changed only global consumption confirmation
UNCHANGED SETTINGS: admission preserved
PARAMETER CHANGE: blocked again
credential_reads=0; external_requests=0
PASS; isolated temporary data cleaned
```

第一次运行的业务断言全部通过，但 Windows 上临时 SQLite 连接未及时回收，清理目录失败；显式回收连接后重跑通过，新临时目录正常清理。第一次留下的 `C:\Users\Elysia\AppData\Local\Temp\meido-consent-probe-nw_fyo22\isolated.db` 仅含合成设置，无真实凭据；后续自动审批以「blocked by policy」拒绝清理，文件保持原样。这没有涉及正式数据库。

在正式版本源码上，使用全新临时 `MEIDO_DATA_DIR`、`MEIDO_ROLES_DIR`、`MEIDO_OWNER_KNOWLEDGE_DATA_DIR`，禁用字节码和 pytest 缓存写入，并沿用测试夹具的非本机网络阻断，执行：

```text
python -m pytest -q -p no:cacheprovider --basetemp <独立临时目录>
  tests/python/test_novelai_configuration.py
  tests/python/test_novelai_tools.py
  tests/python/test_novelai_main.py

11 passed in 16.84s
```

其中公开 HTTP 对话测试覆盖 txt2img、img2img、模型可见生图工具、一次生成 POST、后续模型回合、原回复唯一图片及历史重建；模型服务和 NovelAI 均为本机夹具。[完整调用链测试](https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/tests/python/test_novelai_main.py#L10-L93)。这些结果证明本地实现路径可通，不证明真实账号出图成功。

### 当前恢复入口与修复范围

用户可在 **设置 → 插件 → NovelAI 生图 → 设置**，完成所有参数修改后，勾选 **「我确认生图消费」**，点击 **「保存生图设置」**。保留当前每角色 5 次、账户 20 次上限即可解决已查明的准入阻断；无需重装工具或重启服务。随后以只读工具目录确认 `generate_image.modelVisible=true`，新聊天回合才会获得该能力。真实出图仍需单独核验供应商返回。

代码修复应优先处理以下用户可见行为：

- **最终可用状态**：启用、凭据、用途、确认和额度共同决定状态；未确认时明显显示「待确认消费，聊天暂不可用」，保存成功后保留这一提示。
- **能力说明**：向模型提供不含秘密的已配置能力状态与阻断原因，允许准确解释“已接入但待确认”，保持执行准入检查。
- **搜索结果**：避免「图」等宽泛命中把生图查询加载成整组浏览器工具；区分发现结果和不可调用的能力诊断。
- **聊天失败诊断**：保留经过脱敏的阶段、异常类型、非空原因和适用的服务端标识；让界面区分聊天失败与图片生成失败。

已检查相关 Issue #171、#172、#209、#211 及开放 PR。草稿 [PR #244](https://github.com/sawaki-zexi/meido/pull/244) 目标为 `main`，涉及工具检索、加载、模型终态和执行进度；后续搜索与模型错误改动应先复用该工作。其改动文件未包含 `novelai_generation/config.py` 或 `NovelaiSettings.tsx`，不能把它当作本次消费确认和状态展示问题已经解决的证据。

本次仅新增调研文档，未修改正式配置、读取真实 Token、调用付费生成、切换正式版本或创建外部 Issue/评论。
