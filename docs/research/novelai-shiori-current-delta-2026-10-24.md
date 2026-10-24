# Meido 与 Shiori NovelAI 生图差异（2026-10-24）

对照基线：Shiori-Agent 固定提交 [31667efb33f371f9161eb35ae7474b8ed48957bc](https://github.com/YinFengWindy/Shiori-Agent/tree/31667efb33f371f9161eb35ae7474b8ed48957bc)；Meido `main` 基线为 `8786b3f`。#209 工作树当前已移植 #211 的 CG 提示词行为，但两项工作仍未合入 `main`，且存在未提交修改。下表凡标注工作树能力都不是当前 `main` 能力；主分支集成仍需单独验证。

补充核验：本地 Shiori 研究仓库当前提交为 1c241a2627bbcc99124aef71abaee237ac057499。该提交相对固定基线的 NovelAI 变化已纳入判断：Shiori 移除了角色级 CG 补充要求设置及其辅助模型注入，改为只保留 autoSceneCgEnabled；自动 CG 统一通过 message_push 投递，并把生成/标签数据迁移到宿主私有 storage。Meido 保留 #211 的角色级要求，是当前 Issue 明确要求的能力；Meido 的角色 workspace 附件、SQLite attempt ledger 和持久 outbox 也不是 Shiori 私有 storage 的一比一复制。Shiori 仍保留自动 CG 的一次异常重试和进程外来源触发，因此这些行为继续按外部消费边界处理。

## 已经存在的差异

| 方面 | Shiori | Meido | 判断 |
| --- | --- | --- | --- |
| 工作台 | 独立 Studio，左侧提示词、右侧画布、底部 filmstrip；历史默认取每角色最近 24 条。 | #209 工作树增加独立工作台，同时保留聊天抽屉；历史分页，支持空、加载、失败和未知状态。工作台入口及本轮改动尚未合并。 | #209 工作树的持久历史浏览更完整；当前 `main` 还没有这项工作台。 |
| 历史复用 | Studio 的“复用提示词”只回填正负提示词；聊天重生成读取请求快照并换 seed。 | 工作台可复用完整参数，包括尺寸、模型、采样器、步数、seed、图生图底图、strength/noise 和后台标记；重生成要求新的明确消费确认。 | Meido 能力更完整，交互边界更严格。 |
| 生成参数 | 工具支持 txt2img/img2img、尺寸、模型、sampler、steps、seed、参考图等；Studio 表单本身不提交 seed/sampler/steps。 | 现有聊天抽屉及 #209 工作台暴露这些参数；图生图只接受当前角色拥有的附件 ID。 | Meido 工作流的输入归属更安全，参数可控性更高。 |
| 聊天自主表情 | Shiori 的 `generate_image` 可由模型直接提交完整生图参数，自动 CG 另有宿主控制器。 | 普通聊天工具调用只让角色提交当前表情的正负英文 tags，由宿主默认 txt2img、单张、默认模型/采样器/尺寸/seed 和原回复投递；schema 仍接受同一 allowlist 内的高级字段以兼容主人明确的历史参数复用。主人仍可在聊天抽屉/工作台手动填写完整参数。历史把这类 attempt 标记为 `chat_expression`。 | 角色获得“是否值得展示”的决定权，同时不会把来源、场景权限或投递目标交给模型；手动参数入口继续保留。 |
| 模式开关 | 全局配置有 `allow_txt2img` 与 `allow_img2img`，可分别关闭两种模式。 | 当前工作树已增加 `allowTxt2Img` / `allowImg2Img`，默认均开启；设置页可分别关闭，参数归一化和场景 CG 辅助调用前都会再次拒绝关闭的模式，旧手动许可随服务 revision 失效。 | 该配置差异已补齐；Meido 仍保留服务、角色许可和额度三重门禁。 |
| 质量与负向预设 | 生成设置弹层提供 NSFW、自动质量标签、负向内容预设和模型信息；预设选项为 0/1/2。 | 质量标签、负向预设、NSFW、模型和模式开关统一放在设置 → 插件 → NovelAI 生图；工作台只填写本次 prompt/尺寸/模型等手动参数，配置变更冻结到之后的 attempt，并在历史中显示当次值。UI 使用“不使用（0）/轻度（1）/重度（2）”。 | 配置入口已集中，工作台不再重复编辑服务级偏好；Shiori 的紧凑弹层和模型信息布局仍不同。 |
| 提示标签扩写 | 聊天工具从当前主人消息匹配角色标签库，按命中项排序后把正负 tags 合并到请求；工作台“复用提示词”只回填显式 prompt。 | 真实主人聊天工具已按当前主人消息自动扩写，并将扩写后的 prompt/negative_prompt 冻结到 attempt/history；工作台 UI、指定图片重生成不读取当前主人消息做隐式手动扩写；场景观察仍使用候选中冻结的主人消息走独立 CG 扩写。 | 聊天工具路径已对齐；Meido 保留更清晰的 UI/重生成输入边界，场景 CG 的消息扩写与手动工具路径分离且可冻结。 |
| NSFW | 有 NSFW 模式和独立 NSFW 模型配置；NSFW 开关还影响成人标签扩写。固定源码中，显式指定已配置模型仍可能绕过默认模型选择。 | 有 `nsfwEnabled` / `nsfwModel`；只有 `nsfwModel` 已加入 `allowedModels` 才能开启，未指定模型时切换默认值，显式模型仍须通过 allowlist。提示标签中的 `explicit` 还要求服务 NSFW 开关和角色 `allowedPromptTagRatings` 同时允许；`mature` 仍只受角色许可控制。 | 默认模型选择和成人标签双重门禁已对齐；Meido 仍保留更严格的模型授权。 |
| 模型/服务地址 | default_model、nsfw_model 和 base_url 可配置。 | 生产请求固定到 https://image.novelai.net；模型和采样器使用有限 allowlist，默认只开放 curated。 | Meido 的远端边界更安全，但可配置性较低。 |
| 自动 CG 触发 | 订阅场景观察事件，可能由被动回合、主动消息等事件安排后台 CG；started/changed 绕过冷却。 | 只在真实主人回合被宿主接受并完成回复后创建候选；不由独立 proactive 回合启动。 | 这是产品/安全边界，不应直接照搬 Shiori。 |
| CG 提示词 | 核心场景观察后，插件再做一次提示词转换，并追加第三人称构图 tags；角色设置可保存按角色隔离的 CG 补充要求。 | 当前工作树的一次辅助模型调用同时返回场景状态和正负 tags；角色补充要求在候选创建时冻结并进入辅助模型 system prompt，宿主再追加并去重第三人称构图 tags。 | Meido 的一次调用少一个模型阶段；角色偏好数据流、构图约束和 attempt 冻结已覆盖，仍需合入 `main` 后做完整集成验证。 |
| CG 冷却/去重 | 进程/插件状态保存会话 turn、视觉 key 和冷却；任务被新事件取消。 | SQLite 持久化 scene state、human-run、scene revision、视觉去重和取消收尾；手动生图与 CG 共享回合互斥 claim。 | Meido 在重启、并发和旧结果隔离方面更强。 |
| 标签库数据与匹配 | 每项有 category、可选 reference image；当前消息命中后按命中关键词数排序，最多取 5 项再合并 tags。 | 每个角色最多 250 项，使用 SQL 分页和 rating 门禁；现已增加 category、角色附件 ID 形式的 reference image，并按命中关键词数降序、名称/ID 稳定排序后取前 5 项；参考图只返回公共附件元数据，不进入 NovelAI payload。 | 标签数据和匹配策略已达到 Shiori 的主要 parity，同时保留 Meido 的角色归属与路径隔离。 |
| 重试 | Auto CG controller 对生图工具异常再调用一次，未按“结果未知”区分是否可重发。默认图数还可大于 1，但当前自动 CG 路径只取第一张。 | 每个 attempt 只允许一次 NovelAI POST，HTTP transport retries=0；超时、断线、取消和无法验证响应保留 unknown，不会自动重发；手动工作台和聊天抽屉允许显式 1–4 张，CG 与单槽 replacement 仍固定单张。 | Meido 更适合有费用的不可幂等请求；Shiori 的行为只能作为源码事实，不能推断供应商幂等或扣费结果。 |
| 持久化 | 输出图、request.json、meta.json、JSONL 索引写入插件目录；普通待附加媒体有进程内状态。 | generation/attempt、附件、SQLite history、media outbox 和渠道 receipt 都持久化；区分生成成功、投递成功和不确定。 | Meido 的恢复和重复投递防护更完整。 |
| 权限 | generate_image 注册为 external_allowed=True；角色扩展保存自动 CG 配置，外部上下文也可能触发工具。 | 手动生图使用公共服务级许可、版本确认和额度；CG 另用角色级许可与辅助模型确认，并校验角色/会话/主人回合/来源。模型不能伪造身份字段。 | Meido 明显更严格，尤其适合渠道和后台任务。 |
| 输入图片 | 工具使用工作区路径，服务从路径读取并校验图片。 | 使用当前角色和来源可访问的附件 ID，服务端解析归属后才读取二进制。 | Meido 避免模型传入任意本机路径。 |
| 结果交付 | 自动 CG 直接走桌面 message_push 或渠道 account_send；源码没有同等持久的插件 outbox/真实 receipt。 | 可选择原来源回复、历史或替换指定附件；outbox 绑定原来源并持久 receipt，发送不确定时不盲目重发。 | Meido 更可恢复，也更不容易错投。 |
| 资产写回 | 全局 `auto_writeback_role_assets` 打开后，普通生图也可写入角色 illustration 并设置聊天背景。 | 只在角色明确开启 CG 写回许可时处理 CG，写入本角色 `cardImage`；使用角色代际 CAS，不改头像，失败不重生成。 | Meido 粒度更细、默认更保守；普通手动生图没有等价的自动写回开关。 |
| 上游错误诊断 | 默认模型收到 HTTP 500 时，服务会用同一 token 访问 `https://api.novelai.net/user/data` 并把订阅状态摘要到错误中。 | 不执行额外账号诊断请求；只根据一次生成 POST 的可验证边界记录失败或 unknown。 | Meido 少一次隐式外部请求，避免把诊断副作用混入付费生成失败处理。 |

## 目前真正还缺的能力

1. **主分支集成。** #209 工作台与 #211 CG 要求已在当前工作树组合验证，但仍未合入 `main`；合并后还需验证角色切换、提示词偏好保存、attempt 冻结及工作台历史查看。
2. **主动来源 CG。** Meido 当前刻意限制为真实主人回合。若要支持 proactive 或无人值守 CG，必须先取得供应商关于 human-action 要求的可核验证据，并另做自主运行授权、额度和原来源投递设计。
3. **多图生成的剩余差异。** Shiori 暴露可配置的 `default_samples`，且自动 CG 源码当前只消费返回的第一张；Meido 工作树已补齐服务级默认值和显式 1–4 张，账本按一次 attempt 保存和展示全部返回图片，响应数量与请求不一致时保留 `unexpected_multiple_images` 诊断。仍不同的是 CG 和单张 replacement 继续固定一张，额度按 attempt 计数而不是按 Anlas 或图片数精确计费。
4. **模型与服务地址配置。** Shiori 可配置 default_model、nsfw_model 和 base_url；Meido 把生产请求固定到 NovelAI 官方地址，并使用模型/采样器 allowlist。Meido 边界更窄，但高级用户的配置空间较小。
5. **提示标签的剩余体验差异。** 分类、参考图和 top-5 匹配已补齐；仍与 Shiori 不同的是参考图沿用 Meido 角色附件生命周期，不能保存原始路径，且不会把参考图自动注入生成请求。后续可继续优化标签卡片筛选和批量编辑，但不影响当前匹配 parity。
6. **聊天触发语义仍有产品差异。** Shiori 允许模型直接传更多生成参数；Meido 让模型只决定是否值得生成并提供表情 tags，服务和交付参数统一在插件设置/宿主侧固定。这是有意的付费安全边界，不是遗漏手动工作台。

## Meido 不应为了“看起来一致”而退让的部分

- 不复制 Shiori 的外部来源宽松准入；NovelAI 是可能产生费用的外部事务。
- 不复制自动 CG 的异常重试和隐式多次 POST；没有供应商幂等保证时，unknown 必须保留。
- 不把图片路径、token、角色或会话身份交给模型决定；这些必须由宿主从可信 UI/回合上下文构造。
- 不把“图片已经生成”当成“渠道已经送达”；生成状态、媒体保存、渠道 receipt 分开记录。

## 核验边界

## 一手来源

- 工作台与历史：Shiori [ImageStudioPage.tsx](https://github.com/YinFengWindy/Shiori-Agent/blob/31667efb33f371f9161eb35ae7474b8ed48957bc/plugins/novelai/ui/ImageStudioPage.tsx)、[ImageStage.tsx](https://github.com/YinFengWindy/Shiori-Agent/blob/31667efb33f371f9161eb35ae7474b8ed48957bc/plugins/novelai/ui/ImageStage.tsx)、[novelAiGeneration.ts](https://github.com/YinFengWindy/Shiori-Agent/blob/31667efb33f371f9161eb35ae7474b8ed48957bc/plugins/novelai/ui/novelAiGeneration.ts)；Meido [NovelaiStudio.tsx](../../web/src/features/novelai/NovelaiStudio.tsx)、[useGenerationDrawer.ts](../../web/src/features/novelai/useGenerationDrawer.ts)。
- 参数与 NSFW/质量设置：Shiori [PromptSettingsPopover.tsx](https://github.com/YinFengWindy/Shiori-Agent/blob/31667efb33f371f9161eb35ae7474b8ed48957bc/plugins/novelai/ui/PromptSettingsPopover.tsx)、[studioForm.ts](https://github.com/YinFengWindy/Shiori-Agent/blob/31667efb33f371f9161eb35ae7474b8ed48957bc/plugins/novelai/ui/studioForm.ts)、[service.py](https://github.com/YinFengWindy/Shiori-Agent/blob/31667efb33f371f9161eb35ae7474b8ed48957bc/plugins/novelai/backend/service.py)；Meido [parameters.py](../../backend/app/novelai_generation/parameters.py)、[NovelaiSettings.tsx](../../web/src/features/novelai/NovelaiSettings.tsx)。
- CG 流程与触发：Shiori [auto_cg_controller.py](https://github.com/YinFengWindy/Shiori-Agent/blob/31667efb33f371f9161eb35ae7474b8ed48957bc/plugins/novelai/backend/auto_cg_controller.py)、[scene_prompt.py](https://github.com/YinFengWindy/Shiori-Agent/blob/31667efb33f371f9161eb35ae7474b8ed48957bc/plugins/novelai/backend/scene_prompt.py)、[auto_cg.py](https://github.com/YinFengWindy/Shiori-Agent/blob/31667efb33f371f9161eb35ae7474b8ed48957bc/plugins/novelai/backend/auto_cg.py)；Meido [scene.py](../../backend/app/novelai_generation/scene.py)。
- 消费、持久化与投递：Shiori [client.py](https://github.com/YinFengWindy/Shiori-Agent/blob/31667efb33f371f9161eb35ae7474b8ed48957bc/plugins/novelai/backend/client.py)、[store.py](https://github.com/YinFengWindy/Shiori-Agent/blob/31667efb33f371f9161eb35ae7474b8ed48957bc/plugins/novelai/backend/store.py)、[plugin.py](https://github.com/YinFengWindy/Shiori-Agent/blob/31667efb33f371f9161eb35ae7474b8ed48957bc/plugins/novelai/backend/plugin.py)；Meido [http.py](../../backend/app/novelai_generation/http.py)、[ledger.py](../../backend/app/novelai_generation/ledger.py)、[application.py](../../backend/app/novelai_generation/application.py)。

本记录来自固定源码、现有 fixture 和本地测试：当前工作树已通过组合场景/配置和前端角色设置测试；完整 NovelAI 后端、前端、类型检查和构建结果以最新交付记录为准。仍未合入 `main`，没有使用真实 NovelAI token、账号或付费请求，也没有验证 Anlas 扣费、取消退款、当前模型可用性或真实渠道回执。NovelAI 官方公开契约目前不能为重试、幂等和无人值守 CG 提供足够保证，相关判断只描述两边源码行为。

相关 Meido 源码：[NovelaiStudio.tsx](../../web/src/features/novelai/NovelaiStudio.tsx)、[GenerationDrawer.tsx](../../web/src/features/novelai/GenerationDrawer.tsx)、[parameters.py](../../backend/app/novelai_generation/parameters.py)、[config.py](../../backend/app/novelai_generation/config.py)、[scene.py](../../backend/app/novelai_generation/scene.py)、[ledger.py](../../backend/app/novelai_generation/ledger.py)、[application.py](../../backend/app/novelai_generation/application.py)。
