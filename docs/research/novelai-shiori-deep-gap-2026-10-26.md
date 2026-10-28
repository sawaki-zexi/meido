# NovelAI：Shiori 深度源码对照与下一轮差距

状态：当前源码核验；第 3 节为修复前的固定基线，本轮实施与验证见第 8 节。  
最后核验：2026-10-26。  
事实来源：以下固定提交的源码、测试和 NovelAI 官方文档；未以旧 Research 或草案推断当前实现。

| 对象 | 固定版本 | 本文中的含义 |
| --- | --- | --- |
| Shiori-Agent | `d149de7f052f77c5bb88d4d1393d48dce1870875` | 只读克隆中的 main 快照 |
| Meido main | `048c8ee7001e3557d4f8f6075fb74c5de5a6dafd` | 已合入能力的基线 |
| Meido PR #248 | `b86be29d6ee97879dcb10e954edc47284e6d76ec` | 启用流程修复的核验快照；尚未合入 |

[Issue #246](https://github.com/sawaki-zexi/meido/issues/246)、[PR #248](https://github.com/sawaki-zexi/meido/pull/248) 在核验时仍开放；PR 目标为 main。本文通过固定版本差异区分已有与新增，不把 PR #248 的修复重复列为缺口。当前开放的其他工作未覆盖本文的加权标签合并问题；开新 Ticket/分支时仍应再次检查。

## 1. 结论

**已提取：** Meido 已具备文生图、图生图、服务参数、标签库、历史、多图下载、指定图片重生成、后台原来源交付和自动 CG。PR #248 已补齐启用门槛、工具可见性、就绪与供应商错误说明、等待中 CG 候选的版本冻结。这些都不应再次作为“从 Shiori 补上”的任务。

**已提取与实验：** 最值得优先修复的是**加权标签被普通逗号去重破坏**。Meido 和 Shiori 都存在这一问题；三个合成案例分别丢失 `::`、`}`、`]` 的闭合片段。Meido 的修复可只影响标签合并与行为测试，不依赖 PR #248。

**实施范围：** 本轮以 #249 修复加权正负 tags 的扩写，以独立 #250 补齐标签库搜索与筛选。下一轮再处理可定位、可修正的提交错误与历史参数复用语义；共享场景核心、Story 模式和真正的参考图条件生成属于更大的产品与协议范围。

## 2. 能力矩阵

“主线已有”指基线已存在，引用当前 PR 行号的条目已核对差异；“248 已补”不表示已部署。

| 能力 | Shiori 固定源码 | Meido main / PR #248 | 判断与证据 |
| --- | --- | --- | --- |
| 启用与工具发现 | 插件注册 `generate_image`；本地检查 Token 就绪 | main 有额外消费确认门槛；248 去除门槛，启用后即使缺凭据或次数不足也保留工具并说明原因 | **248 已补。** [S-工具]、[M-就绪]、[M-工具]、[M-就绪测试] |
| 提示标签库 | 全工作区 JSON；分类、匹配词、正负 tags、rating、参考图；关键词命中数排序，取前 5 | **主线已有**角色隔离、最多 250 条、分页、NFKC 匹配、前 5、rating/服务开关双重约束、扩写预览 | 无需重做基础 CRUD 或匹配。[S-标签]、[M-标签]、[M-扩写]、[M-标签测试] |
| 标签参考图 | 复制为插件自有参考资产；展示在标签卡片 | **主线已有**本角色图片附件引用、上传、展示；禁止跨角色或文件引用 | 两边是**标签示意图**，不是发送给模型的参考条件。[S-参考]、[S-标签测试]、[M-标签归属]、[M-标签测试] |
| 权重 | 没有独立权重字段；ASCII 中的 `{}`、`[]`、`::` 可进入提示词 | **主线已有**同样的语法透传，但合并按逗号去重 | **共同正确性缺口**，详见第 3 节。[S-合并]、[M-合并]、[M-标签去重测试] |
| 英文校验 | 生成前及扩写后检查 `isascii()`；错误指出具体 prompt 字段 | **主线已有**ASCII、控制字符、长度校验；标签编辑器本地也校验 | 后端规则已齐；生成表单的定位与恢复仍可改进。不能宣称官方完全不支持自然语言。[S-校验]、[S-校验测试]、[M-参数]、[M-标签交互] |
| 模式、模型、尺寸 | txt2img/img2img；V4.5 curated/full；1024²、1216×832、832×1216；自定义单边≤1024 | **主线已有**相同预设、受限模型/采样器列表；自定义额外要求 64 倍数；最大 28 步、像素上限 | Meido 边界更明确，不能将额外限制描述为遗漏。[S-参数]、[M-配置]、[M-参数]、[M-参数测试] |
| 图生图强度与噪声 | 底图路径；默认 strength=.7、noise=.2；分别约束 .01–1、0–.99 | **主线已有**同范围与默认值；改为来源允许的附件 ID | 不缺图生图或强度滑项；原始路径不能照搬。[S-生成]、[S-参数]、[M-参数]、[M-来源测试] |
| 多图 | 请求用全局 default_samples；实际解析只取第一张，自动 CG 也只投第一张 | **主线已有**手动 1–4 张、保存与展示全部返回图片、选择下载/重生成；替换单槽和 CG 限一张 | Meido 此处能力更完整。[S-解析]、[S-CG重试]、[M-媒体]、[M-多图测试] |
| 历史与下载 | 输出图片、request.json、meta.json、JSONL；工作台显示作品与复用提示词 | **主线已有**持久 attempt/附件/状态；工作台复用参数、逐张下载、失败历史 | 不能再提“补历史/下载”。Shiori 的本插件舞台只有复用按钮，本文不推断宿主全部通用图片菜单。[S-保存]、[S-历史]、[S-工作台]、[M-工作台]、[M-多图测试] |
| 聊天图片重生成 | 取原 request 快照，只换 seed；原媒体槽 CAS 替换；失败保留旧图 | **主线已有**新 attempt/seed、指定附件 CAS、原来源重投或仅保存历史；复用不等于完整请求重放 | 小差距在质量开关/负向预设的复用语义，见第 4 节；不是缺少重生成。[S-重生成]、[S-替换测试]、[M-重生成]、[M-重生成测试] |
| 场景到 CG | 核心观察中立场景，发布冻结事件；插件准入后再转为供应商 tags | **主线已有**单次辅助请求同时返回场景状态与 tags；角色补充要求和第三人称构图已存在；248 提前冻结候选版本 | 有架构差异，无证据说明拆成两次调用必然画得更好。[S-观察契约]、[S-CG转换]、[M-CG观察]、[M-CG提交]、[M-CG冻结测试] |
| 冷却、去重、手动优先 | KV 持久会话 turn、最近成功 visual；已有工具调用抑制 CG；新观察取消旧任务 | **主线已有**真实主人回合计数、已发 visual 持久去重、manual claim 抑制、取消与清理；248 防止等待候选继承新配置 | 保留 Meido 的更强不确定结果隔离；精确冷却边界并不相同，见第 5 节。[S-CG策略]、[S-CG准入]、[M-CG提交]、[M-CG策略测试] |
| 后台与恢复 | 自动 CG 用 retaining runtime task；成功后记录冷却再投递 | **主线已有**受管任务、后台接受、原来源 outbox、received/local_pending 本地恢复；unknown 不重生成 | 不应替换为较轻的插件异步任务。[S-CG准入测试]、[S-CG执行]、[M-后台测试]、[M-恢复]、[M-恢复测试] |
| 失败说明 | 稳定 RPC 类别；某些 500 另读账户资料；CG 异常重试一次 | 248 补稳定供应商错误文案；**主线已有**单次 POST、脱敏、不确定状态、不额外访问账户 | 错误展示已改进，付费自动重试不可照搬。[S-错误]、[S-诊断]、[S-CG重试测试]、[M-HTTP]、[M-就绪测试] |
| 角色素材 | 全局自动写回普通生成到 illustration，并设聊天背景；重生成关闭该写回 | **主线已有**角色独立 CG 写回许可，仅设 card image；CAS 保留主人后来修改；本地失败不再生图 | 普通历史作品缺少直接选作角色素材的动作，但不是数据保存缺失。[S-保存]、[S-重生成]、[M-资产]、[M-资产测试] |
| Story 消费者 | 独立 Story 插件依赖 NovelAI API；横图/full 模型；资源结果独立于叙事 Turn | 当前 NovelAI 模块未建立同等 Story 资源域 | 大功能，不能作为生图小修顺带实现。[S-Story依赖]、[S-Story]、[S-Story结果]、[S-Story测试] |

## 3. 已复现：标签去重破坏权重范围

### 3.1 事实与最小案例

NovelAI 官方说明：数值强调从 `1.5::` 一类起点延续到结束 `::`；花括号与方括号会改变其后文本的权重，闭合符恢复范围。数值强调支持 V4 及以上，负数强调支持 V4.5 及以上。这里只据此解释文本语法，不推断实际图片质量。[官方权重说明](https://docs.novelai.net/en/image/strengthening-weakening/)

Meido 的 `_append` 把基础 prompt 和新增字符串按逗号拆开，对新增片段做大小写不敏感去重；Shiori 的 `_merge_tags` 也按同类字符串去重，且其编辑器将逗号输入拆为 tag 列表。它们均不理解“包含闭合符的片段必须保留”。[M-合并]、[S-合并]、[S-标签编辑]

已运行的合成实验：

| 基础 prompt | 新增标签内容 | Meido 实际输出 |
| --- | --- | --- |
| `1.2::red hair, blue eyes::` | `1.8::green dress, blue eyes::, smile` | `1.2::red hair, blue eyes::, 1.8::green dress, smile` |
| `{red hair, blue eyes}` | `{green dress, blue eyes}, smile` | `{red hair, blue eyes}, {green dress, smile` |
| `[red hair, blue eyes]` | `[green dress, blue eyes], smile` | `[red hair, blue eyes], [green dress, smile` |
| `flowers` | `Flowers, sunlight` | `flowers, sunlight`，原普通去重正确 |

**已提取：** 第一例在 Shiori 同样输出缺少第二个 `::` 的结果。两个实现均有问题，不存在可直接复制的上游修复。

**推断：** 第一例的 `smile` 本应处于第二段强调之外，合并后会进入 1.8 的强调范围。Meido 自动 CG 后续追加的构图 tags 也可能继承这个未闭合范围。正向和负向使用相同 helper，影响两者；风险是请求文本语义已改变，不需要真实生图才能确认。[M-扩写]、[M-CG提交]

当前 Meido 测试验证普通词项的大小写去重、排序前五、rating 和引用归属，没有覆盖此权重闭合案例；Shiori 相关测试也只验证普通 tag 合并。[M-标签去重测试]、[M-标签测试]、[S-标签测试]

### 3.2 实验边界

在固定工作区用 Python 标准库 `ast` 只提取源码中的 `_append` / `_merge_tags` 函数，编译后传入上表字符串。没有导入应用、初始化数据库、读取凭据、调用模型或供应商，也没有写临时文件。实际输出已经在本轮工具记录保留。

这是调研阶段的纯函数级复现；随后 #249 已通过公开 preview 和隔离 HTTP peer 验证聊天、CG 的最终请求及历史，见第 8 节。真实供应商调用不在这些测试之内。

## 4. 剩余差距的排序

| 优先级 | 完整用户结果 | 依据、性质 | 与 #248 的关系 |
| --- | --- | --- | --- |
| **P1 / 首选** | 标签库的加权正负 prompt 自动加入聊天或 CG 后仍保持原有强调范围 | 已复现共同缺陷；影响实际送出的文本语义。第 3 节 | `prompt_tags.py`、`test_novelai_prompt_tags.py` 未被 248 修改，可从最新 main 独立修 |
| **P1 / 后续** | 主人提交无效 tags、尺寸或底图时知道哪项有误，直接修改；结果不明时继续重查原请求 | 已提取错误被泛化，恢复体验差；不是消费幂等缺陷。[M-API错误]、[M-提交交互] | `api.py`、工作台、hook 都由 248 修改，等待其合入再做；或维护者决定并入原 #246 范围 |
| **P2 / 可独立** | 在较多标签中找到目标、按分类/分级筛选，编辑后保留查找位置与正确预览 | Meido 当前长列表+显式分页；Shiori 是卡片+独立编辑器，也**没有**搜索/筛选。是新改进，不是已存在上游能力移植。[M-标签UI]、[M-标签交互]、[S-标签UI] | 纯前端查找可不依赖 248；避免与权重修复共同修改后端匹配语义 |
| **P2 / 先定产品语义** | 复用旧作品时清楚知道哪些生成设置保留，哪些采用当前默认 | Shiori 重放完整请求；Meido 不复制 qualityToggle/ucPreset，而当前设置重新注入。是确定差异，不直接认定违反已批准规范。[S-重生成测试]、[M-旧图参数]、[M-参数] | 相关 hook/application 正由 248 修改；需在它合入后做，不能借快照绕过当前权限 |
| **P2 / 可后续独立设计** | 从历史选定一张生成图，明确应用到当前角色卡片/头像，不用下载再上传 | Meido 有下载和角色上传裁切；自动写回仅 CG。可复用现有图片归属与卡片写入能力。[M-工作台]、[M-角色上传]、[M-资产] | 工作台同文件需等 248；不要直接扩大自动写回范围 |
| **P3 / 大功能** | 多消费者共享场景观察，或独立 Story 画廊/剧情资源，或真正的风格/角色参考条件 | Shiori 前两项提供架构样例；真实 reference 不是两边标签示意图已实现能力。第 5、7 节 | 先 Spec/ADR、消费与来源设计和人工架构批准；不与本轮小修混做 |

### 错误恢复的准确范围

Meido 后端对 `ValueError` 统一返回“生图参数、请求或版本冲突”；表单提交前仅检查就绪、模式和替换张数，随后冻结 requestId/参数。422 也进入通用 catch，表单保持冻结，按钮变为重查。工作台**已有“另开一次生成”按钮**，所以不是完全卡死；但已知本地参数错误和不确定的已发请求没有形成不同的修正路径。[M-API错误]、[M-提交交互]、[M-工作台]

Shiori 把错误分类并结束 submitting，仍可编辑参数；它没有 Meido 的同等消费账本，因此不能照搬“所有失败都释放请求身份”。建议只对宿主能证明**尚未准入或出网**的字段错误开放原地修改，unknown/传输失败继续保留原请求。既有前端测试已固定网络失败重查使用原 requestId，必须保留。[S-失败UI]、[M-前端重查测试]

### 复用与完全重放的准确范围

Shiori 的 `regenerate` 对原 request_payload 做 deepcopy，只换 seed，测试甚至保留快照中的 reference 数组。这个测试是**快照重放能力**，不能反推 Shiori 的正常生成入口已经支持参考条件，因为正常构造的数组仍为空。[S-重生成]、[S-重生成测试]、[S-参数]

Meido `loadParameters` 未复制 qualityToggle/ucPreset；后端新请求把当前服务值写入。既有规格也要求未来新请求遵守当前设置，故较稳妥的第一步是明确“复用所列参数，其余采用当前全局设置”，并显示差异；若要增加“相同画面条件只换 seed”，需要显式设计受信历史值的复用，仍重新校验模型、底图和来源。不能直接接受模型提交任意旧 payload。[M-旧图参数]、[M-准入]、[M-参数]、[M-重生成权限测试]

## 5. 场景观察、CG 与 Story 的真正差异

### 5.1 两层处理与一个受限调用

Shiori 数据流是：

`已完成对话 → 核心中立场景观察 → SceneObservationCommitted → NovelAI 冷却/去重准入 → 供应商 prompt 转换 → 生图 → 原来源图片`

核心输出只有 transition、scene_key、visual_key、visual_description；最近历史与角色资料是观察数据。插件只在可生成时调用第二个提示词模型，角色绘图要求单独放入 system instructions，场景快照仍是数据。[S-观察契约]、[S-观察发布]、[S-CG准入]、[S-CG转换测试]

Meido 数据流是：

`主人真实回合完成 → 冻结候选 → 受管 attempt → 一次辅助观察与 tags → 提交状态/去重/冷却 → 单次 NovelAI POST → outbox/历史`

角色补充要求、角色资料、前场景、主人与回复均在候选保存；模型输出包含状态与 tags，统一验证后才生成。248 额外冻结等待中的候选服务版本和角色版本，防止等待旧任务清理期间继承新设置。[M-CG冻结]、[M-CG观察]、[M-CG提交]、[M-CG冻结测试]

**推断：** 共享中立事件适合未来多个消费者，但当前 Meido 只有这条 CG 链路，直接拆两次模型调用会增加阶段、预算和失败状态。Shiori 观察器还允许一次协议修复请求，不能笼统称“拆层能减少模型费用”。Meido 对未出图观察同样计辅助尝试，有明确测试；是否优化必须先定义新预算语义。[S-观察调用]、[M-CG预算测试]

### 5.2 冷却边界与去重并不完全一致

- Shiori 保存 last_success_turn，`turn-last <= 5` 时阻止普通同场景 CG，通常是成功后的第六个被动回合才放行；started/changed 可绕过冷却。它只比较最后成功的 visual_key。[S-CG策略]、[S-CG准入]
- Meido 保存 last_dispatch，差值 `< 5` 阻止，同场景第五个真实主人回合即可再次生成；测试明确断言这个边界。它对原来源已发出的 visual_key 保留持久记录，unknown 也不会因未成功而释放同一画面；changed 不能绕过已发 visual 去重。[M-CG提交]、[M-CG策略测试]

这是可验证的政策差异，不应仅为“对齐”修改 Meido 的已测试五回合约定。Shiori 对成功后投递失败仍保留冷却值得借鉴，但 Meido 已用更强的 dispatch/claim/outbox 边界覆盖。

### 5.3 不把 Story 误写成共享场景事件的现有消费者

Shiori Story 显式依赖 NovelAI 插件导出的 ImageGenerationAPI，由自己的 director 提供完整 visual_prompt，再以 `story:<id>` 会话和资源键调用生成；它不是直接订阅普通聊天 SceneObservationCommitted 来构建剧情。其资源重试不会创建新的叙事 Turn；图片错误写入资源状态。[S-Story依赖]、[S-Story]、[S-Story结果]、[S-Story失败]、[S-Story测试]

**计划：** 如果 Meido 要 Story，应先定义叙事版本、视觉资源版本、用户发起的重生成、未知消费和原来源归属；可借鉴垂直用户流程，不复制 `intent=scene_cg` 绕过宿主观察准入。

## 6. 建议第一轮：加权标签安全扩写

以下方案已形成 [Issue #249](https://github.com/sawaki-zexi/meido/issues/249)，实施结果见第 8 节。

**What to build：** 主人为角色保存带 `{}`、`[]` 或 `数值::内容::` 的正负提示标签后，本地预览、真实主人聊天和自动 CG 都完整保留强调范围；普通 tags 仍按现有规则去重。主人可以先在本地预览看见确切结果，再由既有生图入口消费。

**范围与数据流：** 只调整“基础 prompt + 已匹配标签 → 最终 prompt”的合并，不改变关键词排序、rating、匹配前五、角色引用、服务参数、CG 调用次数、来源、requestId 或版本。带权重语法的片段不能被按逗号盲拆去重；可以保守保留完整片段，或做足够小的语法感知分段。不要自动加闭合符或改写用户权重，因为官方语法允许开放与复杂组合，修补输入本身会引入新的语义变化。

**验收边界：**

1. 第 3 节数字/花括号/方括号三个案例经公开标签 preview 返回完整结束标记；正向、负向都覆盖。
2. 不同权重中的同名词项不能被跨范围删除；普通 `flowers + Flowers, sunlight` 仍得到单个 flowers。
3. 原输入顺序与权重字面值保持；未匹配标签不加入。原有 role/rating/最多五条和 4000 字符约束继续有效。
4. 一条隔离的聊天或 CG 集成用例证明最终 payload 与预览的标签片段一致；CG 强制追加构图词不会因合并丢标记而意外进入前一权重段。
5. preview 不触发模型或生图；既有生成流程仍最多一次 POST，不增加任何重试。
6. 历史保留实际发送的 prompt；不追改已经生成或结果不明的旧 attempt。

**测试决定：** 以现有 `test_novelai_prompt_tags.py` 的公开 API 预览为红测试入口；必要时新增独立集成测试文件复用主线 fixture，避免修改 248 正在变动的测试。先验证失败，再最小实现；相关标签、参数和真实工具/CG扩写测试通过即可，不因调研重跑全量检查。报告本身只有文档检查。

**风险：** 保守保留权重片段可能比原来保留更多重复词，应优先保持用户语义，并继续执行最终长度限制。不要扩展到完整 NovelAI prompt 编译器、Prompt Chunks 服务、云端词库或参考图生成。

**分支与依赖：** 建票时核对开放 Issue/PR；获取最新 main 后从 main 建 `fix/<issue>-novelai-weighted-tags`。核验时该文件和标签测试未被 248 修改，因此不依赖 248；不得从 248 分支继续开堆叠 PR。若将来 main/其他 PR 改了相同合并行为，重新确认顺序。

## 7. 后续依赖与不应照搬的行为

- **真实参考条件属于新增协议。** 两边正常 V4.5 payload 的 reference 数组均为空；img2img 的 image/strength/noise 是另一条已存在路径。官方 Vibe Transfer 有参考强度与信息提取量，Precise Reference 还有独立行为与费用；这要求拥有者选择图片、参数冻结、费用/缓存与未知结果边界，不能把标签示意图自动上传。本文不提出自动启用。[S-参数]、[M-参数]、[官方 Vibe Transfer](https://docs.novelai.net/en/image/vibetransfer/)、[官方 Precise Reference](https://docs.novelai.net/en/image/precisereference/)
- **保留一次 POST 和 unknown。** Shiori controller 对异常再次调用工具，测试要求两次调用；它的服务成功持久化或写回失败都可能落入异常。Meido 已有单次 POST 和本地恢复，应保留。不能用 seed、成功文字或一般网络异常推断没扣费。[S-CG重试]、[S-CG重试测试]、[S-保存]、[M-HTTP测试]、[M-恢复测试]
- **保留来源权限。** Shiori 工具注册为 external_allowed，但核心观察明确跳过 external context；不能说它所有访客都能自动触发 CG。Meido 的可信主人输入、渠道账号/线程和图片附件范围更明确，不因对齐而开放访客、被动群消息或无人值守付费。[S-工具]、[S-观察来源]、[M-来源]、[M-来源测试]
- **不增加隐式账户诊断。** Shiori 某些 500 再 GET /user/data；Meido 不需要为了显示错误自动扩大外部请求。248 的稳定脱敏解释已满足当前修复目标。[S-诊断]、[M-HTTP]、[M-就绪测试]
- **不替换更可靠的资产与交付。** 普通生成自动替换聊天背景会扩大副作用；Meido 后来的手动卡片修改应继续胜出。选择历史图应用为素材可以另做明确动作。[S-保存]、[M-资产测试]
- **不盲升模型或重解释 ucPreset。** 两边固定实现以 V4.5 为主。官方当前模型文档包含更新模型和自然语言能力，但这不足以验证当前请求字段、响应、权限和测试兼容。这里不从枚举名字推断数字预设的供应商语义，也不改变已有 0/1/2 映射。[S-参数]、[M-配置]、[M-参数]、[官方模型说明](https://docs.novelai.net/en/image/models/)

实施顺序：独立权重合并修复与标签查找各用一票一分支 → 248 合入后完善错误修正与复用语义 → 根据实际使用量决定显式素材应用 → 有明确多消费者/Story需求时再讨论场景核心。标签查找不改变后端匹配政策。

## 8. 本轮实施与验证

- [#249](https://github.com/sawaki-zexi/meido/issues/249)：从 `048c8ee` 建独立修复分支，保守保留含权重符号的完整片段，不自动补符号。普通去重、角色权限、前五项和长度限制保持；公开规则回写 [Reference](../reference/novelai-generation.md)。
- 加权预览先出现 6 个预期失败，修复后标签用例 12 通过；追加的合并长度约束 1 通过，聊天/CG 真实本地 HTTP 流程 2 通过；既有普通扩写与参数回归 24 通过。Python 源码编译通过。命令及日志位于工作区 `.data/validation/`；这些是分组结果，未重复累计同一用例。
- [#250](https://github.com/sawaki-zexi/meido/issues/250)：同样从 `048c8ee` 建独立前端分支，先完整读取当前角色有界分页，再按名称、关键词、正负提示词和分类/分级/启用状态查找。分页失败或 revision 变化不展示局部结果；角色切换清空筛选，编辑草稿不受筛选影响。首轮生图前端范围 65 项通过，其中包含 8 项标签交互；审查补充分类编辑场景后，标签交互 9 项重验通过。TypeScript、网页和桌宠构建及修正后复验通过，不将重叠结果相加。
- #248 的渠道 CI 发现测试替身仍使用旧 `unavailable_reason` 接口；补提交改为 `tool_enabled`，19 个渠道连续接收用例通过。这是夹具适配，没有再次修改生产行为。三个分支均待合入，正式实例未更新。

## 9. 证据与限制

调研阶段完成固定源码、相关测试阅读、Issue/PR 核对及纯函数实验；实现阶段仅运行上述隔离测试和构建。没有读取真实 Token、调用真实辅助模型或 NovelAI、执行真实渠道投递或修改生产数据。

现有测试证明的是其断言的行为，不等同真实供应商画质、余额、扣费、退款、当前模型可用性或全部网络响应契约已经验收。本文的实际图片影响判断限于“请求文本权重范围改变”；没有宣称观察到了某张真实错误图片。

本报告补充而非覆盖 [2026-10-26 就绪诊断](https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/docs/research/novelai-shiori-meido-diagnosis-2026-10-26.md)。旧 [2026-10-24 对照](novelai-shiori-current-delta-2026-10-24.md) 中“工作台/CG 尚未合入”等句子属于当时状态，不作为本轮现状依据。稳定结论在实际落地后回写相应 Spec/Reference。

## 源码与测试索引

以下链接全部固定提交。M 链接中，未被 #248 修改的事实优先指向 main 基线；涉及 #248 文件的链接指向其核验 HEAD。

[S-标签]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/prompt_tags.py#L128-L159
[S-合并]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/prompt_tags.py#L225-L233
[S-参考]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/prompt_tags.py#L65-L115
[S-标签测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/tests/test_prompt_tags.py#L108-L190
[S-标签UI]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/PromptTagGrid.tsx#L36-L92
[S-标签编辑]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/PromptTagEntryEditor.tsx#L38-L85
[S-标签交互]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/usePromptTagLibrary.ts#L62-L117
[S-标签UI测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/PromptTagGrid.test.tsx#L19-L48
[S-校验]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/prompt_validation.py#L4-L10
[S-校验测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/tests/test_service.py#L194-L219
[S-参数]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L331-L445
[S-生成]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L75-L162
[S-解析]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L460-L511
[S-保存]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L234-L303
[S-历史]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/store.py#L64-L135
[S-重生成]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L164-L211
[S-重生成测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/tests/test_service.py#L33-L103
[S-替换]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/rpc.py#L86-L125
[S-替换测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/tests/test_rpc.py#L243-L348
[S-工作台]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/ImageStage.tsx#L33-L77
[S-失败UI]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/novelAiGeneration.ts#L60-L94
[S-错误]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/failures.py#L97-L130
[S-诊断]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L516-L561
[S-观察契约]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/scene/contracts.py#L11-L67
[S-观察调用]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/scene/decision.py#L18-L75
[S-观察发布]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/scene/controller.py#L250-L276
[S-观察来源]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/scene/controller.py#L87-L169
[S-观察需求]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/core/scene/demand.py#L8-L26
[S-CG转换]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/scene_prompt.py#L34-L97
[S-CG转换测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/tests/test_scene_prompt.py#L27-L104
[S-CG准入]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/auto_cg_controller.py#L86-L119
[S-CG执行]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/auto_cg_controller.py#L139-L217
[S-CG重试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/auto_cg_controller.py#L219-L249
[S-CG重试测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/tests/test_auto_cg_controller.py#L216-L315
[S-CG准入测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/tests/test_auto_cg_controller.py#L321-L389
[S-CG策略]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/auto_cg.py#L23-L99
[S-工具]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/plugin.py#L142-L185
[S-Story]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/story/backend/image_generator.py#L15-L57
[S-Story依赖]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/story/backend/plugin.py#L14-L32
[S-Story结果]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/story/backend/service.py#L255-L275
[S-Story失败]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/story/backend/service.py#L323-L344
[S-Story测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/story/tests/test_image_generator.py#L22-L117
[M-标签]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/novelai_generation/prompt_tags.py#L14-L59
[M-合并]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/novelai_generation/prompt_tags.py#L66-L78
[M-扩写]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/novelai_generation/prompt_tags.py#L159-L193
[M-标签归属]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/novelai_generation/prompt_tags.py#L94-L148
[M-标签测试]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/tests/python/test_novelai_prompt_tags.py#L72-L188
[M-标签去重测试]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/tests/python/test_novelai_prompt_tags.py#L105-L117
[M-标签UI]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/web/src/features/novelai/PromptTagManager.tsx#L8-L61
[M-标签交互]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/web/src/features/novelai/usePromptTags.ts#L24-L98
[M-标签UI测试]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/web/src/features/novelai/PromptTags.test.tsx#L16-L109
[M-参数]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/novelai_generation/parameters.py#L17-L102
[M-参数测试]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/tests/python/test_novelai_parameters.py#L6-L89
[M-配置]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/backend/app/novelai_generation/config.py#L15-L69
[M-准入]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/backend/app/novelai_generation/application.py#L248-L313
[M-聊天扩写]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/backend/app/novelai_generation/application.py#L227-L238
[M-聊天扩写测试]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/tests/python/test_novelai_tools.py#L83-L118
[M-旧图参数]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/web/src/features/novelai/useGenerationDrawer.ts#L220-L238
[M-重生成]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/backend/app/novelai_generation/application.py#L530-L561
[M-重生成测试]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/tests/python/test_novelai_regeneration.py#L9-L92
[M-重生成权限测试]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/tests/python/test_novelai_regeneration_authorization.py#L24-L66
[M-工作台]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/web/src/features/novelai/NovelaiStudio.tsx#L120-L206
[M-多图测试]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/web/src/features/novelai/NovelaiStudio.test.tsx#L270-L336
[M-媒体]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/novelai_generation/media.py#L23-L143
[M-API错误]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/backend/app/novelai_generation/api.py#L84-L95
[M-提交交互]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/web/src/features/novelai/useGenerationDrawer.ts#L136-L182
[M-前端重查测试]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/web/src/features/novelai/NovelaiStudio.test.tsx#L372-L394
[M-后台测试]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/tests/python/test_novelai_background.py#L11-L53
[M-HTTP]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/novelai_generation/http.py#L47-L89
[M-HTTP测试]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/tests/python/test_novelai_http.py#L13-L72
[M-恢复]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/backend/app/novelai_generation/application.py#L914-L1016
[M-恢复测试]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/tests/python/test_novelai_recovery.py#L12-L69
[M-资产]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/novelai_generation/assets.py#L20-L85
[M-资产测试]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/tests/python/test_novelai_scene_assets.py#L137-L285
[M-角色上传]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/web/src/features/role-management/useRoles.ts#L126-L168
[M-CG冻结]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/backend/app/novelai_generation/scene.py#L166-L238
[M-CG观察]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/backend/app/novelai_generation/scene.py#L245-L297
[M-CG提交]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/backend/app/novelai_generation/scene.py#L299-L355
[M-CG策略测试]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/tests/python/test_novelai_scene_policy.py#L9-L70
[M-CG预算测试]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/tests/python/test_novelai_scene.py#L158-L185
[M-CG冻结测试]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/tests/python/test_novelai_scene_priority.py#L12-L57
[M-来源]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/backend/app/novelai_generation/context.py#L73-L108
[M-来源测试]: https://github.com/sawaki-zexi/meido/blob/048c8ee7001e3557d4f8f6075fb74c5de5a6dafd/tests/python/test_novelai_context.py#L8-L50
[M-工具]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/backend/app/novelai_generation/tools.py#L30-L97
[M-就绪测试]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/tests/python/test_novelai_readiness.py#L15-L106
[M-就绪]: https://github.com/sawaki-zexi/meido/blob/b86be29d6ee97879dcb10e954edc47284e6d76ec/backend/app/novelai_generation/application.py#L117-L169

