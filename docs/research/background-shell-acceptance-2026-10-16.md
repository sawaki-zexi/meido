# 后台 Shell 与 TaskRuntime 验收记录

状态：当前分支验收记录，尚待独立审查与主干集成。最后核验：2026-10-16（Asia/Shanghai）。

事实来源：[#149](https://github.com/sawaki-zexi/meido/issues/149)、[ADR-0012](../adr/0012-owned-task-runtime.md)、[当前协议](../reference/owned-background-shell.md)、[真实进程测试](../../tests/python/test_background_shell.py)、[角色 HTTP 测试](../../tests/python/test_background_shell_api.py)。初始独立分支基线为 `main@7f4a804`。维护者明确要求统一总 PR 后，按 #137 评论6198017236 的新流程合入已验证总分支 `fa17e79`（代码集成 `59725e4`，包含 #148 固定587b3e7、#152 固定10dce329及main17c），复用来源授权与最小环境修复。未自行合并其他未审功能；后续 loop 重复取消修正由 #148 作者及总集成负责，本票未改其逻辑。本记录只对应 C03 / D02 的 Shell 切片 T07，不代表完整 Shiori 复刻目标完成。

## 独立审查后的修正进度

下文旧的 118 + 105 项结果对应固定提交 `2ec4e5e`，不能代替本节后续修正结果。当前分支已补 P1/P2 和总基线回归，等待总 PR #162 集成与规范/规格两轴独立审查；Issue #149 保持 OPEN。

- 输出兼容 P2：公开角色 HTTP 保存合法 `maxOutputChars=200000` 后，短命令曾被 TaskRuntime 的 100000 限制拒绝（红）；统一支持既有 200000 上限后，该真实 Shell 回合通过（绿）。
- 日志消费 P2：将预创建日志文件替换为同名目录，再释放真实 Python 进程输出，原来出现 pump PermissionError 但任务被记为 succeeded（红）。现在消费错误向任务执行器传播，停止进程，并独立在 SQLite 记录 failed、logComplete=false 和仅含异常类型的 logError。打印后正常 exit0 与继续 sleep10 两种真实进程均通过，PID 消失；故障后角色删除也能非递归移除这个受管空目录（绿）。失败收尾不会重试坏日志路径，也不把写入故障称为预算耗尽。
- 来源授权 P1：总基线合入后，真实 SQLite 私聊/群聊 × 解绑、解绑重绑、响应范围关闭、屏蔽发言者共8个反例仍可读取 owner-only-log；群访客自报正文owner JSON也仍会执行，先确认 **9 failed**。随后复用 #148 FrozenChannelSource 的真实发送者、账号创建代际、绑定 ID/pairedAt 和当前权限，接入后台观察与 immutable origin，原9例 **9 passed**。没有从正文或可变参数解析授权。
- 集成补验：私聊/群聊的平台总开关关闭与账号删除后同ID重建均拒绝读取并停止真实PID；同一绑定的后续真实入站回合可以读取，返回原始origin；原child token取消后后台继续，解绑再配对的新回合无权读/停旧任务且无logPath。完整角色API测试 **19 passed in 24.05s**。
- source_scope_id/source_authority_json 由宿主实际填充并经SQLite持久化；scope排除单条消息时间，保留实际sender、绑定代际、账号代际和原线程/会话。capabilityVersion优先取ToolContext实际调用版本，再查宿主call journal，保留原始parent run和唯一tool call ID。
- 合入保留 #147 断连取消、#136 平台/账号开关及 #154 回复按钮，保留 #148 最终参数实时白名单与纯公开匹配helper，保留 #152 最小环境键名大小写兼容。没有修改loop或扩环境许可集合。
- 真实CLI交叉验收发现新回归：3项native先为2 passed/1 failed，summarize的stdout JSON在字符4098被到达的stderr进度插入而无法解析。新增受控真实子进程，先输出JSON片段，再stderr，再剩余JSON，稳定得到JSONDecodeError（红）。执行器分别保留各流有界尾部，终态组合stdout后附stderr；Runtime预算裁切，日志保留到达顺序。该已知结果及后续task_output一致性转绿，非零/超时/整树/大输出/泵故障等相关回归仍通过；没有用过滤ANSI或删stderr迎合结果。

旧 `53e37f` 的 **20 passed, 9 deselected** 只记录独立P2修复，当时未将红例标为skip/xfail。当前已不再排除这些反例，验证结果如下：

| 检查 | 结果与边界 |
| --- | --- |
| 后台进程/HTTP/Shell/角色配置/Agent Runtime/#148 HTTP与来源helper | `test_background_shell.py test_background_shell_api.py test_shell_tool.py test_role_capabilities.py test_api.py test_agent_runtime.py test_tool_discovery_api.py test_channel_source_authority.py`：**151 passed, 1 skipped in 76.15s**。此轮在追加6个来源边界与stdout/stderr修正前；唯一跳过为Windows无大小写敏感POSIX可执行路径前提 |
| 后续来源/流修正的针对性回归 | `test_background_shell.py test_background_shell_api.py test_shell_tool.py`：**45 passed, 1 skipped in 35.13s**；包括追加6个来源边界、流分离已知结果、40万字符双管道排空、日志故障、真实树停止/恢复和公开HTTP。跳过同上；未重复未受影响的整套测试 |
| 共享渠道/角色删除/浏览器/能力生命周期 | `test_channel_api.py channels/test_role_deletion_recovery.py test_browser_use.py test_capability_layer.py test_capability_plugins.py test_capability_skills.py`：**105 passed, 2 skipped in 40.75s**。两项浏览器原生验收未配置资源，不能将此组称原生浏览器通过；#150有独立原生证据 |
| 真实CLI | 设置 `MEIDO_SKILL_NATIVE_TESTS=1`、已有真实summarize安装路径、Node24目录后运行 `test_builtin_skills_native.py`：**3 passed in 13.93s，无skip**。真实curl天气两公开来源、真实summarize本地文件→本地受控provider、真实公网URL提取；不是模型付费账号调用。启用后缺依赖/网络失败会失败 |
| 静态/构建 | TaskRuntime/Shell4源码文件mypy、main及本票源码/测试Ruff、compileall、TypeScript/Vite build均通过。最终diff/文档链接在提交前检查 |

本地忽略产物：`.data/background-shell-integrated-acceptance.xml`、`.data/background-shell-stream-and-origin.xml`、`.data/background-shell-native-cli.xml`。三组数目存在重叠，不相加作为唯一通过数。真实CLI使用维护者已安装的 `@steipete/summarize` 资源与Node24，不下载或改依赖。

## 环境和公开边界

Windows 11 AMD64、Python 3.13.9、pytest 9.1.1。测试通过公开 AgentTool.execute、真实 Python 子进程及子孙、固定工作目录、本地临时 SQLite/日志和公开角色 HTTP API 执行；只替换外部模型服务的选择结果，不使用假 process/transport 证明进程生命周期。自动提升阈值由测试宿主设为 30 毫秒；没有等待生产默认的 15 秒。

## 红绿证据

- 首个后台命令用例因 TaskRuntime 缺失而红；实现应用级任务服务后，父 token 取消不再影响已返回的后台执行，后续来源回合取得真实完成结果和完整日志。
- HTTP 首轮因模型请求没有 task_output/task_stop 而红；完成候选装配与 Shell 硬开关后，同一角色三个独立主人回合完成后台执行、读取进度、停止。
- 已后台运行进程的宿主授权消失时，新增用例观察到进程仍存活而红；TaskRuntime 增加独立授权/预算观察后，真实进程被回收，查询也不暴露日志位置。
- 角色总日志额度用例发现新任务没有预留空间、旧日志仍存在而红；提交时预留完整运行额度并清理较旧终态后，超额并发无法入场，目录占用受预算约束。
- 扩大回归发现提前 await 任务清理会破坏现有删除屏障，导致管理员写入测试悬挂。将实际清理放在既有屏障后，并在同步删除 gate 立即拒绝新 Shell 后，原有 12 条删除/取消回归通过；未改写聊天断连逻辑。

## 行为结果

| 验收 | 已观察到的结果 |
| --- | --- |
| 前台/后台/自动提升 | 旧 command-only 调用仍前台返回；显式后台立即返回 ID；短宿主阈值提升后保留原硬截止，观察超时和重复查询均不重跑 |
| 输出预算 | 真实 stdout/stderr 合计超过 40 万字符仍被消费并完成；展示尾部和日志存储截断分别报告；超过展示预算但未超过日志预算的完整 10 万字符日志与已知文本逐字相等 |
| 整树所有权 | 真实子进程再创建孙进程；并发两次 stop 后二者都消失；父进程在受控释放信号后成功退出，遗留子孙仍被清理 |
| 前台取消与恢复 | 实际 asyncio 调用取消后 PID 消失，同角色后续命令成功；父回合 token 取消不会取消已经返回的后台任务 |
| 关闭/重启/崩溃 | 正常关闭记录 interrupted；独立真实宿主通过 os._exit 突然退出，Windows Job 回收子进程；新 TaskRuntime 将遗留 running 记录标 interrupted，不重跑 |
| 归属与权限 | 跨角色/来源/未知 ID 返回同样错误且没有日志路径；后续原来源可查；HTTP 关闭 Shell 或删除角色均回收真实进程，删除同时移除日志 |
| 宿主额度 | 每角色并发、保留记录数和日志额度可通过短测试宿主配置验证；模型超出角色 timeout 被拒绝；非零退出保留真实退出码 7 |
| 宿主关联 | HTTP 结果记录真实 parent run、tool call 和 capability snapshot ID；日志位于应用任务根，角色 workspace 不含任务日志 |

## 验证命令

```powershell
python -m pytest -q --tb=short -o faulthandler_timeout=30 tests/python/test_background_shell.py tests/python/test_background_shell_api.py tests/python/test_shell_tool.py tests/python/test_role_capabilities.py tests/python/test_api.py tests/python/test_agent_runtime.py --junitxml=.data/background-shell-acceptance.xml
```

结果：**118 passed, 1 skipped in 58.64s**。唯一跳过项要求大小写敏感的 POSIX 可执行路径；本机 Windows 无此前提。JUnit 开始时间 `2026-10-16T00:25:20+08:00`，`tests=119/errors=0/failures=0/skipped=1`；报告为本地忽略产物，可用上述命令重新生成。

```powershell
python -m pytest -q --tb=short -o faulthandler_timeout=30 tests/python/test_channel_api.py tests/python/channels/test_role_deletion_recovery.py tests/python/test_browser_use.py tests/python/test_capability_layer.py tests/python/test_capability_plugins.py tests/python/test_capability_skills.py
```

结果：**105 passed, 1 skipped in 47.50s**。该组未设置浏览器原生资源，因此浏览器 native 验收跳过；此结果只证明本票的共享装配/生命周期回归，不替代 #144 的原生浏览器验收。

TaskRuntime 与 Shell 四个源码文件 mypy、变更测试及模块 Ruff、Python compileall、`git diff --check` 和 `npm run build`（TypeScript/Vite）均通过。前端没有实现改动。

## 实际限制

Windows 的突发宿主退出树清理已用真实进程验证；POSIX 使用进程组的正常取消/关闭路径，不借此声称突发宿主崩溃也有 Windows Job 的保证。已执行上述3项真实CLI的公开只读来源/本地受控provider；未操作用户账号、发送消息或执行外部写事务。spawn 回灌、通用调度和 Coding 执行器尚未由本票实现，也没有注册空工具占位。
