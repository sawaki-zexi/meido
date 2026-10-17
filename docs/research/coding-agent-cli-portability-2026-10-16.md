# Coding Agent CLI、Herdr 与 codex-delegate 可移植性

状态：草案 / 已提取协议与待审阅方案；P03、S07 尚未实现或验收。最后核验：2026-10-16，Windows。

事实来源：Shiori-Agent 固定 `4f266aeaf42fcd611362e2456417b91f659341cf`；Meido `de00dadba77565d30ac9b2c9ce0504bc8013ef99`；本机 CLI 的 `--version` / `--help`；下列官方资料。目标见[总规格 P03/S07、D05](../specs/shiori-tool-parity-spec.md)。本文区分源码事实、局部检测和建议；不把可执行文件存在当作登录、可用额度或任务成功。

本轮只读源码和公开资料，运行 Codex/Claude 帮助、版本检测；未读取用户认证配置、令牌或系统 vault，未登录、安装依赖、执行模型任务、修改目标仓库、创建提交或 PR。未运行 Herdr，因为 PATH 检测未找到该程序。本文不是 D05 的实施批准。

## 已提取：五个 Herdr 工具的精确输入

以下为固定源码的 JSON Schema；上游未声明 `additionalProperties`，不能误记为已拒绝额外身份字段。实际身份来自宿主，Meido 适配仍须按现有参数规范拒绝身份覆盖。

| 工具 | 上游参数 schema |
| --- | --- |
| `herdr_start_task` | `{"type":"object","properties":{"repository":{"type":"string","description":"本机 Git 仓库根目录绝对路径"},"requirement":{"type":"string","description":"完整需求、约束及验收条件"},"agent":{"type":"string","enum":["codex","claude"]}},"required":["repository","requirement"]}` |
| `herdr_list_tasks` | `{"type":"object","properties":{},"required":[]}` |
| `herdr_get_task` | `{"type":"object","properties":{"task_id":{"type":"string"}},"required":["task_id"]}` |
| `herdr_continue_task` | `{"type":"object","properties":{"task_id":{"type":"string"},"requirement":{"type":"string","description":"完整追加需求"}},"required":["task_id","requirement"]}` |
| `herdr_cancel_task` | `{"type":"object","properties":{"task_id":{"type":"string"}},"required":["task_id"]}` |

来源：[tool.py L17–37](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/tool.py#L17-L37)、[management_tools.py L33–58](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/management_tools.py#L33-L58)。start 捕获 BackgroundResults 目标；list/get/continue/cancel 仅用当前 role 过滤。它们不是 Meido 所需的 role + 原线程 + 原发言者/绑定代际隔离，后者须独立实现。[tool.py L51–67](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/tool.py#L51-L67)、[management_tools.py L60–87](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/management_tools.py#L60-L87)、[task_management.py L16–21](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/task_management.py#L16-L21)。

## 已提取：外部依赖、身份与 CLI 协议

Herdr 是独立终端/agent 宿主，不是这里引入的 Python Coding SDK。插件的 Python 依赖只有 `shiori-sdk>=3.1.1,<4` 和 Pydantic；SDK 提供宿主进程、配置和回报接口，实际执行通过外部 `herdr` argv。[pyproject.toml L5–12](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/pyproject.toml#L5-L12)。插件版本 0.1.0 与 Herdr binary 版本不是一回事。

插件要求 CLI **及所选运行服务**均兼容且 >=0.9.4；先 `herdr --version`，再 `herdr --session <session> status server --json` 检查 running/version/compatible/endpoint_compatible。全局 `herdr session list --json` 返回命名 session 列表，后续操作固定使用配置中的 session，而非 TUI 当前焦点。[cli.py L44–80](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/cli.py#L44-L80)、[sessions.py L11–29](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/sessions.py#L11-L29)。

官方项目现为 [herdrdev/herdr](https://github.com/herdrdev/herdr)，原 ogulcancelik 路径有迁移线索，不以第三方目录为版本依据。本轮用只读 GitHub API 确认 [v0.9.4](https://github.com/herdrdev/herdr/releases/tag/v0.9.4) 对应 `7e26bad94044eba205033111a0ce8033ede7e24f`；这是可审计候选，不宣称最新版，也未下载/运行其二进制。固定 Shiori 只给最低版本，未锁 Herdr/Codex/Claude 的完整组合。其 README 还声明使用 Unreleased SDK 契约，不能从依赖下限推断发布兼容。[README L60–62](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/README.md#L60-L62)。

| 阶段 | 固定 Shiori 的实际协议与证据 |
| --- | --- |
| 建任务 | 仓库必须是绝对目录且 `git rev-parse --show-toplevel` 等于该路径；取当前 HEAD 为 base。生成 task UUID、run UUID、`codex/herdr-<id前12位>`、唯一 agent_name 和插件私有任务目录。 |
| worktree | `herdr --session S worktree create --cwd REPO --branch BRANCH --base SHA --path PATH --label NAME --no-focus`；核对返回 worktree 路径和分支，保存 root pane/workspace ID。 |
| agent | `agent start NAME --kind codex|claude --pane PANE --timeout 30000 -- <agent args>`；上游不直接运行 `codex exec` 或 Claude headless。 |
| 发需求 | UTF-8 的每回合指令文件在 worktree 外；`agent prompt NAME <read-file指令> --wait --timeout 5000`。超时或 prompt_stalled 后观察同一 agent，不能重发需求。 |
| 观察 | `agent get NAME` 核对 pane_id 和 agent kind；`agent read NAME --source recent-unwrapped|visible --lines 100` 是文本，保留末16K字符。其他调用按 `{result:{...}}` 解码，并识别 `{ok:false,error}`。 |
| 继续 | 每任务 operation_lock；有活动 worker 则拒绝。要求原 pane、agent kind 与当前 idle/done 一致；保留 agent/worktree，新 run_id、新结果文件，追加完整需求。 |

来源：[service.py L85–118](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/service.py#L85-L118)、[L167–257](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/service.py#L167-L257)、[cli.py L150–188](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/cli.py#L150-L188)、[task_management.py L143–175](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/task_management.py#L143-L175)。

Herdr agent_name/pane/session 是上游实际管理身份；源码没有持久捕获底层 Codex thread UUID 或 Claude session UUID。不能把一个 pane 名称说成已验证的 Codex resume ID。官方 [Agent automation](https://herdr.dev/docs/agent-automation/) 也说明 wait 观察 agent 生命周期，不追踪单独 turn；超时不证明输入未送达。官方 [Session state](https://herdr.dev/docs/session-state/) 区分 detach 后原进程继续和 server 重启后新进程/会话恢复；两者不能混称原执行仍运行。

## 已提取：结果、交付、取消和危险默认值

每回合 `<run_id>.json` 结果最多256KiB，字段为 task_id/run_id/status/summary/validation/pr_url/blockers；不沿用旧回合文件。插件核验 task/run 一致、status=completed、无 blockers、非空验证列表、HTTPS PR URL，再检查原分支、干净 worktree、HEAD 不同 base 且有文件差异。验证 argv 的 cwd 必须在 worktree 内；重跑每条命令，结束后再次核对 HEAD 和干净状态。最后 `gh pr view <branch> --json url,isDraft,state,headRefName,headRefOid,baseRefName` 核对同 URL、开放 Draft、同分支、同已验证 SHA。[delivery.py L13–31](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/delivery.py#L13-L31)、[L69–149](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/delivery.py#L69-L149)。

这些检查的边界：要求非空验证列表并不证明命令验证了需求；上游允许模型提供任意 argv，路径约束不等于命令权限。PR base 只检查不等于任务分支，没有强制为 main 或配置的目标分支。Meido 需要主人批准的验证命令/仓库配置、固定 remote/base 和本地 Git 事实，不能照搬“代理写 completed 即可信”。

取消先 cancelling，确认原 agent 身份后发 `agent send-keys NAME ctrl+c`，稍后轮询同 pane/kind 的 idle/done 才标 cancelled；无法确认目标、超时或身份变化为 unknown。CLI 命令进程退出/被杀不等于外部 Herdr agent 停止。关闭 Shiori 仅停止监控并标 monitoring_stopped，不杀共享 server、不删 worktree；任务 dict 只在当前进程，不恢复旧追踪。[task_management.py L179–251](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/task_management.py#L179-L251)、[service.py L64–83](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/service.py#L64-L83)、[models.py L1–24](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/models.py#L1-L24)。

必须改写的上游默认值：

- `config.agent_args()` 无条件加 Codex `--dangerously-bypass-approvals-and-sandbox` 或 Claude `--dangerously-skip-permissions`；不是任务用户单独选择。[config.py L25–37](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/config.py#L25-L37)。
- 所有 CLI/验证命令 `env=dict(os.environ)`；完整宿主密钥会继承。Windows 对 .cmd/.bat 的元字符有拒绝，不能因此宣称环境或执行已沙箱化。[cli.py L83–114](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/cli.py#L83-L114)。
- 默认指令直接要求 Full Access、commit、push、Draft PR；没有按具体任务许可分层。Meido 的工具启用不得隐含这些外部事务授权。[delivery.py L46–63](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/delivery.py#L46-L63)。
- Herdr CLI 等待45秒、任务追踪7200秒，与 Meido 当前 TaskRuntime 最大600秒不是可直接互换的预算。[config.py L20–23](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/config.py#L20-L23)。

## 已提取：S07 与本机、官方 Codex/Claude 协议

S07 原 Skill 仅有 `name/description/metadata.requires.bins=[codex]`。主 agent 先检测 CLI；有用户给出的 repo 时不预先探索/指定猜测文件；显式后台 spawn(scripting/general)，子任务里 shell 同步等待 Codex，prompt/result/log/session 文件均置于任务目录；结果回原上下文。Windows 使用 Get-Content 管 stdin，POSIX 用重定向；stdout+stderr 日志正则提取 `session id:`。自动 resume 必须明确 ID，不能 `--last`。[Skill L1–59](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/codex-delegate/SKILL.md#L1-L59)、[L88–116](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/skills/codex-delegate/SKILL.md#L88-L116)。Skill 文字不是强制许可边界，其默认同步21600秒不可抄入 Meido 600秒上限。

本机只读检测结果：PATH 首选 Codex shim 为 `D:/code/node-v24/codex.ps1`，`codex --version` 为 **0.159.3**；Claude shim 同目录，`claude --version` 为 **2.1.283**。还有其他 Codex/Claude shim/native binary，不能默认它们版本相同；生产配置应冻结明确 executable/版本。没有调用 `login status` 或读取 auth.json；登录可用性和额度未知。

官方 [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) 及本机 exec 帮助共同支持：stdin prompt、`--json` JSONL、`-o/--output-last-message`、`--output-schema` 和 `exec resume <SESSION_ID>`。JSONL 的 `thread.started.thread_id` 比 stderr 展示文字更适合作为 session 关联；终态事件与退出码、当前回合结果文件共同核验。官方说明默认 read-only、编辑需显式 workspace-write，`--full-auto` 已不支持；不采用旧教程旗标。本轮没有运行任务来验证事件实际时序。

官方 [Developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli) 与本机帮助列出 `--oss --local-provider ollama|lmstudio`，以及 `--ignore-user-config`；后者不隔离认证。`--ephemeral` 不适合要求后续持久 resume 的任务；宿主已创建 worktree 时不再让 CLI 管第二套 worktree。非交互 resume 的权限参数位置要按所选版本实测，不能只把首次 exec 命令中的 flags 原样复制。尤其本机 `exec resume --help` 没有列出 `--sandbox`，不能据此改用 bypass。

官方 [Authentication](https://learn.chatgpt.com/docs/auth) 区分 ChatGPT 订阅登录和 API key 计费，CLI 会复用本地缓存认证。专用 CODEX_HOME/CLI 授权身份应由主人配置，Meido 不读取/复制桌面当前 Codex 登录、不把角色聊天模型 API key 转交 CLI，也不承诺有订阅就无限免费。

Claude 官方 [Headless](https://code.claude.com/docs/en/headless) / [CLI reference](https://code.claude.com/docs/en/cli-reference) 与本机帮助支持 `-p`、JSON/stream-json 输出、明确 session resume、`--permission-mode`、`--permission-prompts none` 和 allowed/disallowed tools。终态 JSON 包含 session 元数据；不从 TUI idle 推断业务成功。具体 Claude 凭据隔离和精确权限配置仍待单独核验，不将“有这些参数”记为已完成隔离。

## 建议：交审阅的最小完整架构

建议建立独立 CodingTask 应用服务，两个真实适配器分别保留 **Herdr 管持久 agent** 与 **Codex exec 子进程**协议；共用 TaskRuntime 归属、预算、日志和本地取消设施，不把 P03 五工具改名包装为普通 spawn。S07 的纯 Skill+现有 Shell 不能直接满足仓库/环境/stdin/长预算要求：现有 Shell 固定角色 workspace、禁止模型选择 cwd/env/stdin，SpawnExecutor 又把写操作限制在任务目录。[ADR0004](../adr/0004-role-scoped-shell-tool-boundary.md)、[shell_tool.py](../../backend/app/agent_runtime/shell_tool.py)、[spawn_tool.py L155–257](../../backend/app/agent_runtime/spawn_tool.py#L155-L257)。

计划中的宿主边界：

1. 主人预配置仓库 allowlist（稳定 repo ID、规范绝对根、固定 remote/base、可用角色）、CLI executable/版本/命名 Herdr session、CLI 模型及独立认证 profile。工具参数不能自选 env、进程参数、认证目录或外发目标。模型给出的 repository 路径仍须解析并命中配置，拒绝 symlink/junction 越界及非根目录。
2. 每任务在应用私有根分配不可预测目录，prompt/result/event log 与独立 worktree 分开。冻结原 role/origin、配置版本、base SHA、目标分支和许可；不写主 checkout，不借当前活动对话或桌面 run 身份。读取/结果/继续/取消均重验原来源。
3. 持久 CodingTask 与每次 run/attempt；唯一提交键、每任务 continue CAS/锁，不允许并发回合。继续固定原 executor、worktree 和明确 session ID；不能切到 `--last`、另一个 Herdr pane 或另一个模型连接来掩盖失败。进程重启后先标 interrupted/unknown 并核验已有资源，不自动重复提交可能已接受的 prompt。
4. 复用 [TaskRuntime](../../backend/app/task_runtime/runtime.py) 的全局8/角色3并发、日志/记录配额与取消所有权。独立维护 remote agentStatus 和 deliveryStatus：停止本地 Herdr CLI/monitor 只能证明本地句柄释放，不能把 TaskRuntime.cancelled 当成远端回合已停。先取消确认再继续；无法确认保留 unknown。是否允许任务超出现有600秒上限是单独待批事项。
5. 使用最小环境及主人显式配置的 CLI 认证；原始日志有角色归属、字节预算和清理期，普通模型结果只给已脱敏片段。隔离 HOME/config、MCP、hooks、plugins、Git credential helper 与 CLI 自带扩展加载必须有实际版本探针；一段 Skill 禁令不能代替宿主/OS 约束。默认保留 sandbox/权限检查，缺许可明确 blocked。
6. 本地修改、执行验证、创建本地 commit、fetch/push、创建 Draft PR 分开记录授权。不把让模型“不要 push”当成网络或 Git 凭据强制隔离。建议无发布许可时不给 GitHub/SSH凭据，明确只交本地成果；发布路径由宿主固定 argv 和指定 remote/base 完成。验证命令由仓库批准配置选择，不直接执行代理结果里任意 argv；核对开始/结束 HEAD、干净状态和权威 PR head/base/repo。只有本地模式通过不能计作 P03 经授权 PR 交付已完成。
7. 完成仅回原来源，结果复制后 ACK；渠道保留 pending/sent/failed/uncertain，已有 attempt 不重发。现有 [TaskCompletionWorker](../../backend/app/task_runtime/completion.py) 和 [main.py L4198–4284](../../backend/app/main.py#L4198-L4284) 的来源授权、提示和模型冻结依赖 spawn，不能直接借 spawn.enabled 为 Coding 授权。建议独立薄完成 outbox，复用 SessionStore 幂等内部追加与 ChannelHost lease/receipt；具体共享 helper 以 #158 合入后的事实为准，不预先泛化 worker。

S07 的实现入口仍需审阅：推荐由上述服务提供受控 CodexExecutor，保留后台任务、结果文件/session 回读与原来源回灌。如何让现有后台 spawn 调用这个受控入口，或使用专门 CLI 委托工具代替内层任意 Shell，是未定接口；不能先扩大通用 Shell 的 cwd/env/stdin 再声称只是安装 Skill。纯自然语言 Skill 的格式转换可以准备，但不计作 S07 行为完成。

## 必须批准与待核实

- D05 的具体仓库 grant、CLI 认证/profile、执行权限和本地修改/commit/远端发布许可；Herdr 是否必须使用专用、由谁启动/停止的命名 server，禁止操作主人已有共享 session 的范围。
- 600秒任务上限是否足够；如允许更长，需说明全局/角色并发、磁盘/模型成本预算、关闭时停止或监控脱离策略。不能沿用上游2小时/6小时默认绕过现有宿主上限。
- S07 受控 CLI 入口与后台 spawn 的具体接口，以及 P03 本地成果与 Draft PR 完成标准的不同状态；这些尚未批准，不请求真实执行。
- 对固定 Herdr 0.9.4 源码/二进制进一步核验：启动 argv/环境是否进入远端 pane、agent/session 关联、检测更新与配置隔离、跨 Windows/POSIX 的取消和重启；本机尚无 Herdr 原生证据。
- Codex 0.159.3 新建/resume 的实际权限继承、结构化事件/结果持久顺序、隔离配置是否仍加载项目扩展；Claude 2.1.283 的精确协议和认证隔离。帮助和滚动官方文档不能代替这些运行证据。

## 计划：验收及免费最小任务的可行边界

先在临时 Git 仓库和真实子进程 CLI fixture 做公开 HTTP：start→list/get→后台结束→原上下文收到→同 session continue→cancel。fixture 故意制造 split UTF-8/坏 JSON/超大输出/握手超时、已接收但响应丢失、旧 result、错 task/run/session、同任务并发继续、取消 unknown、宿主重启；SQLite 证明幂等和结果 ACK 不重跑。真实 Git 验证独立 worktree、未授权路径/remote、伪验证、旧 PR head/base、验证后修改；远端 PR 用受控 HTTP/CLI fixture，不能把它当真实账号验收。

再做所选 CLI 原生 smoke：只在另获准的临时仓库完成一处可验证文件修改，运行固定本地测试、读取当前回合结果与 session，resume 一次，再确认进程树释放。Codex 官方 OSS/local-provider 使**已有本地模型服务且资源足够时**存在不访问付费 API 的候选；本轮未检测本地模型服务/模型是否可用，也不承诺其效果。可控本地 provider fixture 证明真实 CLI 协议而非真实模型任务质量；订阅登录也不是零成本/不限额度的证明。若无可用免费本地模型，本阶段需维护者另外选择实际 CLI 认证/成本许可后才运行，不能以 help/version、fake CLI 或 skipped 代替真实任务。

本草案未运行产品测试、未执行 Coding Agent 任务。后续以固定 executable/version、argv（不含秘密）、临时 repo base/head、session/attempt、退出/取消状态、实际验证和回灌证据补全；实现完成前仍按仓库规范与规格分别复核。
