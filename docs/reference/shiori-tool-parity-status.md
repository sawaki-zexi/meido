# Shiori 复刻实施与验收状态

状态：当前 / 实施中，完整目标尚未完成。

最后核验：2026-10-16（Asia/Shanghai）。源码基线已同步 `main@5acbf0c21b7e25a2ae24cf59d570c2bf7c6e5e05`（图标与落地页背景音乐）；最新两套完整CI均通过的集成为 `e71145aade2644d6b56225fddf3673cbf236cabd`，含 #173 与来源验证journal竞态修正。该准确头的 [PR CI](https://github.com/sawaki-zexi/meido/actions/runs/38901230442) 与 [push CI](https://github.com/sawaki-zexi/meido/actions/runs/38901224739) 各后端2308 passed/35 skipped/2 warnings（409.99秒、447.33秒），各前端215项/38文件及TypeScript/Vite通过。此前929974f的来源验证单失败及可重放竞态、同字节最终测试RED→GREEN均保留在[来源作者验收](../research/proactive-source-authoring-acceptance-2026-10-16.md)，不将受控复现称作历史CI唯一首因。#176独立源eddfdb9两轴无遗留，四提交已整合为3c5e4c3；准确后端交叉308项及前端221项、构建通过，新HEAD完整CI待核验。#179冻结ecde3b4正接受完整两轴审查，已发现回执恢复写NULL及固定32条扫描饥饿问题，尚未集成。旧失败历史和原生限制保留，跳过项不计原生验收。

事实来源：[目标规格](../specs/shiori-tool-parity-spec.md)、[固定版本源码调研](../research/shiori-tool-system-and-portability.md)、下表所列 Issue/PR、仓库行为测试和单独标明的原生验收。规格中的旧基线描述不代表最新代码状态。

## 交付与批准范围

- 总目标是 31 个核心工具、45 个插件工具、8 个 Skill、主动决策和 Drift。工具名字或 schema 存在不等于行为验收完成。
- 已获批准并合并 PR #145、#143、#146、#147、#150、#151，对应 #138–#144 中的六张实施票已关闭。
- 用户随后明确要求剩余多个 PR 汇总为一个大 PR；剩余改动汇入 [草稿 PR #162](https://github.com/sawaki-zexi/meido/pull/162)。原 Skills 草稿 PR #161 已关闭为被替代，未单独合并，#152 保持开放。
- D01–D03 及屏幕感知已批准，见 ADR [0011](../adr/0011-versioned-tool-activation.md)、[0012](../adr/0012-owned-task-runtime.md)、[0013](../adr/0013-independent-generic-schedules.md)、[0014](../adr/0014-explicit-screen-vision-binding.md)。2026-10-16 主人另明确批准 [Windows 桌面控制](../adr/0015-owned-windows-computer-use.md)、[NovelAI](../adr/0016-bounded-novelai-generation.md)、[Coding Agent](../adr/0017-owned-coding-agent-runtime.md)、[主动决策/Drift](../adr/0018-explicit-autonomy-and-drift-grants.md)四项具体规格第3节，正在实施；真实账号、消费及外部事务仍按各自范围另批。桌宠宿主选择仍待回答。
- 汇总 PR 不具有合并批准；实现、审查、CI 和必要验收完成后仍需人工批准。剩余 Issue 不因进入集成分支而关闭。

## 范围追踪

“集成分支”表示尚未进入 main；“待实现”不能解释为永远以不可用占位交付。

| 标识 | 当前实现位置与证据 | 尚待完成 |
| --- | --- | --- |
| G01 配置与目录 | #163、#167、#158 已进入集成分支；文件 4、网络 2、消息 2、记忆写入 2、发现 1、桌面推送 1、调度 3，共 15 项网页授权入口，两轴审查及公开 HTTP/React 用户操作通过 | 继续保留 Shell/浏览器等专用开关；后续能力配置另走明确流程 |
| C01 工具发现 | #148 已进入集成分支；同 run 版本化激活、冻结候选、服务 grant 修订、真实来源和实时撤权；连续取消补充修复已两轴独立复核 | 总 PR 的最终跨模块验收 |
| C02 文件 4 工具 | main，PR #143；真实文件、图片 HTTP 往返、根目录和原子写行为测试 | 后续工具集成不得扩大文件授权 |
| C03 Shell/任务 3 工具 | #149 已进入集成分支；独立 TaskRuntime、SQLite、日志、实际进程树生命周期及来源授权经过两轴审查 | 后续 spawn/调度/Coding 集成回归；不得扩大 Shell 许可 |
| C04 网络 2 工具 | main，PR #146；受控 HTTP、公开网页与真实搜索服务验收 | 后续集成回归 |
| C05 记忆/消息 5 工具 | main，PR #147、#151；真实 MemoryEngine/SQLite/HTTP，取消后写入回滚与范围验证 | 后续任务和来源集成回归 |
| C06 投递/账号 4 工具 | main 复用 PR #128、#136 的渠道宿主与账本；#167 桌面文本/媒体已两轴复核并集成；#170 分类有界目录与当前连接事实已集成，轮询误停 P1 修复后的两轴复核均无遗留；真实来源、分页与本地协议纵切见[契约](channel-directory.md)及[验收](../research/channel-directory-acceptance-2026-10-16.md)；#181飞书握手与停止资源修复也已两轴复核并集成，见[连接契约](feishu-owned-connection.md)及[验收](../research/feishu-owned-stop-acceptance-2026-10-16.md) | 总 PR 最终跨模块验收及明确授权后的真实平台收件证据 |
| C07 子任务 2 工具 | #157 已进入集成分支；真实 TaskRuntime 子 loop、冻结 profile/模型、预算、原来源管理和完成交付已两轴审查；连续取消、pending 去重、跨库恢复与锁冲突修复的最终复核均无剩余 finding | 总 PR 跨模块集成与最终验收；未使用真实平台账号或付费模型 |
| C08 调度 3 工具 | #158 固定 `e992e0f` 两轴最终复核均无剩余 finding并进入集成；交叉 400 passed、前端174及构建、5源类型检查通过；`4b1b3b8` 两套CI全绿，后端1712 passed/16 skipped；持久找回/ACK、取消及worker存活见[契约](owned-schedules.md)及[验收](../research/owned-schedules-acceptance-2026-10-16.md) | 总 PR 最终验收；未使用真实平台账号或付费模型，现有待办不计作此项实现 |
| C09 群/成员 4 工具 | #165、#166 已进入集成分支；可信历史昵称、成员来源、完整群分页、笔记字段省略/清空及离线资料；两轴最终复核通过，事务直接读取父取消并等待写线程退出 | 总 PR 跨模块回归和最终验收；不得扩大来源范围 |
| C10 MCP 3 工具 | #159 已进入集成分支；真实 stdio、版本化发现、配置 UI、持久连接意愿及严格凭据清理已两轴复核；Linux 回收、Windows asyncio 环境、中文 schema 编码及 peer_error 清理竞态均实证修复并两轴复核；`c03a9fb` 两条 CI 通过 | 总 PR 最终跨模块验收 |
| P01 浏览器 28 工具 | main，PR #150；全部 28 schema 映射核验，21 项真实原生浏览器测试通过，无 skip | 后续 MCP/配置集成回归 |
| P02 桌面控制 8 工具 | #169 固定源 `7aa8108` 已两轴复核并整合；8工具/图像下一模型请求、单观察/lease/Job收尾有真实专用窗口和HTTP/provider证据。规范审查两项P2修正后独立复核无遗留；集成交叉60 passed/1原生deselected，前端专项及TS/Vite、9源mypy和c052848两条完整CI通过 | 总PR最终跨模块验收；多DPI/多屏/锁屏等未验收环境见[原生手册](../handbook/computer-use-native-acceptance.md)，不扩大真实应用操作许可 |
| P03 Coding Agent 5 工具 | #174固定源`5a5a2c4`及关闭/配置夹具修正均已两轴复核并通过`fff08b1`两条CI；7项Herdr/两合成CLI原生通过，见[验收](../handbook/coding-agent-acceptance.md)。#175固定源`0e2da975`完成完整两轴及修正复核后合入，独立Codex exec复用spawn coding，见[S07验收](../handbook/codex-delegate-acceptance.md)；准确13c集成两轴与两套完整CI通过。#176独立源`eddfdb9`完成精确提交/发布及两轴修正复核，已整合`3c5e4c3`且集成两轴无遗留；准确交叉308 passed/3 warnings、前端221项、构建、13源默认类型及6源check-untyped-defs通过 | #176新HEAD完整CI；旧首push unknown原因未解释，隔离通过不当作根因修复。真实认证/消费/发布另批，不能以合成CLI代替真实服务 |
| P04 屏幕感知 1 工具 | #160 已进入集成分支；提前拒绝/重复取消连接回收最终两轴复核均无剩余 finding；相关后端 196 passed/1 native 条件 skip，前端 69 passed；真实受控 Windows 主屏/125% DPI/本地视觉及取消通过，无原图落盘 | 总 PR 最终集成；尚无双屏、混合 DPI、实际锁屏/远程断开或 frozen 分发实证，也未调用真实远端视觉服务 |
| P05 桌宠 1 工具 | 待实现；Electron/PySide6 宿主选择待回答 | 真实透明置顶窗口、动作包、move/play 与完整生命周期 |
| P06 生图 1 工具 | #171/#172已两轴复核并通过准确13c两套完整CI，含当前主人txt2img/img2img、唯一原来源、历史、指定图片CAS重生成和受管后台。#173源`0d1b918`完成人类回合CG、受限标签与卡片写回；首次资产落库丢责任P2真实RED后修正，源和集成两轴均无遗留；`929974f`本地交叉321项、前端215项及构建通过，后续`e71145a`两套完整CI均通过，见[手册](../handbook/novelai-generation-acceptance.md) | 后续功能集成回归；真实NovelAI账号生成和费用另批 |
| P07 QQ 附件 1 工具 | #164 已进入集成分支；多字节长行自动字符分页、当前/引用真实入站与存储/Runtime 往返、旧附件/跨群拒绝均经过两轴独立审查 | 平台实机验收另列；传输夹具不是 QQ 真人收件证据 |
| S01 weather | #152 已进入集成分支；真实 curl、wttr.in、Open-Meteo 和公开 HTTP 工具往返 | 最终配置集成；Shell 仍须主人显式授权 |
| S02 summarize | #152 已进入集成分支；真实 summarize 0.25.2、Node 24、URL 提取及本地模型夹具摘要回传 | 两个公开视频仅得到 `transcriptSource: unavailable` 和网页模板，未取得真实字幕；不能以 exit 0 宣称视频摘要成功 |
| S03 feed-manage | #168 已完成两轴独立复核并进入集成分支；内置 Skill、动态服务目录/授权和真实公开 HTTP 七动作通过；独立复跑 8 HTTP + 2 原生用例，集成含 2 原生交叉 94 passed，含 RSS/Atom、下一模型请求、fresh list、重启、503 旧缓存与撤权；详见[验收](../research/feed-skill-native-acceptance-2026-10-16.md) | 总 PR 最终验收；主动来源/ACK/通知和 Drift 仍不属于此项完成范围 |
| S04 meme-manage | #152 已进入集成分支；指令、资源与角色目录边界已有行为测试。准确e71145a另完成本地主人上传→真实表情目录复制/视觉请求→桌面媒体→存储重建后从历史找ID再发送，1 passed/9.52秒，见[媒体链验收](../research/meme-skill-media-acceptance-2026-10-16.md) | 总PR后续集成回归；任意新路径不自动登记媒体ID，Drift文本循环不继承此视觉/复制能力 |
| S05 skill-creater | #152 已进入集成分支；HTTP 写入新 Skill 后下一轮发现/显式激活，资源 hash、依赖名、真实 Windows junction 反例通过 | 总 PR 最终回归 |
| S06 create-drift-skill | #179从准确已验证fa独立实现并冻结ecde3b4；普通聊天作者、同hash后续Drift消费、配额/工具/发送/恢复及前端已有本地合成证据，完整两轴审查进行中；尚未集成 | 修复回执恢复写NULL及扫描饥饿等审查问题，完成两轴复核和准确总集成验收 |
| S07 codex-delegate | #175固定源`0e2da975`已两轴复核并合入；显式Skill、准确依赖、多仓库、真实受管UTF-8/JSONL/SQLite/Git、后台/exact继续取消与原来源ACK有本地合成证据，不新增普通工具；见[手册](../handbook/codex-delegate-acceptance.md) | 准确13c总集成两轴与两套CI已通过；真实CLI账号、首次/resume模型与原生权限另验 |
| S08 create-proactive-source | #178固定最终源`0eb8ea4`完成完整两轴及P3提示/P2事件身份兼容修正复核；真实对话与FileTools创建六份产物，owner只读hash预览、原MCP trust/具体grant和v2受管验证后，同产物由真实clock/A01消费，桌面receipt与持久ACK在App/stdio重建后保持；context-only合法，v1不追认collection证明。ACK夹具时序修正已两轴复核，准确fa两套完整CI通过；见[手册](../handbook/proactive-source-authoring.md) | 总PR最终跨模块验收；真实账号与服务事务另批 |
| A01 主动决策 | #177固定源`3b21afe`及后续源/集成修正均两轴无遗留；含真实自主身份/许可、来源、草稿/finish、canonical桌面投递/ACK/Runtime恢复。准确f7be两套CI各2134项后端/186项前端及构建通过；详见[手册](../handbook/proactive-decision-acceptance.md)。#178已启动，#180仍依赖后续共享实现 | 总PR最终跨模块验收；未验收真实账号或模型消费 |
| A02 Drift | #179/#180具体架构已批准；#177/#178前置已通过准确fa两套CI，#179冻结ecde3b4接受完整两轴审查，采用同一tick空输入分支、独立开关与显式目标；#180原生依赖当前仅#179，尚未开始共享生产改动 | #179审查修正、两轴复核和准确总集成；随后完成#180原外部来源闭环 |

渠道补充切片 #166 完整群资料、#167 桌面文本/媒体与 #170 分类目录/连接事实均已完成两轴复核并进入集成分支。C06 的真实平台收件验收仍未完成。

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

上述修复的 `c03a9fb` 两条 CI 均通过；Linux 全后端 **1520 passed、16 skipped、2 warnings / 235.19s**，前端 **174 passed / 26 files**，TypeScript/Vite 构建通过。此为该固定提交的全量验证，不是仅重跑原失败项。

#168 规范审查无 finding；规格审查独立 Windows **10 passed / 44.11s**，确认 18 次脚本化模型请求、19 次真实远端调用、17 次 localhost 请求及撤权失败结果。最终源码 `256c2cc` 汇入 `8bb2a2e` 后，Feed、内置 Skills/显式激活、MCP schema、桌面推送 HTTP 与 Runtime 历史交叉 **94 passed / 107.13s**，包含两条实际 Feed 原生用例，无 skip。服务源码与隔离 venv 仅保存在外部证据目录，未新增项目依赖或访问真实账号。

#170 原固定 `73f0803` 的两轴审查均独立复现 Telegram 轮询误停。`d0894c3` 按实际连接/权限单调代际区分诊断写入，两份原探针修复后均通过，两轴复核无遗留。后续纠正验收脚本短环境变量并隔离旧头像/SDK夹具；最终 `8d2ff2a` 在完整三项临时环境变量与整组网络guard下 **210 passed /73.85秒**，两原探针同环境通过。生产规范独立 **20 passed**、规格独立 **68 passed**，最终夹具两轴各独立 **23 passed**；重叠不相加。旧模块47项类型诊断逐条无新增，前端输入未变，继承174项及构建验证。汇入 `3feeb21` 后生产/测试/前端与审查源逐文件一致，无冲突。旧脚本与两轮guard失败未被改写成成功；另已复现基线就存在的飞书SDK停止后线程存活问题，单列#181，不以夹具修正代替生产修复。详见[验收记录](../research/channel-directory-acceptance-2026-10-16.md)。

#181 修复SDK同步握手、虚假停止成功、清理失败替代及取消遗失归属，并保留旧保存配置/密钥的回滚。固定 `f079ef6` 的整组guard交叉 **428 passed/173.27秒**，专集33项包含其中；独立规范18+15项、规格33+16项均通过。规范另发现新增类型诊断，`09259c9` 只补实际HTTPX异步生成器的可关闭静态契约；同一silent/no-incremental参数已由两位原审查者独立通过，SDK/HTTP专集15项复跑通过，未冒称重跑全部428。两轴最终均无遗留。汇入 `83e853e` 时全树与审查源一致；文档随后只更新证据和状态。原无效夹具、撤guard后未知区间及静态检查flags修正均保留，详见[验收记录](../research/feishu-owned-stop-acceptance-2026-10-16.md)。

这些数量仅说明对应固定范围，不能替代尚未实现的工具、真实平台收件或未取得的 YouTube 字幕证据。集成分支继续推进时，更新本表和最终验收记录。

#169 的独立源提交 `7aa810870ece832419a73d773d4ae7ed7baa6cb0` 经规范和规格两轴复核后进入总分支。真实专用窗口完整八工具/HTTP下一provider图像链路7 passed；作者本机全后端为1844 passed/14 skipped/1 MCP schema worker deadline失败，相关参数范围重跑9 passed，不能把两次结果合写为全量无失败。规范审查另发现lease构造超时晚获mutex的线程遗留和手册第三数据根缺失；真实随机mutex RED后修正，相关30 passed、原生收尾2 passed，独立修正复核通过。早期作者验收没有完整记录owner-knowledge根覆盖，已撤销“三数据根均隔离”的推断；新修正和根集成检查显式设置全部三根。

总分支对非文档内容核对与源提交完全一致，集成60 passed/1原生deselected及前端专项1 passed、TypeScript/Vite通过。随后9源文件mypy发现四项新类型声明问题；补齐容器类型和dataclass字典工厂后，mypy全过、相关26 passed/1原生deselected。此段保留首次类型检查失败，不改变实际工具权限或原生资源锁。

集成 `966a539f730d350f7654522ce36cf84c484b99f4` 的 [push CI](https://github.com/sawaki-zexi/meido/actions/runs/38853231583) 完整通过：后端1841 passed/25 skipped/2依赖warning（284.07秒），前端175 passed/27文件及构建。[同HEAD的PR CI](https://github.com/sawaki-zexi/meido/actions/runs/38853238075) 前端通过，后端在 `test_memory_tool.py` 文件摘要后约21分钟未继续输出（04:59:41至05:21:06 UTC），由维护任务保留原始日志后取消以诊断，不能计为通过；尚无堆栈确定其停在该文件收尾还是下一测试。相邻两文件本地定向12 passed/8.66秒，未复现，不据此归因为基础设施。后续CI打开pytest的90秒 faulthandler 堆栈输出以定位潜在再次挂起；此选项不改变用例超时、断言或准入行为。

诊断增量 `c052848` 经两轴窄范围复核无遗留后，新的 [PR CI](https://github.com/sawaki-zexi/meido/actions/runs/38856303468) 与 [push CI](https://github.com/sawaki-zexi/meido/actions/runs/38856299362) 均完整通过：后端分别1841 passed/25 skipped/2 warnings（235.57秒、260.90秒），前端各175 passed/27文件及构建。邻近记忆六模块本地135 passed，同进程保留重复用例的八轮96 passed，均未复现旧挂起。新CI没有90秒堆栈触发，旧取消原因仍未解释，不以本轮绿色结果冒称根因修复。

#171 源 `b786a867` 的公平恢复批次P2经两轴独立复核后汇入。NovelAI、桌面控制与角色能力集成交叉108 passed/10原生deselected（96.19秒），前端专项9 passed/6文件及TS/Vite、11源mypy与compileall通过。实际删除门禁回归RED后，将记忆准入关闭移至所有owned资源等待之前，新增和两个既有删除/取消测试3 passed；不改变生图POST/发送次数或凭据许可。集成冲突与时序修正随后通过两轴独立复核，不以源分支的80项替代总分支检查。

该首轮108项中的旧角色能力模块没有整组网络guard，其外联边界未知，已撤回整组零外联推断。独立审查的guard阻断了四个旧聊天能力用例的无关向量服务连接，作为验收隔离P2修正。夹具现独立关闭无关自动记忆并持有guard，原断言不变；原交叉范围另加两个删除回归，完整三根+整组guard重跑 **110 passed/10原生deselected（91.93秒）**。早期未知区间不因新证据补写为成功；真实NovelAI服务仍未调用。

`0dee4e4` 的 [PR CI](https://github.com/sawaki-zexi/meido/actions/runs/38859073785) 与 [push CI](https://github.com/sawaki-zexi/meido/actions/runs/38859069072) 均全通过：后端各1922 passed/25 skipped/2 warnings（260.63秒、440.21秒），前端各180 passed/31文件及TS/Vite。此后才从该已验证基点开始#172，不把本地CAS进度当作重生成/后台交付已完成。

#174 源 `5a5a2c4` 修正资源槽、初次原子交接、最终发布授权与worker故障恢复后，两轴复核均无遗留，汇入 `aab1078`/`3634586`。私有Coding模块、专用测试、固定补丁与vendor和源提交一致；共享配置/UI保留CU与Novel。准确集成交叉 **167 passed/1个CU原生deselected（268.70秒）**，包含全部48项Coding专用及7项真实Herdr/合成CLI原生用例；前端完整32文件182项、14源默认mypy、compileall与TS/Vite通过。该mypy未声明检查全部未注解函数体。原生短路径成功不消除已记录的长路径限制。

独立集成规格16项通过；规范53项通过但发现Coding清理未确认会跳过其余独立owner的退出。真实Runtime回归先2项失败，改为逐一尝试各owner、最后保留单异常或组合异常。首轮相关33项pytest通过后，外部runner关闭先前已取消的worker仍失败，整条命令不报绿；另补终态worker、关闭调用方取消及两者同时取消的真实RED，区分旧worker终态与新的取消请求。修正后同范围 **36 passed（32.21秒），进程exit0**，完整三根与guard持有至所有owned退出；未确认Coding仍保留账本与可重试职责。固定 `a825e11` 的规范增量独立24项、规格增量独立22项均通过，两轴均无遗留；新准确HEAD完整CI尚待完成，没有合并或关票。

`9bed061` 的两条新CI前端均182项/32文件及构建通过，Linux后端各1965 passed/34 skipped/1 failed/2 warnings。失败是配置测试把符号链接的Python路径作为应通过路径门禁的SHA夹具。WSL同一测试先真实复现2 passed/1 failed，夹具使用解析后的实际文件后Linux3项、Windows3项均通过，原坏SHA与有效保存断言保留，生产链接/哈希门禁未改。完整日志已保留；这两条失败不计通过，后续新准确HEAD仍须完整CI。

后续固定`fff08b1`的两条完整CI均通过：后端各1966 passed/34 skipped/2依赖warning，前端各182 passed/32文件及TS/Vite，见本文开头的准确运行链接。配置夹具两轴窄复核均无遗留；这不代表之后177的提交已通过CI。

#177固定源`3b21afe`修正最终发布授权、主人获锁身份重验、首poll/150秒持久间隔与后续刷新旧健康后，作者扩大247 passed/1 symlink权限skip，两轴最终复核均无遗留。合入后`261b468`的集成规格独立68项后端、7项React及6项关闭/取消通过，规范独立95项与3项删除边界通过，两轴均无遗留。共享冲突保留已有CU/Novel/Coding入口、事务与资源关闭；memory门禁先于任何owned等待。总分支全三根和完整网络guard组合 **420 passed/1权限skip/1 CU原生deselected（611.98秒），进程exit0**；前端完整33文件186项、TypeScript/Vite、20源mypy（检查未注解函数体，忽略第三方缺失stubs）及compileall通过。准确HEAD CI仍待完成；各组重叠不相加，skip不计原生通过，详见[验收手册](../handbook/proactive-decision-acceptance.md)。

`6a1ef4f`两条CI随后失败：PR112 failed/2015 passed、push111 failed/2016 passed，均35 skipped/2 warnings，前端均186项及构建通过。多数失败由两处旧startup夹具在短命loop遗留主动worker、后续合法拒绝替换owner引发；另三项POSIX契约spy错误，以及一项真实重复取消打断模型finally的竞态。两处无关startup隔离和按平台实际入口观察不改变原断言/生产门禁；执行器改为原gate内单次取消，保留入场前取消。真实RED后Windows/WSL相同相关37项均通过，startup/真实记忆HTTP等17项通过。固定`c105ae8`扩大**267 passed/1权限skip（322.38秒），进程exit0**，两轴修正复核均无遗留；前端0diff，沿用6a两条CI的186项及构建。新准确HEAD完整CI仍待完成；#178前置依赖继续保留，不以旧绿色放行，详见手册。

准确`237db7a`的PR CI后端2128 passed/35 skipped/2 warnings，push CI后端2 failed/2126 passed/35 skipped/2 warnings；前端两次186项及构建通过。失败定位为来源核验首次恢复快照的SQLite短时写锁；真实持锁RED后，仅原生BUSY/LOCKED延后本轮，其他数据库错误重抛、原nonce/key和copy/ACK责任不变。相同相关组Windows52项、WSL52项及变更单源mypy/编译通过，三新根和内外guard至owned退出。固定`35048fb`两轴增量复核均无遗留：规范独立Windows18项/WSL6项，规格独立Windows7项通过；外部探针初装setup error如实保留，未计产品RED。新准确HEAD两套完整CI仍待完成，#178依赖继续保留，详见[验收手册](../handbook/proactive-decision-acceptance.md)。

随后准确f7be两套完整CI均通过，计数与链接见本文开头；#178已解除满足的依赖并从f7be独立实施。#172源60860fb、#175源0e2da975均完整两轴/修正复核无遗留后合入；固定`43fc03a`的私有模块与各准确源一致，主动/MCP/TaskRuntime与f7be一致。共享main、SessionStore和ChatView保留两侧目标；最终INSERT前授权、角色同步stop、任何await前delegation seal、记忆删除门禁和逐owner关闭均保留。

此准确生产/测试输入的总组合 **610 passed/1 skipped（696.01秒），runner exit0**：全部Coding/Codex、NovelAI、所选主动/Runtime/MCP/Skills/spawn/Shell及角色删除/记忆HTTP。原七项Herdr/两合成CLI原生均实际通过；唯一skip为主动上下文Windows创建符号链接缺权限，不计原生通过。三新MEIDO根在collection/import前建立，内外local guard持到所有owned退出，零非本地阻断。完整前端 **202 passed/35文件（26.23秒）**、TypeScript/Vite、Coding19源默认mypy（两个未注解函数提示保留）、Novel11源check-untyped-defs/ignore缺失第三方stubs、compileall和diff通过；不称全仓strict检查。

固定43fc集成两轴均无遗留。规范独立后端32项、真实组合角色PUT1项、React5项及App共存DOM1项通过；规格独立组12 passed/1外置probe装配失败，补齐既有必填字段后该case1项通过，另App共存DOM1项通过。生产Novel工厂真实OneBot后台/原群替图、公开Coding/Skill管道、角色off/on跨模块撤权、消息CAS/最终授权及独立owner收尾有直接证据。外置probe装配/依赖解析错误保留，仅修审查夹具后转绿，不算产品RED或被整组通过掩盖。各组重叠不相加；新准确HEAD两套完整CI仍待，#173/#176继续保留依赖，真实账号/模型费用/平台真人收件及发布未验收。

随后准确 `13c2aea` 两套完整 CI 均成功（链接与计数见开头），#173/#176 已解除已满足的原生依赖并从该已验证基点建立独立分支。该结果不代表后续主线同步或 #178 已完成验收；总 PR 仍为草稿，未合并、未关闭剩余 Issue。
