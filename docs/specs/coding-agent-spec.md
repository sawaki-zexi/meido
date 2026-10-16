# Coding Agent 委托：Herdr 与 codex-delegate

状态：当前规格，第 3 节 D05 具体架构已于 2026-10-16 获主人明确批准，实施进行中；覆盖实现、隔离本地构建和无费用 fixture，不表示 P03/S07 已实现或获准运行付费 CLI、访问账号、推送或创建 PR。决定见 [ADR-0017](../adr/0017-owned-coding-agent-runtime.md)。

关联：[Parent #137](https://github.com/sawaki-zexi/meido/issues/137)、[总规格 P03/S07、D05、T15/T20](shiori-tool-parity-spec.md)、[CLI与固定Shiori调研](../research/coding-agent-cli-portability-2026-10-16.md)、[Herdr源码隔离核验](../research/herdr-runtime-isolation-2026-10-16.md)。最后核验：2026-10-16，接口基线 Meido `504d5b0b307736c14da7d1da87a1bad36e010c76`，Shiori `4f266aeaf42fcd611362e2456417b91f659341cf`，Herdr `7e26bad94044eba205033111a0ce8033ede7e24f`。

## 1. 用户结果与范围

主人批准仓库、原生CLI profile及有限执行许可后，角色能启动真正的Herdr Coding任务，查看状态/成果，向同一原生会话追加要求并取消；修改发生在独立worktree，宿主运行获准验证并核对实际Git交付。主人审阅确切成果后可另行授权固定remote/base的推送及Draft PR，回报真实PR head。

`codex-delegate` Skill可发现且需显式激活；它通过既有spawn的受控coding profile后台启动真正的Codex exec，回读结构化结果和精确session ID，并将结果回原角色/原来源。它与Herdr共用归属和交付模块，保留两种原生协议；不能将五个Herdr名称改成直接Codex别名，也不新增第77个普通工具。

范围包括修改、固定验证、本地commit、经授权发布、精确continue、取消、重启后的诚实状态与来源隔离。只有健康检查、fake CLI或纯Skill文本不算完成。访客、群友、旁听、Drift及内部完成事件无新建/继续/发布资格；宿主确认的主人渠道回合沿用现有线程、账号和配对代际，不能借桌面默认来源。

## 2. 已提取的事实与不能沿用的默认值

- Shiori实际调用外部Herdr CLI；需要CLI及server兼容且≥0.9.4，使用worktree/agent/pane协议，不是Python Coding SDK。其固定源码默认强加Codex bypass或Claude skip-permissions、完整os.environ及commit/push/Draft PR指令；本方案全部改写。[固定协议与行号](../research/coding-agent-cli-portability-2026-10-16.md#已提取外部依赖身份与-cli-协议)。
- Herdr官方0.9.4的Windows PTY会补回注册表环境，daemon可能经WMI脱离Job；TerminateProcess权限/返回与等待不足以证明树退出。只给短命客户端minenv、设置CODEX_HOME或看到socket消失都不能称隔离。[源码证据§3–4](../research/herdr-runtime-isolation-2026-10-16.md#3-已提取agent-从既有-pane-shell-启动不接受每次调用的独立-envcwd)。
- Herdr原生session关联依赖集成hook；其自动恢复可能丢失原权限/model参数。默认禁止自动restore，不能用pane名、idle/done或`--last`替代精确native ID。[源码证据§5](../research/herdr-runtime-isolation-2026-10-16.md#5-已提取attach-与-native-resume-的身份不同)。
- [TaskRuntime](../../backend/app/task_runtime/runtime.py#L129)已有全局8/角色3并发、每attempt≤600秒、日志/保留配额、原来源与取消；上述历史取证基线没有submission_key，当时仅只读核对任务分支接口。#158 的submission_key/retain_until_ack/acknowledge_result接口现已完成两轴复核并进入经两套CI验证的总集成 `4b1b3b8`，本票复用同一接口。现有[SpawnExecutor](../../backend/app/agent_runtime/spawn_tool.py#L155)限制任务目录，不适合作为任意repo/env/stdin入口。
- 本轮只运行help/version：Codex 0.159.3、Claude 2.1.283；版本不等于登录/额度/真实任务成功。Codex exec/resume参数不同，Claude permission模式不等于OS沙箱；滚动官方文档不能代替固定版本原生探针。[OpenAI官方CLI](https://learn.chatgpt.com/docs/cli/reference)、[非交互事件](https://learn.chatgpt.com/docs/non-interactive-mode)、[Claude官方CLI](https://code.claude.com/docs/en/cli-reference)。

依循[ADR-0004](../adr/0004-role-scoped-shell-tool-boundary.md)、[ADR-0009](../adr/0009-multichannel-conversation-threads.md)、[ADR-0011](../adr/0011-versioned-tool-activation.md)、[ADR-0012](../adr/0012-owned-task-runtime.md)。独立Coding实现不扩大通用Shell的cwd/env/stdin/白名单。

## 3. 已批准的一揽子具体选择

| 决策 | 采用方案 |
| --- | --- |
| 原生首发 | Windows x64本机受管执行；非Windows先有公开协议fixture，原生显示未验收/不可用，不自动改到WSL或远程。跨平台原生支持另补实际监管和CLI证据。 |
| Herdr依赖 | 选择固定0.9.4源码的Meido owned-mode补丁构建，独立版本后缀、补丁清单、构建/资源哈希和许可证记录；官方不变版不准进入ready状态。具体必修项见§4。 |
| 所有权 | 每CodingTask专用Herdr命名server/数据根；每attempt监管器持有server及批准启动链的进程树。终态不保留耗用模型/CPU的idle agent；continue新建进程并精确native resume。 |
| 预算 | 默认每attempt600秒，含启动/模型/验证/本地commit；540秒后不开始新操作，余量用于收尾，绝不沿用上游2小时/6小时。全局Coding最多2、每角色1、每repo1，且实际占用TaskRuntime既有8/3及存储配额。 |
| 默认许可 | 全局Coding与角色Coding默认关；repo/profile必须显式分配。新任务默认允许获准worktree编辑+固定验证，localCommit/fetch/push/Draft PR分别默认关；启用UI说明CLI模型费用和本机权限。 |
| 有限执行量 | 每角色默认5个native attempts/UTC日，每任务最多10个attempts；continue也计一次，未知提交也占用。它们是本地软上限，600秒不是精确token/费用限制，不承诺订阅免费。 |
| CLI profile | 固定真实executable、版本/哈希、agent kind、model ID、认证来源、权限模板及配置manifest；角色只引用稳定profile ID，不传聊天模型密钥，不自动读取/复制桌面CLI登录。 |
| CLI权限 | Codex采用workspace-write+never，禁止网络升级；Claude采用restricted+dontAsk、仅Read/Glob/Grep/Edit/Write。拒绝bypass/full-access/skip-permissions和模型自选flags；验证/commit/发布由宿主执行。 |
| 扩展与认证 | 专用CODEX_HOME/CLAUDE_CONFIG_DIR和显式认证；Herdr仅允许固定哈希的session/state上报hook，其他用户/项目hooks、MCP、plugins、自动更新/恢复禁用或在固定manifest中明确排除。原生验证不通过则profile不可用，不回退主人默认配置。 |
| 本机权限承诺 | owned-mode保证批准启动链的环境和进程所有权，不承诺隔离同OS账号全部文件、keychain或恶意代码。本版本只支持主人信任的repo/CLI和批准验证程序；不提供“任意不可信仓库OS安全沙箱”模式。 |
| S07入口 | 扩展既有spawn `profile=coding`，要求run_in_background=true、retry_count=0和repository；factory转CodingTask/Codex adapter，不构造聊天provider。其他三profile不变，不放宽Shell。 |
| 幂等与未知 | 持久CodingTask/Attempt/命令dispatch/outbox；同宿主call查回原任务，continue CAS且不自动重发prompt。ACK丢失保留unknown，取消/重启不自动继续。 |
| 发布 | 原生agent无宿主发布凭据；完成本地验证/commit后，主人对task+确切HEAD+remote/base给一次性发布批准，宿主broker执行固定Git/GitHub事务；agent自报PR URL不作成功证据。 |
| 结果与清理 | 结果文件256KiB、单帧JSONL64KiB、TaskRuntime日志每attempt1MiB/模型摘要8000字符；每角色最多10个保留CodingTask、任务文件2GiB。满时拒绝，不自动force删除成果worktree。 |

§3是一项完整建议，常数为可逆首版界限，不要求逐个常数审批。改变原生平台、放宽CLI能力/网络、延长600秒或允许跨终态活agent须另审；CLI账号/费用与外部发布还须相应明确授权。批准架构不等于原生验证或发布许可。

原生同账号执行的剩余权限必须在配置页说明：不主动导入秘密不等于恶意程序不能读取同账号资源；Git helper禁用、CLI工具限制和canary不是完整OS隔离证明。宿主broker拒绝无许可发布是强制行为，但本规格不承诺拦住任意获准本机代码通过OS服务/自行找到的凭据发布。需要这种强保证时必须另行选择实际隔离OS环境，此模式不伪称满足。

## 4. Herdr owned-mode 必须具备的补丁

owned-mode是显式且可检测的能力协议，宿主必须核对该能力版本和构建哈希；单改一个TerminateProcess flag不够，也不以官方版本号≥0.9.4替代。

1. Windows portable-pty从宿主allowlist构造环境，关闭HKLM/HKCU Environment补回及任何值trace；pane不得再次合并整process env。所有shell固定无用户profile启动，CLI由受控launcher使用固定绝对路径，不能靠PATH找到另一版本。
2. 只接受显式专用命名server、数据根和本地IPC；不走auto-detect共享session、远端、TUI attach、WMI/daemon脱离路径。server以foreground方式受监管启动，不允许breakaway；自有Job建立于server运行前，启动失败必须回收。
3. 正确取得终止权限、检查系统调用返回并等待退出；owned stop返回结构化server代次、进程/Job退出结果及失败，而非只有socket消失或warn。取消升级杀整个自有server/Job，不按可复用pane名误杀共享进程。
4. owned模式禁自动update、manifest下载、插件启动和resume_agents_on_restore；只加载本attempt固定的配置及集成。状态/session hook有字节限制和固定路径/哈希，只上报session/state，不带认证；session绑定同时核对服务代次和terminal/pane/workspace/agent kind。
5. 显式resume必须使用宿主给定的native ID与冻结的完整权限/model模板，不得采用Herdr标准restore的简化argv。owned能力应拒绝缺模板的恢复，不能默认新建native会话。

Windows Job负责批准启动链的普通/晚生/无pipe后代；fixture还须覆盖Herdr原WMI脱离路径已关闭。它不是对恶意同账号程序经外部OS服务另建进程的全面防逃逸承诺。owned环境、全部已知句柄和Job内后代退出均未确认时，cleanup_state不能标complete，profile暂停新准入，不能以TaskRuntime.cancelled掩盖残留。

后续补丁使用固定Herdr Apache-2.0源码，保留LICENSE及适用NOTICE/修改说明；Shiori适配保留MIT署名。Windows发行须包含核验的ConPTY等必需资源，不能只复制exe；本轮不安装、构建或再发行任何内容。

## 5. 配置、来源与工具 interface

全局配置保存repo grants与executor profiles，角色仅保存coding.enabled=false、repoGrantIds、profileIds、defaultHerdrProfileId、defaultCodexProfileId、maxAttemptsPerDay=5。repo grant固定repoId、规范绝对根/common-dir、允许角色、remote标识和无凭据URL、base分支、验证命令ID→固定argv/cwd、允许操作及revision。

repo注册验证Git根而非子目录；每次启动重新resolve真实路径，拒绝symlink/junction逃逸、repo替换、带秘密的remote URL和不一致common-dir。主checkout不被模型修改；没有fetch许可时只使用已存在的本地base SHA，缺失明确blocked。外部用户修改原repo/git元数据时按实际Git状态复核，repo锁不宣称约束其他程序。

ToolContext与SessionStore真实run/user消息产生TaskOrigin；渠道复用FrozenChannelSource的账号创建/配对/来源范围，持久sender scope。所有start/list/get/continue/cancel/log/outbox重验原role与来源；同角色不同访客、旧配对或重建账号不能继承。当前许可只能撤销冻结资格，不能用新配置偷偷扩权；profile变更后旧任务需明确重新批准或保持blocked。

| 现有名称 | 计划输入与执行 |
| --- | --- |
| herdr_start_task | 保留repository、requirement必填与agent=codex/claude可选；repository须命中repo grant，agent选择对应获准Herdr profile；始终走真正Herdr adapter。 |
| herdr_list_tasks | 保留空对象，仅列同原来源的Herdr CodingTask。 |
| herdr_get_task | 保留task_id；回本地/task/runtime/native/交付事实，不能跨来源读全日志。 |
| herdr_continue_task | 保留task_id、requirement；同task CAS新attempt，冻结原repo/worktree/executor/profile/native session；无可信session或未清理不能继续。 |
| herdr_cancel_task | 保留task_id；取消当前attempt并确认本地收尾，远端状态不明回unknown，不冒称业务未执行。 |
| spawn | 新增coding profile及仅此profile必填repository；task为需求，显式后台，retry_count必须0；需spawn.enabled∩coding grant∩repo grant∩Codex profile grant。 |
| spawn_manage | 保留list/get/cancel，新增continue+task只用于受控coding job；普通spawn对此action拒绝。coding job_id为稳定CodingTask ID，返回当前runtimeTaskId/attemptId和executor，按登记映射路由，不能任意访问所有TaskRuntime kind。 |

所有schema拒绝额外身份/env/cwd/stdin/argv/auth/path字段；requirement/task≤20000字符，label≤100。P03四个管理工具只管理Herdr任务，spawn_manage管理S07 coding及原普通spawn；共享CodingTask ID格式及模块，不混称执行协议。管理动作不自动安装CLI或重建丢失原生会话。

P03工具定义start/continue risk=external、list/get risk=read_only、cancel risk=mutating，source=builtin:coding、external_allowed=false，候选/激活按ADR-0011；缺CLI/补丁/原生探针/配置/许可显示具体不可用原因。start/continue异步返回accepted与持久task/attempt ID，整个后台attempt仍受600秒；管理入口不另建模型回合。活任务由其受管监管器轮询真实agent状态，get返回观测时间和状态，不能把陈旧缓存标为当前原生成功。S07 Skill声明bin依赖、指明受控profile与结果/session回读；激活Skill不授予repo、CLI或发布权限。运行不得为coding分支先创建无用途的聊天provider。

## 6. CodingTask 模块、attempt 与资源

新增一个deep Module，外部Interface为start、inspect/list、continue、cancel与独立可信publish。Herdr adapter和Codex exec adapter是两个实际变化的内部Seam；权限、目录、会话、结果、Git验证和outbox由Module集中持有，callers不拼argv/解析日志。TaskRuntime继续是唯一通用任务宿主。

SQLite持久CodingTask、attempt、native_binding、publication和completion_outbox。CodingTask ID跨continue稳定；每attempt有独立ID、TaskRuntime ID、授权revision、base/开始HEAD、command dispatch、结果/日志引用、session、cleanup及交付状态。规范化role+来源scope+run+call+操作为唯一请求键；UI操作用宿主认可requestId，同键参数不同报conflict。

准入事务检查当前grant、每日attempt额度及每task10次上限并占用；同attempt仅一个launcher，提交前持久原TaskOrigin、kind、attemptId和launch claim。明确使用 #158 集成后的 `submission_key=attemptId`、`retain_until_ack=True`、`completion=False`；TaskRuntime按原scope+kind+key持久查回，不能以executor是否入场或taskId是否已回写推断“没有提交”。当前许可仍有效时，丢失submit返回值只允许同key幂等交接；保存返回taskId后query，不再submit以推进工作。

taskId缺失的恢复先用原origin的tasks.list(kind)核对description中宿主写入的attemptId，再query匹配任务；它不要求executor先运行。重启不自动提交旧native执行；没有可信查回、只剩已prune墓碑或来源已撤权时保留unavailable/interrupted/unknown，不换key重建executor。原prompt dispatch已持久且ACK丢失时只观察原session/结果，绝不因task查回、取消或重启重放prompt。该依赖只复用 #158 同一Runtime交接，不新增按key查询的第三套通用协议。

TaskRuntime.submit(kind=coding_herdr或coding_codex，budget=600s/1MiB/8000chars，submission_key=attemptId，retain_until_ack=True，completion=False)实际执行监管器；模块额外2/1/repo1准入不能绕过其8/3/保留配额，未ACK结果仍占原有保留配额。exec、Herdr客户端、server、辅助CLI和固定验证共用最初deadline，不因继续轮询重置。每attempt仅一次prompt dispatch，业务模型错误无自动retry；continue是新明确动作与新预算，不是隐式重试。

目录按role/task归属：worktree、native home/session、固定配置位于长期task根；每attempt独立handoff包含UTF-8 prompt、结果schema、result和事件日志。handoff只给本attempt可写，不给账本/其他任务权限；宿主元数据不在代理写目录。限制任务文件2GiB及10个保留task，满时拒绝；不把Git元数据或session文件暴露为普通模型附件。

worktree分支为codex/meido-<随机task前缀>，必须新建，冻结base SHA，拒绝与既有分支碰撞；Herdr使用真实worktree create/open后核对root/common-dir/branch/HEAD。S07由宿主Git创建后交Codex，不让Codex再管理第二层worktree。不自动force删除或重置成果，显式清理须验证宿主所有权、无活进程与路径。

## 7. 两种原生协议及权限

Herdr每attempt启动自己的`--session <owned> server`，先status核对版本/owned能力/服务代次，再worktree create/open、agent start固定kind与权限模板、绑定pane/terminal/workspace。首次prompt从handoff读取完整需求；发前持久dispatch，客户端超时仅观察同一agent，不再次发prompt。读取terminal仅作有界诊断，idle/done不代表验证成功。

Herdr Codex首轮采用显式workspace-write、approval never、固定model；继续采用`codex resume <exact_native_id>`并重复权限/model模板。Herdr Claude采用restricted、dontAsk、明确Read/Glob/Grep/Edit/Write表面及无额外MCP，继续`--resume <exact_native_id>`重复相同模板。固定集成hook是session上报例外，不能同时启用会关闭全部hook的bare/safe模式后声称Herdr仍能取得同样绑定。

Codex exec首轮通过stdin传完整UTF-8需求，显式workspace-write/approval never、固定model、--json、--output-schema和--output-last-message；result是本attempt的新文件。以thread.started.thread_id绑定native session，匹配终态事件、退出码和结果，禁止stderr正则当权威来源。继续用`exec resume <exact_id>`；本机该子命令不列--sandbox，权限用该版本支持的config形式明确恢复并在原生探针核实，不能丢flag或改bypass让它跑通。

Codex exec的ignore-user-config不隔离认证，认证仍读CODEX_HOME；固定权限配置不依赖忽略后已失效的profile。不得使用--last/--all选最新、--ephemeral丢会话、--worktree再造树或隐式fallback model。JSONL每帧64KiB、总日志受1MiB约束，UTF-8分块正确，错误帧/未知必需字段失败而不伪造成功。[官方非交互协议](https://learn.chatgpt.com/docs/non-interactive-mode)。

profile的认证只能来自主人专门配置的CLI认证目录或单独vault凭据；不检查/复制桌面当前auth.json、OAuth/keychain。CODEX_HOME/CLAUDE_CONFIG_DIR与HOME/APPDATA/XDG等由owned启动模板设置，Git全局配置/credential helper/交互askpass/SSH agent不继承。认证本身可被所选CLI读取，不能承诺“secret永不进入该CLI进程”。

项目的CLI扩展配置属于可执行输入，repo grant登记相关manifest哈希；变更后拒绝准入直至主人重新审阅。固定Herdr hook以外的hooks/MCP/plugins/技能自动执行/更新均禁；如果固定CLI/平台无法证明所配置禁用行为，则该profile是blocked。命令行help只证明参数存在，不把它当认证/keychain、配置加载或原生sandbox已隔离的证据。[Claude工具权限说明](https://code.claude.com/docs/en/permissions)。

首版Claude不提供Bash/PowerShell/WebFetch/动态MCP；宿主执行验证和Git。Codex工作区写入权限与网络关闭策略也须独立原生验证，不能从never推导“什么都不限制”。仓库构建/验证程序按主人批准的本机权限运行，仍可执行repo代码；这是可信repo模式的明确风险，非通用强隔离承诺。

## 8. 600秒、取消与精确continue

正常完成、超时、撤权、取消、应用关闭都先保存当前结果/状态，再关闭自有agent/server并等待Job及已知句柄退出；600秒deadline包含执行阶段，收尾不允许继续模型工作。先请求ctrl+c/正常停止，最多5秒后升级整个owned Job，最多15秒确认；不得只杀短命herdr客户端或认为pane关闭已够。

attempt分别保存runtimeStatus、nativeTurnStatus、cleanupState、deliveryStatus。只有Job/已知资源清空才cleanup=complete；无法确认记cleanup_failed+native unknown并冻结该profile/任务，不返回虚假的“全部已停止”，不释放给新attempt。普通代理可能已完成外部动作，cancelled不表示回滚文件、模型消耗或发布。

TaskRuntime终态后不保留活Herdr server/agent；CodingTask仅保留worktree/session文件和引用。continue新建监管器/server代次、打开同worktree，核对旧native ID/agent kind/同认证及权限revision，再显式resume；这是同原生对话身份的新OS进程，不宣称原进程一直存活。缺session、历史被清、ID不一致或权限无法复原时blocked，不新建会话冒充继续。

宿主重启不自动恢复Herdr agent；owned Job关闭并确认旧资源后记录interrupted。prompt已dispatch但ACK丢失为unknown，先读取现有会话/结果核实；无证据不能重发旧要求。主人可明确给新要求在已核实的精确session继续，仍保留此前unknown事实；无法核实则仅允许另建新任务，不能静默fork。

撤权/角色删除/账号解绑立即阻止准入和交付并取消当前资源；原来源失效不改投桌面或另一渠道。删除前必须完成资源处理，清理失败保留诊断与隔离准入状态，不能先删所有权记录再留下活进程。

## 9. 交付验证、commit 与发布

每attempt结果≤256KiB，严格JSON至少含task_id、run_id=attempt ID、status、summary、validation命令ID列表、blockers；不读旧文件/其他attempt路径。模型自报completed、验证文字、exit0或pr_url都不是完成事实；原生session/任务身份、结果与真实Git必须一致。

宿主选择repo grant批准的固定验证命令，拒绝模型提供的任意argv或改cwd；在同worktree运行并限剩余deadline，记录退出码/摘要。前后核对repo/root/branch、diff、HEAD及未提交文件；验证后再改文件使证据失效。获准localCommit时由宿主固定argv创建本地commit，关闭hooks/signing/credential交互，核对commit确有本次diff和最终干净状态；未授权仅交validated_diff，不冒充commit/PR交付。

宿主状态区分needs_input、validated_diff、validated_commit、awaiting_publication、delivered_draft、failed、unknown；partial/blocked也回具体原因。不要求模型提前创建PR以满足结果schema，否则会反向强迫未授权外部事务。

发布入口是可信UI/主人明确批准的应用事务，不新增普通工具：对当前task/attempt/validated HEAD、固定repo remote/base、拟创建分支和Draft PR正文生成一次性approval记录。主人审阅具体diff、验证与正文后批准，发布broker才获得所需凭据；原生agent已退出且从未取得broker token/SSH agent。CLI auth与发布凭据完全分开。

broker在独立受管操作中占TaskRuntime配额与≤600秒期限，先重验角色/来源/授权和实际HEAD/干净树；固定argv只推确切SHA到自有分支，不force、不改其他分支，然后建Draft PR。核对平台返回的仓库、URL、OPEN+Draft、headRefName/headRefOid与批准值、baseRefName及remote归属；不接受仅HTTPS字符串或“base不是自己”。

push/建PR发前持久publication dispatch。超时/ACK丢失先只读核对同repo/分支/SHA/PR，无法确认保留publication unknown，不自动重复创建PR、换分支或追加commit；需要新的明确操作。发布后验证HEAD已变化则显示交付失效，不用旧证据冒称当前成果。无发布许可的本地模式不能算完整经授权PR验收已完成。

## 10. 原上下文回灌、日志与 Skill

每attempt一条业务持久completion outbox，固定原role/session/source与确定消息ID；Coding任务使用completion=False，保留TaskCompletionWorker现有spawn协议。Coding业务恢复器只扫描本业务attempt/outbox并查询同一TaskRuntime，不改通用worker的kind路由、spawn授权或模型依赖，也不建立第三个通用任务宿主。完成阶段协调原角色锁，使用结构化结果模板，不为整理结果再调用付费聊天模型。

所有runtime终态（succeeded/failed/cancelled/timed_out/interrupted）及业务unknown/来源撤销/交付抑制，都有同一持久结果交接和清理责任；不能仅在成功分支处理。先将有界runtime结果、native/cleanup状态及必须保留的成果引用复制到本业务记录，与 `runtime_ack_dirty=True` 同事务提交，再调用 `tasks.acknowledge_result(taskId, originalOrigin)`；确认成功后才清dirty。业务结果复制之前不能ACK，ACK也不代表外部发送完成或所有进程已停。

恢复器在业务已终态、消息已sent/failed/uncertain或不允许交付时仍扫描ACKdirty；临时SQLite/ACK失败保留dirty重试，不把失败当执行失败，不重跑CLI或重发消息。ACK成功但清dirty前崩溃时幂等再ACK，ACK后prune也不得生成第二executor；若结果已不可用且没有业务副本则保留unavailable诊断。cleanup_failed另有持久资源责任和准入冻结，不能用ACK把它抹除；角色删除沿§8先处理资源再清理本角色数据。

复用SessionStore幂等内部完成消息与ChannelHost动态来源检查/真实receipt；业务outbox的pending→sending在外部send前持久，发送unknown不重发。真实receipt、message_synced和runtime_ack_dirty是分别持久并可恢复的事实：先复制业务结果即可ACK runtime，投递可稍后进行；receipt已落库而本地消息同步失败只恢复本地步骤，不降级已有sent事实。失效来源标交付抑制并完成结果交接/ACK责任，不能改投、另建主人会话或将内部完成当主人新输入维护记忆。

普通工具结果只给有界摘要、验证结果、task/attempt/native session与产物引用；无认证文件路径/内容、raw env、完整终端或整段请求。终端与日志按可能含秘密处理，去ANSI、限制字节、对明确注入的秘密脱敏，禁止记录原始认证payload；不得承诺正则能清除未知所有秘密。原始调试记录仅主人本地按归属查看，默认不用外部模型总结。

S07 Skill指引：先查CLI/profile可用性，用户已提供repo时直接传repo grant对应根和完整需求，不预先猜文件；调用spawn coding后台→回读task/attempt/result/native ID→需要追加时spawn_manage continue精确job→原来源接收。禁止“shell里启动CLI后脱离”、随意cwd/env、--last和自动重试。Skill依赖诊断/显式激活按既有SkillRegistry实现，安装文件不等于工作流通过。

## 11. 验收、垂直 Tickets 与仍需补证

#158 的实施依赖已在总集成 `4b1b3b8` 满足并通过两套CI；第3节随后获主人明确批准。#174 实施计划见 [批准后计划](https://github.com/sawaki-zexi/meido/issues/174#issuecomment-6207491034)。架构批准不能代替生产实现、原生验收或账号许可。

| 层次 | 必须观察到的通过条件 |
| --- | --- |
| 公开HTTP+真实CLI fixture | P03五工具与S07 Skill/spawn/manage完整往返，真实临时Git worktree与SQLite；UTF-8分块、坏/超大JSON、旧结果、错session/role/source、同call/continue竞争及父回合结束回灌。 |
| owned原生监管 | 固定补丁构建和ConPTY资源；合成env canary（不读真实注册表秘密）、无用户profile、原WMI路径拒绝、晚生/无pipe后代、重复cancel、server崩溃/退出、撤权，确认Job/句柄清空而非只看socket。 |
| 固定CLI权限与session | 独立临时HOME/认证fixture，验证项目hooks/MCP/plugins不意外加载、权限/网络canary拒绝、native ID与结构化结果可回读、首次与resume权限相同；Herdr允许的固定hook仍能绑定精确native session。 |
| 故障恢复与交付 | submit已创建但返回taskId丢失、executor尚未入场仍查回唯一任务；所有终态结果复制前/后、ACK失败及ACK成功清dirty前崩溃、业务已终态但ACKdirty均恢复；ACK后prune同key不重跑。prompt已收但ACK丢失/重启无重放，cleanup失败禁新任务；HEAD/branch/base/repo替换、假验证、验证后修改、未授权发布、旧PR head；真实receipt丢ACK不二发，本地故障只恢复本地。 |
| 完整原生任务 | 获准临时可信repo中真实Codex exec、Herdr Codex、Herdr Claude分别完成修改→固定验证→本地commit→精确session continue→取消/资源清理；经另批测试repo发布许可完成一次真实Draft PR并核对head/base/repo。 |

所有fixture用独立数据/角色/认证目录、假凭据及localhost，不登录或读取真实vault/auth配置；缺依赖显式未验收，不能把skip/fake CLI/help计入真实任务成功。已有本地OSS provider可能提供不访问付费API的Codex候选，但不保证本机模型存在、质量、Claude免费资格或零资源成本；无可用免费路径须另获明确账户/费用许可。

已建立三张垂直票，Parent #137，§3 已批准，按真实前置依赖依次实施：

1. [#174：主人通过Herdr在可信repo完成本地任务并安全查询/取消](https://github.com/sawaki-zexi/meido/issues/174)，包含owned补丁与Codex/Claude真实入口、任务/来源、独立worktree和验证；第3节已批准，可开始实施。#158 的Runtime幂等、保留/ACK seam及故障回归已在两轴复核与 `4b1b3b8` 两套CI中满足，不再构成当前阻塞。
2. [#175：主人通过codex-delegate后台完成并精确继续同一原生任务](https://github.com/sawaki-zexi/meido/issues/175)，包含coding profile、Skill/管理路由、session和持久完成，依赖 #174 共享模块。
3. [#176：主人审阅确切成果后发布可信Draft PR](https://github.com/sawaki-zexi/meido/issues/176)，包含本地commit、一次性发布批准与未知恢复，依赖 #174/#175。

沿用主人已批准的总PR聚合分支例外，不重复等待main、不建立堆叠功能PR；依赖尚未复核集成时不实施消费该seam的生产改动。

最终P03/S07验收须三票全行为、相关测试/类型检查/构建、独立规范与规格两轴审查，以及上表真实原生/获准发布证据；仅本地模式或一款CLI通过不计全部完成。§3批准覆盖所选owned补丁实现、必要依赖的工作区隔离构建、发行资源验证和无账号/费用fixture，可随实施推进，不为每个可逆本地步骤另设审批。架构批准不等于原生验收完成；读取/设置真实CLI认证、实际账号/模型或付费请求、真实外部发布仍需相应明确授权。提案阶段仅写规格；批准后已开始隔离原生构建及无费用 fixture，尚无完整验收结论。

当前本地证据：#174 已完成固定 owned 补丁/发行资源构建和 Windows 进程夹具，见[Herdr验收](../handbook/coding-agent-acceptance.md)；#175 已接入独立 Codex exec、显式 Skill/spawn/manage、原来源 copy/ACK 和无费用协议闭环，见[codex-delegate验收](../handbook/codex-delegate-acceptance.md)。这些不等于固定真实CLI的权限、认证/扩展加载、精确resume和账号模型验收；实际账号/模型及经授权发布仍待相应许可和证据。失败保持不可用，不用“minenv”“专用目录”或纯Skill文字补成OS沙箱结论。
