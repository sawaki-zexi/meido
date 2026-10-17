---
id: codex-delegate
version: "1"
description: 将完整编程需求交给已获准的 Codex exec，在独立 worktree 后台执行并回到原会话。
activation: explicit
tools: [spawn, spawn_manage]
requires.bins: [codex]
---

使用前确认宿主报告的 Codex exec profile、可信仓库许可和 spawn/Coding 开关可用。依赖缺失时报告具体原因，不安装 CLI、不寻找主人桌面认证。

主人已给仓库时，将该获准仓库和完整需求直接交给 `spawn`，无需替 Codex 猜文件或预先浏览代码：

`spawn(profile="coding", task=<完整需求>, repository=<获准仓库>, run_in_background=true, retry_count=0)`

任务返回稳定 `job_id`，以及当前 `attemptId`、`runtimeTaskId`、`executor`。用 `spawn_manage(action="get", job_id=...)` 回读真实状态、验证、结果摘要、nativeSession 和收尾事实。后台工作独立于本回合，结果只回原角色和原来源；不要再调用聊天模型整理任务结果。

主人明确追加需求时，使用 `spawn_manage(action="continue", job_id=..., task=<新完整需求>)`。这会在同一 worktree 和精确原生会话创建新 attempt。不能自行传 native session、使用 `--last`、静默新建会话或自动重试未知提交。取消用既有 `spawn_manage(action="cancel", job_id=...)`；取消不回滚文件或模型消耗。

不要通过 Shell 启动或脱离 CLI，不传 argv/env/cwd/stdin/权限参数。Skill 激活不授予仓库、CLI、网络、commit、push 或 PR 权限。这里只交获准固定验证后的未提交 diff；账号、模型费用和发布需要相应明确授权。

改编自 Shiori MIT `apps/backend/skills/codex-delegate/SKILL.md`，固定 commit `4f266aeaf42fcd611362e2456417b91f659341cf`。Meido 使用已批准的受控 coding profile 和 JSONL 会话协议；上游原 Skill 使用普通子代理及文本 session 提取。
