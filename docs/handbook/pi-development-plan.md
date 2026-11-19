# 新版 Meido 开发计划与验收

状态：开发顺序已确认 · 最后核验：2026-11-19

事实来源：[重构决策](../specs/pi-refactor-decisions.md) · [各模块规格](../specs/pi-based-meido-design.md) · [仓库开发规范](../../AGENTS.md)

桌面 Pi 聊天与角色定稿先串行交付。它们合入后，前端体验、QQ 渠道、角色记忆、图片与表情四条线可以同时开工。跨模块联调按各票自己的前置推进。

## 开工与合入顺序

1. **固定规格。** `D00` 让主线具备新版重构的文档入口。拟采用同仓库的独立新版启动入口，维护者在该票确认主线部署边界和隔离启动方式。文档记录新版的 Runtime 和会话决策怎样衔接旧 ADR。
2. **完成桌面聊天。** `F01` 用测试角色打通真实 Pi。`F02` 加角色草稿、定稿及正式聊天。两票合入后，已具备创建角色、流式回复、停止和重启续聊的基本闭环。
3. **开启四条线。** 同时可开始 `U01`、`U02`、`Q01`、`M01`、`M05`、`P01`。前端首页和试聊从各自页面接入。后续单票的具体前置见任务表。
4. **按前置联调。** `I01` 只等主人配对和基础记忆。`I02` 只等 QQ 群应答与权限。`I04` 等群权限和基础素材库。这样三个场景可以分别完成。
5. **验收首版。** `E01` 等各末端票完成，用专用 NapCat 账号和测试群走一遍端到端路径。Telegram、飞书聊天、Discord 和官方 QQBot 在此之后逐个平台验收。

每张开发票使用单独工作区和分支。前置 PR 合入后，从最新 `main` 创建后续分支，PR 指向 `main`。若两张票需要修改同一个聊天入口、授权判定或持久化契约，先在最早使用该行为的票中落地公共契约，再由另一张票接入。

## 旧版与新版如何并行

开发期保留仓库现有的 Python 后端、Web 和桌面入口，日常实例继续通过本机 Meido.ps1 启动已发布的旧版。旧版代码中的角色、渠道、记忆和素材行为可用作需求与回归参考，现行运行边界见[旧版 Runtime 决策](../adr/0003-tau-inspired-agent-runtime-boundary.md)和[旧版多端会话决策](../adr/0009-multichannel-conversation-threads.md)。

D00 的拟议方案是在同一仓库新增独立的 Node.js/Pi 启动入口。新版在独立工作区使用自己的端口、数据目录和测试账号。F01 验证两套入口能各自启动，正式实例仍从已有发布目录启动。

E01 验收完成后再决定切换日期。切换前保留可重新启动的旧版发布和数据备份，并逐项确认需要导入的人设与模型连接。旧版代码的退役另行建票，安排在正式切换验证之后。

## 任务表

任务从[首版父 Issue #384](https://github.com/sawaki-zexi/meido/issues/384)查看。每行编号链接到对应开发 Issue，直接前置也登记在 GitHub。

| 编号 | 阶段 | 开发 Issue | 直接前置 | 参考规格 |
| --- | --- | --- | --- | --- |
| [D00](https://github.com/sawaki-zexi/meido/issues/385) | 准备 | 固定 Pi 重构的首版规格与开发入口 | 无 | [方案](../specs/pi-refactor-decisions.md) |
| [F01](https://github.com/sawaki-zexi/meido/issues/386) | 桌面 | 让测试角色通过 Pi 完成桌面聊天 | D00 | [方案](../specs/pi-runtime-integration-design.md) |
| [F02](https://github.com/sawaki-zexi/meido/issues/387) | 桌面 | 填写角色卡并开始正式聊天 | F01 | [方案](../specs/pi-role-creation-design.md) |
| [U01](https://github.com/sawaki-zexi/meido/issues/388) | 并行 | 在聊天首页搜索和切换角色 | F02 | [方案](../specs/pi-chat-home-design.md) |
| [U02](https://github.com/sawaki-zexi/meido/issues/389) | 并行 | 比较草稿试聊回复并收录示例 | F02 | [方案](../specs/pi-role-creation-design.md) |
| [Q01](https://github.com/sawaki-zexi/meido/issues/390) | 并行 | 连接 QQ 私聊并在网页查看历史 | F02 | [方案](../specs/pi-multichannel-development.md) |
| [Q02](https://github.com/sawaki-zexi/meido/issues/391) | 并行 | 通过 QQ 私聊配对和解绑主人 | Q01 | [方案](../specs/pi-multichannel-development.md) |
| [Q03](https://github.com/sawaki-zexi/meido/issues/392) | 并行 | 在 QQ 群点名应答并按发言人授权 | Q02 | [方案](../specs/pi-multichannel-development.md) |
| [Q04](https://github.com/sawaki-zexi/meido/issues/393) | 并行 | 让获准群友查询网页和本群消息资料 | Q03 | [方案](../specs/pi-multichannel-development.md) |
| [Q05](https://github.com/sawaki-zexi/meido/issues/394) | 并行 | 从获准会话查询账户并向授权目标发消息 | Q03 | [方案](../specs/pi-multichannel-development.md) |
| [M01](https://github.com/sawaki-zexi/meido/issues/395) | 并行 | 在桌面记住、纠正并查证一条经历 | F02 | [方案](../specs/pi-memory-design.md) |
| [M02](https://github.com/sawaki-zexi/meido/issues/396) | 并行 | 组合关键词和向量召回并展示本轮回忆 | M01 | [方案](../specs/pi-memory-design.md) |
| [M03](https://github.com/sawaki-zexi/meido/issues/397) | 并行 | 在每次成功压缩后提取待整理记忆 | M01 | [方案](../specs/pi-memory-design.md) |
| [M04](https://github.com/sawaki-zexi/meido/issues/398) | 并行 | 让角色在晚间空闲时整理长期记忆 | M03 | [方案](../specs/pi-memory-design.md) |
| [M05](https://github.com/sawaki-zexi/meido/issues/399) | 并行 | 让角色主动写日记并供主人查看 | F02 | [方案](../specs/pi-memory-design.md) |
| [P01](https://github.com/sawaki-zexi/meido/issues/400) | 并行 | 在网页收藏、查找并发送图片和动图 | F02 | [方案](../specs/pi-media-library-design.md) |
| [P02](https://github.com/sawaki-zexi/meido/issues/401) | 并行 | 在网页生图、改图并找回原版本 | P01 | [方案](../specs/pi-media-library-design.md) |
| [P03](https://github.com/sawaki-zexi/meido/issues/402) | 并行 | 让角色在次数限额内自主调用生图 | P02 | [方案](../specs/pi-media-library-design.md) |
| [P04](https://github.com/sawaki-zexi/meido/issues/403) | 并行 | 生成原创表情并通过一次视觉评估决定是否采用 | P02 | [方案](../specs/pi-media-library-design.md) |
| [I01](https://github.com/sawaki-zexi/meido/issues/404) | 联调 | 在配对后的 QQ 私聊使用桌面主人记忆 | Q02、M01 | [方案](../specs/pi-multichannel-development.md) |
| [I02](https://github.com/sawaki-zexi/meido/issues/405) | 联调 | 让获准角色按群设置旁听和自然接话 | Q03 | [方案](../specs/pi-multichannel-development.md) |
| [I03](https://github.com/sawaki-zexi/meido/issues/406) | 联调 | 让群资料和成员记忆遵守来源与受众权限 | Q03、M04 | [方案](../specs/pi-memory-design.md) |
| [I04](https://github.com/sawaki-zexi/meido/issues/407) | 联调 | 在 QQ 收藏和发送原动图并按范围读取附件 | Q03、P01 | [方案](../specs/pi-media-library-design.md) |
| [I05](https://github.com/sawaki-zexi/meido/issues/408) | 联调 | 在获准群聊中按图包偏好主动选用表情 | I02、I04 | [方案](../specs/pi-media-library-design.md) |
| [E01](https://github.com/sawaki-zexi/meido/issues/409) | 验收 | 完成首版桌面与 NapCat QQ 端到端验收 | U01、U02、Q04、Q05、M02、M05、P03、P04、I01、I03、I05 | [方案](../specs/pi-based-meido-design.md) |

## 模块怎样接在一起

- **聊天。** 网页或渠道提交角色、线程、发言者和当前消息。宿主装入该线程允许使用的历史，运行独立 Pi 回合。回复、工具进度及最终状态回到所属线程。
- **记忆。** 正式回复成功触发结构化记忆结算。每次手动或自动压缩成功触发同一个 Pending 提取流程，来源是该次移出的原话。角色夜间空闲时整理 Pending。跨端共用获准的主人记忆，原话、附件和群资料仍按来源范围访问。
- **渠道。** 配对和群准入确定发言者身份。工具发现、运行和实际操作目标使用同一授权范围。渠道接口返回成功则显示已发送，返回错误则展示失败原因。
- **素材。** 网页和 Pi 使用同一角色素材库。生成任务产出素材，原创表情经过一次视觉评估后可以采用。发送时按当前授权和目标渠道使用原件或受平台限制的引用，保留发送接口的结果。

## 验收顺序

| 验收点 | 演示操作 | 重点检查 |
| --- | --- | --- |
| 桌面可用 · F01、F02 | 创建角色、发消息、停止、重启后继续聊 | 实际 Pi 工具往返、一次完成、重复定稿、同名角色隔离 |
| QQ 文字可用 · Q01–Q03 | 从测试私聊问答、配对，再到测试群点名 | 网页独立历史、重复入站、权限撤销、成功和错误直接返回 |
| 记忆可用 · M01–M04、I01 | 桌面教称呼、QQ 追问并纠正，分别触发一次手动和自动压缩 | 跨端称呼生效、原话范围、旧任务晚到、一次 Pending、夜间空闲整理与重启恢复 |
| 网页素材可用 · P01–P04 | 收藏 GIF、重发原件、生成并改图、再画一张原创表情 | 动画与版本保留、额度竞争、一次生成和一次评估、采用和丢弃 |
| 首版联调 · E01 | 创建并试聊、配对、跨端问答、群权限和自然接话、收藏并发送 GIF、在网页生成原创表情 | 两角色、两群、解绑与重配、来源证据、真实模型与专用 NapCat 测试账号的表现 |

实现时先在正常入口用失败用例固定行为，再完成最小实现。用真实 Pi SDK 加可控模型核对实际输入、工具、取消和终态。渠道先用真实桥接进程加测试对端，再在专用测试群核对实际平台返回。记忆和生图分别使用可控时钟、可计数模型或供应商测试端验证后台任务。按票运行相关测试、类型检查和构建，并记录版本、命令和结果。

## 实施时确认的配置

- **第一个开发入口。** 由 D00 确认隔离启动方式及其与本机常驻实例的关系。
- **生图和评估。** 在 P02–P04 确认供应商支持的编辑和任务查询能力、实例额度配置、评估模型及样图标准。测试额度与发行版默认值分别记录。
- **本期以外的使用场景。** 首次使用引导和完整模型配置流程另有[后续方案](../specs/pi-first-use-design.md)。飞书共享资料的用途见[共享上下文规格](../specs/feishu-shared-workspace-spec.md)。旧 Meido 提供行为和测试参考，新版采用独立的数据目录。
