# Owned Herdr 编程任务

状态：当前 #174 本地实现，生产 CLI 认证与真实模型未验收；#175/#176 尚未交付。最后核验：2026-10-16。事实来源：[应用](../../backend/app/coding_tasks/application.py)、[配置](../../backend/app/coding_tasks/config.py)、[执行器](../../backend/app/coding_tasks/executor.py)、[API](../../backend/app/coding_tasks/api.py)、[原生验收](../../tests/python/test_coding_api_native.py)。完整目标见[具体规格](../specs/coding-agent-spec.md)，本页描述当前代码。

## 许可与配置

全局 `enabled` 和角色 `agentConfig.coding.enabled` 默认关闭。角色字段为 `repoGrantIds`、`profileIds`、`defaultHerdrProfileId`、`defaultCodexProfileId`、`maxAttemptsPerDay`（默认5，1–100）；首版只执行 Herdr，独立 codex_exec 路径由 #175 接入。旧客户端省略 coding 或只更新部分子字段时保留现有配置。

仓库登记精确、存在的 Git top-level 绝对路径、commonDir 与文件系统身份；不接受子目录或可移动别名。授予显式角色 ID、baseRef、remote，以及固定 validation 数组：每项 `id`、绝对程序 `argv`、worktree 内相对 `cwd`。代码会执行主人信任的仓库及验证命令；这不构成同账号 OS 安全沙箱。localCommit/fetch/push/draftPr 的 true 当前拒绝，不能借设置提前启用 #176。

CLI profile 指定 kind（codex/claude）、executor、程序路径及 SHA-256、固定 version/model、专用 authDirectory 和 allowedRoleIds。保存只读取该明确目录的有界认证文件以冻结摘要，不搜索桌面认证，不登录或调用模型。首次只有 unverified；只有宿主两次真实 CLI 核验流程可记录 verified，API 不接受自报状态。认证、profile、仓库、宿主或授权代际变化会使旧运行失效；关闭再开启不能复活旧工具。

Herdr 源码、owned 补丁、已验收二进制、ConPTY 及 notices 锁在[manifest](../../third_party/herdr-owned/manifest.json)。新增构建不能用自签 manifest 替代第一方哈希。配置健康检查只验证本地依赖，不调用真实模型。已核验只是该固定 CLI profile 的证据，不是任意代码的系统隔离保证。

## 工具与来源

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| herdr_start_task | repository、requirement、可选 agent | 返回稳定 taskId/attemptId，启动有界后台尝试 |
| herdr_list_tasks | 无 | 仅同角色、原来源的任务 |
| herdr_get_task | task_id | 安全的业务、runtime、native、cleanup、validation、delivery 状态 |
| herdr_continue_task | task_id、requirement | 原 worktree、branch、baseHead、精确 native UUID；新 attempt |
| herdr_cancel_task | task_id | 停止并等待本地资源，不回滚编辑或承诺费用退款 |

五项均为 `builtin:coding`、deferred、external_allowed=false；主人显式启用 tool_search 后可在同一运行解锁。模型不能提交路径/argv/env/auth/source 身份或发布许可。真实桌面主人 user/run 与宿主 marker 或实际 FrozenChannelSource 提供身份；访客、内部完成和伪造来源不能继承授权。父回合完成不撤销已提交任务，但来源解绑、角色/账户停用、配置代际变化会撤销执行和交付。

## 执行、结果与收尾

每次 Herdr 尝试使用现有 TaskRuntime：kind=coding_herdr、600秒、1MiB日志、8000字符尾部、completion=false、submission_key=attemptId、retain_until_ack=true。本业务额外限制全局2、每角色1、每仓库1；每角色保留10任务、每任务最多10尝试；UTC 日计数持久。每任务文件2GiB/10000项，结构结果256KiB，终端/IPC捕获有界。验证命令每项最多60秒，全部受原始期限与60秒收尾余量约束。

每任务私有 server，Windows suspended 创建进程后先加入私有 kill-on-close Job 再恢复；server、client、验证及版本核验登记私有 named Job。禁用 daemon/WMI、注册表环境、PowerShell profile、session恢复/后台更新/plugin；只允许固定 agent 可执行程序与参数模板。CLI home/Herdr配置私有，认证副本仅留执行期。固定项目 CLI 扩展当前不支持，首次和继续均检查原仓库及worktree的Git项目范围，发现即拒绝。

完整需求写入 UTF-8 handoff，CLI 写新 result.json；宿主核对 task/attempt/native binding、实际 Git diff 和全部批准的命令。模型摘要不证明验证通过。宿主在验证前后及所有 owned 进程退出后比较实际文件快照；迟到编辑使结果失效。`validated_diff` 表示可审查的未提交修改，不能冒称 commit/push/PR。

结果目录位于本 attempt 私有 handoff 内；固定模板以 `--add-dir` 额外授予该精确目录，使受限CLI能写 result，而不批准整个task根。该参数语义分别见 [Codex官方参考](https://learn.chatgpt.com/docs/cli/reference) 和 [Claude官方参考](https://code.claude.com/docs/en/cli-reference)。Claude仍使用dontAsk；显式settings仅为worktree与当前handoff两处生成Edit绝对路径allow规则（Windows盘符按官方规范化并转义模式字符），同时拒绝Bash/PowerShell/WebFetch/MCP。`--tools`只声明工具表面，不能替代编辑许可；[官方权限规则](https://code.claude.com/docs/en/permissions)说明dontAsk及Edit对编辑工具的作用。以上是实现模板，真实CLI是否正确执行这些权限仍由获准原生核验决定。

cleanup 失败仍持有强引用和可重试 owner，冻结后续准入；runtime 终态与本地资源完成分别处理。私有 named Job 日志允许重启后仅观察原资源是否消失，不猜 PID、不接管其他进程。结果副本和 ACK dirty 同事务持久，ACK丢失/prune只重试本地确认；不重跑执行。恢复扫描可轮转，不由旧 unknown 墓碑挡住后续记录。

canonical outbox 与原会话完成消息同 sessions.db 事务。完成使用 `internalEvent=coding_completion` 和确定消息 ID，不伪造 user/run 或新会话。渠道固定原 FrozenChannelSource 目标，经 ChannelHost.account_send 发一次；发送前持久 dispatch，未知回执不重发，真实已 sent 回执仅恢复本地投影。角色删除先关闭准入并等待 owned 资源；未提交 worktree及其所有权档案保留在 coding_retained_artifacts，未擅自强制删除。

## 主人 API

路径前缀 `/api/coding`。GET/PUT `/configuration`；POST `/repositories`、`/profiles` 通过稳定 id 编辑或撤销许可；PUT `/native-build`。GET `/probes`，POST `/profiles/{id}/probe` 需精确 revision、roleId、buildSha256、`approveNativeTurns=2` 和稳定 requestId。每次核验最多600秒，首次与精确继续共两个 native task turns，内部可能多次收费模型请求，不等于两次 API调用。具体原生拒绝记录或支持版本缺失会 blocked。

GET `/roles/{role}/tasks` 是主人桌面管理入口，可查看本角色各来源；POST `/roles/{role}/tasks/{task}/cancel` 可在源许可撤销后停止本角色残留资源。模型工具仍采用更窄的原来源范围。所有配置请求128KiB上限，普通工具结果不含认证、原始终端、完整需求或本地路径。
