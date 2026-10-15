# Shiori 复刻实施与验收状态

状态：当前 / 实施中，完整目标尚未完成。

最后核验：2026-10-16（Asia/Shanghai）。源码基线：`main@17c2b28af5d643f3f90667973d72e4ca7f9fa1d5`；本轮集成核验：`feat/137-shiori-tool-parity@2afd73abcefb674f54b40e78416d1b42eecd3d43`。

事实来源：[目标规格](../specs/shiori-tool-parity-spec.md)、[固定版本源码调研](../research/shiori-tool-system-and-portability.md)、下表所列 Issue/PR、仓库行为测试和单独标明的原生验收。规格中的旧基线描述不代表最新代码状态。

## 交付与批准范围

- 总目标是 31 个核心工具、45 个插件工具、8 个 Skill、主动决策和 Drift。工具名字或 schema 存在不等于行为验收完成。
- 已获批准并合并 PR #145、#143、#146、#147、#150、#151，对应 #138–#144 中的六张实施票已关闭。
- 用户随后明确要求剩余多个 PR 汇总为一个大 PR；剩余改动汇入 [草稿 PR #162](https://github.com/sawaki-zexi/meido/pull/162)。原 Skills 草稿 PR #161 已关闭为被替代，未单独合并，#152 保持开放。
- D01–D03 已批准，见 ADR [0011](../adr/0011-versioned-tool-activation.md)、[0012](../adr/0012-owned-task-runtime.md)、[0013](../adr/0013-independent-generic-schedules.md)。屏幕感知具体方案随后已获明确批准并记录到 [0014](../adr/0014-explicit-screen-vision-binding.md)；[Windows 桌面控制具体方案](../specs/computer-use-spec.md)及桌宠宿主选择仍待回答。
- 汇总 PR 不具有合并批准；实现、审查、CI 和必要验收完成后仍需人工批准。剩余 Issue 不因进入集成分支而关闭。

## 范围追踪

“集成分支”表示尚未进入 main；“待实现”不能解释为永远以不可用占位交付。

| 标识 | 当前实现位置与证据 | 尚待完成 |
| --- | --- | --- |
| G01 配置与目录 | #163、#167 已进入集成分支；文件 4、网络 2、消息 2、记忆写入 2、发现 1、桌面推送 1，共 12 项网页授权入口，两轴审查及公开 HTTP/React 用户操作通过 | 继续保留 Shell/浏览器等专用开关；后续能力配置另走明确流程 |
| C01 工具发现 | #148 已进入集成分支；同 run 版本化激活、冻结候选、服务 grant 修订、真实来源和实时撤权；连续取消补充修复已两轴独立复核 | 总 PR 的最终跨模块验收 |
| C02 文件 4 工具 | main，PR #143；真实文件、图片 HTTP 往返、根目录和原子写行为测试 | 后续工具集成不得扩大文件授权 |
| C03 Shell/任务 3 工具 | #149 已进入集成分支；独立 TaskRuntime、SQLite、日志、实际进程树生命周期及来源授权经过两轴审查 | 后续 spawn/调度/Coding 集成回归；不得扩大 Shell 许可 |
| C04 网络 2 工具 | main，PR #146；受控 HTTP、公开网页与真实搜索服务验收 | 后续集成回归 |
| C05 记忆/消息 5 工具 | main，PR #147、#151；真实 MemoryEngine/SQLite/HTTP，取消后写入回滚与范围验证 | 后续任务和来源集成回归 |
| C06 投递/账号 4 工具 | main 复用 PR #128、#136 的渠道宿主、目录与账本；#167 桌面文本/媒体、实际来源、取消事务及下一轮模型历史已完成两轴复核并进入集成分支 | 在线状态、目标种类/成员查询、明确授权后的真实平台收件证据 |
| C07 子任务 2 工具 | #157 已进入集成分支；真实 TaskRuntime 子 loop、冻结 profile/模型、预算、原来源管理和完成交付已两轴审查；连续取消、pending 去重、跨库恢复与锁冲突修复的最终复核均无剩余 finding | 总 PR 跨模块集成与最终验收；未使用真实平台账号或付费模型 |
| C08 调度 3 工具 | #158 已从包含 #157、#167 的固定集成创建独立任务分支，ADR-0013 已批准；instant 与 soft 真实 HTTP/TaskRuntime/文件读取/桌面回传初链红绿通过，时间规则专项 97 passed，受管任务交接与 ACK 专项 13 passed | 完成故障恢复、时区、来源及取消竞态，两轴复核和总集成；现有待办不计作此项实现 |
| C09 群/成员 4 工具 | #165、#166 已进入集成分支；可信历史昵称、成员来源、完整群分页、笔记字段省略/清空及离线资料；两轴最终复核通过，事务直接读取父取消并等待写线程退出 | 总 PR 跨模块回归和最终验收；不得扩大来源范围 |
| C10 MCP 3 工具 | #159 已进入集成分支；真实 stdio、版本化发现、配置 UI、持久连接意愿及严格凭据清理已两轴复核；Linux 回收、Windows asyncio 环境、中文 schema 编码及 peer_error 清理竞态均实证修复并两轴复核 | 最新修复 CI 与总 PR 最终跨模块验收 |
| P01 浏览器 28 工具 | main，PR #150；全部 28 schema 映射核验，21 项真实原生浏览器测试通过，无 skip | 后续 MCP/配置集成回归 |
| P02 桌面控制 8 工具 | #169 具体规格已进入集成分支；固定 CUA 源码最小补丁、单普通权限驱动、单观察、独占 lease 与资源释放方案经独立提案复核，等待人工批准 | 批准后完整实现和专用受控窗口实机验收；当前无生产实现或原生构建 |
| P03 Coding Agent 5 工具 | 待实现；[CLI 调研](../research/coding-agent-cli-portability-2026-10-16.md)已核实固定 Shiori 五工具、外部 Herdr 协议、危险默认值与本机 help/version；TaskRuntime 可复用 | D05 具体方案审阅；真正启动/查询/继续/取消与交付核验，仓库和外部操作许可明确 |
| P04 屏幕感知 1 工具 | #160 已进入集成分支；提前拒绝/重复取消连接回收最终两轴复核均无剩余 finding；相关后端 196 passed/1 native 条件 skip，前端 69 passed；真实受控 Windows 主屏/125% DPI/本地视觉及取消通过，无原图落盘 | 总 PR 最终集成；尚无双屏、混合 DPI、实际锁屏/远程断开或 frozen 分发实证，也未调用真实远端视觉服务 |
| P05 桌宠 1 工具 | 待实现；Electron/PySide6 宿主选择待回答 | 真实透明置顶窗口、动作包、move/play 与完整生命周期 |
| P06 生图 1 工具 | 待实现 | NovelAI 独立凭据、媒体归属、额度/幂等、实际生成与展示 |
| P07 QQ 附件 1 工具 | #164 已进入集成分支；多字节长行自动字符分页、当前/引用真实入站与存储/Runtime 往返、旧附件/跨群拒绝均经过两轴独立审查 | 平台实机验收另列；传输夹具不是 QQ 真人收件证据 |
| S01 weather | #152 已进入集成分支；真实 curl、wttr.in、Open-Meteo 和公开 HTTP 工具往返 | 最终配置集成；Shell 仍须主人显式授权 |
| S02 summarize | #152 已进入集成分支；真实 summarize 0.25.2、Node 24、URL 提取及本地模型夹具摘要回传 | 两个公开视频仅得到 `transcriptSource: unavailable` 和网页模板，未取得真实字幕；不能以 exit 0 宣称视频摘要成功 |
| S03 feed-manage | #168 已完成两轴独立复核并进入集成分支；内置 Skill、动态服务目录/授权和真实公开 HTTP 七动作通过；独立复跑 8 HTTP + 2 原生用例，集成含 2 原生交叉 94 passed，含 RSS/Atom、下一模型请求、fresh list、重启、503 旧缓存与撤权；详见[验收](../research/feed-skill-native-acceptance-2026-10-16.md) | 总 PR 最终验收；主动来源/ACK/通知和 Drift 仍不属于此项完成范围 |
| S04 meme-manage | #152 已进入集成分支；指令、资源与角色目录边界已有行为测试 | 最终媒体引用与整体验收 |
| S05 skill-creater | #152 已进入集成分支；HTTP 写入新 Skill 后下一轮发现/显式激活，资源 hash、依赖名、真实 Windows junction 反例通过 | 总 PR 最终回归 |
| S06 create-drift-skill | 待实现 | Drift 目录创建、发现和真实执行 |
| S07 codex-delegate | 待实现，依赖 TaskRuntime 和子任务 | 真实 CLI、后台执行、session/结果回读和原上下文回灌 |
| S08 create-proactive-source | 待实现，依赖 MCP 和主动运行 | 来源创建、信任授权、事件消费、正文与持久 ACK |
| A01 主动决策 | 待实现；普通工具同名不算主动专用协议 | 来源抓取、候选/兴趣/去重、单次草稿、finish 提交/跳过和 ACK |
| A02 Drift | 待实现 | 空闲触发、独立目录/预算、受限工具、挂载 MCP、单次推送尝试和真实发送状态收尾 |

剩余渠道差距已拆成可验收后续切片：#166 完整群资料分页、字段保留/清空与真实来源，#167 纯桌面文本/媒体推送均已完成两轴复核并集成。C06 的账号与真实平台验收仍未完成。

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

#167 两轴最终复核均无剩余 finding；规范复核独立 13 passed，规格复核 Python 96 passed、React 4 passed。汇入 `a00e23a` 后桌面推送、Runtime 历史、子任务恢复、群、屏幕、MCP schema 与发现交叉 **170 passed**，全前端 **173 passed / 25 files**，TypeScript/Vite 通过。桌面事务最终授权门禁是取消与写入的线性化边界；未把已获授权并完成的提交描述为可被后来 Stop 回滚，详见[验收记录](../research/desktop-push-acceptance-2026-10-16.md)。

`7b724db` 的一条 Linux 工作流曾有 MCP `peer_error` 单项偶发提前断连，另一条工作流通过；随后 `cb6347e` 的两条 CI 均通过，全后端 **1511 passed、14 skipped、2 warnings**，前端构建通过。`de00dad` 的新 CI 再现同一 MCP 故障（**1518 passed、16 skipped、1 failed**），并出现密钥首次查看偶发空值（前端 **172 passed、1 failed**）；另一条工作流通过。没有将一次绿色结果当作故障已定位，所有 skip 均未计作原生通过。

MCP 故障已在 Windows 原命令和 Linux 真实慢退出 fixture 复现：RPC 错误已收到，等待进程清理时的授权轮询却用普通断连覆盖精确错误。`2bb57ea` 在开始清理前保存宿主安全错误，reader 不再覆盖，未改变撤权优先级或提前返回。Windows/Linux 相关五组各 **72 passed**，新 fixture 真正重复 **20 passed**；规范独立 **16 passed**，规格 Windows/Linux 各 **21 passed**，额外真实进程与公开 HTTP 探针均确认下一模型请求前 PID 已退出、仅一次业务调用且无秘密回显。两轴无剩余 finding，汇入 `750874e` 后 MCP HTTP、Feed、发现 HTTP 交叉 **43 passed / 104.28s**，包含两条实际 Feed 原生用例，无 skip。

密钥问题已用真实 React concurrent root 和 DOM 提交后的已启用按钮点击确定性复现：初始化 passive effect 会作废刚发起的显式读取。`c597ddd` 将两处清理改为 layout effect，保留原请求代际、隐藏、草稿和卸载边界；既有 MemorySettings 测试未放宽。新增竞态 red→green，重点 **23 passed**，全前端 **174 passed / 26 files**，TypeScript/Vite 通过；两轴各独立重点 **23 passed**，无剩余 finding。汇入 `2afd73a` 后完整前端及构建输入与已验证任务分支逐字相同，未把这些检查描述为再次全量执行。最新 CI 仍须重新核验。

#168 规范审查无 finding；规格审查独立 Windows **10 passed / 44.11s**，确认 18 次脚本化模型请求、19 次真实远端调用、17 次 localhost 请求及撤权失败结果。最终源码 `256c2cc` 汇入 `8bb2a2e` 后，Feed、内置 Skills/显式激活、MCP schema、桌面推送 HTTP 与 Runtime 历史交叉 **94 passed / 107.13s**，包含两条实际 Feed 原生用例，无 skip。服务源码与隔离 venv 仅保存在外部证据目录，未新增项目依赖或访问真实账号。

这些数量仅说明对应固定范围，不能替代尚未实现的工具、真实平台收件或未取得的 YouTube 字幕证据。集成分支继续推进时，更新本表和最终验收记录。
