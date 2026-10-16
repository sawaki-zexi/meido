# 创建并执行角色长期 Drift 技能

状态：当前 / #179 已审源3d56102本地整合为总分支3eed571的操作流程；本地组合及集成两轴复核通过，新HEAD完整CI待核验，尚未合并，不表示真实账号或付费模型验收完成。

事实来源：[作者正文](../../backend/app/builtin_skills/create-drift-skill/SKILL.md)、[配置](../../backend/app/proactive_runtime/config.py)、[产物与信任](../../backend/app/proactive_runtime/drift_skills.py)、[执行](../../backend/app/proactive_runtime/drift.py)、[设置界面](../../web/src/features/role-management/DriftSettings.tsx)、[真实作者闭环测试](../../tests/python/test_proactive_drift_authoring_api.py)。最后核验：2026-10-16。

## 先保存作者范围，再创建文件

在角色自主设置中勾选“允许创建或更新 Drift 技能”。展开“配置 Drift 工具许可”，选择作者可声明的工具及资源；确有需要时允许声明 MCP 或受限 Shell 依赖。保存后宿主分配作者范围 ID 与版本。此时可以保持 Drift 关闭，也不用提前授予运行时 MCP 许可。

给角色配置文件工具，在普通主人聊天输入 `$create-drift-skill 创建一个长期读书笔记任务`。S06 正文包含三份可直接写入的完整模板；模型通过真实 FileTools 保存：

- `drift/skills/<id>/SKILL.md`：合法 Meido frontmatter 和每次执行的有限步骤，最后保存。
- `drift/skills/<id>/references/drift.json`：匹配的 skillId、MCP 工具依赖、只读资源与 Shell 规则 ID。
- `drift/work/<id>/notes.md`：可跨多次运行更新的工作文件。

SKILL.md 使用 `id/version/description/tools/activation`；MCP 依赖放在支持文件 `references/drift.json`，不要写成未知 frontmatter。支持文件的相对路径以技能发现根为准，宿主复用 SkillRegistry materialize 和实际内容 hash。更新已有任务时保留 work 文件；工作进度不作为不可变指令支持文件。

正常聊天的文件根是角色 workspace，因此路径包含 `drift/`。仅当宿主以真实 Drift 身份激活 S06 时，文件根已是 Drift 专用根，使用 `skills/...` 和 `work/...`。改变文本不能改变身份或根。宿主 `state/` 只能生成进度投影，模型不能通过大小写别名写入。

宿主只对获准作者范围内、真实 S06 来源及工具调用写入的实际字节登记作者证据。模型填写的 hash、trust=true 或主人自称不能代替证据。作者当下只有 FileTools，也可以声明主人后续另授的 MCP/Shell；作者声明上界和后续执行许可分开保存。

## 补齐同一产物的依赖

刷新“Drift 长期技能”。`available` 表示当前产物、信任及依赖满足；`pending_dependency` 保留具体缺项；`untrusted` 需要重新核对作者范围或明确采用；`invalid` 需要修正真实文件。保存文件不表示已经执行或投递。

明确选择“Drift 模型连接”，打开 Drift，授予该技能所需实际本地工具。普通角色工具许可、作者声明范围、技能声明及本次冻结许可必须同时满足。Drift 不会回退到聊天或 A01 模型。

MCP 依赖按 `serverId` 和远端操作名声明，可附精确定义 hash。先按现有 MCP 流程保存受信服务及角色许可，在主人聊天通过 `tool_search` 加载 `mcp_add` 并明确连接一次。设置中的许可目录只读取已观察缓存，不会为了刷新目录而启动服务。选择观察到的服务配置版本、grant 版本、风险及每个所需工具的定义 hash，再保存 Drift MCP 许可。每次真实运行还会重新握手核验；多余 owner 操作不会自动出现在技能 schema。

没有新增作者范围或改动产物时，补依赖后刷新原 hash 即可。若作者范围扩大、停用重开或文件来源未验证，在技能卡片预览当前真实文件并明确采用：宿主持久保存不透明 previewId、文件 hash 与目录/文件身份，采用时再次比较原作者范围和配置版本。503 保留同一次采用 ID 供重查；409 要重新预览并确认，不自动把旧授权换成新目录。即使相同字节换目录，旧预览也失效。

Shell 只接受宿主 `python-version` / `git-version` 规则 ID，要求角色 Shell 专用开关开启且现有命令白名单允许该绝对可执行程序；技能 sidecar 和当前 Drift 许可也须声明同一 ID。正常角色设置即可启用，不需要向普通工具列表手工加入 `shell`。宿主固定 argv、Drift cwd、最小环境和执行文件 hash，不接受命令、脚本、stdin、凭据或任意输出路径。该机制不构成操作系统沙箱。

## 等待合格运行并检查进度

宿主时钟只在没有正在运行的主人对话、上次主人回合结束满十分钟、全部启用来源可信且没有 alert/content 时选择 Drift；来源失败不会伪报为空。Drift 与 A01 共用原 tick，不能在同一次 tick 各运行一次。

Drift 最少间隔三小时，UTC 每日最多四次，并计入原角色每日 24 次、应用每日 120 次准入。进入时即消耗尝试和冷却，失败不退还。原 TaskRuntime 任务最多 600 秒，Drift 最多 20 步、8192 预留输出 token；模型纠错和缺少 finish 仍消耗预算。

运行先明确 `activate_skill`。需要 MCP 时只能挂载本次已冻结且符合当前技能依赖的服务。宿主依次 stage、持久保存版本、publish；下一次真实模型请求才能看到新增 schema。目录变化、撤权或能力版本持久化失败都会拒绝实际业务调用。

进度卡显示实际 `runCount/lastRun/oneLine/nextAction/messageResult`。`finish_drift` 一次事务提交精确结果和下一步，重复相同 finish 返回原收据，冲突拒绝；下一合格运行读取同一工作文件及原下一步。`drift/state/<id>.json` 是可恢复投影，投影失败时保留 SQLite 中的同一版本，重启只修复投影，不重做模型、Shell 或发送。

## 发送、停止和常见失败

本票只启用桌面默认目标；外部渠道跟踪在 [#180](https://github.com/sawaki-zexi/meido/issues/180)。技能、角色、冻结 Drift 工具许可及目标同时允许 message_push 时，每个 run 才有一次发送机会。正文必须非空，可选最多四个本 run 获准的当前角色已有公共附件 ID。设置最多保存十六个附件 ID；宿主核验归属、实际文件身份和内容，不接收路径、URL 或 base64。

发送前先持久占用机会；失败、超时、取消都不恢复机会。真实桌面 canonical message 和 sent receipt 在同一事务提交。发送成功后的模型 schema 与实际 dispatch 都只允许 write_file、edit_file、finish_drift。finish 的 message_result 必须匹配：未 dispatch 为 silent、真实 sent receipt 为 sent、确定失败回执为 failed、回执不明为 uncertain。取消、预算耗尽或没有合法 finish 都保持 aborted，不生成假进度。

已结束任务只有在原 Runtime 结果副本与 ACK 完成后才做 receipt-only 对账；只读原 intent 的真实记录，不重发、不补 finish。不确定发送会阻止该角色后续自主发送，仍可继续静默任务。不要靠删除错误、改目标或重启解除占用。

回执对账每批最多查询三十二条原记录，逐条持久保存观察位置；应用重建后继续扫描，暂时查不到回执的历史记录仍保留，不会永久挡住后来的回执。观察位置不代表已发送、已失败或已解除发送占用。

已经真实结束且完成 Runtime 结果 ACK 的工具完整输出沿内部 transcript 的七日保留期清理，仅保留有限调用身份和 hash。已解决运行的 finish 详情沿九十日规则压缩；当前技能进度与工作文件保留。未 ACK 结果、未知发送和未完成投影仍保留各自必要恢复事实，过期内容清理不会重新获得发送机会。

真实主人消息会立即 seal 旧自主身份，随后取消并回收该 run 的 MCP、Shell 子进程及读写线程；必须完成 owned join 才能开始新自主工作。Runtime 终态、业务 finish、文件投影和结果 ACK 是不同状态；界面进度可见不表示所有清理已经结束。

工作文件单份不超过 8 MiB，角色 Drift 根不超过 256 项和 64 MiB，全局自主工作/缓存/日志不超过 1 GiB。预留包含替换旧文件、临时文件和目录开销，宿主进度保留少量空间；满额会拒绝写入或新准入。发现未知文件也计入，links、junction、hardlink、大小写碰撞和超出扫描边界的目录会拒绝。修复实际文件并等待原 journal 清理；不要把删除 journal 当回收完成。

内置 meme-manage 会被真实发现和校验，但当前 Drift 不支持其所需的可见图片核验、表情库写入与复制，因此整项显示 pending_dependency；现有许可勾选不能补齐该执行能力，请在主人聊天使用。内置 S06 只有明确允许“在自主运行中创作 Drift 技能”且其余依赖齐全时才可执行。普通聊天中的原有表情流程保持原权限与行为。
