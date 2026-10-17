# #158 本地受控验收

状态：行为验证、两轴复核与总 PR #162 本地集成完成，`4b1b3b8` 两套CI通过；未合 main。
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

初始快照a976edd的152项不覆盖下列四个正式问题。四项均独立复现、加入回归先红后绿；该轮随后经原审查者复核，后续组合问题与最终结论见下文：

- 实际使用应用默认模型的soft计划误将角色未显绑ID解析为无配置；现在按创建时实际resolved连接ID精确恢复。默认不变/换新默认继续原连接，原连接修改/删除明确拒绝；不接触真实凭据。
- 结果已复制后ACK遇SQLite busy，后续ready/sent或重启丢失重试；现在持久ACK dirty且未确认记录不参与历史清理。覆盖ready、旧sent迁移、ACK成功后结果已prune但确认回写丢失；同origin通过墓碑幂等ACK，跨角色/会话/线程/账号/绑定、unknown、disabled和deleted均拒绝。另覆盖交接已执行、taskId回写丢失后cancel找回原任务并ACK。
- 合法稀疏cron后继超过五年使共享worker退出；现在首次仍限五年，后继在400年公历周期内按日历有界搜索。2028-02-29 Tue续排2056，2044 Mon续排2072。支持日历确实耗尽时当前slot仍投递，剩余次数保留并进入blocked_time_range；无效持久规则只使本计划failed并持久原因，其他角色照常投递。补Windows年份9999的UTC/时区边界。
- owned-call等待线程以额度错误结束时吞掉已收到的取消，导致close挂住；现在先join线程，再优先传播原CancelledError。真实ProcessExecutor占满并发时，一次close即可完成。cancel工具也使用同一回收能力，连续取消和底层ValueError/PermissionError保持取消语义；此项工具加固未单列为审查finding。

当前完整专项命令在原5组基础上加`tests/python/test_schedule_cancellation.py`：**177 passed**（应用33、时间105、HTTP13、来源9、交接14、取消3）。必要共享消费者回归：`python -m pytest tests/python/test_background_shell.py tests/python/test_spawn.py tests/python/test_spawn_completion_recovery.py tests/python/test_spawn_api.py tests/python/test_desktop_push.py -q`：**84 passed**。本轮无前端改动，未重复原173项前端和无关全后端。

修复后5源mypy、Ruff、compileall、相对链接和diff检查通过；共享文件仍与固定cb6347e逐条83/83一致，无新增诊断。

## 复核后的任务清理闭环

53c1275复核暴露两条相关遗漏：taskId确认丢失后过期/撤权封存未找回原任务，以及cancel找回taskId后状态写入遇真实SQLite busy而遗漏ACK。两份外部原探针在53c1275重现；新增三条行为回归先红，再统一修改清理责任转绿。

soft现在从claim起保存清理责任，taskId与dirty同事务关联；所有终态保留该责任，统一找回、stop、ACK，成功后才清除。还验证内层PermissionError/KeyError、外层source PermissionError、链接/状态写入真实SQLite busy、31天清理保护与旧封存无id记录恢复。并行提交回归覆盖尚未进入Runtime时取消/过期，以及已接受但未返回id时取消、迟到返回或抛错；晚到错误改写expired终态及计划状态的回归也先红后绿。

真实角色disable→封存→删除回滚enable组合先红后绿。既有TaskRuntime.list在同锁内对disabled角色明确抛PermissionError，调度保持dirty，恢复后找回并ACK；不新增Runtime seam或参数，不放宽ACK、unknown及跨来源权限。现有真实消费者为schedule和spawn_manage，后者已有PermissionError处理。TaskRuntime marker查找受默认每角色100条保留额度约束，不宣称无界查询。

两份外部原探针在新实现转绿：Standards expired/revoked两项retainedTasks均0；Spec原脚本使用全新临时目录原样执行，cancelled/synced1且retain_until_ack从1恢复0。后者保留普通历史符合默认保留期，不等同于结果必须立即删除。

本轮完整专项在上轮命令加入`tests/python/test_schedule_cleanup.py`：**191 passed**（新增14项）；必要共享消费者原命令：**84 passed**。5源mypy、Ruff、compileall、改动文档相对链接和diff检查通过。共享mypy与固定cb6347e仍为83条错误逐条一致（另14条note也一致），新增0、移除0；本轮无前端或其他共享生产文件改动。

## ef3af2b复核后的错误处理保护

ef3af2b两轴复核中，Spec发现PermissionError处理分支自身写入遭SQLite busy会逸出共享worker，释放锁和恢复角色后也不再推进。外部原探针先复现worker以OperationalError终止；窄修复只在该分支持久化写入处保护既有OperationalError/OSError可重试边界，保留原occurrence和清理责任。相同原探针修复后worker持续运行。

新增真实行为回归先红后绿：实际ProcessExecutor完成而taskId回执丢失、角色临时disabled时独立SQLite writer持锁，释放锁并恢复角色后，同一worker完成原任务ACK，另一角色计划实际桌面送达一次，原执行不重跑。此前固定ef3af2b的191项专项和84项共享回归证据继续保留，本次仅按错误分支选择受影响测试和静态检查，不重复全前端或全后端。

`python -m pytest tests/python/test_schedules.py tests/python/test_schedule_cleanup.py tests/python/test_schedule_cancellation.py tests/python/test_schedules_authority.py tests/python/test_task_runtime_handover.py -q`：**74 passed**。相关5源mypy、变更源码/测试Ruff、compileall、文档相对链接和diff检查通过。

## 总集成本地验证

最终源快照e992e0f的规范和规格两轴复核均无剩余finding；两位各自窄差异18项通过，继承此前固定ef3af2b的专项/共享及独立原探针证据，不将重叠计数相加。

汇入总分支时仅状态表与第三方声明冲突，保留Feed与调度两份归属声明；调度、TaskRuntime及本票共享生产文件与经复核源逐文件一致。调度七文件、后台Shell、spawn/恢复、桌面推送、发现、渠道API、MCP HTTP/来源和Todo交叉 **400 passed、6条已有SDK警告**；全部前端 **174 passed /26 files**，TypeScript/Vite、5源mypy、compileall和12份改动文档相对链接检查通过。

随后总集成 `4b1b3b8858896b907a147b65612de0bb02896305` 的 [push CI](https://github.com/sawaki-zexi/meido/actions/runs/38817647017) 与 [PR CI](https://github.com/sawaki-zexi/meido/actions/runs/38817638972) 均成功：Linux后端 **1712 passed、16 skipped、2 warnings**，前端 **174 passed /26 files** 及TypeScript/Vite构建。skip不计原生验收；两套CI结果不相加。

## 限制与尚待完成

模型/渠道/凭据/文件/数据库均为临时本地fixture，没有真实平台收件、付费模型或原生桌面操作。pending/uncertain只能反映平台实际收据，不虚构确认API。两轴复核、本地总集成交叉及新CI已完成；总PR其他切片仍在实施，未合main、未关闭Issue。
