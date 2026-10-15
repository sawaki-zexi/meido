# 后台 Shell 与 TaskRuntime 验收记录

状态：当前分支验收记录，尚待独立审查与主干集成。最后核验：2026-10-16（Asia/Shanghai）。

事实来源：[#149](https://github.com/sawaki-zexi/meido/issues/149)、[ADR-0012](../adr/0012-owned-task-runtime.md)、[当前协议](../reference/owned-background-shell.md)、[真实进程测试](../../tests/python/test_background_shell.py)、[角色 HTTP 测试](../../tests/python/test_background_shell_api.py)。独立分支基线为 `main@7f4a804`，未合并其他功能分支。本记录只对应 C03 / D02 的 Shell 切片 T07，不代表完整 Shiori 复刻目标完成。

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

Windows 的突发宿主退出树清理已用真实进程验证；POSIX 使用进程组的正常取消/关闭路径，不借此声称突发宿主崩溃也有 Windows Job 的保证。没有执行外部 CLI、用户账号或外部事务。spawn 回灌、通用调度和 Coding 执行器尚未由本票实现，也没有注册空工具占位。
