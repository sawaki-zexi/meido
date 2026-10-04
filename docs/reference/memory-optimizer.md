# 记忆 Optimizer 配置与恢复契约

状态：当前

事实来源：`backend/app/memory_optimizer.py`、`backend/app/main.py`、`tests/python/test_memory_optimizer.py`

最后核验：2026-10-05

## 配置

| 环境变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `MEIDO_MEMORY_OPTIMIZER_ENABLED` | `true` | 是否启动定时长期记忆归并循环。`0`、`false`、`no`、`off` 表示关闭。 |
| `MEIDO_MEMORY_OPTIMIZER_INTERVAL_SECONDS` | `64800` | 定时扫描间隔，单位为秒。实际间隔不会低于 60 秒。 |

Optimizer 只为存在可用模型配置的角色提交任务。角色之间共用一个 Optimizer 串行执行通道；普通回合的语义记忆处理仍由独立 worker 执行。

## 文件状态

每个角色的 `memory/` 目录使用 `.optimizer.json` 保存 `last_memory_optimized_at`、`self_rules_version`、`self_update_pending` 和快照提交标记。归并前会把 `PENDING.md` 原子改名为 `PENDING.snapshot.md`，归并期间新产生的候选写入新的 `PENDING.md`。

进程中断或归并失败时，残留快照会在下一次启动恢复。MEMORY 提交成功后，即使 SELF 更新失败也不会回滚 MEMORY；`self_update_pending` 会使角色在后续扫描中重试 SELF 更新。
