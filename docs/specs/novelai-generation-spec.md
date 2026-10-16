# NovelAI 生图、历史与人类回合场景 CG

状态：当前规格，第 3 节 D05/P06/T19 一揽子架构已于 2026-10-16 获主人明确批准，实施进行中；仅覆盖实现、隔离本地构建与无费用 fixture，真实生成和费用另批。所有“采用/必须”仍是验收目标，不表示已实现。决定见 [ADR-0016](../adr/0016-bounded-novelai-generation.md)。

关联：[Parent #137](https://github.com/sawaki-zexi/meido/issues/137)、[总规格 P06/D05/T19](shiori-tool-parity-spec.md)、[源码与官方契约调研](../research/novelai-generation-portability-2026-10-16.md)。最后核验：2026-10-16，Meido `504d5b0b307736c14da7d1da87a1bad36e010c76`，Shiori `4f266aeaf42fcd611362e2456417b91f659341cf`。

## 1. 用户结果与完整范围

主人独立配置 NovelAI 服务并批准角色的有限消费额度后，角色在主人真实输入的当前回合中按需 txt2img 或使用当前获授权底图 img2img。图片保存为角色附件，在原会话显示；后台生成完成后也只回原来源。主人能浏览持久历史、查看实际参数并明确发起一次新的重生成，替换指定消息图片而不影响其他附件。

主人另行开启人类回合场景 CG 并选择辅助模型后，宿主从该回合已完成的对话提交场景观察，按场景变化、视觉去重和冷却生成图片。手动生图、CG、历史、重生成和媒体单次消费都属于本票，不能以只有健康检查或只有一个工具名称验收。

首版允许桌面及当前渠道权限仍有效的、宿主确认的主人输入回合；主人在群中发言仍只使用当前群输入并回当前群。访客、群友、旁听、定时任务、Drift、内部完成事件不能借用角色开关消费；不宣称完成上游 external_allowed=true 的全部访客范围。扩展访客消费须单独提出权限方案。

无人值守主动 CG 是未验证扩展。官方公开接口要求生成由 human action 发起；持续开关不视为覆盖该要求，用户批准也不能替代供应商契约。当前范围实现“实际人类输入触发的自动 CG”，不引入无输入的后台轮询生成；不以此删除整个 CG 工作流。[官方接口说明](https://image.novelai.net/docs/doc.json)。

## 2. 已有事实与复用边界

- [TaskRuntime](../../backend/app/task_runtime/runtime.py#L129) 已有 submit/query/list/stop、TaskOrigin、默认全局8/每角色3并发及每任务600秒硬上限；启动后不盲重跑 interrupted 任务。本文取证基线未含 submission_key；#158 的最终源快照 `e992e0f` 已完成幂等交接、结果保留/ACK 并通过两轴复核，本轮总集成后复用同一 seam，不另造第三套交接协议。
- [ToolContext](../../backend/app/agent_runtime/tools.py#L78) 只有宿主 role/session/run/call/取消信息；渠道授权通过 [FrozenChannelSource](../../backend/app/channels/source_authority.py#L39) 捕获真实账号、发送者、配对与来源代际，后续检查只能收回权限。
- [SystemCredentialStore](../../backend/app/credentials.py#L17)、[角色附件](../../backend/app/chat_attachments.py#L90)、[SessionStore](../../backend/app/session_store.py#L151) 和 [ChatView](../../web/src/features/chat/ChatView.tsx#L21) 可复用。普通 update_message 没有附件 CAS；TaskCompletionWorker 当前回灌文本，不能直接声称它已可靠交付图片。
- [ChannelHost.account_send](../../backend/app/channels/host.py#L253) 支持 attachments、attachment_ids、原来源和授权回调；adapter.send 返回 [DeliveryResult](../../backend/app/channels/models.py#L111)，状态为 pending/sent/failed/uncertain。媒体能力依具体 adapter 核验，不伪造所有平台均支持图片。
- 辅助模型复用 [RoutedModelAdapter.stream_rich_messages](../../backend/app/ai/model_adapter_registry.py#L122) 与已有稳定配置 ID；不新建聊天代理，不把 NovelAI token 当模型连接。当前没有本票的生成服务或宿主场景事件。

依循 [ADR-0009](../adr/0009-multichannel-conversation-threads.md)、[ADR-0011](../adr/0011-versioned-tool-activation.md)、[ADR-0012](../adr/0012-owned-task-runtime.md)。适配保留上游 MIT 署名；服务和模型适用独立条款，不能从源码许可证推导账号或图片权利。

## 3. 已批准的一揽子具体方案

| 决策 | 本草案采用的方案 |
| --- | --- |
| 服务与凭据 | 一个应用级 NovelAI 服务，固定 https://image.novelai.net；token 存独立系统 vault 键。生产不开放自定义 endpoint；不读取角色聊天密钥或整进程环境。 |
| 启用与消费许可 | 服务、角色 manual、角色 auto-CG、角色资产写回均默认关。开启 manual/CG 时主人必须确认该用途的有限消费许可及“次数不是 Anlas 估价”；仅 role enabled 不授予无限消费。 |
| 当前来源 | 只接真实主人当前输入或主人明确的 UI 生成/重生成操作；渠道沿用被冻结的 owner_context、账号/配对代际与线程授权，禁止模型自报身份。 |
| 模型与参数 | allowedModels 默认仅 nai-diffusion-4-5-curated；主人可显式加入 nai-diffusion-4-5-full。默认单张、1,048,576像素、28步、k_euler_ancestral；不自动切换模型或升级版本。 |
| 有限额度 | 默认账户合计20次/UTC日，每角色 manual含重生成10次/日、CG含辅助调用3次/日；同一真实输入回合最多一个生成意图。均为本地尝试上限，不是官方费用或免费额度。 |
| 并发与期限 | 每 attempt 使用现有 TaskRuntime，kind=novelai_image；预算300秒，受现有≤600秒硬上限、全局8/角色3配额约束；另加同 NovelAI 账户最多1个在途 attempt，忙时明确返回busy。 |
| 付费事务 | 持久 generation/attempt/dispatch 状态，当前授权与有限额度原子预占；每 attempt 最多一次生成 POST。seed、correlation ID 不是扣费幂等键。 |
| 不确定结果 | dispatch 后 timeout/cancel/restart/无法核实的响应为 unknown，保留消费占用，不重发、不声称退款；用户须另发新的明确重生成动作才能再次消费。 |
| 媒体归属 | img2img 只接当前来源允许的 attachmentId；无 raw path/URL。结果为角色附件ID；持久媒体 outbox 只附原消息或回原来源一次，uncertain 不改投。 |
| 本地故障 | 保存、消息附图、附件 CAS、receipt 同步失败仅恢复本地步骤；已收图后不再生成。失败图保留在归属历史中供主人处理。 |
| CG | 仅当前人类输入回合的宿主场景观察；独立显式辅助模型ID、一次≤600 token调用、五回合冷却、scene/visual去重、manual互斥。同来源新主人输入先使旧CG候选及其发布资格失效，等待其本地取消收尾后才准入新CG。无人值守主动扩展不启用。 |
| 有界资源 | 输入10MiB；响应压缩32MiB、总解压64MiB、最多16项；每张输出像素≤本次批准值，正常只请求一张。生成媒体角色1GiB/全局5GiB上限，满时拒绝新消费。 |
| 自动资产写回 | 独立默认关；获准后只写当前角色自有图片并按角色资产API更新，不由图片元数据指定路径，不因开启CG自动取得此权限。 |

以上为一次可批准的方案，常数是可逆的首版产品界限，实施不再要求逐个常数确认。调整必须保持有限值、完整校验和原有权限边界，增加外部消费范围或放开无人值守 CG 则另审。

UTC日计数由持久账本记录，时钟回退不清空已用额度；提高/降低上限不抹掉历史计数。unknown 不因跨日恢复为可重发，新的消费仍受当日额度。CG辅助模型已出网即占一次CG尝试，即便最终无图；确定在任何外部请求前取消/拒绝才释放该预占。

## 4. 配置、工具与应用 API

计划服务配置公开投影为 enabled、tokenConfigured、allowedModels、defaultModel、allowedSamplers、defaultSampler、maxPixels、maxSteps、dailyAttemptLimit、revision；不返回token。token仅接受明确设置/清空操作，使用严格清理的独立 vault 键；普通保存不读取、回填或重写旧token。已存NovelAI token保持脱敏，不新增reveal/secret读取端点，也不沿用ADR-0010针对模型/embedding密钥的查看例外。

计划在 `agentConfig.novelai` 保存 manualEnabled、manualDailyAttempts=10、sceneCgEnabled、sceneCgDailyAttempts=3、scenePromptModelConfigurationId=null、autoWritebackRoleAssets=false、consentRevision。开启对应用途时记录主人确认的设置版本；CG开启必须有存在的辅助连接ID，绝不回退 active/chat 配置。降低额度立即生效，连接/许可修改使在途使用资格失效。

工具 definition：name=generate_image、risk=external、source=builtin:novelai、exposure=direct、external_allowed=false、timeout_seconds=330、output_limit=4000。外层余量只用于300秒执行deadline触发后的本地取消收尾，不能延长外部请求期限或先返回后留后台资源。这是主人来源许可，非“禁止所有渠道”；渠道主人仍须通过现有 owner_context 门禁。冻结候选、版本与执行检查沿用ADR-0011，Skill/模型/搜索结果不能增权。目录解释关闭、缺凭据、缺额度、辅助连接失效；目录和配置健康检查只读本地，不试生图或访问账号诊断接口。

| 工具参数 | 具体约束 |
| --- | --- |
| prompt / negative_prompt | prompt必填、非空≤4000字符，negative≤4000；首版保留上游ASCII英文提示词适配，不把此要求写成所有NovelAI模型的官方限制。 |
| mode / base_attachment_id | mode必填txt2img或img2img；后者必填当前人类消息及其宿主核验引用附件allowlist中的图片ID，前者禁止底图字段；保留引用不等于开放任意历史附件。 |
| size_preset / custom_width / custom_height | square=1024×1024、landscape=1216×832、portrait=832×1216；custom每边64–1024且为64倍数，须同时提供；面积始终≤1,048,576。64倍数是首版收紧值，真实模型接受能力仍须验收。 |
| steps / seed / sampler / model | steps整数1–28默认28；seed整数0–2³²−1，省略则宿主生成并持久后再发送；sampler在显式allowlist中，首版允许k_euler_ancestral、k_euler；model只能选当前allowedModels，默认curated。 |
| strength / noise | 仅img2img允许；有限number，strength=.7且范围.01–1，noise=.2且范围0–.99；拒绝bool、NaN、Infinity和隐式字符串转换。 |
| intent / scene_key | intent默认user_requested；scene_cg必须非空scene_key且匹配本次宿主已提交观察。模型不能靠填写intent申请CG许可或制造场景。 |
| background | 可选bool，默认false；true为明确受管后台执行，父回合结束仍继续，结果由原来源outbox回灌；不是延长普通前台取消的借口。 |

所有对象 additionalProperties=false，条件必填在schema和运行入口双检；拒绝role/session/source/token/endpoint/path等额外字段。n_samples首版固定1，沿用来源默认而不开放付费批量；上游其余固定v4.5参数按被测请求构造保留，不宣称支持Vibe/人物参考、inpaint或其他未暴露能力。

应用API拟采用：GET/PUT `/api/services/novelai` 与明确token设置/清空操作；POST `/api/roles/{roleId}/generations`、GET同路径的分页历史、GET `.../{generationId}`、POST `.../{generationId}/regenerate`、POST `.../{generationId}/cancel`。UI请求带稳定requestId，服务将其绑定本地真实主人操作；不能仅凭模型伪造requestId进入UI信任入口。

UI生成表单包含模式、底图附件选择、参数、用途额度说明；角色资料配置两种许可和辅助连接；聊天图片提供“重新生成”并明确将再次消费，历史显示attempt、实际seed/模型、状态与原图。取消/unknown不展示“未扣费”。同一按钮的网络重试复用requestId，新动作生成新ID，不自动把失败改成新动作。

## 5. 一个服务拥有支付状态与数据流

新增 `novelai_generation` 业务模块，拥有配置、参数校验、来源/输入解析、尝试账本、HTTP、解码、媒体与outbox；不把身份和付费状态散落到tool、CG hook和HTTP路由。其外部操作只有：

```text
submit(context, request, expected_revision, delivery_mode) -> GenerationView
get/list(context, generation_id或cursor) -> 归属结果
regenerate(context, generation_id, request_id, expected_attachment_id) -> GenerationView
cancel(context, generation_id) -> GenerationView
commit_human_scene(context, completed_run_id) -> 受限CG准入结果
accept_human_run(context, accepted_run_id) -> 失效原来源旧CG候选并登记受管收尾
invalidate(role_id或service_revision) / close() -> 等待本地资源收尾
```

context由宿主从ToolContext/真实UI操作构造，读取SessionStore.get_run和user_message_for_run验证当前真实输入；渠道调用capture_channel_source，保存不可变FrozenChannelSource。generation固定role/source/run/call/intent；attempt是一次付费尝试；重生成保留generation关联但创建新attempt与明确新请求键。

SQLite存generation、attempt、daily_budget、media_outbox及按role/source隔离的scene_state。调用去重键为宿主规范化role+原source scope+run+call+intent；另有独立、原子且持久的 `role/source/humanRun` 消费意图claim，模型换call或intent也不能再开attempt。首个manual意图占用后即抑制该回合CG；同调用键查回原状态，其他调用返回该回合已占用，参数冲突拒绝。unknown不释放claim。UI用可信新操作requestId申请生成/重生成，不能靠伪造模型参数进入此入口；CG另唯一约束role/source/humanRun/visualKey。未授权者不能借去重查询旧结果。

流程：当前授权/配置校验→严格参数与owned input冻结→事务写attempt及预占→提交TaskRuntime→执行前重验→至多一次POST→受限解码和暂存→保存附件/历史→原消息或完成outbox→真实receipt→仅本地ACK。已保存结果不重新读入模型图片上下文；工具结果只回安全结构与附件ID，宿主负责展示。

本模块锁和唯一claim使同attempt只有一个launcher；使用 #158 集成后的 TaskRuntime `submission_key=attempt_id`、`retain_until_ack=True`、`completion=False`，不依赖 executor 首次入场才回写taskId来证明未提交。任务结果只含有界安全状态；复制到本业务持久记录后留下ACK待同步标志并幂等确认，ACK暂时失败、结果已prune与重启都不能重跑执行。重启不重交旧付费attempt。TaskRuntime预算300秒、日志64KiB、文本尾部4000字符；日志仅状态码/ID/阶段，不记输入图片或远端响应正文。HTTP与辅助调用共享该原始deadline，不重置期限。

TaskRuntime负责执行、停止、deadline和总并发；业务账本负责外部消费事实，两者终态允许不同：runtime timed_out/cancelled对应attempt unknown。本模块不新增第二个通用队列/任务宿主，也不将业务unknown塞成TaskRuntime已有的成功状态。

## 6. 单次请求、恢复与媒体预算

attempt状态为 prepared→dispatching→received→succeeded，另有rejected/cancelled_before_dispatch/failed_response/unknown/local_pending。预占与prepared同事务；dispatch前在服务准入锁内读取当前grant/代际/额度并原子CAS，成功后才取token、组装内存base64并发一次POST。授权修改使用同准入锁；若撤销发生在dispatch之后，进入在途取消路径，不能倒推未消费。

token只用于固定官方origin的Bearer；禁用重定向，不使用整env或隐式代理配置。生成客户端显式无自动重试，POST 408/429/5xx/transport失败也不重发；不复制上游CG重试和HTTP三次策略，不在500后调用/user/data。correlation ID只作诊断，seed不作支付幂等。

优先请求ZIP，同时以fixture覆盖官方对象JSON `{images:[{image,index,seed}]}`；严格验证base64、图片签名/完整解码及实际像素。限制压缩32MiB、解压64MiB、16项和每图像素；不信任Content-Length/ZIP声明值，不提取返回路径、请求图片URL或执行元数据。意外多图在预算内完整保存并报协议异常，不静默丢图后标正常成功；超预算拒绝并保留消费不确定说明。

响应先写模块私有二进制spool并原子rename，received持久后才做附件导入；此后本地失败为local_pending，可重试导入/历史/outbox。若响应未持久就崩溃，dispatching恢复unknown，不能再POST。已知401/402等可记failed_response并保留已发尝试计数，不由状态码承诺未扣费；5xx、断线、坏包不伪装确定失败且安全重试。

重启：prepared且持久dispatch标志从未置位的旧attempt结束为cancelled_before_dispatch，不自动续跑；dispatching变unknown；received/local_pending只恢复本地步骤；succeeded只恢复未完成outbox。aux_dispatched在辅助调用前独立持久，若已置位则即便生成尚未dispatch也保留CG尝试计数；两种外部调用都未开始才释放预占。相同请求始终返回旧记录；只有新的明确生成/重生成动作能创建新attempt。

原始输入base64/token不进入prompt、tool result、审计、日志、history JSON或request.json。仅保留owned attachment引用、摘要和规范化非秘密设置；base64内存请求发送后释放。二进制spool只属原角色，完成导入或角色删除后清理；正常历史与已引用附件不因TaskRuntime日志回收而消失。

角色生成媒体1GiB、全局5GiB包含spool、失败图、local_pending和已导入图。出网前原子预留96MiB，覆盖32MiB响应spool与最多64MiB持久图片；逐图解码直接写最终附件的私有staging文件，再同文件系统rename，不另存一套完整解压副本。若实现需要额外磁盘副本，必须在写入前增加对应预留，不能突破硬上限。按实际存量结算，local_pending继续占用；满时拒绝新消费，不能靠重新生成补偿存储失败。history每页默认20/最多50，仅当前来源可读；主人桌面管理页可明确浏览该角色各来源，普通渠道工具无跨线程管理权限。去重墓碑和当日计数不因隐藏历史而删除；角色删除清理本角色墓碑/媒体/历史，不删全局token或抹掉账户当日消费计数。

## 7. 场景 CG 与辅助模型

仅真实人类回合完成后触发一次候选；内部完成、访客、旁听、主动回合无资格。真实新主人run被宿主接受时，`accept_human_run`先原子seal同来源旧CG候选、推进human/scene revision并撤销旧结果发布资格；登记有所有权的取消收尾，CG下一次准入前必须await完成，应用close也等待该收尾。主聊天无需等待旧CG网络退出。该策略比只在已识别新场景后取消更保守；旧CG若已dispatch仍可能消费并记unknown，UI启用说明须明确此后果。

若本回合已有手动生成意图（包括unknown），不再CG；同human-run独立claim阻止换call和hook重入。CG取得账户唯一slot前先完成旧候选收尾，因此不会出现“旧生成占slot、新观察无法产生取消”的循环。其后才预占有限CG尝试、账户并发与TaskRuntime配额，不能用无界后台aux调用绕过额度。

在同attempt预备阶段用显式scenePromptModelConfigurationId调用一次辅助模型，max_tokens=600、30秒内并受300秒总deadline；使用rich adapter、tools=()、取消穿透，严格解析有界JSON：transition、scene_key、visual_key、visual_description、prompt、negative_prompt、size_preset。字段分别限制枚举/80/80/1200/4000/4000字符和尺寸枚举，拒绝工具调用及额外字段。

输入仅当前来源本次user消息、已完成assistant回复、当前角色描述，以及该来源最小前态scene/visual keys、视觉描述、主人回合计数与scene revision；不读其他线程/屏幕。这些是非可信场景数据，不能提供身份、endpoint或授权。辅助请求固定读取该revision，返回后以revision与最新humanRun做CAS；不匹配即过期，不提交或启动生成。宿主验证后提交带真实role/source/humanRun的不可变scene observation，再由CG策略消费；辅助模型输出不直接启动外部工具。该事件当前尚无实现，需要本票新增，不能写成已存在。

scene_state只存当前scene/visual key、主人回合计数、最近占用的visual/回合、pending attempt及最后已提交humanRun。宿主在短事务中比较旧state、验证transition并提交新state和事件ID；同humanRun最多提交一次，closed清当前场景，none不制造场景，same须对应当前scene，started/changed才建立新scene。所有JSON字段必填且additionalProperties=false；需要生图的观察必须有非空keys和prompt，role/source/human标志只在宿主事件封套中出现。这里选择一次调用同时观察场景和产英文tags，不再追加第二次“转tags”模型请求。

closed/none只更新场景不生图；已生成dispatch的同visual_key不重复，成功或unknown后五个实际主人回合冷却。尚未生成dispatch就取消的临时visual预留可释放，让后续新真人回合重新考虑；已发生的辅助调用计数与旧human-run claim不释放。started/changed保留上游绕过冷却的语义，但不能绕过已dispatch的visual去重、当前grant或日预算。scene_cg缺scene_key/不匹配宿主观察在外部请求前拒绝。

新场景取代旧候选时取消旧任务并await收尾；未dispatch可释放对应生成预占，已辅助出网仍计CG尝试，已生成dispatch为unknown或保留已收到图而停止交付。关闭CG、删除角色、辅助配置删除或许可代际变化遵守相同规则；迟到结果不重新显示或投另一会话。

标签库保留受限list/upsert/delete和当前消息匹配扩写，由主人管理、按角色许可与内容rating过滤；扩写结果仍受prompt长度与模型授权约束。资产自动写回另验autoWritebackRoleAssets与当前角色代际，失败保留正常历史，不触发重生成。

## 8. 单次消费、后台投递与重生成

前台generate_image在TaskRuntime中执行并等待300秒内结果；普通前台取消穿透。成功后outbox绑定原run的回复消息，宿主给metadata.attachments追加公共附件投影；消息已完成/被删除时不无条件改写，转为原来源的明确内部完成消息或历史待处理，不能重复附入两个回复。

background=true必须作为明确受管提交记录；工具先回accepted/taskId/generationId，完成阶段等原角色回合锁后回原session。父回合结束不等同撤权；来源解绑、账号/角色停用、服务许可变化仍可撤回交付。保留TaskCompletionWorker现有spawn协议；NovelAI任务为completion=False，本业务media_outbox恢复器查询同一TaskRuntime结果并完成附件投递、receipt/ACK本地同步。该恢复器只处理本业务记录，不新增通用任务宿主或改用spawn.enabled授权。

media_outbox每attempt只有一个规范消费记录，唯一键不包含deliveryMode；它记录唯一messageId/替换槽或history_only目标，另存投递模式及消息同步状态。前台回复与后台完成不能各自消费同一attempt。原消息不可附图时，仅在尚未被消费/dispatch的同一记录内CAS切换原来源内部完成目标；已附图、sent或uncertain绝不再开另一投递模式。状态pending→sending→sent/failed/uncertain，另记message_synced。本地消息写入失败可重试；外部send前持久sending，崩溃恢复uncertain且不重发。receipt先持久，再重试本地同步；已sent不能被后续SQLite错误改为failed。投递unknown与生成unknown分开显示。

渠道发送复用ChannelHost.account_send，固定原source账号/线程/真实目标，actor=reply、authorize_source动态复核，attachment_ids由宿主解析。adapter不支持媒体时在出网生图前拒绝该自动交付模式，主人可明确选仅保存历史；不能自动改投桌面、私聊或返回外部可访问路径。不能以返回文字含“图片已发送”当receipt。

重生成必须由新的明确主人操作发起并确认再次消费；重新校验当前grant/model/底图仍可访问，不把旧snapshot当授权。新seed和新的attempt持久后才发POST。SessionStore新增窄接口 `replace_attachment_once(role, session, message, expected_id, new_attachment, attempt_id)`，事务校验消息归属及旧槽，只替换该ID并保留其他metadata/附件；同attempt重放返回原结果，旧图已变报conflict。

CAS失败只保留新图历史并提示“已生成，原消息已改变”；不二次付费。后台完成写入另用 `append_generation_completion_once(origin, attempt_id, attachments, status)`，验证原会话仍存在，不能创建第二个主人会话或伪造user消息。这两项为计划新增业务seam，当前代码未提供。取消接口返回本地停止与消费状态，绝不承诺供应商取消或退款。

## 9. 故障测试与可观察验收

| 场景 | 必须证明的行为 |
| --- | --- |
| 真实localhost HTTP | txt2img/img2img完整请求、固定Bearer目标、真实PNG/ZIP/对象JSON解码；断线/slow accept/408/429/5xx/坏包均最多一个POST，大小/解压/像素上限生效。 |
| SQLite与崩溃 | 同key并发/参数冲突、同humanRun不同call/intent争抢唯一claim、预占竞争、launch交接与ACK暂时失败、dispatch/received/spool/消息/receipt各切点；重启不二次POST，unknown占用不复活，切换投递模式不重复附图。 |
| 身份与撤权 | 伪造role/source/env、跨角色/跨访客底图、旧配对、账号代际、群友及内部回合拒绝；dispatch前后撤权、角色删除/停用均无迟到展示或改投。 |
| 资源与取消 | TaskRuntime全局/角色/账户并发及300/600秒限制实际被消费；取消等待本地HTTP/文件/辅助调用收尾；无泄漏任务、spool、秘密输出。 |
| 完整产品闭环 | 同真实HTTP对话运行发现工具→生成→原回复附件→历史重建→指定槽重生成；后台原session与渠道真实receipt；CG前态/revision CAS、五回合/scenevisual/手动互斥、新真人回合先seal旧候选并join后再准入、过期辅助结果丢弃及标签/资产写回许可。 |
| UI与秘密 | 配置保存不读真实token；截图/DOM/网络日志无token/base64/path；缺配置/额度/协议/模型/媒体能力均有准确错误，旧请求不在切换/撤权后显示。 |

测试使用独立临时DB/角色目录和MemoryCredentialStore或同等测试实现，不碰真实vault；fixture server只有localhost。脚本化provider证明公开管线与数据流，不证明任意模型必然遵从。测试默认不得连接NovelAI账号或消耗服务；禁止用依赖缺失的skip冒充通过。

## 10. 垂直 Ticket 与最终门槛

#158 的实施依赖已在总集成 `4b1b3b8` 满足并通过两套CI；第3节随后获主人明确批准。#171 实施计划见 [批准后计划](https://github.com/sawaki-zexi/meido/issues/171#issuecomment-6207490472)。架构批准不能代替实现验收或真实消费许可。

已按完整用户结果拆三票，Parent 均为 #137，§3 已批准；按真实前置依赖串行，不分数据库/API/UI层：

1. [**#171：主人按请求生图并在原会话看到唯一图片与历史。**](https://github.com/sawaki-zexi/meido/issues/171) 配置/许可/有限预算、txt2img+img2img、TaskRuntime、attempt unknown、媒体outbox和本地HTTP全闭环；第3节已批准，可开始实施。#158 已完成两轴复核并进入经两套CI验证的总集成 `4b1b3b8`，实施依赖已满足。沿用主人已批准的本地总PR分支流程，不重复等待main或建立堆叠功能PR。
2. [**#172：主人明确重生成指定图片并可靠接收后台结果。**](https://github.com/sawaki-zexi/meido/issues/172) 新付费动作、CAS、真实receipt/不确定投递、重启恢复、多附件不丢失；Blocked by：前票。
3. [**#173：主人输入场景后角色自动生成获准CG。**](https://github.com/sawaki-zexi/meido/issues/173) 辅助连接、宿主场景事件、冷却/去重/取消/手动互斥、标签与显式资产写回；Blocked by：前两票；不依赖P04、桌宠或无人值守主动扩展。

最终P06/T19验收要求三票完整行为、相关测试/类型检查/构建、两轴独立审查均通过；受控fixture不足以宣称NovelAI真实可用。另获明确真实成本许可后，锁定账号配置者、模型/参数、最多请求数及可接受费用，分别留证txt2img、owned-img2img、历史/展示、重生成、CG；每次重生成也计入许可。未获许可时清楚记录“真实生成未验收”，不索取真实凭据或先试一次。

待补证仅三项：被选模型ID及实际响应/尺寸限制的原生验证；真实账号消耗与取消后计费事实；供应商对无人值守主动CG及任何官方幂等服务的明确契约。前两项属于另批成本后的验收，第三项阻塞该扩展而不阻塞人类回合CG实现；任何结果都不能由本次架构批准代替。
