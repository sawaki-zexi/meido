# Shiori 角色生图一致性来源核查

状态：当前源码研究；未检查用户所见图片的实际请求记录，不代表真实生成效果验收。

最后核验：2026-10-29（Asia/Shanghai）。事实来源：Shiori 固定提交 `d149de7f052f77c5bb88d4d1393d48dce1870875`；同时通过 GitHub 读取最新 main 的场景控制器、Story director/image_generator/service/rpc、NovelAI service/plugin/prompt_tags、角色 store 和 prompt_block，以下关键行为仍一致。

## 结论与前述回答的修正

**已提取：** Shiori 有多条把稳定角色信息送入绘图提示词的路径。没有自动 Character Reference 接入，不能推出“它没有保证角色外观的机制”，更不能据此推出“必须新增 Character Reference 才能画得像”。稳定且足够具体的外观文字可反复进入 tags；用户观察到三种入口都像角色，与这些实现并不矛盾。源码能证明输入路径，不能证明某一张图片的视觉质量或该安装实际使用了哪组设置。

**纠正：** 自动 CG 的 `role_description` 不是 `role.description` 简介，而是 `role.system_prompt` 的值，经 `SceneDecisionInput.role_prompt` 传入。也不要把它误称为当前 `RolePromptCompiler` 的完整输出：保存新版 profile 时，兼容 `system_prompt` 字段优先取 `behavior_rules`，空时取 `profile`。因此具体安装该字段究竟包含多少外貌信息，仍需检查其实际角色记录。[场景输入](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/scene/controller.py#L210)、[事件赋值](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/scene/controller.py#L272)、[兼容字段映射](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/roles/store.py#L350)。

## 三条生成路径

| 入口 | 已证实进入模型的身份和外观来源 | 实际生图方式 |
| --- | --- | --- |
| 普通聊天画自己／表情 | 编译后的角色 profile、性格、规则等；独立注入的 SELF 自我认知；当前对话。模型自行把这些信息转成英文 tags | `generate_image` 接受完整 prompt；可选择文生图或显式图生图 |
| 自动场景 CG | 场景观察器读取 `system_prompt[:4000]` 和最近对话，明确要求视觉描述保留角色可见外观与动作；后续绘图转换器收到未在此处截断的 `system_prompt` 字段、视觉描述、对话，以及该角色单独配置的绘图补充要求 | 自动控制器设置 `mode=txt2img`，转换器输出正负 tags 和画幅 |
| Story | 建立故事时冻结角色记录；Director 每次收到快照中的名称、简介、`system_prompt`、背景，另有玩家外观、近期剧情与摘要 | Director 写完整 `visual_prompt`；图片适配器用 `txt2img`、`nai-diffusion-4-5-full` 提交 |

来源：[角色编译](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/roles/role_prompt_compiler.py#L34)、[SELF 注入](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/prompt_block.py#L212)、[场景观察指令](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/scene/decision.py#L18)、[场景绘图转换](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/scene_prompt.py#L43)、[CG 文生图设置](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/auto_cg_controller.py#L150)、[Story 快照](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/story/backend/service.py#L68)、[Story Director 输入](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/story/backend/director.py#L100)、[Story 图片提交](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/story/backend/image_generator.py#L35)。

**已提取：** Story 对同一个 `sceneKey` 已有成功的人物 CG 时跳过自动追加生成。因此连续阅读中看到稳定人物画面，有时是复用同一张已生成图片，不一定是多次生成都复现同一张脸。[复用条件](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/story/backend/rpc.py#L503)。

## 标签、原图和重生成各自起什么作用

- **已提取：标签库可补固定外观文字，但不是自动角色绑定。** 聊天工具取 `current_user_message` 作为匹配文本；最多匹配五条已启用条目，合并正负 tags。没有标签文件时返回空列表，没有默认内置某个角色的外观词。条目配图 `image_path` 不参与 `expand` 的生成参数。是否命中用户为角色维护的 tags，需要实际生成记录或标签配置证明。[工具入口](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/tool.py#L147)、[缺省与扩写](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/prompt_tags.py#L82)。
- **已提取：图生图才显式读取原图。** 传 `mode=img2img` 和 `base_image_path` 后，服务读取图片并写入 `parameters.image`，同时发送 strength/noise。没有发现普通新生成流程自动把角色头像或立绘塞进该字段；自动 CG 和 Story 入口均明确使用文生图。[原图加载及提交](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L114)。
- **已提取：重生成保留原请求，只更换随机种子。** 这会保留 prompt、模型、尺寸，以及原请求中可能存在的图生图或参考参数；不能用“新请求的参考数组为空”断言所有历史重生成都不可能携带参考图。[重生成](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L164)。
- **已提取：当前内置新生成流程没有专门的 Character Reference 输入。** 参数构造中的 `reference_image_multiple` 等数组为空，未见 director reference 参数。已检查 bundled NovelAI 的调用前 hook：它处理自动 CG 冷却、去重和第三人称构图，不是外观图片注入。这里不涵盖用户另装的自定义插件或外部修改。[参数构造](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L392)、[调用前 hook](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/plugin.py#L180)。

## 能下的结论与尚缺的证据

**推断：** 三条入口都可通过稳定角色文字与模型生成的稳定外观 tags 取得较好的形象一致性；CG 绘图补充要求、可选标签库、重复使用历史生成参数、Story 复用人物 CG 都可能进一步强化用户看到的一致性。这是现有实现提供的可解释机制，不等同于逐像素保证。

**已提取：** 内置生成服务在返回后验证图片可解析并保存，没有把生成图片与角色立绘做外貌一致性评分、失败后按相似度重画的校验环节。自动 CG 的重试针对生成/结果错误，不是“画得不像”。[结果解析](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L460)、[CG 重试](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/auto_cg_controller.py#L219)。

**未知：** 用户所见安装具体用了哪个模型、怎样的角色定义/SELF/绘图补充要求、最终 prompt 是否命中标签、图片是新生成还是重生成/复用，尚无该次真实记录。最有区分力的下一份证据是其成功作品保存的请求快照：查看 `input`、`action`、`model`、`parameters.image` 和参考字段，并与角色外貌文字逐项对照。不需要先改架构或要求用户补一套新配置。

**比较边界：** 本记录不声称上述文字上下文路径是 Meido 完全缺失的 Shiori 独有能力。Meido 已有角色 prompt、CG 角色文本和 Story 角色快照；要解释两边效果差异，还需比较实际进入图像请求的外观词，而不能只按功能名或是否支持 Character Reference 判断。
