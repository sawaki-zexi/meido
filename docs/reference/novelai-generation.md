# NovelAI 生成、指定图片重生成与原来源交付契约

状态：当前本地实现，#171/#172 受控验收，#173/#211 作者验证；独立审查与准确集成以固定提交记录为准，真实账号与生成费用未验收。最后核验：2026-11-12（#357 V5 多语言提示词、#358 V5 Full Medium）。事实来源：[应用](../../backend/app/novelai_generation/application.py)、[参数与请求](../../backend/app/novelai_generation/parameters.py)、[API](../../backend/app/novelai_generation/api.py)、[账本](../../backend/app/novelai_generation/ledger.py)、[SessionStore](../../backend/app/session_store.py)、[真实消息与渠道测试](../../tests/python/test_novelai_main.py)。完整目标见[NovelAI 规格](../specs/novelai-generation-spec.md)，验收命令与范围见[手册](../handbook/novelai-generation-acceptance.md)。

此实现覆盖独立服务、当前主人 txt2img/img2img、聊天中的自主表情生图、指定槽重生成、受管后台、角色附件、唯一原来源交付及持久历史。#173 增加完成主人回合后的场景 CG、受限标签库与独立许可的卡片图写回；普通聊天工具调用默认请求当前回合的一张 `chat_expression`，显式高级参数仍受同一 allowlist 校验，`scene_cg` 仍只能由宿主观察路径提交。

## 历史参数复用与重生成（2026-10-28）

对照 Shiori `d3acaed186b172a78a5892e0307e9affd89759f0` 的 `plugins/novelai/backend/service.py::regenerate` 与 `ui/useImageStudio.ts`：工作台和聊天抽屉恢复原图所有可变图像参数，包括 `qualityToggle` / `ucPreset`。公共偏好刷新不再静默改写已载入历史的模型、步数或其他参数；本次偏好可显式编辑或复制当前公共值。复用保留 seed；重新生成默认换 seed。

服务端重生成先从角色归属的原 attempt 读取规范化图像快照，再应用本次明确编辑；`parameters: {}` 可重放原图而无需客户端重建参数。未编辑时出网内容仅 seed 及其派生的图生图噪声种子改变，包括图生图底图、强度、噪声、采样、尺寸、模型、张数及质量/负向预设。当前模式、模型白名单、尺寸/步数、来源、底图归属、有限次数和新请求身份仍重新校验，旧记录不会授予新权限。图像参数不继承旧 scene 身份、投递或后台许可；替换单图仍只支持一张。

本次 `qualityToggle` / `ucPreset` 覆盖只允许明确的本地主人 API 操作，不提供给聊天模型工具。旧记录未存偏好时使用当时协议的 `false` / `0`，不伪称未知历史包含新设置。

## 配置与权限

### V5 基础生图（2026-11-12，#356）

服务支持 V4.5 Curated、V4.5 Full、V5 Curated 和 V5 Full。公开配置的 `modelCatalog` 返回当前支持的 `id` 与 `label`，设置页据此列出可允许模型。初始允许列表和默认仍为 V4.5 Curated，既有配置通过显式选择添加 V5。保存 `allowedModels` 时，新界面同时发送写入专用的 `managedModelIds`，服务保留旧允许列表中该界面未管理的模型。旧客户端省略该字段时管理范围为两个 V4.5 模型，完整提交旧列表也会保留已允许的 V5。读取后原样带回的 `modelCatalog` 作为只读字段忽略，修订冲突校验继续执行。

V5 文生图和图生图经过同一准入、任务、账本、附件和原来源出口。请求冻结 `params_version=4`、`scale=7`、`noise_schedule=karras`，图生图额外噪声种子为主 seed 减一。原始 `prompt` / `negative_prompt` 保留供编辑和重用，`effective_prompt` / `effective_negative_prompt` 保存真正发送的质量及负向预设扩写结果。界面预设 0/1/2 分别为不使用/轻度/重度，V5 的供应商提示元数据为 0/3/2。V5 支持中文、日文、英文及混合 Unicode 提示词，V4.5 保留英文 ASCII 约束。公开模型目录的 `supportsUnicode` 标明此能力，前端缺少该字段时沿用 ASCII 检查。

V5 Full 的用户模型仍是 `nai-diffusion-5-full`，强度以冻结的 `intensity=high|medium` 保存；缺少该字段的新请求按兼容规则冻结为 High，旧记录没有证据时在工作台和抽屉显示“未知”。只有 Full 能力目录返回 `medium` 时才显示 Medium。Medium 出网时映射为 provider 模型 `nai-diffusion-5-full-medium`，内部固定 `steps=14` 和 `sampler=k_euler_ancestral`，并拒绝自定义负向提示词及非零负向预设；服务端设置仍须允许该采样器和步数。切换强度保留未提交的提示词、步数、采样器和预设草稿，限制以校验消息说明，切回 High 后可继续编辑；切离 Full 会清除不适用的强度字段。旧服务响应缺少 `intensities` 时前端只安全回退为 High，不猜测 Medium。

当前有效默认为 V5 时，故事插画使用该 V5。V4.5 设置沿用故事原有的 Full 优先规则。场景候选在辅助模型开始前保存实际生图模型，辅助提示词使用对应版本，并按冻结的质量、负向预设和模型归一化。V5 指定 WebP 与普通透明通道，受支持的 PNG/WebP 响应在保存和下载时保留原始字节。

协议证据与真实账号验收边界见 [V5 规格](../specs/novelai-v5-integration-spec.md#11-基础阶段协议核验)。事实来源为[模型目录](../../backend/app/novelai_generation/model_catalog.py)、[请求构造](../../backend/app/novelai_generation/parameters.py)、[完整生成测试](../../tests/python/test_novelai_v5.py)、[历史重放测试](../../tests/python/test_novelai_regeneration.py)。

应用服务固定 `https://image.novelai.net`；生产配置和工具不接受自定义 endpoint。独立系统 vault service 为 `meido.novelai`，键为 `token`。普通设置读取/保存、目录健康信息不读取旧 token，不访问账号诊断或试生成；只有明确设置、清空和实际单次 dispatch 使用凭据。设置/清空失败保留 cleanup pending 和不可用状态，不显示为已成功保存。

`GET/PUT /api/services/novelai` 的公开字段是 enabled=false、tokenConfigured、credentialCleanupPending、revision、manualEnabled、allowTxt2Img=true、allowImg2Img=true、allowedModels、defaultModel、nsfwEnabled=false、nsfwModel=nai-diffusion-4-5-full、allowedSamplers、defaultSampler、maxPixels、maxSteps、dailyAttemptLimit、defaultSamples=1。`enabled` 是聊天表情工具和主人手动工作台的唯一全局开关；工作台仍保留完整参数输入。旧 `manualEnabled` 读取返回有效 `enabled`，写入时仅在未提交 `enabled` 时作为其别名。`manualDailyAttempts` 仍控制每个角色的手动/聊天尝试上限。`confirmManualConsumption` 继续兼容接收但不再准入，`manualConsentRevision` 仅为兼容字段，新保存不伪造确认。PUT 另须 expectedRevision；变更推进版本并撤销在途资格。关闭某个模式会阻止对应手动请求和聊天表情请求；场景 CG 固定使用 txt2img，并在辅助调用前检查该开关。默认 curated v4.5、Euler ancestral、1,048,576像素、28步、账户20次/UTC日；full 模型只能由主人明确加入 allowlist。NSFW 默认模型模式也必须先满足 allowlist，只影响未指定模型时的默认选择，不改变角色标签 rating 许可。各值有有限上限；次数不是 Anlas 估价。

独立 token 操作是 `PUT /api/services/novelai/token`（严格仅 token 字段）及 DELETE 同路径。不存在 token 查看接口。请求和错误不回显秘密、原始响应正文或输入图片。

角色 `agentConfig.novelai` 默认保留 `manualEnabled=false`、`manualDailyAttempts=10` 以兼容旧角色文件；当前运行时的聊天表情和主人手动工作台由插件公共设置 `enabled` 与 `manualDailyAttempts` 统一控制。角色侧实际生图配置为 `sceneCgEnabled=false`、`sceneCgDailyAttempts=3`、`scenePromptModelConfigurationId=null`、`sceneCgPromptInstructions=""`（最多2000字符）、`autoWritebackRoleAssets=false`、`promptTagsEnabled=false`、`allowedPromptTagRatings=[general]`、`consentRevision=0`。角色 `consentRevision` 保留兼容但不再影响新请求。CG 须开启角色开关、服务就绪、允许文生图，并明确选中存在的模型配置 ID，null/已删除 ID 不回退全局模型；补充要求只作为该角色的主人绘图偏好加入 CG 辅助模型 system prompt，不进入场景事实消息，也不影响聊天表情或手动工作台。宿主会在 CG 提交前追加并大小写去重第三人称、电影构图、角色入镜 tags 及第一人称/POV/自拍负向 tags。写回须同时启用 CG。降低额度不清除计数。旧客户端省略整个字段或只提交部分字段时保留既有许可；新增标签默认值不改变旧 #172 请求的权限指纹。实际许可变化在角色文件写入前推进私有持久 epoch，关闭再开启也不能复活旧冻结工具。手工写 enabledTools 不能绕过许可。

工具 `generate_image` 为 direct / risk external / source builtin:novelai / external_allowed=true，外层330秒、输出4000字符。普通聊天只需要必填 `prompt` 和可选 `negative_prompt`。角色自己判断这一回合是否值得展示表情，决定调用时须把已知主体外貌、服饰与当前情绪、动作、场景、构图一并转成符合实际模型语言要求的完整提示词；宿主不自动补入角色外观，不要求主人填写参数，也不应每回合调用。工具 schema 仍保留 mode、尺寸、模型等可选字段，以兼容主人明确要求的历史参数复用；普通聊天省略这些字段时，宿主固定 `txt2img`、一张图默认、默认模型/采样器/尺寸/步数/seed、`intent=chat_expression` 和原回复投递，显式高级字段仍受同一 allowlist 和来源校验。完整参数仍由主人手动工作台/API 使用。来源由宿主核验。桌面 SQLite run 须来自真实 `/messages` 主人输入，渠道执行使用当前真实输入的账号、身份、目录代际与原线程授权。符合现有渠道许可的群友可生图，自然回合沿用当前来源并采用受管后台交付。CG 仍限真实主人回合，`manage_image` 仍限主人。执行、外部投递和本地发布分别重查冻结授权。定时、内部完成、主动及伪造输入无法借用当前人类输入资格。[来源实现](../../backend/app/novelai_generation/context.py) · [自然来源回归](../../tests/python/channels/test_natural_tools_background.py)

公开配置新增 `availability={available,code,message}`，仅判断本地配置，不读取凭据、不请求供应商。code 为 `ready`、`service_disabled`、`credential_missing`、`credential_cleanup_pending` 或 `generation_modes_disabled`。启用插件即注册 direct 工具；工具描述再结合角色和日额度解释暂不可用原因，缺凭据或额度不足不隐藏能力。执行时仍重查来源、修订、模式和额度，不能由描述授予权限。设置、插件列表与工作台显示对应状态；工作台还检查历史中的今日用量。参数/凭据变更允许未来的新请求，旧冻结任务仍被撤销。

历史、单条查询和工具结果新增 `errorMessage`，由受控 `errorCode` 生成：401/403 提示凭据或权限，402 提示 Anlas/额度，429 提示请求频繁，5xx 提示供应商暂不可用，网络异常说明结果无法确认；响应类型、压缩、空内容及大小超限有独立说明，工作台和聊天历史显示稳定错误代码与尝试编号。没有实际附件时不声称“图片保留在历史”。保留原 status/consumption；不展示原始错误正文，也不据此重试。未出网的本地次数耗尽返回 HTTP 429 和明确提示，原 requestId 仍可按既有规则重查。

## 角色形象与模型工具（2026-10-28，#263）

对照 Shiori `d3acaed186b172a78a5892e0307e9affd89759f0`：角色身份来自 `role_profile` 和 `SELF.md`；`generate_image` 要求模型转换完整英文 tags，服务不会自动从角色图片提取外观。Meido 保留相同文字身份来源，并修正旧工具描述中「只描述情绪/动作」的遗漏。

Meido 的头像、卡片图、插画位于角色 `workspace` 之外，不能靠现有 `read_file` 直接查看。当前角色有图片时，首轮直接提供只读 `inspect_role_image`：目录只含当前角色的资产 ID；省略 ID 分页列出，指定 ID 才把图片送入当前视觉模型。它不接受路径、URL 或其他角色身份，不把图片加入聊天附件、不改变分类发送许可、不调用辅助模型或 NovelAI。普通主人回合可在设定缺少视觉细节时先看参考图，再组织完整生图 tags；图片名称不是外貌事实。非主人来源、已撤销来源、变更或移除的冻结资产、路径链接和明确不支持视觉的模型均不能读取图片；未知模型的实际视觉能力仍由当前服务决定。

验证通过本机 HTTP 模型/NovelAI 替身：首轮看见工具 → 读取当前角色头像 → 下一次实际模型请求含头像像素 → 完整外貌 tags 进入 NovelAI `input` 与 V4 caption → 原回复只收到生成图。此检查证明装配、图像传递和提示词传输，不证明任何真实模型必然遵循说明或真实账号已出图。

## 参数与身份

所有手动/API 参数对象额外字段拒绝；角色、来源、token、endpoint、path 等不能由模型传入。手动/API 参数包括按实际模型校验的 prompt/negative_prompt（各最多4000个 Unicode 码点）、mode、img2img 当前附件ID、尺寸、1–28整数 steps、uint32 seed、allowlist model/sampler，以及仅 img2img 的有限 strength/noise。聊天 `generate_image` 的普通调用只使用 prompt/negative_prompt，宿主在同一归一化入口补齐上述受控字段并设置 `intent=chat_expression`；schema 中的高级字段只作为兼容输入，仍受同一 allowlist、附件归属和来源校验，不能改变场景或服务许可。真实主人聊天工具在参数归一化前会读取本次宿主确认的当前主人消息，按角色标签库扩写正负 tags；扩写后的 prompt/negative_prompt 才会写入 attempt/history。工作台 UI 请求和指定图片重生成不读取当前主人消息做隐式手动扩写；场景 CG 使用候选中冻结的主人消息走独立 CG 扩写路径。bool/字符串不代替数字，NaN/Infinity 拒绝。省略 seed 时宿主先生成并持久再发送。

尺寸为 square=1024×1024、landscape=1216×832、portrait=832×1216，或每边64–1024且64倍数的 custom；面积受当前服务上限约束。`samples` 默认使用服务级 `defaultSamples`（1–4，默认1）、手动聊天/工作台最多4，CG与单槽 replacement 固定1；账本按一次 attempt 保存和展示全部返回图片，v4.5 完整请求常数保留在 parameters.py。工具底图可用当前消息/宿主核验引用的 `base_attachment_id`，或 `inspect_role_image` 目录中的 `base_role_image_id`（avatar、card、本角色插画 ID）；图生图必须二选一。角色底图使用与看图工具相同的有界读取和链接检查，冻结为本角色附件后再提交实际像素；较大的图片沿用看图读取器的压缩。重生成保留该底图副本，不跟随之后的头像或立绘变化。普通文生图不自动读取参考像素。桌面来源的显式 UI 可选择本角色当前可解析的 owned 附件。渠道重生成仅继承原来源图片 allowlist 及该次已冻结的角色底图副本，并重新校验当前来源许可与角色附件文件；桌面管理历史不能加入其他私有附件。输入最多10MiB并完整解码，拒绝 raw path/URL、跨角色或普通工具调用选择任意历史附件。

一次实际 human run 只有一个持久消费意图 claim，换 call/intent 也不能再消费。同调用/同 UI requestId 参数一致返回原记录，冲突拒绝；当前授权仍须成立。重查先找保存的 snapshot，不重新随机 seed，不因原底图随后删除而重新消费。新的明确 UI 操作才创建新的请求键。

## 执行、恢复与资源

账本与 SessionStore 使用同一 SQLite 数据库的独立业务表。prepared 及有限额度、96MiB存储预占、账户唯一在途 slot 在同一事务；UTC 日取当前日与已用最大日，时钟回退不重置额度。每角色生成媒体1GiB、全局5GiB，出网前必须有足够预留。unknown 保留尝试占用；明确出网前拒绝/取消才释放计数。

执行由现有 TaskRuntime 拥有，kind=novelai_image、300秒、日志64KiB、尾部4000字符、completion=false、submission_key=attempt_id、retain_until_ack=true，受既有全局8/角色3并发约束。已准入但 taskId 回写失败时，前台和后台都必须 stop/join，后台不得假报 accepted；恢复按同一 Runtime handover 找回结果，复制业务事实后本地 ACK。ACK/标记失败不会重新提交或 POST。Runtime cancelled/timed_out 与业务 unknown/已收图可以不同。

每 attempt 最多一次生成 POST。HTTP200/201 接收 ZIP、JSON 图片对象或 Shiori 兼容的 base64 字符串数组、原始受支持图片；二进制或缺失 MIME 仅按 ZIP/PNG/JPEG/WebP 签名识别后进入完整校验，显式禁重试、重定向、环境代理/证书隐式配置；HTTP期限沿同一原始 deadline。已知400/401/402/403/422可为 failed_response；408/429/5xx、断线、超时、取消及坏包为 unknown，均不承诺未扣费或退款。

HTTP 支持 identity/gzip/deflate，压缩传输与 HTTP 解码后的 spool 分别≤32MiB；解码本身受限，损坏、截断或未知编码拒绝。ZIP 总解压≤64MiB、最多16项、每图像素≤本次批准面积。按实际读取计数，不信任长度声明或 ZIP 路径。Pillow 完整验证与加载；每图直接写角色附件的 staging 再 rename，不另存完整解压副本。staged 附件引用先持久，再删除完整 spool，最后结算 succeeded；本地故障 local_pending 仅重做本地步骤，复用已写二进制。坏包中已验证图片保留历史，按真实 raw spool/图片/孤立二进制结算，不丢历史、不用重新生成补偿。

正常完成附件为公共 id/name/mediaType/size/kind，无路径/base64。意外多图完整保存，记录 unexpected_multiple_images；工具报协议异常，canonical outbox 仅留历史，不作为正常原回复自动展示或发送。

## 唯一展示与交付

每 attempt 只有一条 canonical media outbox。桌面真实 `/messages` 在 run/final assistant text 都完成后，通过 `append_generation_attachments_once` 在同一 SQLite 事务追加附件和消费 outbox，保留旧 metadata/其他附件。显式 UI 生成使用 `append_generation_completion_once` 向已存在的原角色桌面 session 插入唯一 internal_completion；不伪造 user/run，也不新建第二会话。

普通前台 UI HTTP 断连/取消先 fsync 原请求的私有取消墓碑并封存发布资格，再停止并等待 Runtime；即使导入稍后成功，恢复也不得迟到插入消息。图片和真实消费事实仍可在原角色历史查看。关闭、撤权、角色删除都等待已拥有任务收尾；删除只清本角色历史/墓碑/媒体，保留全局 token 和账户当日已消费计数。

渠道只在原 owner 来源和 adapter 支持图片时准入；含生成能力的回合等待最终回复，再以原账号/目标一次发送文本与附件。发送前持久 sending/consumed；真实 receipt 先写原角色受管小 journal，再复制业务表，后续 SQLite/消息写失败仅本地恢复。sent 不降级为 failed；真实丢回执为 uncertain，重启 sending 亦转 uncertain，不补发、不改投桌面或私聊。

发送器已返回而本地 journal 暂时不可写时，宿主持有原 receipt 的本地结算义务；文件系统恢复后，维护循环仅补写 journal、SQLite 和原消息，不再次发送。该暂存不把实际 sent 改为 uncertain；发送器尚未返回的 sending 记录不因此提前结算。若进程在 journal 成功前退出，仍按已持久化的 sending 事实在重启后结算 uncertain，不能重发。

本地恢复器是应用拥有并 join 的维护任务，只处理已登记的媒体、原消息、receipt和 ACK，不是第二个通用任务宿主，不重新出网生成或重发渠道消息。每批最多100条，按创建时间和ID稳定排序，以持久游标轮转；暂时无法查询 Runtime 的旧义务保留但不会反复占满同一批，游标到末尾后回到起点。无待同步消息的 suppressed 墓碑不再作为消息同步义务；并发恢复调用仅由一轮执行。

## 应用接口与历史

### 主人回合场景 CG

[场景协调器](../../backend/app/novelai_generation/scene.py)由原宿主持有。真实主人输入被接受时，同步推进本来源 human/scene revision 并封存旧 CG，再登记 owned 取消收尾；主聊天无需等待旧网络退出，下一 CG 则等待该收尾。原主动回合优先级和完成回调继续组合执行。当前主人回复真正完成后最多一次候选；访客、旁听、内部完成、主动回合及失败回复没有资格。该回合已有手动意图（包括 unknown）时不观察，持久 human-run claim 和账户唯一 slot 阻止并发消费。

完成后的可选候选登记遇到本地I/O或SQLite写锁时，保留已完成聊天及原渠道正常发布，不触发辅助/生成或自动重放。该错误边界不吞接受主人时的同步seal或已拥有任务的取消收尾责任。

辅助调用与生图属于同一 attempt、同一个 300 秒 TaskRuntime deadline。候选创建时冻结服务修订、角色许可、完整角色设定（简介、背景、性格、行为规则和回复约束）及本会话最近六条已完成对话。明确 ID 的 rich adapter 分两步执行：先观察场景、保留角色可见外观与动作（max_tokens=600），只有通过冷却和去重判定的场景再按冻结模型转换正负提示词与画幅（max_tokens=1000）。转换阶段再次收到完整角色设定、近期对话及视觉描述，角色绘图补充要求作为独立主人偏好放入 system prompt。每步 tools=()、至多30秒、输出至多16KiB；没有重试，图片 POST 仍至多一次。拒绝截断响应、重复/额外 JSON 字段、工具调用、无效枚举或越界文本。两步开始、接收及提交时均校验 humanRun、scene revision、模型 epoch、角色/来源许可和标签版本；配置修改/删除、关闭再开及迟到结果不能获得新资格。宿主在标签扩写后追加 CG 构图约束，并在提交前大小写不敏感去重。

Story 使用建立故事时冻结的角色设定，Director 明确将角色已知外观及标志配饰转换为英文 tags，只有剧情明确变化才覆盖。当前有效模型为 V4.5 时，图片适配器优先使用已允许的 `nai-diffusion-4-5-full`，否则沿用当前服务模型。当前有效模型为 V5 时使用该模型，并接受资源中保存的多语言提示词。同场景已有成功人物 CG 的复用行为保持。以上机制传递角色身份信息。

none/closed、冷却和重复视觉仍是已发生辅助调用的 CG 尝试，账户与 CG 日额度不退；只有辅助和生成均未出网才释放次数。same 必须匹配当前场景，需距上次生成 dispatch 至少5个真实主人回合；started/changed可越过冷却，但不能越过已 dispatch 的 visual_key 墓碑、预算或许可。visual 墓碑最多每角色10,000条，满时拒绝新的生成；不删除旧墓碑以放行重复。重启不继续未确认候选，也不发布旧迟到 CG；已有 sent receipt 仍仅修复原消息事实。

历史新增 purpose=manual/chat_expression/scene_cg、phase=auxiliary/generation/completed、auxiliaryConsumption 与 auxiliaryStatus；辅助阶段没有冻结生图参数时 parameters=null。completed_without_image 表示本次观察完成但未生图，其原因包括 scene_none、scene_closed、scene_cooldown、scene_visual_duplicate；失败辅助为 scene_observation_failed。生成和辅助消费可能性分开显示，不把“没图”说成免费或退款。

### 标签和卡片图

提示词边界在编辑、保存、预览及正式生成中统一使用 Unicode 码点。长度在去除首尾 ASCII 空格前计算，组合字符分别计数，补充平面字符计一个码点。拒绝 C0、DEL、C1、U+2028/U+2029 和未配对代理项，正向提示词还拒绝 Unicode 全空白。只去除首尾 ASCII 空格，保留其他 Unicode 空格、组合字符、emoji 连接符及原始码点，不对提示词做 NFC/NFKC 规范化。前端保留超长草稿供修正并避免原生 UTF-16 maxlength 截断。普通逗号标签的既有去重和空格整理见下文。[校验](../../backend/app/novelai_generation/prompts.py) · [多语言回归](../../tests/python/test_novelai_multilingual.py)

聊天工具的描述和 schema 随冻结的有效模型提供语言说明，并列出可覆盖模型的语言约束。CG 转换使用 attempt 中冻结的模型。两者继续包含已知外观要求，正式准入重新验证标签扩写结果和模型许可。预览通过不会代替正式准入。工作台与聊天抽屉把当前选择模型传给标签预览，模型、角色或配置修订变化会丢弃旧预览结果和错误。

旧版本曾允许保存含 DEL（U+007F）的 ASCII 标签。列表读取对此保留原始提示词，供主人查看和修正，数据库记录保持原文，其他字符与字段仍须通过校验。未命中或停用的旧标签按既有匹配规则跳过。新保存内容、已匹配的预览扩写和正式提交继续执行当前严格字符边界，含 DEL 时拒绝使用。

[标签库](../../backend/app/novelai_generation/prompt_tags.py)仅由主人管理当前角色，最多250项；name/category最多80字符、keywords为1–16个各最多80字符的非空字面词、正负提示词各最多4000个 Unicode 码点，可在 V4.5 设置下保存多语言草稿，rating限定general/mature/explicit。标签可引用当前角色 workspace 中的图片附件 `reference_attachment_id`；保存时拒绝跨角色、缺失或非图片附件，列表只返回公共附件元数据，不暴露路径。匹配对当前消息做 NFKC/casefold 字面包含，不执行正则或表达式；同时过滤服务 `nsfwEnabled`（`explicit` 标签必须开启）、角色 `promptTagsEnabled`、`allowedPromptTagRatings` 和条目 enabled，按命中关键词数降序、名称/ID稳定排序后最多取5项。真实主人聊天工具使用当前主人消息自动扩写并冻结结果；工作台和重生成保持显式或历史冻结 prompt，不走这条隐式手动路径；场景 CG 使用候选中冻结的主人消息走独立 CG 扩写。预览与实际 CG 使用同一扩写，最终提示词仍受4000字符上限；库修改推进 revision，旧观察不能使用新标签结果继续生成。

- GET/POST `/api/roles/{roleId}/novelai/prompt-tags`：最多50项分页及 owner 新增，返回 tags/revision/nextCursor 或 tag/revision；旧数据缺少 category/reference 字段时分别回退为 `composition`/`null`。
- PUT/DELETE `.../{tagId}`：仅当前角色既有32位十六进制 ID，更新或删除并推进 revision，跨角色404。
- POST `.../preview` 接受 text、prompt、negative_prompt 和可选 model。text 最多12000个码点，正负提示词各最多4000个码点。指定模型必须属于已知且当前允许的模型，省略时使用当前有效 NSFW/default 模型。只校验实际选中、匹配的提示词是否符合模型语言要求，返回扩写、matchedIds、revision。预览在本地完成。

扩写仅对普通无权重片段按逗号和大小写不敏感去重。基础提示词或单条标签的正负提示词包含 `::`、`{}`、`[]` 任一权重符号时，该完整片段按原文字保留，不拆分或去重其中的词，也不自动补闭合符或改写权重；因此加权片段可能保留重复词。预览、聊天工具和场景 CG 共用此规则，最终各 4000 字符上限不变。依据：[NovelAI 权重语法](https://docs.novelai.net/en/image/strengthening-weakening/)。

[资产写回](../../backend/app/novelai_generation/assets.py)仅在 autoWritebackRoleAssets 明确开启、当前 CG 获准且单图时，设置本角色 cardImage；不改头像。以候选完成时捕获的 cardImageUrl 做 RoleStore 版本 CAS，主人后来手选卡片图优先。生成附件之外的卡片实际字节副本，在写入前另预留角色和全局空间，包含替换时旧副本与新副本的峰值。正常历史及原来源交付不因写回失败重生成。

writebackStatus为not_requested/writing/uncertain/applied/failed。manifest或SQLite故障保留本地义务，维护只核对 owned 临时/备份、manifest及摘要后结算或回滚；崩溃在文件替换后但manifest提交前时恢复旧卡片，不再生成、复制或覆盖后来的主人编辑。恢复每批最多100条并有持久轮转游标。无法确认旧文件事实时保留预占及 uncertain。

仅在原CG准入当时已明确允许写回时，prepare同一事务先登记条件资产责任；它不依赖媒体消息未同步或Runtime未ACK。首次资产记录或实际副本预留落库失败时，该责任仍由原100项恢复队列及持久游标选择，成功生图的历史保持writing。资产记录与额外实际字节预留同事务持久后才转交责任；最终applied/failed释放预留，未获准、原卡片已换或许可ABA只能明确失败。无图/失败生成清条件责任，没有原写回许可的CG保持not_requested；不因后来开关许可扫描或回填旧历史。[真实故障回归](../../tests/python/test_novelai_scene_assets.py)还验证原Runtime结果已实际回收后，该本地责任仍可完成，且不增加辅助、生成或原消息。

`background=true` 先写入有限 attempt、原来源和唯一 outbox，再以同一 submission_key 交接现有 TaskRuntime。只有 taskId 及 `background_accepted` 持久成功后才返回 accepted（工具）或 HTTP202（UI），公开 `backgroundAccepted=true`。父回合完成、父 token 取消或受理后的 HTTP 断开不会撤回这次明确后台；显式停止、许可/来源失效、角色删除和应用退出仍 seal、stop 并 join。交接回写失败不假报受理，不留下隐式付费后台。

交付模式为 foreground_reply、source_completion、replacement 或主人明确选择的 history_only，outbox 主键始终只有 attemptId。后台完成只在原角色锁可用后发布，忙角色以有界试锁跳过并保留恢复义务。桌面和渠道都在现有原 session 写唯一 internal_completion，无 user/run；渠道先持久 sending/consumed，再由真正 ChannelHost.account_send(actor=reply) 向冻结的原 account/thread/chat 发送 owned 附件。原入站的回复/群提及/话题信息来自宿主快照，模型和 UI 没有改投字段。媒体能力不支持时，在生成 POST 前拒绝自动交付；仅历史必须由主人另行明确选择。

重生成是新的明确主人动作：当前配置和 grant 重新核验，新 attempt 关联同一 generation 与 parentAttemptId，省略 seed 时宿主生成与原图不同的 seed，显式沿用旧 seed 拒绝。渠道图片的确认界面在再次消费前说明会向原渠道再次发送一条新的媒体消息；明确选择仅保存历史时，不向原渠道或会话发送媒体。同 requestId 的参数/目标/模式指纹一致才重查原 attempt，不增加 POST。SessionStore.replace_attachment_once 在调用者的同库事务中只替换 expectedAttachmentId，保留其他图、文件、metadata 和顺序；canonical receipt 对键顺序无关，同 attempt 重放不改写。新动作若已能确认原槽不存在，在消费前409拒绝并提示选择仅历史；同 requestId 重查仍返回原事实。生成期间旧槽或消息改变则新图保留历史，deliveryStatus=history_only、errorCode=replacement_conflict，不再次消费。意外多图仍保留 unexpected_multiple_images，不覆盖为槽位冲突。

普通前台已收图而原最终回复失败/不可用时，原角色锁释放后仅能在未消费的同一 outbox 内 CAS 到原来源完成模式；新模式不能绕过 seal 或当前授权。已 sending/sent/uncertain/consumed 的记录不切换模式或重发。渠道实际 sent receipt 的 journal 可在重启后修复 SQLite/消息，已知 sent 不降级；没有 receipt 的 sending 重启为 uncertain，原消息也投影为 uncertain，绝不补发。消息投影与 message_synced 标志同事务，渠道更新事件只公开安全消息投影。

- POST `/api/roles/{roleId}/generations`：可信 UI 操作，requestId、expectedRevision、parameters，另可明确 deliveryMode=history_only。参数 background 默认 false；前台等待结果，后台完成交接后 HTTP202 返回同一 generation 投影。
- GET 同路径：默认20/最多50、nextCursor 分页及持久尝试/存储使用量；桌面主人可明确管理该角色所有来源。
- GET `.../{generationId}?attemptId=...`：该角色、该谱系的精确 attempt；省略 attemptId 兼容返回最新记录。
- POST `.../{generationId}/cancel`：body={attemptId} 停止精确本地尝试并封存展示，不承诺远端退款；省略 body 兼容旧客户端停止最新记录。
- POST `.../{generationId}/regenerate`：requestId、expectedRevision、sourceAttemptId、expectedAttachmentId、parameters，另可 deliveryMode=replacement（默认）或明确 history_only。
- GET `.../attachment/{attachmentId}?messageId=...`：核对当前消息、角色/session、canonical outbox、实际输出及 generationAttemptIds 后返回精确生成记录；不匹配404，不扫描所有历史。

工具查询严格留在实际来源；桌面管理接口的跨来源历史权不能被渠道模型借用。状态包括 prepared、dispatching、received、succeeded、local_pending、unknown、failed_response、cancelled_before_dispatch、rejected；generation 与 delivery 状态分开。图片归属文件和历史不随 TaskRuntime日志 prune 消失。

主人可在后续原会话调用 `manage_image`，必填 `action=get|cancel`、`generation_id` 和 `attempt_id`，编号来自真实生图结果。宿主使用当前真实输入捕获 NovelAI 来源，精确匹配角色和既有业务 scope，并复验原尝试的账号、会话、主人绑定及目录授权；不存在与越权统一拒绝，不开放桌面的跨来源历史权限。群友不能发现或执行此管理工具，也不因此获得 Shell 管理权限。管理旧尝试独立于新生图开关、凭据和次数；取消后仍可查询，来源失效则拒绝。

管理返回有界的生成、`taskStatus`、投递及消费事实，不返回提示词、来源授权或本机日志路径。`taskStatus=null` 表示 Runtime 记录不可用，业务历史仍保留；管理调用成功不代表图片生成或发送成功。取消沿用封存未发布结果、停止并等待原任务、协调账本的流程，清理等待后再复验来源；已发生的取消不会因随后撤权而撤回，但结果不会再交给失效来源。已发送或不确定的真实投递记录不变，不重生成、不重发、不承诺供应商取消或退款。当前受控验收覆盖普通主人 QQ 后续回合，以及自然生成→显式管理、显式生成→自然管理的双向查询和停止。管理 scope 复用稳定会话归属；当前输入与原尝试的完整授权仍分别核验，解绑后重新绑定不会恢复原尝试的管理权。

V4.5 请求常数改编自 Shiori-Agent 固定 `4f266aeaf42fcd611362e2456417b91f659341cf`（Copyright 2026 YinFengWindy，MIT）；完整许可随[模块](../../backend/app/novelai_generation/SHIORI-LICENSE.txt)保留。源码许可不替代 NovelAI 服务条款或图片权利。

## 工作台导航生命周期（2026-10-28，#261）

按 Shiori `d3acaed186b172a78a5892e0307e9affd89759f0` 的 `plugins/novelai/ui/novelAiPageStore.ts` / `novelAiGeneration.ts` 对齐：首次打开后，当前工作台在应用内导航到聊天/角色/Story 时保持挂载但隐藏且不可聚焦。返回时保留草稿、提示标签编辑状态、原请求身份和实际生成结果；离开页面不是取消生成，不新增请求或新消费。仍复用原请求和当前角色范围；关闭整个应用、切换工作台角色或明确取消沿用原前台终止机制，不把前台请求升级为永久后台任务。
