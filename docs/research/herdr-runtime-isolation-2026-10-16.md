# Herdr 0.9.4 运行隔离与 D05 接入边界

状态：草案 / 固定源码核验与待审建议；P03、S07 未实施，本文不是 D05 批准。最后核验：2026-10-16。

事实来源：Herdr `v0.9.4` / `7e26bad94044eba205033111a0ce8033ede7e24f`，Shiori `4f266aeaf42fcd611362e2456417b91f659341cf`，Meido 当前源码及 [D05](../specs/shiori-tool-parity-spec.md#d05-coding-委托与外部服务许可)、[前段 CLI 调研](coding-agent-cli-portability-2026-10-16.md)。Herdr 只读克隆位于仓库外 `D:/meido-artifacts/herdr-audit-20261016`；另建仓库外固定 Shiori 克隆核对原协议，没有改动既有克隆。下文“已提取”表示源码/API 事实，“推断”表示接入含义，“建议”均待审。

本轮仅检索、读取固定源码及官方 release、Win32 文档；没有下载二进制、安装、编译、启动 Herdr server、连接模型或账号，没有读取认证文件、注册表环境值或 vault。Windows 可构建、可发行不等于本机已验证运行；没有原生退出、认证隔离或真实任务成功证据。

## 1. 已提取：发行、许可与 Windows 依赖

- 官方 [v0.9.4 release](https://github.com/herdrdev/herdr/releases/tag/v0.9.4) 发布于 2026-10-13，指向上述提交；资产含 Windows x64 ZIP、Linux x64/ARM64、macOS x64/ARM64。固定 [Cargo.toml](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/Cargo.toml#L1-L12) 声明 Apache-2.0，完整许可见 [LICENSE](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/LICENSE)。若后续再发行修改版，需要单独完成第三方许可和修改范围记录，本轮没有打包。
- 固定 [release workflow](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/.github/workflows/release.yml#L100-L187) 包含 `windows-2022` / `x86_64-pc-windows-msvc`，Rust 1.96.1、Zig 0.16.0、`cargo build --release --locked`；[CONTRIBUTING](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/CONTRIBUTING.md#L122-L137) 指明原生 Windows 使用 Visual Studio Build Tools 的 SDK。不能只安装 Rust 就假定源码构建就绪。
- 固定 [Windows 文档](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/docs/next/website/src/content/docs/windows-beta.mdx#L6-L70) 声明原生支持；[ConPTY 加载实现](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/vendor/portable-pty/src/win/psuedocon.rs#L125-L180) 要求系统支持 ConPTY，并验证随包 ConPTY 文件。固定 release 工作流另打包这些运行资源；不能只复制一个自行构建的 exe 就声称复现官方发行环境。Windows ARM64 的 x64 仿真仅有 release 的 best-effort 说明。

## 2. 已提取：命名 server、IPC 与配置不是同一层隔离

`herdr --session <name> server` 直接进入 headless server；没有 `server start` 子命令。无子命令的 `herdr --session <name>` 是自动检测、必要时启动后台 daemon、再 attach TUI。不要为了后台任务先启动 TUI。[main.rs](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/main.rs#L565-L581)、[autodetect.rs](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/server/autodetect.rs#L1-L30)。

命名会话数据位于 Herdr config root 的 `sessions/<name>/`，JSON API 为 `herdr.sock`，客户端为 `herdr-client.sock`。显式 `--session` 优先于继承的 socket override；只有 `HERDR_CONFIG_PATH` 不会改变会话数据根。`XDG_CONFIG_HOME`、`XDG_STATE_HOME` 可改变对应根，Windows 默认还有 APPDATA/LOCALAPPDATA 回退。故接入需同时固定专用根、名称与目标 socket，拒绝隐式 default、自动探测共享 session 和远程目标。[session.rs](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/session.rs#L31-L186)、[socket_paths.rs](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/server/socket_paths.rs#L15-L69)、[config/io.rs](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/config/io.rs#L30-L95)。

POSIX 使用 Unix socket，Windows 将路径转为 namespaced local pipe 并写 marker 文件；其权限按操作系统账号/提权边界控制，**不是 Meido 的角色、来源或 repo grant**。有本地 API 访问权的进程可以操作该 server 内的终端。[ipc.rs](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/ipc.rs#L37-L89)。同一账号下多个命名会话不是 OS 安全边界。

`herdr --session <name> server stop` / `session stop <name>` 发 `server.stop`，最多等 15 秒并检查 API/client socket 不再可达；这个成功条件不是所有后代进程已退出的证明。[session.rs](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/session.rs#L241-L305)、[等待实现](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/session.rs#L424-L438)。

## 3. 已提取：agent 从既有 pane shell 启动，不接受每次调用的独立 env/cwd

`agent start <name> --kind codex|claude --pane <id> -- <args...>` 经 IPC 提交名称、种类、pane、参数及启动等待时间。服务端选定终端，将 `codex`/`claude` 加参数转成 shell 命令、写入现有 pane；它不是在本地 CLI 子进程内直接创建 agent，也没有每次 agent start 的 env/cwd 字段。[cli/agent.rs](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/cli/agent.rs#L289-L371)、[app/agents.rs](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/app/agents.rs#L144-L229)。只给短命 `herdr agent start` 客户端设置 CODEX_HOME，不能改变已经运行的服务端 shell 环境。

pane 基础环境复制服务端环境，只清除少数外层 agent 身份字段，再注入 HERDR 的 socket/pane 标识；没有建立通用环境 allowlist。默认 shell 模式还可能读取 shell 启动配置，Windows PowerShell 路径并非普遍附带 `-NoProfile`。exe 选择使用服务端 shell 的命令解析；冻结一个“Codex 路径”设置而不控制服务端 PATH/启动器也不充分。[pane.rs 环境](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/pane.rs#L184-L220)、[shell 构造](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/pane.rs#L2123-L2157)、[CommandBuilder](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/vendor/portable-pty/src/cmdbuilder.rs#L147-L262)。

Herdr 自己的集成安装路径识别 `CODEX_HOME` 和 `CLAUDE_CONFIG_DIR`，无值才回退用户主目录；Claude 集成会修改 settings.json/hooks，Codex 集成会修改 hooks.json/config.toml。这只证明 **Herdr 的路径选择**，不是相应 CLI 的所有认证、keychain、项目配置或扩展都被隔离的证明。[integration/env.rs](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/integration/env.rs#L14-L16)、[路径解析](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/integration/env.rs#L58-L107)、[targets.rs](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/integration/targets.rs#L124-L246)。

**Windows 明确缺口：** vendored portable-pty 先复制 process env，随后枚举 HKLM 系统及 HKCU 用户 `Environment`，将变量补回或覆盖；trace 语句含变量名和完整值。Herdr pane 没有调用该 builder 的 `env_clear`。因此官方不变版不能宣称“给 server 最小 env 后，agent 就只继承这份 env”；设置的隔离目录也可能被注册表同名变量覆盖。trace 泄漏是启用相应日志级别后的风险，不是声称默认一定记录了秘密。[cmdbuilder.rs](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/vendor/portable-pty/src/cmdbuilder.rs#L147-L229)、[日志过滤](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/logging.rs#L9-L30)。本轮没有枚举本机注册表来验证此路径。

Herdr 不提供“禁全部 CLI hooks/plugins/MCP”的执行边界。完全禁 hooks 又会禁掉其原生 session 关联来源：固定 Codex/Claude 集成从 SessionStart payload 上报 session_id，服务端才持有 `agent_session`。建议若选 Herdr，就明确区分“仅允许固定哈希的 Herdr 状态/session 上报 hook”与“禁止其他 hooks/plugins/MCP”；若维护者要求连该 hook 也禁用，可靠 native session 关联如何获得仍是待解决接口，不能用终端名称替代。[Codex hook](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/integration/assets/codex/herdr-agent-state.ps1#L9-L52)、[Claude hook](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/integration/assets/claude/herdr-agent-state.ps1#L9-L52)。CLI 认证 profile、项目扩展与 OS 凭据访问仍须所选版本的独立原生验收；没有新引入 Codex/Claude 参数事实。

## 4. 已提取：取消不是 ctrl+c 回执，官方 Windows 强回收还不能闭合

官方 agent 命令有 `send-keys`，没有精确的 `agent kill-tree`。它向当前 agent 的 pane 输入按键，返回排队成功。Shiori 原取消先核对 pane/kind，再发 ctrl+c、等待 idle/done；这只能作为回合状态观察，不能证明底层树终止。[Herdr send-keys](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/app/api/agents.rs#L383-L427)、[固定 Shiori 取消](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/herdr/backend/task_management.py#L215-L251)。

`pane close` 会关闭目标 pane、移除无人附着的 terminal runtime 并执行 shutdown。底层 shutdown 只取一次 shell/session PID 列表，依次 Hangup、Terminate、Kill 各等 250ms；仍存活仅 warn，函数返回 `()`。关闭 API 不带 `expected_terminal_id` 原子比较；先 get 后 close 也有目标替换窗口，Meido 不能只信可复用 pane 名。[pane close](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/app/api/panes.rs#L1963-L2022)、[runtime shutdown](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/app/runtime.rs#L30-L42)、[pane 回收](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/pane.rs#L1702-L1816)。

Windows 源码额外有可核对缺口：`signal_processes` 用 `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` 取得句柄，随后调用 `TerminateProcess` 且不检查返回值。`ProcessHandle::open` 没有补充权限；Win32 官方要求 PROCESS_TERMINATE，并说明终止调用是异步的。因此这一路径不足以证明强回收成功，固定 PID 快照也不能覆盖之后新生/逃逸后代。[windows.rs](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/platform/windows.rs#L2620-L2667)、[句柄构造](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/platform/windows.rs#L2909-L2918)、[Microsoft TerminateProcess](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-terminateprocess)。这是源码与 API 合同核验，尚未运行 Herdr 原生失败探针。

另一个独立问题是 daemon 所有权：Windows 检测启动者处于 kill-on-close Job 时，主动通过 WMI 创建 detached server；通常的“杀本地 CLI Job 就杀 server”不成立。PTY CreateProcess 没有给每个 pane 分配自己的 kill-on-close Job；源码中的 `StatusCommandGuard` 只用于状态命令，不能当 pane 的所有权证据。PTY 自身未设置 breakaway 标志，但 daemon 的 WMI 路径已经使其可能不属于原 Job。不能据“ServerStop socket 消失”或进程树快照宣称所有 agent 后代释放。[daemon WMI](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/platform/windows.rs#L1364-L1421)、[PTY 创建](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/vendor/portable-pty/src/win/psuedocon.rs#L336-L386)、[状态命令 Job](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/platform/windows.rs#L1159-L1227)。

## 5. 已提取：attach 与 native resume 的身份不同

attach 重连仍运行的 Herdr server，只恢复控制视图；server 重启恢复 native agent 则创建新的终端/进程，并根据已保存 session ref 执行 `codex resume <id>` 或 `claude --resume <id>`。默认 `resume_agents_on_restore=true`，依赖集成提供 session ref。原启动的其余 permission/model/config flags 并未由这个标准恢复计划原样带回，不能假定权限冻结仍成立；TUI 名称相同也不证明接续了同一 native session。[默认配置](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/config/model.rs#L266-L281)、[恢复 argv](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/agent_resume.rs#L197-L215)、[新终端恢复](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/persist/restore.rs#L584-L629)。

可用于关联的 API 字段包括 terminal_id、workspace_id、pane_id、agent kind、agent_session 的 source/kind/value。建议宿主同时绑定专用 endpoint/server 代次、CodingTask/attempt、repo/worktree、上述身份和授权配置版本；重启只能在重新校验原来源后显式接续精确 native ID。没有可信 ID、ID 不一致或原权限配置无法复原时保留 interrupted/unknown，不改用新会话或 `--last`。Herdr 字段本身不是不可伪造的 Meido 身份授权。[AgentInfo](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/api/schema/agents.rs#L187-L237)、[Codex 初始 resume 识别限制](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/agent_resume.rs#L134-L152)。

## 6. 已提取：终端内容有局部界限，不是安全结果协议

`agent read` 返回终端快照，支持 text/ANSI、visible/recent/recent-unwrapped；recent 默认 80 行，显式 lines 上限 1000，结果含 truncated。pane scrollback 默认 10,000,000 bytes，可配。行数不等于任务总输出字节预算，ANSI 清除不等于秘密脱敏；没有看到此读取路径对凭据做脱敏。[read_terminal_snapshot](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/app/api_helpers.rs#L109-L158)、[AgentRead API](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/app/api/agents.rs#L273-L313)、[scrollback 配置](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/config/model.rs#L1054-L1056)。

建议宿主对每次 CLI 输出、累积日志、结果文件和模型摘要分别限字节并脱敏；只接受当前 attempt 的结构化交付文件及真实 Git/验证事实，不从 idle/done 或 terminal raw text 推断成功。Herdr 自身日志轮转和 scrollback 配额不替代 TaskRuntime 配额。原始终端/持久历史都按可能含秘密处理，普通工具结果不直接回传整份内容。[Herdr 日志](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/logging.rs#L9-L30)、[Meido TaskBudget](../../backend/app/task_runtime/runtime.py)。

## 7. 已提取：worktree 可明确命中仓库/分支，但不提供 repo 授权

`worktree create --cwd <repo> --branch <branch> --base <ref> --path <abs>` 支持明确源仓库、目标与 base；`worktree open --cwd <repo> --path <existing-worktree>` 可打开宿主已创建的 checkout。源码先检查本地分支是否存在：存在时 `git -C <repo> worktree add <path> <branch>`，否则使用 `-b <branch> ... <base>`。所以已有分支时 base 参数不重新固定其 head；任务提交前后仍须检查实际 branch/head/repo common-dir。[CLI](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/cli/worktree.rs#L154-L269)、[argv](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/worktree.rs#L239-L322)。

`worktree remove --workspace <id>` 管理 linked checkout，默认不删 branch，force 另有清理未登记残留目录的分支。建议默认保留成果 worktree；成功、取消和 app 退出都不自动 force 删除。清理必须核对宿主所有权、实际规范路径/linked 元数据及无活进程，不能使用模型结果给出的任意目录。Git 命令并不自动隔离 credential helper、repo hooks、共享 Git config 或网络发布授权；`--trust-repository` 仅为本次命令追加 safe.directory，不能作为授权捷径。[remove/force](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/worktree.rs#L154-L190)、[残留恢复](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/worktree.rs#L346-L371)、[保留 branch 测试](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/worktree.rs#L822-L860)。

## 8. 待审建议：保留 Herdr 协议的两个具体平台方案

官方不变版不能直接作为“最小环境 + owned Job 强回收”的 Windows 执行器。以下二选一尚未批准；均保留 P03 的真实 Herdr agent/worktree/查询/继续/取消协议，不以直连 Codex 冒充 P03。

| 候选 | 必须具体承接的工作 | 实证门槛 |
| --- | --- | --- |
| A：固定源码的受控 Herdr 构建 | 在 portable-pty 禁用注册表环境补回与值 trace，保留宿主 allowlist env；服务端使用固定无用户 profile 的 shell/受控 CLI 启动路径；禁止 owned 模式进入 WMI/daemon 脱离 Job；修正 PROCESS_TERMINATE、检查返回/等待真实退出并向上报告失败；由宿主从 server 启动前取得不允许 breakaway 的 Job/等价强所有权，确认所有后代为零。只修 access flag 不足以替代树所有权。 | 新版本、构建哈希、许可证和具体 patch 范围固定；Windows 真实 fixture 覆盖晚生后代、无 pipe 后代、连续取消、server 崩溃/退出、撤权与 env canary，且不读取用户真实环境值。 |
| B：主人明确配置的专用隔离 OS 环境 | 在隔离用户/VM/容器等实际可用环境中运行官方固定 Herdr，明确不挂载主人认证/用户注册表/任意仓库；监管器持有整个环境的停止与退出证明；映射准许 repo、专用 CLI 认证及产物导出。 | 先选择具体平台与文件/进程生命周期；不能只写“隔离账号”就假定能回收全部后代。Windows/WSL/VM 的网络、挂载与权限需实际验收，当前没有选定实现。 |

两案都需要为 Codex/Claude 固定独立认证 profile、准许的 repo 与 CLI 权限，不复制桌面登录，不透传角色模型 Key；禁止强制 bypass。若只批准任意代理可读写的同账号环境，就必须准确标注残余宿主权限，不能声称 OS 沙箱。固定 Herdr 内的自动更新、agent manifest 更新、插件启动及自动 resume 应关闭或被明确固定清单取代；不能让运行中自动下载改变审计版本。[更新触发](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/app/runtime.rs#L97-L143)、[启动 hook](https://github.com/herdrdev/herdr/blob/7e26bad94044eba205033111a0ce8033ede7e24f/src/server/headless/bootstrap.rs#L95-L106)。

## 9. 待审建议：每 CodingTask 专用 server，attempt 仍受 600 秒

建议选 **每 CodingTask 一份专用命名 server/数据根**，不要按角色/profile 共用一个有多个任务的 server。这样可以停止整个自有 server 作为取消升级路径，不触碰主人共享 session；复用角色/profile server 虽省进程，但 pane 替换、单任务强杀与共享故障域都更难闭合。

具体归属建议：CodingTask 持久保存授权、repo/worktree、executor/profile、精确 native session ref 与每次 attempt；TaskRuntime 只持有本次 attempt 的监管器、server、agent、CLI 子进程和日志。**先保持每 attempt 最大 600 秒**，复用当前并发/日志上限，不引入无人持有的后台 agent 或长预算。正常结束、超时、撤权、取消及应用退出，都先复制/确认当前结果并释放本 attempt 的活进程。TaskRuntime 终态后，CodingTask 只持有持久文件与会话引用，没有仍消耗模型/CPU 的“空闲 persistent agent”。这是一项待批的生命周期选择，不是当前已实现行为。[现有所有权 ADR](../adr/0012-owned-task-runtime.md)、[现有 600 秒上限](../../backend/app/task_runtime/runtime.py)。

continue 为原 CodingTask 新增一次 CAS 锁定 attempt，重新核对 grant/source/配置、独立 worktree 与 native session，启动新 server/进程显式 resume；这保持 native 对话身份，**不声称同一 OS 进程一直存活**。建议 `resume_agents_on_restore=false`，禁止 Herdr 自行恢复；若 native CLI 的准确 session/权限继承尚未实证，就阻止自动接续并给 interrupted/unknown。任何已提交但 ACK 丢失的 prompt 不盲目重发。若产品必须让原 agent 在终态后持续存活，则需另批持久资源监管与 idle/总寿命预算，本建议没有取得该许可。

## 10. 待审建议：S07 通过现有 spawn 的受控 coding profile 接入

可行候选是扩展现有 `spawn` 的 `profile="coding"` 和 `repository`（仅解析主人预配置 repo grant），由宿主 factory **只对该 profile** 转入 CodingTask/CodexExecutor；保留 `research/scripting/general` 的 SpawnExecutor 行为，不新增第 77 个普通工具，也不开放通用 Shell 的 cwd/env/stdin。现有 profile enum 只有三项、execute 直接构造 SpawnExecutor，所以这仍是需要规格与红绿实施的接口变化，不能声称只改 Skill 文本就已支持。[SpawnTool](../../backend/app/agent_runtime/spawn_tool.py)、[ADR0004](../adr/0004-role-scoped-shell-tool-boundary.md)。

建议该入口要求显式后台、retry_count=0、角色 coding grant + repo grant + CLI profile grant；task 文本不决定认证目录、argv、环境、仓库外路径或发布权限。不得仅凭 spawn.enabled 获得 coding 许可，也不为 coding 分支无意义创建聊天模型 provider。返回 task/job 关联、结果文件/session/attempt 与原上下文回灌；继续/取消/查询仍使用宿主映射与原来源核验。spawn_manage 与 P03 五工具如何共享同一 CodingTask ID、如何准确表现 unknown/资源清理失败，需要后续 Spec 明确，不把这次候选硬写为现行契约。

S07 的 CodexExecutor 与 P03 的 HerdrExecutor 共用归属/预算/结果/outbox，分别保留真实 exec 与 interactive Herdr 协议。纯直连 Codex 可作为维护者另选的范围调整，但没有批准其替代完整 P03。修改/固定验证/本地 commit/指定 remote push/Draft PR 分别授权；无发布许可时只交本地成果，不把自然语言“不要 push”当强制隔离。

## 收束：下一份 D05 Spec 最少需要决定的选择

1. Windows 选择 A 固定受控源码构建，还是 B 具体隔离 OS 执行环境；A 的 patch 和 B 的平台均须明确，不默认安装官方不变版。两者未验收前只能报告依赖/隔离未就绪，不能记 P03 完成。
2. 是否接受“每 CodingTask 专用 server、每 attempt 600 秒、终态零活进程、continue 精确 native resume”的生命周期；若要求跨终态活 agent，则另议实际监管与预算。
3. CLI profile 的认证与权限选择、是否仅允许固定 Herdr session/state hook、允许的 repo/验证/本地 commit/发布操作，以及采用 S07 `spawn profile=coding` 入口。当前未解决的精确 CLI resume 权限、认证/keychain/项目扩展隔离和 ID 验真留到固定版本原生探针，不能提前宣称完成。

本轮仅新增本 Research；未改生产、ADR、总规格、工具目录、认证或预算，也未请求真实运行授权。
