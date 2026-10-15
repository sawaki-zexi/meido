# #158 本地受控验收

状态：本地行为验证完成，待两轴审查与总 PR #162 集成；未合 main。
最后核验：2026-10-16。基线：cb6347e；TaskRuntime 两 seam 独立提交0927686。
事实来源：下列命令、行为测试与[调度契约](../reference/owned-schedules.md)。

## 已验证

`python -m pytest tests/python/test_schedules.py tests/python/test_schedule_time_rules.py tests/python/test_schedules_api.py tests/python/test_schedules_authority.py tests/python/test_task_runtime_handover.py -q`：**152 passed**（24应用/故障/保留、97时间、9公开HTTP、9真实来源、13任务交接）。

- 实际主人HTTP创建、list、注入clock到时桌面投递、下一模型历史读取、cancel。初次因缺宿主入口红，soft因缺submission_key红，再实现转绿。宿主接收时间覆盖准备/模型40秒延迟，拒绝模型伪造时间/身份。重复宿主call在名义宽限过后仍返回原计划；该回归先红后绿。
- spawn关闭且父run完成，soft经实际TaskRuntime/SpawnExecutor读取临时文件，只回传一次，不增加普通run/user，完成worker无schedule项目。另用真实ProcessExecutor覆盖交接已执行但taskId确认丢失、重启interrupted、取消setup及跨线程shutdown等待。
- SQLite双worker桌面及真实ChannelHost dispatch竞争、取消与桌面写事务顺序、sent/pending/failed/uncertain、权威收据成功但本地同步失败后的重启修复。pending后续权威sent只修复原attempt。所有情况均断言实际发送次数，未以accepted断言送达。
- 角色/发言者/线程/访客、解绑重绑和账号重建；显式主人目标冻结对应绑定，其他身份变化不会撤销它。关闭schedule撤销计划，独立管理仍可查询取消。
- DST gap/fold、五/六字段cron、weekday、闰日、时区固定、计数/回拨、五年秒级cron批量计数；30天清理、每角色1000封存历史、pending/uncertain超过31天仍保留。

受影响回归命令：`python -m pytest tests/python/test_background_shell.py tests/python/test_background_shell_api.py tests/python/test_spawn.py tests/python/test_spawn_api.py tests/python/test_spawn_completion_recovery.py tests/python/test_desktop_push.py tests/python/test_desktop_push_api.py tests/python/test_tool_discovery_api.py tests/python/test_channel_api.py tests/python/channels/test_host.py tests/python/channels/test_telegram_runtime.py tests/python/test_todo_plugin.py -q`：**195 passed**；6条既有SDK弃用警告。Todo当天规则继续通过。

`npm run test:web`：**173 passed**；`npm run build`（TypeScript/Vite）通过。角色基础授权新增3项仍默认关闭，保存/重开/未知配置保留通过。

Ruff、compileall、diff whitespace和改动文档相对链接通过。`python -m mypy backend/app/schedules backend/app/task_runtime/runtime.py --follow-imports=silent --show-error-codes`：5源文件通过。

对 main.py、session_store.py、agent_runtime/runtime.py、multichannel.py、channels/models.py 使用同一mypy参数，与独立固定cb6347e工作树逐条比较：两者均83条既有错误；仅归一化诊断行号/引述行号后，错误消息及数量完全相同，新增0、移除0。未声称全仓mypy通过。

## 首次两轴审查后的修复

初始快照a976edd的152项不覆盖下列四个正式问题。四项均独立复现、加入回归先红后绿，当前待原审查者独立复核：

- 实际使用应用默认模型的soft计划误将角色未显绑ID解析为无配置；现在按创建时实际resolved连接ID精确恢复。默认不变/换新默认继续原连接，原连接修改/删除明确拒绝；不接触真实凭据。
- 结果已复制后ACK遇SQLite busy，后续ready/sent或重启丢失重试；现在持久ACK dirty且未确认记录不参与历史清理。覆盖ready、旧sent迁移、ACK成功后结果已prune但确认回写丢失；同origin通过墓碑幂等ACK，跨角色/会话/线程/账号/绑定、unknown、disabled和deleted均拒绝。另覆盖交接已执行、taskId回写丢失后cancel找回原任务并ACK。
- 合法稀疏cron后继超过五年使共享worker退出；现在首次仍限五年，后继在400年公历周期内按日历有界搜索。2028-02-29 Tue续排2056，2044 Mon续排2072。支持日历确实耗尽时当前slot仍投递，剩余次数保留并进入blocked_time_range；无效持久规则只使本计划failed并持久原因，其他角色照常投递。补Windows年份9999的UTC/时区边界。
- owned-call等待线程以额度错误结束时吞掉已收到的取消，导致close挂住；现在先join线程，再优先传播原CancelledError。真实ProcessExecutor占满并发时，一次close即可完成。cancel工具也使用同一回收能力，连续取消和底层ValueError/PermissionError保持取消语义；此项工具加固未单列为审查finding。

当前完整专项命令在原5组基础上加`tests/python/test_schedule_cancellation.py`：**177 passed**（应用33、时间105、HTTP13、来源9、交接14、取消3）。必要共享消费者回归：`python -m pytest tests/python/test_background_shell.py tests/python/test_spawn.py tests/python/test_spawn_completion_recovery.py tests/python/test_spawn_api.py tests/python/test_desktop_push.py -q`：**84 passed**。本轮无前端改动，未重复原173项前端和无关全后端。

修复后5源mypy、Ruff、compileall、相对链接和diff检查通过；共享文件仍与固定cb6347e逐条83/83一致，无新增诊断。

## 限制与尚待完成

模型/渠道/凭据/文件/数据库均为临时本地fixture，没有真实平台收件、付费模型或原生桌面操作。pending/uncertain只能反映平台实际收据，不虚构确认API。仍需仓库规范/需求规格两轴复核和总集成交叉检查；未推送、未合并、未关闭Issue。
