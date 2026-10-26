# NovelAI 工作台与 Shiori 前端对齐调研

状态：当前（固定基线对照；本轮落实状态见下方补充，尚未合入正式版本）
最后核验：2026-10-26
事实来源：以下三个固定提交的源码及已有测试；本轮只读核验，没有运行真实生成、模型调用或使用正式数据，也没有重新执行全量测试。
研究方式：research 技能的后台源码调研。本报告补充前端与交互差距，不重述此前后端全面调研。

**2026-10-26 实施补充：**下列对照表描述调研起点，不把旧界面当作本轮完成后的现状。#248 已加入正负提示词页签、绘图偏好弹层、可预览/移除的底图、常驻生成按钮、大图与底部缩略图、收起的高级参数和图片信息，以及完整页面的标签库入口。#252 已加入卡片网格、独立编辑、还原/保存状态和删除确认；搜索筛选及本地预览保留。仍有第 4 节的功能语义差异，不宣称全面对等。正式规格见[工作台与提示词库展示](../specs/novelai-shiori-ui-spec.md)。

**验证补充：**工作台分支 NovelAI 前端 70 项通过，标签分支 68 项通过（分支基线不同，不相加）。两个实际组件通过隔离浏览器组合预览：1440×900 与 390×844，共 7 张界面快照；无浏览器脚本错误、窄屏无页面横向溢出，标签编辑往返工作台保留草稿。使用内存假 API 和本地示例矢量图，不是实际 NovelAI 出图。独立规范/规格审查发现的草稿覆盖与偏好失败回读问题已以公开界面红绿回归修复，并复核关闭。临时预览页面已移除，图片和日志留在忽略目录中。

| 对象 | 固定版本 | 本文使用边界 |
| --- | --- | --- |
| Shiori-Agent | d149de7f052f77c5bb88d4d1393d48dce1870875 | NovelAI 插件前端及必要的后端、Story 边界 |
| Meido 工作台 / 设置 | c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe | D:\meido-246-novelai；PR #248 的在审实现 |
| Meido 提示标签库 | 51470dd939635f81ddd8e41b658fa7d2d16a5533 | D:\meido-250-prompt-tags；PR #252 的搜索版 |

**版本边界：**这是两个 Meido 分支分别与同一上游的比较，不代表两个未合并分支已经组合在 main 中。本轮协调记录中 PR #248、#251、#252 均未合入；加权片段合并由 #251 处理，不重新提出重复实现。后续提交必须重新核对，不以本报告的固定版本代替最新代码。

## 1. 结论

**已提取：不能说“功能已经完全相同”。** Meido 已有文生图、图生图、尺寸 / 模型 / 采样控制、多张生成、历史、下载、参数复用、聊天重生成、后台核查和角色 CG 设置；现有测试明确覆盖这些路径。Shiori 的突出差距是工作台的信息层级，以及标签库“浏览卡片 → 独立编辑 → 还原 / 保存 → 删除确认”的完整交互。历史请求参数复用与角色素材用途还存在语义差异，见第 4 节。[Meido 工作台及历史测试][M-工作台测试]、[多图与下载测试][M-多图测试]、[聊天与后台测试][M-聊天测试]、[重生成测试][M-重生成测试]、[角色 CG 测试][M-角色CG测试]

**推断：**本轮按 Shiori 调整前端，应把“提示输入 + 大画布”恢复为页面主体，把低频参数、完整记录和本地预览移到次级展开区域。这能形成明显、可演示的结果，无需重新设计付费请求、权限和恢复机制。搜索版标签库则保留 Meido 已有的搜索与筛选，把展示和编辑流程重排；Shiori 本身没有搜索筛选。[Shiori 工作台布局][S-布局]、[卡片列表][S-卡片]、[Meido 当前标签库][T-标签]

两处容易误读的事实：

- **Shiori 当前尺寸控件是下拉选择，不是图标卡片。** 方 / 竖 / 横均标有实际像素，自定义宽高在下方。采用尺寸图标按钮属于 Meido 的适配设计，应保留文字、像素与键盘操作。[上游 SizeField][S-尺寸]、[尺寸定义][S-表单语义]
- **Shiori 的齿轮弹层只包含 NSFW、自动质量标签、负向预设与只读模型。** 它修改全局插件配置并自动保存，不包含本次 seed、步数或采样器。Meido 保留这些高级参数并折叠展示是适配，不能为了外观一致删掉能力，也不能把全局偏好误当成本次临时参数。[上游设置弹层][S-快捷设置]、[配置同步][S-设置同步]及[失败回滚测试][S-设置测试]、[Meido 参数表单][M-工作台]

## 2. 已提取：工作台与导航对照

| 项目 | Shiori d149de7 | Meido c3c47ea | 本轮可落实的差距 |
| --- | --- | --- | --- |
| 页面主次 | 左栏宽 clamp(300px,32%,392px)，右栏为占余高画布及其下方胶片条。[源码][S-布局] | 左侧长表单，右侧画布下还展开状态与参数面板；图片最大高度 430px。[源码][M-舞台]、[CSS][M-样式] | 增大画布，缩短常驻表单；完整记录默认收起，胶片条紧邻画布。 |
| 角色与正负提示词 | 角色头像 / 选择和齿轮在顶部；正向、负向页签共享大文本区，负向已有内容时有提示点。[源码][S-提示面板] | 角色在页头，表单内再次放头像；两个文本框同时显示，后接全部控制。[源码][M-工作台] | 改为正负页签，切换不丢文本，已有负向内容可发现；角色身份明确。 |
| 底图 | 虚线“添加参考图”卡；选择后出现图片、更换 / 移除按钮，下面是强度 / 噪声滑块。[源码][S-底图] | 显式模式选择后展示底图名称下拉、文件输入和两个数字框，没有底图预览卡。[源码][M-工作台] | 加真实图片预览、换图 / 移除及数值可见的滑块；继续支持选择本角色已有附件。 |
| 模式与尺寸 | 底图是否存在决定 txt2img / img2img；尺寸下拉加自定义输入。[源码][S-表单语义] | 文 / 图模式显式选择；尺寸和 64 倍数校验已经存在。[源码][M-工作台]、[参数校验][M-参数] | 首选只调整呈现；若改为“添加底图自动切模式”，必须同时遵守服务模式开关、清理互斥参数、检查附件归属。 |
| 常用与高级设置 | 小弹层保存全局三个提示偏好，模型只读；点击外部 / Escape 关闭。[源码][S-快捷设置] | 全局偏好在独立设置面板；本次 model / sampler / steps / samples / seed 等常驻。[源码][M-设置]、[工作台][M-工作台] | 常用参数清晰，低频本次参数可折叠。快捷全局偏好如纳入，须与设置页同一配置源并处理版本冲突。 |
| 提交位置 | 左栏内部滚动，底部固定全宽“生成”，忙时显示生成中。[源码][S-提示面板] | 按钮位于长表单末尾；费用与投递说明也占常驻高度。[源码][M-工作台] | 固定生成区，展示当前张数；只有重生成时出现对应消费 / 投递说明。保留准确语义。 |
| 生成中 | 按请求尺寸比例显示画框、秒表及光效；舞台选择逻辑优先展示正在生成。[源码][S-舞台状态]、[状态选择][S-舞台选择] | 忙时按钮变更并锁表单，但舞台优先显示旧 selectedImages，没有对应的前台生成舞台分支。[源码][M-舞台] | 用户点击后能明确看到“本次正在生成”；不把旧图误当新结果，不显示虚构完成百分比。 |
| 成图 | 图片 contain，占可用舞台；尺寸 / seed 在底部悬浮，旁边“复用提示词”；新结果有一次入场动画。[源码][S-舞台] | 多图网格、外链放大、下载、选择某张后再生成、完整参数；信息分散在数块面板。[源码][M-舞台] | 沿用大图及浮层层级，但保留多图选择、下载、参数复用和重生成，不能退化为只显示第一张。 |
| 失败与不可用 | 区分令牌问题与其他失败；卡片给“去设置”或“知道了”；已有历史图片上可叠不可用提示。[源码][S-舞台状态]、[舞台][S-舞台] | 已有状态说明、已清洗服务商错误、历史失败、设置入口、同请求核查与后台状态。[源码][M-工作台]、[舞台][M-舞台] | 改视觉容器和动作位置，不能把 unknown 换成普通失败，也不能给失败卡绑定自动付费重试。 |
| 胶片条 | 64px 缩略图、选中边框、历史计数、时间可访问名称、提示词 title。[源码][S-胶片条] | 64px 缩略图、加载更多、无图失败记录；可访问名称直接使用 attemptId。[源码][M-舞台] | 显示可理解的时间 / 状态摘要，保留无图记录和分页。Meido 一条记录可有多张，不能用记录数冒充总图片数。 |
| 导航与草稿 | 二级导航明确列出“生图 / 提示词库”；共享表单 store，往返不丢提示词。[源码][S-导航]、[状态及测试][S-表单状态测试] | 主侧栏是角色列表；标签库由工作台按钮打开 modal；同一工作台未卸载，因此开关设置和标签弹窗不必重置表单。[源码][M-App]、[舞台末尾][M-舞台]、[设置往返测试][M-设置往返测试] | 设置明确的工作区导航与整页标签容器，保留角色上下文；内页切换要显式保留工作台草稿 / 当前请求。 |

**视觉细节的依据：**上游使用柔和渐变玻璃舞台、圆角、浅描边、轻阴影；生成时扫光与闪烁，新图轻微模糊到清晰，减少动态效果偏好关闭循环动效。可采用 Meido 自己的颜色 / 圆角变量重现层级，不必搬入 Shiori 的 SDK、主题全局类或看板娘资源。[上游 scoped CSS][S-动效]

**测试证据的范围：**上游已有正负页签 / 设置弹层测试、舞台失败 / 历史可浏览 / 复用提示词测试；Meido 已有加载、错误、缺凭据、角色隔离、未决请求保留、多图和原请求核查测试。本次只是阅读这些测试，不声称已经完成视觉验收。[上游面板测试][S-面板测试]、[舞台测试][S-舞台测试]、[Meido 请求与加载测试][M-请求测试]

## 3. 已提取：提示标签库与设置对照

| 项目 | Shiori d149de7 | Meido 51470dd | 本轮处理 |
| --- | --- | --- | --- |
| 浏览 | 整页自适应图片卡片网格，4:5 预览、底部渐变名称 / 分类、停用徽标、卡片整面打开，菜单编辑 / 删除。[源码][S-卡片] | 搜索筛选后仍为纵向 article、图片、编辑 / 删除按钮。[源码][T-标签] | 改为图片卡片网格，空图要有占位；保留名字、分类、分级、状态。 |
| 查找 | 卡片组件没有搜索 / 分类 / 分级筛选。[源码][S-卡片] | 已有 NFKC + 大小写不敏感搜索，查名字 / 关键词 / 正负词，组合分类 / 分级 / 状态，结果计数、清空及无结果说明。[源码][T-标签载入]、[筛选][T-标签修改] | **这是 Meido 已新增能力，不是 Shiori 移植项。** 改卡片后完整保留。 |
| 数据完整性 | 一次读取插件目录列表。[源码][S-标签草稿] | 遍历角色库所有页，检查 revision 一致、重复游标 / 重复条目 / 250 上限；完整载入后才呈现。[源码][T-标签载入] | 保留全库搜索、失败不冒充空库和 A→B→A 过期响应隔离。 |
| 编辑入口 | 列表和独立编辑页互斥；顶部返回、还原、保存；未修改 / 保存中禁用对应操作。[源码][S-标签路由]、[编辑器][S-标签编辑器] | 列表下方始终放新建 / 编辑表单，再下方是预览表单；没有单独的新建页。[源码][T-标签] | “新建”或卡片点击进入编辑面；避免用户滚过整个列表找表单。 |
| 编辑版式 | 左参考图，右基本信息 / 启用，下面匹配词和正负词；分级为可读中文。[源码][S-标签编辑器] | 普通纵向表单；编辑分级选择仍直接显示英文值；有参考图上传和移除。[源码][T-标签] | 保留图片上传 / 移除、角色私有附件、现有分级值，只改标签文案和版式。 |
| 还原 / 保存 | 还原回到已保存条目；新建回到空草稿。保存后留在该条目详情，原值更新。[源码][S-标签草稿] | resetDraft 总是清空并退出编辑；保存成功也清空编辑草稿。[源码][T-标签修改] | 区分“返回列表”和“还原更改”；还原不发请求。保存失败保留草稿，成功后明确反馈并更新当前基线。 |
| 删除 | 先显示带名称的确认框，忙时保护，错误留在框内，成功后关闭。[源码][S-标签路由] | 点击删除直接调用 DELETE。[源码][T-标签]、[删除动作][T-标签修改] | 补确认 / 取消与失败可恢复；只点击菜单删除不能发请求。 |
| 本地匹配预览 | 上游上述工作区只有列表与编辑器，没有同屏本地扩写预览入口。[源码][S-标签路由] | 有输入及基础正负 tags 的精确服务端扩写预览；保存 / 删除 / 上传、修改预览输入和切角色会使旧预览失效。[源码][T-标签修改] | 保留为独立次级展开区或页签；不要因为模仿上游删除 Meido 已有能力。 |

搜索、组合筛选保草稿、第二页命中、分类修改后筛选保持、A→B→A 隔离、CRUD 和预览不触发生图已有测试，可作为本轮交互重排的回归边界。Shiori 的卡片测试确认空库 / 加载 / 卡片状态，但本轮未找到专门覆盖编辑器还原和删除确认的同名单元测试；这两项的上游结论来自实际组件与 hook，不把测试不存在误说成实现不存在。[Meido 搜索测试][T-搜索测试]、[CRUD / 预览测试][T-标签测试]、[上游卡片测试][S-卡片测试]

**设置与角色参数：已提取。** Shiori 全局设置通过插件 JSON Schema 在宿主生成；角色侧用“自动场景 CG”能力卡，卡内补充要求有读取 / 保存状态和重试。Meido 已有独立插件设置、凭据独立读写、模式 / 额度 / 模型 / 质量偏好分组，以及角色 CG 辅助连接、补充要求、卡片写回和标签分级许可。差距是卡片层级和局部编辑体验，不是这些开关缺失。[Shiori 注册][S-配置入口]、[角色卡][S-角色设置]、[补充要求][S-CG编辑器]、[Meido 设置][M-设置]、[角色设置][M-角色设置]、[设置测试][M-设置测试]

**计划：**设置延续 Meido 的服务状态 / 公共参数 / 独立凭据分组；角色 CG 可以用开关卡和设置展开区域表达。保留“NSFW 默认模型”与“角色内容许可”的区分，不照搬上游 general / sensitive / adult 到 Meido 的 general / mature / explicit 数据枚举。普通设置保存不读取或重交已保存 token，已有测试应继续成立。[Meido 设置与凭据测试][M-设置测试]

## 4. 已提取：仍有功能语义差异，但不宜夹进换肤

| 差异 | 确定证据 | 边界与建议 |
| --- | --- | --- |
| 历史重生成是否只换 seed | Shiori 深拷贝原始 request_payload，仅换 seed，已有针对性测试。Meido loadParameters 读取 prompt、尺寸、模型、采样等，但没有 qualityToggle / ucPreset；请求归一化从当前设置写入二者，并禁止调用方直接提供。[上游实现][S-重生成]、[上游测试][S-重生成测试]、[Meido 复用][M-历史复用]、[参数实现][M-参数]、[参数测试][M-参数测试] | 这是可演示的行为差异。未来明确“复用参数”和“原请求重生成”契约及授权检查，再改前后端；本轮不要把按钮改名成“完全复刻”。 |
| 已知参数错误后的编辑恢复 | Meido 提交前冻结 requestId / 参数，catch 后保留该 attempt；表单随 attempt 禁用，提供“重查本次生成”和“另开一次生成”。[提交][M-提交]、[表单和恢复入口][M-工作台] | 现状不是永久死锁。后续可将确定未受理的参数拒绝与未知结果分开，让已知拒绝原地编辑；未知结果仍只核查原请求，不能直接清身份重发。 |
| 生成图成为哪类角色素材 | Shiori 成功写回会 add_illustration 并 set_chat_background；Meido 的获准 CG 通过预期旧 URL 检查更新当前卡片图，原生成图仍在历史。[上游][S-素材]、[上游测试][S-素材测试]、[Meido][M-素材]、[Meido 测试][M-素材测试] | “插画库 / 聊天背景 / 设为卡片图”是三个用户结果。若要上游完整素材体验，需要先定义角色素材模型及手动应用入口，不宜在此 PR 隐式扩大自动写回。 |
| 导航期间的任务生命周期 | Shiori 表单 / 结果在模块级 store；Meido App 条件渲染工作台，hook 卸载时使 scope 失效并 abort 本地请求。[上游状态][S-表单状态]、[Meido App][M-App]、[hook][M-请求状态] | 本轮只需保证“工作台 ↔ 标签库”内部往返不丢草稿 / 冻结请求。跨聊天模块持续运行涉及恢复和请求所有权，不能只把 store 改为全局就视为完成。 |
| Story 完整剧情模式 | 上游 Story 自己注册 create / input / continue / CG retry / regenerate RPC、持有独立页面和控制器，并通过显式 NovelAI 依赖产生 scene / character 资源。[插件][S-Story插件]、[图像适配器][S-Story图像]、[Story 页面][S-Story页面] | Meido 当前 App 视图及 NovelAI 工作台没有该独立 Story 入口。[Meido App][M-App] 这属于另一个产品模块，不是本次工作台“漏了一个功能”；须另写规格与垂直 Ticket，不能把 Story 的生命周期塞进 NovelAI UI。 |

**共同没有的能力，不能列作 Meido 落后：**Shiori 底图卡是 img2img 输入，不等于 Vibe / 风格参考图；其 V4.5 reference_image_multiple 等数组仍为空。标签图片也是目录说明图，前端选图不会自动产生供应商参考强度语义。不能以“参考图”这个标签推断上游有多参考图或角色一致性功能。[底图构建][S-表单语义]、[供应商参数][S-参考协议]、[标签草稿保存][S-标签草稿]

## 5. 计划：本轮完整用户结果和落实清单

建议第一轮结果：用户进入生图后，能在明确角色下编辑正负提示词、看清底图、选择尺寸并提交；画布即时进入可理解的处理中状态，结果以大图呈现且能查看多图 / 下载 / 历史；进入提示标签库可以查找图片卡片、独立编辑、还原、保存或确认删除，返回生图后草稿与进行中的请求保持一致。

### 5.1 现有 246 / PR #248：工作区、画布与设置

1. 建立“生图 / 提示标签库”内部导航，保留角色上下文和无角色空态；使用已有 expanded 标签组件边界承载整页库，避免把库的 CRUD 逻辑复制到工作台。
2. 左栏顺序为角色及设置入口 → 正 / 负页签及文本区 → 底图卡 → 尺寸 → 高级参数展开；底部固定“生成 N 张”和紧凑额度说明。详细的重生成投递说明仅在相关流程出现。
3. 右侧以大画布为主，底部工具区展示尺寸、seed、当前多图位置、下载和复用；完整生成 / 投递 / 消费 / 写回状态放可展开详情，不能隐去未知结果和恢复动作。
4. 为初始加载、无历史、前台处理中、后台已接收、成图、确定失败、未知结果、缺凭据分别提供准确状态。处理进度以阶段 / 已等待时间表达；光效只表示处理中。
5. 胶片条紧邻画布；选中状态、时间、失败条目、分页和多图计数均可理解。新结果仅首次播放入场动画，减少动态效果偏好有静态呈现。
6. 设置延续现有入口和保存协议。快捷偏好弹层如超出本轮边界，可先让齿轮打开现有设置，明确这是交互适配，不能宣称已复制上游局部自动保存。

### 5.2 现有 250 / PR #252：查找、卡片与编辑

1. 顶部保留搜索、三个组合筛选、计数与清空；“新建提示标签”作为主要操作。下方显示响应式 4:5 图片卡片，空图有占位。
2. 列表和编辑面互斥；编辑页顶部返回、还原、保存，主体参考图与基本信息并列，匹配词和正负 tags 有明确分区；窄屏改为单列。
3. 还原回到当前已保存条目；创建时还原空草稿。保存失败保留输入，成功更新还原基线。返回列表后的草稿策略要明确；上游本身未提供离开未保存页确认，不能误称已有保护。
4. 删除确认显示准确名字；取消不发请求，确认只发一次，失败留在确认框并允许取消 / 再次尝试。角色切换后不能把旧弹窗确认应用到新角色。
5. 本地扩写预览作为次级区域保留；图片上传 / 移除和错误反馈不丢失。正负词仍是原始文本，不复用上游按逗号切片的编辑实现，避免破坏加权片段。
6. 保留完整载入与 stale response 保护；搜索 / 清空不覆盖编辑草稿，不把加载失败展示成“没有符合条件的标签”。

### 5.3 合并与范围协调

**计划：**沿用现有 #248 和 #252 的分支 / PR，不从未合并功能分支新开堆叠分支，也不为相同工作台另开重复 PR。246 负责承载布局、工作台和导航，250 负责提示标签组件内部流程；共享样式按组件命名空间协调，一处公共修改由一个分支承担。#251 加权行为是独立在审工作，UI 不再次实现逗号解析。若发现必须依赖另一分支新增契约，应记录依赖并等待其合入，再从最新 main 同步。

## 6. 计划：验收与测试边界

- **布局验收：**在桌面常见窗口与窄屏查看空库、长提示词、长角色名、长标签名、没有参考图、横竖图和多图；无关键按钮溢出或不可达，画布在有历史详情时仍是主要内容。键盘可切换页签、打开 / 关闭弹层和确认框，图标有可访问名称。
- **状态行为测试：**正负页签来回内容不丢；上传 / 更换 / 移除底图后的有效参数正确；生成开始即有状态，结束后展示对应新结果；既有历史不因缺凭据消失；内部标签库往返不制造新请求、不丢冻结 identity；角色切换不泄露前角色图片。
- **标签行为测试：**点击卡片进入正确编辑条目；还原无网络调用；保存成功更新基线，失败保留草稿；点删除只弹确认，取消零 DELETE，确认一次 DELETE，失败可恢复；搜索 / 筛选 / 全页加载和 A→B→A 已有用例继续通过。
- **关键回归：**保留多图选择后下载 / 重生成、仅历史默认交付、显式渠道投递、同 requestId 核查、后台接受后轮询、未决请求遇设置变化仍冻结等现有外部行为测试。[工作台多图测试][M-多图测试]、[请求测试][M-请求测试]、[重生成测试][M-重生成测试]
- **测试方法：**前端行为使用现有 fakeBackend / 合成附件，不访问实际 NovelAI。视觉改动以实际页面检查证明；不为 CSS 类名或组件拆分写镜像测试。实现后运行相关测试、类型检查和构建；需要后端契约改变的另列依赖，不在本轮调研中假称完成。

## 7. 未验证事项

本报告没有启动页面做逐像素截图对比，因此样式尺寸来自源码，不是测量截图。没有真实服务商调用，不证明任何付费模型的当前账户可用性或费用。没有把 Shiori 的宿主自动设置表单、看板娘、Story 演出系统纳入同一轮前端移植。新增模型、真实多参考图、跨模块后台生命周期、完整角色插画库和历史严格回放均应另行设计；不能用“前端按 Shiori 来”隐含批准这些语义变化。

[S-布局]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/ImageStudioPage.tsx#L20-L55
[S-提示面板]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/PromptPanel.tsx#L43-L109
[S-底图]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/BaseImageField.tsx#L40-L77
[S-尺寸]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/SizeField.tsx#L15-L46
[S-表单语义]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/studioForm.ts#L4-L76
[S-快捷设置]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/PromptSettingsPopover.tsx#L19-L85
[S-设置同步]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/useNovelAiPromptSettings.ts#L24-L111
[S-设置测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/useNovelAiPromptSettings.test.tsx#L38-L130
[S-舞台状态]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/StageStates.tsx#L22-L94
[S-舞台]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/ImageStage.tsx#L18-L77
[S-舞台选择]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/studioSelectors.ts#L46-L72
[S-胶片条]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/ImageFilmstrip.tsx#L12-L56
[S-动效]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/novelai.css#L5-L111
[S-卡片]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/PromptTagGrid.tsx#L36-L92
[S-标签路由]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/PromptTagLibraryPanel.tsx#L17-L75
[S-标签编辑器]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/PromptTagEntryEditor.tsx#L81-L162
[S-标签草稿]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/usePromptTagLibrary.ts#L41-L117
[S-导航]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/NovelAIPageSidebar.tsx#L22-L52
[S-表单状态]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/novelAiPageStore.ts#L100-L145
[S-表单状态测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/novelAiPageStore.test.tsx#L107-L132
[S-面板测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/PromptPanel.test.tsx#L48-L98
[S-舞台测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/ImageStage.test.tsx#L27-L94
[S-卡片测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/PromptTagGrid.test.tsx#L18-L48
[S-配置入口]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/index.tsx#L33-L47
[S-角色设置]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/roleSettings.tsx#L6-L27
[S-CG编辑器]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/ui/AutoCgPromptSettingsEditor.tsx#L10-L24
[S-重生成]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L164-L201
[S-重生成测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/tests/test_service.py#L33-L140
[S-素材]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L235-L271
[S-素材测试]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/tests/test_service.py#L335-L370
[S-参考协议]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/novelai/backend/service.py#L393-L428
[S-Story插件]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/story/backend/plugin.py#L14-L60
[S-Story图像]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/story/backend/image_generator.py#L15-L57
[S-Story页面]: https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/story/ui/StoryPage.tsx#L10-L29
[M-工作台]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/NovelaiStudio.tsx#L95-L158
[M-舞台]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/NovelaiStudio.tsx#L160-L228
[M-样式]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/styles.css#L184-L249
[M-工作台测试]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/NovelaiStudio.test.tsx#L23-L138
[M-多图测试]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/NovelaiStudio.test.tsx#L251-L370
[M-请求测试]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/NovelaiStudio.test.tsx#L372-L503
[M-设置]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/NovelaiSettings.tsx#L20-L132
[M-设置测试]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/NovelaiSettings.test.tsx#L9-L175
[M-设置往返测试]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/NovelaiEntries.test.tsx#L9-L51
[M-App]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/App.tsx#L175-L218
[M-请求状态]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/useGenerationDrawer.ts#L21-L68
[M-提交]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/useGenerationDrawer.ts#L136-L182
[M-历史复用]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/useGenerationDrawer.ts#L220-L238
[M-参数]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/backend/app/novelai_generation/parameters.py#L17-L95
[M-参数测试]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/tests/python/test_novelai_parameters.py#L6-L66
[M-聊天测试]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/GenerationChat.test.tsx#L10-L75
[M-重生成测试]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/GenerationRegeneration.test.tsx#L24-L140
[M-角色设置]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/NovelaiRoleSettings.tsx#L11-L46
[M-角色CG测试]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/web/src/features/novelai/SceneCg.test.tsx#L19-L71
[M-素材]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/backend/app/novelai_generation/assets.py#L20-L83
[M-素材测试]: https://github.com/sawaki-zexi/meido/blob/c3c47ea47f89cbb162c0adf7c88300e8c65a4bbe/tests/python/test_novelai_scene_assets.py#L137-L175
[T-标签]: https://github.com/sawaki-zexi/meido/blob/51470dd939635f81ddd8e41b658fa7d2d16a5533/web/src/features/novelai/PromptTagManager.tsx#L8-L76
[T-标签载入]: https://github.com/sawaki-zexi/meido/blob/51470dd939635f81ddd8e41b658fa7d2d16a5533/web/src/features/novelai/usePromptTags.ts#L6-L58
[T-标签修改]: https://github.com/sawaki-zexi/meido/blob/51470dd939635f81ddd8e41b658fa7d2d16a5533/web/src/features/novelai/usePromptTags.ts#L68-L126
[T-搜索测试]: https://github.com/sawaki-zexi/meido/blob/51470dd939635f81ddd8e41b658fa7d2d16a5533/web/src/features/novelai/PromptTags.test.tsx#L17-L133
[T-标签测试]: https://github.com/sawaki-zexi/meido/blob/51470dd939635f81ddd8e41b658fa7d2d16a5533/web/src/features/novelai/PromptTags.test.tsx#L135-L231
