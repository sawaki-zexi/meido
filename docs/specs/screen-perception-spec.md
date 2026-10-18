# 角色按需屏幕感知

状态：已实施，待总集成两轴审查。第 3 节已于 2026-10-16 获人工批准；独立分支已通过公开协议/故障进程测试和 Windows 单屏受控原生验收。双屏、混合 DPI、真实锁屏/断开场景仍未验收，见[验收记录](../research/screen-perception-acceptance-2026-10-16.md)。

关联：[#160](https://github.com/sawaki-zexi/meido/issues/160)，Parent [#137](https://github.com/sawaki-zexi/meido/issues/137)，[完整规格 P04/T16/D04](shiori-tool-parity-spec.md)。最后核验：2026-10-16，Meido `17c2b28af5d643f3f90667973d72e4ca7f9fa1d5`，Shiori `4f266aeaf42fcd611362e2456417b91f659341cf`。

事实来源：[固定源码核验](../research/screen-perception-source-audit.md)、[运行契约](../reference/screen-perception.md)、[ADR-0001](../adr/0001-local-model-connection-boundary.md)、[ADR-0006](../adr/0006-provider-specific-model-adapters.md)、[ADR-0011](../adr/0011-versioned-tool-activation.md)、[ADR-0014](../adr/0014-explicit-screen-vision-binding.md)。本文保留批准时的“建议/计划”措辞；“已提取”描述固定上游基线。当前实现、验收证据与尚未验证的边界以运行契约及独立验收记录为准，不把架构批准当作全部运行时证据。

总体复刻目标及 D01–D03 的批准不替代本方案的架构批准。本票也不决定桌宠采用 Electron 还是 PySide6，不依赖 Computer Use 的控制驱动。

## 1. 用户结果与范围

计划：主人在“设置 → 插件 → 角色功能”选择角色和已保存的视觉模型连接，开启屏幕感知。桌面聊天中角色可调用 `observe_screen({})` 看一次主屏，当前对话仅得到经过视觉连接校验的文字摘要。关闭能力、停止回复或删除角色立即撤销在途观察的使用资格，并等待本机资源收尾。

仅按需观察，不持续录屏、不监听应用、不操作鼠标键盘、不生成桌宠动作。首个真实采集实现建议支持 Windows 用户交互桌面；其他平台显示具体不可用原因。后端在服务/容器/远程机器中运行不等于能观察浏览器所在设备的屏幕。

原始图像只在本次采集与独立视觉请求的内存中存在，不成为聊天附件或媒体文件。文字摘要可以像普通工具结果一样进入当前对话与其正常历史；不能把“原图不保存”解释为“所有屏幕内容绝不进入历史”。

## 2. 已提取的兼容契约

Shiori 输入 Schema：

```json
{"type":"object","properties":{},"additionalProperties":false}
```

Shiori 成功输出为 JSON 字符串，字段恰为：

```json
{"available":true,"interface_summary":"编辑器正在显示文档","activity_key":"writing"}
```

上游 summary 最长 400 字符，activity_key 最长 80；内部还有 targets/risks/bubble/experience_candidate，但最终工具不会把这些交给角色。上游是 loose JSON 校验、文本不脱敏；其 vision purpose 还允许绑定缺省时回退聊天 ID。精确源码和内部字段限制见 [核验表](../research/screen-perception-source-audit.md#已提取shiori-的实际契约)。

计划保留模型可调用的空对象输入和成功三字段语义。严格 JSON、明确的独立绑定、资源预算及可靠取消是本方案的加强，不能描述成上游已有全部保证。

计划工具 definition 为 risk=read_only、external_allowed=false、exposure=direct、source=builtin:screen_perception。符合宿主许可时直接可见，与上游 always_on 语义一致；开关未开或配置缺失时目录保留原因，运行能力集合不授予该工具。无需再加一个会与开关产生歧义的 enabledPlugins 许可。

## 3. 已批准的具体选择

| 决策 | 采用方案 | 取舍/其他选择 |
| --- | --- | --- |
| 配置归属 | `agentConfig.screenPerception` 同时存 enabled 与 visionModelConfigurationId；复用已有模型配置 ID。 | 一次角色保存原子更新开关与绑定；无需新凭据库或只转发字段的 endpoint。另一方案是 Role 顶层 vision 字段和单独绑定 endpoint，但会增加多请求保存的一致性处理。 |
| 视觉绑定 | 必须显式选择；可由主人明确选择与聊天相同的已保存 ID，仍是独立视觉请求。 | “独立绑定/用途”不等于“强制两个不同 ID”。无论选择何种 ID，都不自动取 chat/active 配置，也不把原图放入主聊天请求。 |
| 实际采集 | Windows 主屏，Pillow ImageGrab，短命 Python 子进程。 | Pillow 已是依赖；子进程换取可终止的真实采集 deadline。见第 5 节。 |
| 模型实现 | 直接复用已有 RoutedModelAdapter 的 rich stream 与 ModelAdapterRegistry。 | 保留现有十种协议/认证和图像编码，不再另写 vision HTTP gateway；能力与预算由屏幕模块约束。某个远端模型不支持图像时仍须明确报错。 |
| 模型输出 | 专用中性 prompt，内部严格两字段 JSON，只归纳界面与活动。 | 角色侧三字段结果与上游一致；不请求不消费上游最终也不输出的坐标、气泡和经验候选，减少无用途实现。见第 6 节。 |
| 许可范围 | 默认关闭；当前由主人发起的角色桌面回合。 | 群、访客、已配对私聊、定时/后台回合均不从桌面开关自动获得观察许可。若将来开放其他来源，另行给出显式授权设计。 |

维护者在本次对话明确回答“批准该方案，继续实施”，批准 Windows 首发、可显式同 ID、短命采集进程及严格内部两字段 JSON。该批准允许继续实现本票；运行时效果仍须按第 9 节验收，不覆盖桌宠宿主选择或向未授权外部视觉服务上传。

## 4. 配置、UI 与不可用反馈

计划扩展原有 AgentToolConfig：

```json
{"screenPerception":{"enabled":false,"visionModelConfigurationId":null}}
```

- 默认关闭；未迁移角色使用上述默认值。关闭保留已选 ID，清空绑定必须显式操作。角色不保存复制的 endpoint、模型名或密钥。
- 角色创建/更新复用既有 POST/PUT roles。开关与 ID 同一次保存；开启必须有存在且完整的保存连接，否则返回 422。禁用时允许空绑定。GET roles/role 原样返回该非秘密配置。
- 普通资料编辑、旧客户端未提供 screenPerception、角色卡导入只改资料时，保留原设置；显式完整 screenPerception 可替换。必须覆盖服务端省略字段行为，以及 draftFromRole/payloadFromDraft/创建草稿恢复，不能仅依赖 UI 保留。
- RoleEditor 能力区新增开关“允许按需观察主屏”和保存连接选择器；选项显示服务商、模型及协议，使用已有模型列表。缺连接提供进入模型设置的入口，不在角色中维护第二份密钥。
- 文案明确“画面发送到所选视觉连接，本地不保存原图；分析摘要会用于当前对话”。不在打开表单、获取目录或保存时截屏/探测模型。UI 没有原图预览按钮。
- 独立绑定没有隐式 fallback：先检查 ID 非空，再调用 ModelConfigurationStore.get(id)；不能触发 get(None) 的 active 配置语义。连接删除后保留失效 ID 并显示“连接已不存在”，要求重新选择，不改为另一个连接。
- 工具目录必须始终能解释关闭、未绑定、绑定失效、协议不可用、非 Windows 等状态。平台/配置满足仅显示“调用时验证”；目录健康检查不宣称模型已经支持视觉，不读取桌面。
- 已注册协议的图像编码支持与远端模型的真实视觉能力分开判断。未知/不支持图像的 adapter 在截图前拒绝；远端拒绝图片或认证失败时显示归一化失败，不伪造摘要，不将普通文本 ping 测试标为视觉测试。

`enabledTools`、插件列表或模型自行返回的授权字段不能绕过 screenPerception.enabled 和来源门禁。此独立开关应像既有宿主开关一样进入候选和执行两处检查；不重写 ADR-0011 的能力版本逻辑。

## 5. 一个 deep module 拥有完整观察

计划新增一个 `screen_perception` module，拥有采集、预算、视觉请求、输出校验、取消和图像释放。AgentTool 只将宿主绑定和 ToolContext 交给它，返回安全结果；主聊天装配不接触原图。

建议的 interface 只有三个操作：

```text
observe(context, expected_vision_configuration_id) -> Observation
invalidate(role_id=... 或 configuration_id=...) -> await 所有匹配观察收尾
close() -> 拒绝新观察并 await 全部收尾
```

expected ID 来自宿主冻结的工具候选，模型 schema 中不存在。context 仍用现有 role_id/session_key/run_id/signal，不能添加模型可覆盖的 role、来源或连接。模块自己验证真实 RoleStore、SessionStore 当前 run/用户消息和绑定，不把复杂生命周期散落到 caller。

宿主注入现有角色/模型配置读取、现有 rich model adapter，以及实际会变化的采集进程启动 callable（无参数，返回本模块拥有的 asyncio subprocess Process）。生产 callable 仅启动固定 Pillow worker；测试 callable 启动真实的受控 PNG/阻塞 fixture 子进程。模块始终自己读有界管道、验帧、kill/wait，因此测试不会替换自有 IPC/取消逻辑。该入口不能由模型、角色配置或外部渠道指定命令。HTTP 视觉服务是既有 AI 协议 seam，使用本地协议 fixture；不再给 adapter 套只有一次转发的 VisionGateway、CaptureManager、ResultValidator 层。IPC framing 和 JSON 校验是模块私有实现，不各自增加公共 interface。

### 5.1 采集方案比较

| 方案 | 结论 |
| --- | --- |
| 主事件循环直接 ImageGrab | 与上游最接近，但同步 OS 调用阻塞停止和其他请求；不选。 |
| ImageGrab 放线程 | 少一个进程，可保持事件循环响应；运行中的 native 调用无法强制取消，deadline 后可能继续保留原图，无法满足本票清理结果；不选。 |
| 短命子进程中的 Pillow | 建议。每次用当前 Python executable 启动受控模块入口，无 shell/用户参数；仅内存管道回一帧。超时/取消可以 kill 并 wait，消除迟到采集。增加解释器启动、IPC 和打包入口核验。 |
| Computer Use 原生驱动/桌宠 IPC | 可提供更多平台能力，但会引入驱动、安装及其控制/桌面 lease 知识；当前只读主屏不需要这些，不作为本票依赖。 |

建议 production worker 使用 `ImageGrab.grab(all_screens=False)`，不接收显示器、bbox、window 或路径参数。首发仅 Windows 10/11 交互桌面：查询当前进程 WTS session active/unlocked，并检查输入桌面；查询失败、session 0、断开、锁定/安全桌面、不可访问或尺寸无效均拒绝，不改截其他屏幕或缓存帧。仅 OpenInputDesktop 成功不能证明会话已连接，见源码核验的官方约束。主屏判定和 DPI 在 worker 中处理并核对实际返回尺寸；平台行为以受控 native 验收为准，不能仅凭参数名字宣布正确。

worker 用 `CREATE_NO_WINDOW`，stdin 关闭，受控最小环境保留系统必需变量（包括不区分大小写的 SYSTEMROOT），不继承模型密钥、用户命令或 CLI 配置。它只运行固定 Pillow 采集代码，不生成后代进程、不调用 Linux screenshot fallback。

IPC 仅为有长度前缀的 ≤1 KiB 元数据与 PNG 二进制；父进程逐段读并校验长度、真实 PNG 解码尺寸、EOF 和退出状态，不用 pickle，不先无界 communicate 再验长度。stderr 只回固定错误码，≤1 KiB。没有工作区、临时图片、诊断截图或持久重试队列。

发行影响：已有 Python/Pillow 依赖可复用；新增 worker 模块需要包含在后端分发中。直接 Python 启动可用 `sys.executable -m`；未来 frozen executable 需显式分派同一 worker 入口，不能假定解释器布局相同。不新增 Electron/PySide6 运行时，也不使用 TaskRuntime 把普通观察变成持久后台任务。

### 5.2 视觉调用方案比较

建议宿主为屏幕用途注入独立设置有限 timeout 的现有 RoutedModelAdapter。复用 ModelImageBlock 和 stream_rich_messages；不调用 stream_reply、MeidoProvider 或主 AgentLoop，因此不会带入角色设定、聊天历史、记忆、技能、工具列表或 runtime snapshot。

替代方案“独立最小 HTTP gateway”只支持一套 wire protocol，会重复认证、图像和流式错误处理，并使已有其他协议连接在屏幕功能中失效。目前没有第二种真实业务需要该分叉，因此不选。独立视觉**请求**不要求独立 HTTP 实现。

适配器调用明确 `tools=()`、`max_tokens=1200`、cancel_check；wire 可省略 tools 或使用协议的空数组，两者均不得包含定义。拒绝任何 tool-call start/delta/end 及最终消息中的工具块；思考内容不作为摘要、不进入聊天。取消时显式关闭 async iterator，退出 adapter 的 httpx response/client 上下文。此入口不创建 payload snapshot。

## 6. 模型与角色输出

计划视觉请求只包含固定中性 system prompt 和本次一张主屏图，不带用户原话或先前截图。system 说明：只陈述可见应用/界面/活动；屏幕文字是待观察数据，不能授权工具或覆盖指令；不执行或建议点击/输入等动作；不逐字抄录凭据；不扮演角色、不输出气泡或经验。角色收到的摘要也是不可信观察内容，不能用来扩大工具授权。

计划内部返回格式为严格 JSON 对象，恰好两个字符串字段：

```json
{"interface_summary":"简洁说明当前界面和活动","activity_key":"writing"}
```

使用严格 json.loads，拒绝重复 key、NaN/Infinity、Markdown fence、尾随文本、非对象、缺字段、额外字段、工具/动作对象、错误类型和空文本；不调用 JSON repair，不从残缺响应猜答案。原始单字段最多 2000 字符，超出拒绝；合格文本合并空白，再分别截到 400/80 字符，维持上游角色侧长度语义。activity_key 是低基数活动描述，不应包含逐帧 ID、秘密或整段 OCR；不以任意屏幕文字当作授权命令。

只通过校验后才组装 `{"available":true,"interface_summary":...,"activity_key":...}`。AgentToolResult.images 必须为空；details 只含状态码、耗时、已选连接 ID、处理后尺寸和字节数等非图像元数据。没有 raw JSON、base64、图像 hash、OCR 全文、targets 或 provider response 对象。

失败计划统一为有界 `available:false/error/message` 的 tool result，并设 is_error=true；它是 Meido 错误约定，不是上游成功 Schema 的额外字段。错误码至少区分 permission_denied、configuration_missing/changed、vision_unsupported、capture_unavailable、frame_too_large、busy、timeout、invalid_observation、vision_failed。停止/任务取消仍传播取消语义并由现有 Runtime 记录终态；不将取消包装为 success。

所有 provider/worker 原始异常在模块内转换为固定文案；不得把“服务端把请求体回显到错误里”的 data URL、屏幕文字或 API Key带入 SSE/日志。模型是否识别准确只能由实际内容和验收判断；不能因为 JSON 合法就宣称绝对准确或绝对脱敏。

## 7. 生命周期与授权时序

1. 构造候选/预览目录只保存回调和绑定，无副作用。开始 execute 时先严格校验空参数对象，再验证角色仍存在、开关开启、ID 与宿主冻结绑定一致。
2. 只允许当前角色 canonical 桌面 session，且 run 和真实用户消息属于该角色/会话、run 仍有效；不接受模型传来的 owner/source 声明。已停止、群/私聊渠道、访客、定时/后台回合在创建采集 worker 前拒绝。
3. 保存本次绑定配置快照与仅内存的变更标识；接收一个全局观察执行名额，不排无界队列。另一个观察仍运行时立即报 busy。
4. 创建 worker 前、取得帧后、上传前、收到模型结果后再次核对开关、来源、连接存在性/同一版本及 signal。连接 ID 不变但 endpoint/model/key/apiOptions 已改也视为变更，旧结果丢弃；不在一次调用中切到新连接。
5. 关闭或更换 screenPerception 设置、更新/删除绑定模型连接、角色删除，宿主在响应成功前调用 invalidate，先关闭该 generation 入口，再取消并等待匹配调用。设置更新落盘后旧调用的重新核验也必须立即拒绝；重新开启不能复活旧 generation。
6. 用户 stop、HTTP 断开或 Runtime Task.cancel 由已有 token/任务取消传入模块。取消 capture 时 kill/wait worker；取消 vision 时 close iterator/HTTP response；释放 buffer/image/配置引用与名额后才允许调用终止。重复取消不能跳过 finally 提前放开名额。
7. 应用 shutdown 调 close；角色 delete 的既有删除 gate 先阻止新观察，再等待 invalidate，最后删除角色数据。删除失败回滚角色不能自动恢复旧观察；下一次需新调用。stop/delete 返回成功时不应有仍可上传的 worker 或视觉请求。

“释放图像”指终止拥有它的进程、关闭流并撤销可达引用；Python/OS 可能产生不可原地擦除的内部副本，本方案不承诺安全擦除或远端服务已忘记收到的数据。取消不能撤回已经上传的字节；许可验证必须发生在上传前，撤销后不继续发送、不把迟到结果当作新观察。

## 8. 固定宿主预算

以下建议值均是宿主常量，模型不能传入覆盖，待受控 native 证据确认后才能调整并记录。

| 范围 | 建议限制与超限结果 |
| --- | --- |
| 并发/队列 | 全模块同时 1 次完整观察，零等待队列；名额仅在真实资源收尾后释放。 |
| 原始主屏 | 采集前查询尺寸，最多 16,777,216 pixels；实际图像再次检查。超限不采集/不发送；这限制像素，不宣称限制整个进程的 RSS。 |
| 发送尺寸 | 保持完整主屏长宽比，不裁剪；长边≤2048、总像素≤4,194,304。PNG 如过大可最多按长边 1536、1024 再编码两次，仍超限报错。 |
| PNG/编码 | PNG≤4 MiB，base64≤5,592,408 ASCII 字符；只向独立 vision 请求组装 data URL，父进程不保留另一个长期原图副本。 |
| capture | 5 秒，含解释器启动、截图、缩放、编码及 IPC；超时 kill/wait。 |
| vision | 最多 12 秒，且不超过剩余整体 deadline；max_tokens=1200 是请求提示，不能当作 provider 必然遵守的硬限制。 |
| 整体/收尾 | observe deadline 18 秒；取消/异常清理目标≤2 秒；ToolDefinition.timeout_seconds=20。达到清理预算仍必须保持 gate，不返回“已释放”的假成功；作为资源错误报告并保留真实进程等待监督。 |
| 模型文本 | 模块消费的累计解码文本（含被丢弃的思考）≤16 KiB；超限关闭流。严格校验前原始每字段≤2000字符，角色字段400/80。 |
| 工具结果 | 完整 JSON≤4 KiB，不截成非法 JSON；错误文案≤200字符；无图像附件。 |

已提取限制：现有通用 model adapter 没有统一的 HTTP wire body 字节上限，SSE 解析可能先形成一个较大的事件再交给 caller。上表 16 KiB 是**模块可消费的解码文本预算**，不是整个 HTTP/SDK 内存硬上限；本票不以新 gateway 假装修复所有 AI 协议的传输限制。整体 deadline 与有限模型 token 提示仍生效；若实施验收发现单事件超大构成实际问题，应将通用 adapter 的有界响应改动单独写清后审阅，不静默扩大本票范围。

原图生命周期不依赖缓存、重试队列或异步审计。每次显式调用重截新帧；失败、超时、断连均不退回上一帧。

## 9. 公开验收与红绿顺序

批准后以一条公开行为红例推动一条实现，不测试私有 parser/IPC helper、不 mock 自有观察流水线。沿用既定 AgentTool/HTTP 公开 seams；采集进程启动是本方案实际变化的系统依赖入口，远端模型使用既有协议 seam，真实 OS 路线另做 opt-in 验收。

| 可演示行为 | 公开 seam / 证据 |
| --- | --- |
| 新角色默认关闭；选择连接、关闭保留、省略字段保存资料、重启后 ID 不丢；失效 ID 不回退 active/chat | 角色/模型配置 HTTP + RoleStore 公开读取；前端表单交互。未知 ID 开启返回422，编辑其他字段不触发 capture/provider。 |
| `{}` 成功；额外 role/model/bbox/source 参数拒绝；群、访客、配对私聊、后台、错误 role/session/run 和关闭硬拒绝 | AgentTool.execute + 真实 SessionStore/run；外部 capture callable 调用次数证明拒绝发生在采集前。enabledTools 单独开启也不能绕过。 |
| 截受控图→真实模型协议→严格摘要，只把三字段结果给主对话 | AgentTool.execute/HTTP + 本地 OpenAI 兼容 vision fixture；另一个现有非 OpenAI 协议 fixture 验证不是新写死的 gateway。fixture 检验所选模型/图像、无工具定义、无角色/历史/记忆；返回可核验 marker。 |
| 原图不进主聊天、历史、普通审计、文件 | HTTP provider 双连接：主 provider 发 tool_call，独立 vision fixture 收图片，再次主 provider 只收摘要；SessionStore 公开历史、事件及审计公开读取中不得有 data URL/base64/原始 PNG 字节专用标记；检查受控 workspace/data/temp 无截图文件。可见文字可以成为合法摘要，不能用“画面文字绝不出现”代替原图隔离断言；正常用户附件有独立流程，不能把屏幕混为附件。 |
| 无效 JSON/fence/重复 key/未知字段/工具事件/action/超长输出/远端回显原图的错误 | AgentTool.execute + 模型协议 fixture 的实际响应；明确 is_error、固定安全文案、没有原始响应泄漏。合法超长摘要在原始字段预算内归一化到400/80。 |
| 截图阻塞/超字节/坏 PNG/尺寸不符、vision 长连；运行中关闭/换连接/删连接/stop/delete | AgentTool.execute + HTTP 控制动作；受控采集进程/视觉服务器使用事件控制。心跳证明 loop 响应；确认真实 child terminal、HTTP 断开、迟到输出不发布、名额不提前重用，连第二次取消也不留下上传工作。 |
| 主屏、DPI、锁屏、权限缺失、native cleanup | Windows opt-in：用户先在主屏打开仅含验收标记的受控窗口，确保其他敏感内容移开；真实生产 worker + 本地 vision fixture 解码并确认标记，双屏时副屏另一标记不得出现在帧中。默认 skip，明确开启后依赖缺失 fail；不上传真实桌面到付费/用户视觉服务。 |

受控 native 证据记录 OS、Pillow/Python 版本、显示器/DPI、处理后尺寸/字节、工具/HTTP 状态、取消时延和 worker 终态；不保存截图、base64、用户视觉 endpoint 密钥。实际采集须在具体方案获批后、受控桌面已准备好时进行。2026-10-16 已按此边界完成单屏主屏与取消验收；未采集任意用户桌面，也未向用户或付费视觉连接上传。

## 10. 变更落点与审批交付

计划落点：一个 backend screen_perception module（含固定 capture worker）；一个薄 ObserveScreenTool；现有 models/RoleStore 的配置保留；main 的候选/目录/配置撤销/删除/shutdown 局部装配；RoleEditor/draft/api types 的开关与选择器。使用当前 Pillow/httpx，不新增驱动、桌宠宿主、AI SDK 或持久观察存储。

现有正常聊天与其他工具继续使用原来的 model adapter；屏幕调用不得修改 active 模型、角色聊天绑定或已存在历史。实现前重新同步总 PR 的已验证集成基线，协调其他票对候选、角色保存和生命周期的改动；子 Issue 记录各自验收和依赖，统一在总 PR 审查。

第 3 节已获人工明确批准，决定记录到 ADR-0014。实现后继续按第 9 节公开 seams 做两轴审查及总集成回归；独立验收不替代尚未验证的平台场景。总 PR 的合并仍需单独批准。
