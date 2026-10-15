# Shiori 复刻实施与验收状态

状态：当前 / 实施中，完整目标尚未完成。

最后核验：2026-10-16（Asia/Shanghai）。源码基线：`main@17c2b28af5d643f3f90667973d72e4ca7f9fa1d5`；本轮集成核验：`feat/137-shiori-tool-parity@e474e7c426107a48fecfd061d3b438877b0a9688`。

事实来源：[目标规格](../specs/shiori-tool-parity-spec.md)、[固定版本源码调研](../research/shiori-tool-system-and-portability.md)、下表所列 Issue/PR、仓库行为测试和单独标明的原生验收。规格中的旧基线描述不代表最新代码状态。

## 交付与批准范围

- 总目标是 31 个核心工具、45 个插件工具、8 个 Skill、主动决策和 Drift。工具名字或 schema 存在不等于行为验收完成。
- 已获批准并合并 PR #145、#143、#146、#147、#150、#151，对应 #138–#144 中的六张实施票已关闭。
- 用户随后明确要求剩余多个 PR 汇总为一个大 PR；剩余改动汇入 [草稿 PR #162](https://github.com/sawaki-zexi/meido/pull/162)。原 Skills 草稿 PR #161 已关闭为被替代，未单独合并，#152 保持开放。
- D01–D03 已批准，见 ADR [0011](../adr/0011-versioned-tool-activation.md)、[0012](../adr/0012-owned-task-runtime.md)、[0013](../adr/0013-independent-generic-schedules.md)。屏幕感知具体方案随后已获明确批准并记录到 [0014](../adr/0014-explicit-screen-vision-binding.md)；桌宠宿主选择仍待回答。
- 汇总 PR 不具有合并批准；实现、审查、CI 和必要验收完成后仍需人工批准。剩余 Issue 不因进入集成分支而关闭。

## 范围追踪

“集成分支”表示尚未进入 main；“待实现”不能解释为永远以不可用占位交付。

| 标识 | 当前实现位置与证据 | 尚待完成 |
| --- | --- | --- |
| G01 配置与目录 | #163 已进入集成分支；文件 4、网络 2、消息 2、记忆写入 2、发现 1，共 11 项网页授权入口，两轴审查及公开 HTTP/React 用户操作通过 | 继续保留 Shell/浏览器等专用开关；后续能力配置另走明确流程 |
| C01 工具发现 | #148 已进入集成分支；同 run 版本化激活、冻结候选、服务 grant 修订、真实来源和实时撤权；连续取消补充修复已两轴独立复核 | 总 PR 的最终跨模块验收 |
| C02 文件 4 工具 | main，PR #143；真实文件、图片 HTTP 往返、根目录和原子写行为测试 | 后续工具集成不得扩大文件授权 |
| C03 Shell/任务 3 工具 | #149 已进入集成分支；独立 TaskRuntime、SQLite、日志、实际进程树生命周期及来源授权经过两轴审查 | 后续 spawn/调度/Coding 集成回归；不得扩大 Shell 许可 |
| C04 网络 2 工具 | main，PR #146；受控 HTTP、公开网页与真实搜索服务验收 | 后续集成回归 |
| C05 记忆/消息 5 工具 | main，PR #147、#151；真实 MemoryEngine/SQLite/HTTP，取消后写入回滚与范围验证 | 后续任务和来源集成回归 |
| C06 投递/账号 4 工具 | main 复用 PR #128、#136 的渠道宿主、目录与账本；#167 桌面文本/媒体与实际来源已在任务分支完成公开 HTTP 和故障回归 | #167 最终两轴审查与集成；在线状态、目标种类/成员查询、明确授权后的真实平台收件证据 |
| C07 子任务 2 工具 | #157 已进入集成分支；真实 TaskRuntime 子 loop、冻结 profile/模型、预算、原来源管理和完成交付已两轴审查；连续取消、pending 去重、跨库恢复与锁冲突修复的最终复核均无剩余 finding | 总 PR 跨模块集成与最终验收；未使用真实平台账号或付费模型 |
| C08 调度 3 工具 | #158 本地实现独立计划/occurrence/outbox；公开 HTTP instant/soft、真实 TaskRuntime/文件、SQLite/渠道收据和来源撤权测试见[调度契约](owned-schedules.md)；复用已验证 #167 内部桌面写能力 | 两轴审查、总 PR 集成交叉与最终验收；尚未使用真实平台账号或付费模型 |
| C09 群/成员 4 工具 | #165、#166 已进入集成分支；可信历史昵称、成员来源、完整群分页、笔记字段省略/清空及离线资料；两轴最终复核通过，事务直接读取父取消并等待写线程退出 | 总 PR 跨模块回归和最终验收；不得扩大来源范围 |
| C10 MCP 3 工具 | #159 已进入集成分支；真实 stdio、版本化发现、配置 UI、持久连接意愿及严格凭据清理已两轴复核；Linux 回收、Windows asyncio 环境与中文 schema worker 解码均已实证修复并两轴复核 | 最新集成 Linux CI 与总 PR 最终跨模块验收 |
| P01 浏览器 28 工具 | main，PR #150；全部 28 schema 映射核验，21 项真实原生浏览器测试通过，无 skip | 后续 MCP/配置集成回归 |
| P02 桌面控制 8 工具 | 待实现；目标为 Windows 原生驱动、窗口引用、独占 lease 与资源释放 | 完整实现和受控实机验收 |
| P03 Coding Agent 5 工具 | 待实现；使用已批准 TaskRuntime 归属和生命周期 | 真正启动/查询/继续/取消与交付核验；仓库和外部操作许可明确 |
| P04 屏幕感知 1 工具 | #160 已进入集成分支；提前拒绝/重复取消连接回收最终两轴复核均无剩余 finding；相关后端 196 passed/1 native 条件 skip，前端 69 passed；真实受控 Windows 主屏/125% DPI/本地视觉及取消通过，无原图落盘 | 总 PR 最终集成；尚无双屏、混合 DPI、实际锁屏/远程断开或 frozen 分发实证，也未调用真实远端视觉服务 |
| P05 桌宠 1 工具 | 待实现；Electron/PySide6 宿主选择待回答 | 真实透明置顶窗口、动作包、move/play 与完整生命周期 |
| P06 生图 1 工具 | 待实现 | NovelAI 独立凭据、媒体归属、额度/幂等、实际生成与展示 |
| P07 QQ 附件 1 工具 | #164 已进入集成分支；多字节长行自动字符分页、当前/引用真实入站与存储/Runtime 往返、旧附件/跨群拒绝均经过两轴独立审查 | 平台实机验收另列；传输夹具不是 QQ 真人收件证据 |
| S01 weather | #152 已进入集成分支；真实 curl、wttr.in、Open-Meteo 和公开 HTTP 工具往返 | 最终配置集成；Shell 仍须主人显式授权 |
| S02 summarize | #152 已进入集成分支；真实 summarize 0.25.2、Node 24、URL 提取及本地模型夹具摘要回传 | 两个公开视频仅得到 `transcriptSource: unavailable` 和网页模板，未取得真实字幕；不能以 exit 0 宣称视频摘要成功 |
| S03 feed-manage | #168 已有完整计划；固定外部服务在隔离依赖和默认生产 MCP 环境下完成 49 项检查、24 次真实工具调用，覆盖七动作与重启持久化 | Skill 实现、真实动态授权与公开 HTTP 对话闭环；协议检查不等于 Skill 验收 |
| S04 meme-manage | #152 已进入集成分支；指令、资源与角色目录边界已有行为测试 | 最终媒体引用与整体验收 |
| S05 skill-creater | #152 已进入集成分支；HTTP 写入新 Skill 后下一轮发现/显式激活，资源 hash、依赖名、真实 Windows junction 反例通过 | 总 PR 最终回归 |
| S06 create-drift-skill | 待实现 | Drift 目录创建、发现和真实执行 |
| S07 codex-delegate | 待实现，依赖 TaskRuntime 和子任务 | 真实 CLI、后台执行、session/结果回读和原上下文回灌 |
| S08 create-proactive-source | 待实现，依赖 MCP 和主动运行 | 来源创建、信任授权、事件消费、正文与持久 ACK |
| A01 主动决策 | 待实现；普通工具同名不算主动专用协议 | 来源抓取、候选/兴趣/去重、单次草稿、finish 提交/跳过和 ACK |
| A02 Drift | 待实现 | 空闲触发、独立目录/预算、受限工具、挂载 MCP、单次推送尝试和真实发送状态收尾 |

剩余渠道差距已拆成可验收后续切片：#166 完整群资料分页、字段保留/清空与真实来源已完成两轴复核并集成；#167 纯桌面文本/媒体推送已在独立任务分支完成相关回归，待两轴审查与集成。C06 的账号与真实平台验收仍未完成。

## 已完成的本轮集成验证

工具发现与四个内置 Skill 合并后，公开 HTTP/原生组合用例 97 passed、1 个 Windows 不适用用例 skip；其中实际 CLI/公开网页的 3 个原生 Skill 用例均运行通过。随后连续取消补充修复在集成分支重跑发现、Google 调用 ID、Skills HTTP 与渠道 API：52 passed。独立修复复核另有 44 项发现/撤权测试及 4 项异常/超时/取消兼容测试通过。

后台 Shell 进入集成分支后，重跑后台任务、真实进程、工具发现、Google 调用身份和三项实际 Skill CLI：80 passed、1 项平台条件 skip。#149 的来源、预算及日志错误修复均经过两轴独立复核；此前各组数量存在重叠，不相加为唯一通过数。

网页授权进入总分支后，文件、网络、消息、记忆、发现和专用门禁 HTTP 回归 25 passed；全部前端 153 passed，TypeScript/Vite 构建通过。QQ 附件补充修复经两轴独立审查后，在最新总分支重跑附件/真实入站/聊天附件 18 passed。各组包含已有测试，不相加作为覆盖范围或唯一通过总数。

总分支 `e5ba0ba` 的 CI 发现三项测试资源隔离失败；真实进程断言及生产权限保持不变，测试注入临时 TaskRuntime 和内存凭据接口后，`42b5442` 的 Linux CI 为 **1112 passed、12 skipped**，前端构建通过。skip 不计作原生验收，浏览器/Skill 原生证据仍使用各自记录。

#165 在独立任务分支完成渠道/HTTP/任务交叉 **259 passed**；两轴审查发现的昵称时间、旁听迁移、目录覆盖入站昵称及文档问题已修正，独立修复复核 **84 passed**。汇入 `af61ad3` 后，关闭生命周期、成员/来源、后台 Shell、发现与角色目录交叉 **74 passed**。相关新代码静态检查通过；触及的旧渠道模块共有 40 项既有 mypy 报错，经固定基线逐条比对无新增，并未声称这些旧模块全量类型检查通过。具体契约见[成员查询](member-lookup.md)。

#159 两轴审查发现并修复凭据删除丢重试归属、stdio 写入背压不受超时/取消约束，以及系统凭据分片清理吞异常。最终修复的两轴复核均无剩余 finding；分片与 manifest 故障只用实际凭据适配器配内存 keyring，未触真实系统凭据。汇入 `e365f6e` 后，MCP、发现、后台 Shell 与成员 HTTP 交叉 **117 passed**；MCP 设置 React **9 passed**、TypeScript/Vite、5 个 MCP/凭据模块 mypy 与 compileall 通过。原生浏览器此前关联验收仍有效，未把未重跑的检查计作本轮证据。严格清理与真实故障进程证据见 [MCP 契约](mcp-services.md)和[协议调研](../research/mcp-stdio-protocol-audit.md)。

`077ebc3` Linux CI 为 **1204 passed、12 skipped、2 failed**；两项失败均位于 MCP stdio 异常输出与连续取消的进程回收，前端构建通过。该平台问题已在隔离 WSL 复现，修复前不能把 Windows 定向通过视为全部 CI 完成。

上述两项故障已在固定 Linux 环境复现并修正：输出超限后持续排空 stdout，并等待原进程组全部退出，未放宽子进程存活断言。独立 WSL MCP 交叉 **67 passed**，三种故障各重复十轮共 **30 passed**，两轴修复复核均无剩余 finding。汇入 `c4679af` 后 Windows stdio **20 passed**，相关 5 模块 mypy/compileall 通过。随后 `cef144a` 修正最小环境中 `SYSTEMROOT` 的大小写匹配，真实 asyncio 套接字服务从无法握手到通过，相关 **27 passed**，两轴各自复核 **2 passed**；已汇入 `7176fc4`。这些本地结果不能代替新提交的 Linux CI。

固定外部 Feed 服务在上述默认 Windows 生产环境中完成 **49 checks、24 次真实 tools/call、21 次本地 HTTP 请求**；两次真实进程握手、五工具发现、七种订阅操作和重启持久化通过，未注入临时 `SYSTEMROOT` 覆盖。此证据仅覆盖协议与服务行为，未计作 #168 Skill 或公开对话验收；没有调用主动 poll/ACK、真实订阅账号或付费模型。

#157 最终修复在独立分支完成行为/恢复交叉 **70 passed**，两路最终复核各自恢复专项 **16 passed**、无剩余 finding。汇入 `d332b30` 后，子任务、持久恢复、MCP 来源、后台 Shell、发现、角色能力和关闭交叉 **100 passed**；子任务/基础工具网页 **4 passed**，TypeScript/Vite 通过。源分支审查及历史检查组见[子任务验收](../research/owned-subagents-acceptance-2026-10-16.md)。

#166 的父取消直读修复通过两轴最终复核，原始锁等待与更窄取消窗口探针均通过。汇入 `94dad87` 后，群资料/HTTP、真实 Runtime 取消、子任务/恢复、发现和 MCP 来源交叉 **181 passed**。独立任务取消不会反向取消父任务或兄弟任务。具体契约见[群资料](group-context.md)。

`0ae83ac` 的 Linux CI 原 MCP 两项故障已通过，但整组仍为 **1324 passed、12 skipped、11 failed**：群/子任务账号重建夹具使用真实系统凭据，以及旧渠道 Runtime provider 夹具未补齐内部执行器需要的接口。正以实际失败复现修正测试隔离，不放宽权限或真实投递断言；当前不能声称全部 CI 通过。

上述测试隔离及 provider 夹具修正后，Linux 受影响三文件 **90 passed**；同期真实 Feed HTTP 又发现隔离 schema worker 未明确 UTF-8 编码，合法中文结果被错误拒绝，两行编码边界修复保留所有 schema/预算规则。Windows MCP 五文件 **71 passed**，两轴独立复核分别 **50 passed** 和 **7 passed**，强制 CP936 探针确认合法中文通过、非法类型仍拒绝。隔离 Linux 全后端原始结果为 **1418 passed、14 skipped、1 failed**；唯一失败是恢复测试在另一条回执修复前提前关闭 worker。改为等待两条持久同步确认并保留原精确历史/发送断言后，Windows/Linux 恢复专项各 **16 passed**；未以这些专项结果声称整组重跑成功。上述修复汇入 `e474e7c` 后，schema、渠道交付及恢复交叉 **25 passed**，新 CI 仍须核验。

#160 最终两轴独立复核分别完成重点 **23 passed** 和屏幕/HTTP **82 passed、1 skipped**，并有独立真实 TCP 探针确认原早退资源释放问题已关闭。汇入 `3765ac2` 后，屏幕、子任务/恢复、群、MCP 来源与角色能力交叉 **139 passed**；角色屏幕/子任务/基础工具/草稿 React **14 passed**，TypeScript/Vite 通过。受控原生证据仍使用[屏幕验收记录](../research/screen-perception-acceptance-2026-10-16.md)，未重复捕获或把跳过项当作实机通过。

这些数量仅说明对应固定范围，不能替代尚未实现的工具、真实平台收件或未取得的 YouTube 字幕证据。集成分支继续推进时，更新本表和最终验收记录。
