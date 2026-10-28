# 角色通用时间任务契约

状态：已通过两轴复核并进入总 PR #162 集成分支；`4b1b3b8` 两套CI通过，尚未合入 main。
最后核验：2026-10-16。
事实来源：[应用服务](../../backend/app/schedules/application.py)、[时间规则](../../backend/app/schedules/time_rules.py)、[工具](../../backend/app/schedules/tools.py)、[HTTP验收](../../tests/python/test_schedules_api.py)、[恢复投递](../../tests/python/test_schedules.py)、[来源授权](../../tests/python/test_schedules_authority.py)、[任务交接](../../tests/python/test_task_runtime_handover.py)。决定见[ADR0013](../adr/0013-independent-generic-schedules.md)。

## 工具与范围

普通主人对话默认提供 schedule、list_schedules、cancel_schedule；创建计划仍须真实主人请求及来源许可，不依赖旧 enabledTools 列表。停止计划使用 cancel_schedule，删除或来源失效继续撤销执行；普通 Shell 默认权限不授予 soft 任务受限 Shell。

schedule 必填 tier（instant/soft）、trigger（at/after/every/cron）、when。instant 只接受非空 message；soft 只接受非空 prompt。可选 name、timezone、count、channel、chat_id；拒绝额外字段，模型不能供应身份、接收时间或幂等键。name≤100、when≤200、message≤4000、prompt≤20000字符。按真实 role/source/run/call 幂等。

目标默认原上下文；桌面支持 channel=desktop、chat_id=owner/user。显式渠道目标须为当前角色账号目录中的唯一目标且已有会话；冻结账号代际、目标 ID/种类/chat ID，主人目标还冻结配对。失效不能改投。桌面消费 SessionStore 内部写能力，外部消费 ChannelHost 当前 lease 和权威 attempt，不调用公开 message_push。

列表范围包含角色、实际发言者、线程和绑定代际。返回 id/name/tier/原 trigger/when/timezone、nextAt、remainingRuns（无限为null）、planStatus、started/delivered/skippedRuns、receivedAt/receivedTimeSource、最近 occurrence 的执行/投递、task/attempt和错误，不输出快照、密钥或日志路径。

取消接受至少8字符且范围内唯一的 ID 前缀，或精确 name。同名取消当前范围全部；ID与name同时给出须匹配相同集合。重复取消安全；未知/跨范围统一拒绝。取消与桌面提交共用事务，已进入外部发送保留真实 pending/sent/uncertain。

## 时间与次数

默认固定 IANA Asia/Shanghai，存 UTC instant，不读主机时区。at 接受 HH:MM 或 ISO，显式时刻不得早于宿主接收时间、最远365天。after/every duration 为按 d/h/m/s 顺序的组合，1秒至365天；interval 为 UTC elapsed time。after 基点为入站持久 hostReceivedAt，覆盖排队/准备/推理延迟；旧消息标记 createdAt_fallback。拒绝模型 request_time。

every 也接受五/六字段 cron，cron 是显式同义入口；六字段秒在前。支持集合/范围/步长，weekday名称及0/7均指周日；DOM/DOW 为 AND。创建时首个时刻必须在五年内；已接受规则的后继按公历400年周期有界查找，保留间隔超过五年的合法稀疏slot。若支持的日历范围确无后继，当前合法slot仍投递，计划进入blocked_time_range并保存原因，未消耗次数不视为已完成。无 offset 本地 DST gap拒绝、fold要求明确offset，显式offset须符合选定zone；cron跳过gap、只取fold0。多年秒级 missed slots 按日历批量计数；单条持久时间规则无效时只将该计划标为failed并保存原因，不终止其他角色的调度。

单次count缺省1且只能1；循环缺省无限，或1..1000整数，bool拒绝。所有越过名义slot消耗次数，送达另计。单次迟到≤300秒补一次，之后expired；重启重复计划历史slot全跳过并推进严格未来。正常tick仅执行最近仍在grace内的slot，其余计skipped；长执行不并发，时钟回拨不复用已占slot。

每角色最多100活动/不确定阻塞计划、1000未封存occurrence。封存历史保留30天且最多1000条；未确认/不确定记录不静默删除。

## 执行、恢复和投递

instant 原样投递冻结文本。soft 使用独立 TaskRuntime(kind=schedule, completion=False)，120秒/12轮/最终20000字符/累计60000字符及既有宿主任务日志配额。复用 general SpawnExecutor，只运行创建时冻结且当前仍许可的 read/list/web、任务目录write/edit和argv白名单Shell。禁屏幕、browser/computer、MCP、发现、递归委托、schedule、记忆/Todo写及渠道发送。独立工作集只接收prompt，不创建普通run、主人消息或第二会话。

模型非秘密配置、角色与工具定义创建时冻结，使用当时实际解析的连接ID（包括应用默认连接），到时按该ID精确恢复；切换默认连接不改投，原连接消失/改变则失败。当前许可持续复核，新增许可不扩权。soft使用宿主submission_key原子交接；崩溃窗口可找回同一task，重启queued/running为interrupted而不重跑。

soft从claim起持久保存任务清理责任，taskId关联与ACK待确认标记同事务写入。过期、撤权、取消和异常终态均保留该责任；找回原任务、停止和ACK由同一恢复路径处理，SQLite busy或重启不会使其丢失。结果复制后ready/sent均可独立重试本地ACK，不重跑执行或投递。无taskId时按原来源和occurrence marker查找，受TaskRuntime既有每角色100条保留额度约束；终态同时关闭提交的最终授权门，经过同一Runtime锁确认无任务后才标记not_submitted并解除责任。任务管理暂时禁用时list明确抛PermissionError，不能把不可管理误认为空列表；角色删除失败恢复enable后继续清理，实际删除仍全量清除任务和调度。该错误不放宽ACK或跨来源访问权限。

ACK前仍占任务/日志额度且不清理对应occurrence；ACK后普通回收，幂等墓碑保留到角色删除。ACK已成功而确认回写丢失、结果已回收时，同原来源的keyed ACK仍幂等成功，未知/跨范围或已删除角色保持拒绝。所有投递终态均受持久写门保护，晚到的提交错误不能改写已确定的终态。

宿主关闭、工具停止及连续取消均等待交接/停止线程退出；等待中的线程随后抛普通错误时，仍保留原调用取消语义，不能把关闭转换成重试。

尚未实际dispatch的离线/预算/SQLite busy在原scheduledAt+300秒内按1/5/30秒退避，不推迟名义时刻。模型已提交或外部attempt已建立后不重新执行/发送。渠道最初本地消息为pending，收据先持久，再同步消息；本地失败只留dirty修复。sent才计送达，pending等待原attempt，uncertain阻塞重复计划且不盲重发。账本按稳定occurrence marker精确查找，不限最近100条。

本次模型与渠道均为本地临时fixture，未发送真实平台消息或使用付费模型。未接入TodoPlugin，不改变当天提醒者、当天暂停和次日资格。

## 工具目录迁移兼容（2026-10-26）

soft 冻结的是创建调用所属请求的有限已选工具，不是随后发布的聊天目录；同回复先搜索不能把未展示工具加入计划。到时严格解析原定义，仅为后来新增的可选元数据使用字段默认值，未知格式拒绝；仍与当前实际定义及权限取交集。同名新实现、后加工具和模型配置变化不会扩权。TaskOrigin 兼容与 bridge 回滚见 [受管任务](owned-background-shell.md#taskorigin-历史兼容2026-10-26)和 [ADR-0026](../adr/0026-request-bound-lightweight-tool-loading.md)。
