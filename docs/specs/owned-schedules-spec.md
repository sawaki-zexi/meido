# 通用时间任务实施契约

状态：已批准目标；本地实现及受控验收完成，待两轴复核与总 PR #162 集成。最后核验：2026-10-16。
事实来源：Issue #158、ADR0012/0013、已验证总集成 cb6347e 中的 TaskRuntime、SpawnExecutor、SessionStore 桌面推送接口。

当前接口见[Reference](../reference/owned-schedules.md)，实际检查与限制见[验收记录](../research/owned-schedules-acceptance-2026-10-16.md)。本文记录实施前提交的计划及已批准范围，不将真实平台收件或总 PR 集成标为完成。

主人通过实际对话调用 schedule / list_schedules / cancel_schedule，计划只属于当前角色及真实来源。通用计划独立于待办；不改变当天提醒者、当天暂停或次日资格。

## 数据流与改动范围

宿主 HTTP / 渠道接收时间随入站消息持久保存。工具只能从真实 run 的主人消息取得 receivedAt；旧消息明确标记 createdAt 回退。创建以 role + origin + run + call 幂等，模型不能指定身份、接收时间或投递键。

独立 schedules 模块在 sessions.db 持久保存计划和 occurrence/outbox，原子占用名义时间、消耗次数并推进 nextAt。instant 冻结固定文本；soft 使用 TaskRuntime(kind=schedule, completion=False)，复用 general SpawnExecutor 的有限工具执行。计划创建和当前 schedule 授权构成投递许可，不读取 spawn.enabled 或 message_push 开关。TaskRuntime 只增加实际被调度消费的宿主 submission_key 幂等交接和结果保留直到 ACK 两个接口；不改变 spawn completion worker。

SessionStore 从现有推送写入提取小型共享私有追加器，保留公开推送 API；schedule 内部追加验证原 session、当前授权和 occurrence/cancel 状态。桌面完成消息与 occurrence sent 在同一 SQLite 事务提交。渠道发送进入现有 ChannelHost lease，稳定 occurrence marker 关联权威 attempt；pending/uncertain 不算 sent、不盲重发。角色删除清理计划并停止所属任务。

soft 保存创建时角色、非秘密模型连接和许可工具定义快照。到时重建候选并与冻结定义、当前许可取交集；连接变化失败。独立任务不创建普通 run、主人消息或第二会话，禁屏幕、浏览器、computer、递归委托、schedule、MCP、工具发现、记忆/Todo 写和渠道发送。

## 时间、次数与恢复

支持 instant/soft，at/after/every/cron；every 接受 duration 或五/六字段 cron。默认固定 Asia/Shanghai，UTC instant 存储；interval 为 UTC elapsed time。after 基于宿主 receivedAt，不基于模型/工具调用时刻。duration 1秒至365天，显式 at 最远365天。无 offset 的 DST gap 拒绝、fold 要求明确 offset；cron gap 跳过、fold0，DOM/DOW 为 AND。cron 首次接受范围五年，已接受规则后继按公历400年周期有界搜索；真正日历范围耗尽持久blocked原因，仍投递当前合法slot，单计划错误不终止共享worker。

单次 count 仅1，循环可省略或1..1000，bool拒绝。所有越过的名义 occurrence 都消耗次数，送达另计。单次迟到300秒内执行一次，超过 expired；启动时重复计划历史 slot 跳过并推进严格未来，正常 tick 只取最近仍在 grace 内的 slot，不集中补发。每角色100活动计划、1000未封存 occurrence；name100、when200、message4000、prompt20000字符。历史30天、每角色1000条，未封存记录不静默删除。

soft launching 使用稳定 submission_key 查回同一 task，已持久 queued/running 重启为 interrupted，不重跑；结果复制到 occurrence 后 ACK。尚未 dispatch 的离线/预算/SQLite busy 在原 deadline 内按1/5/30秒退避，永久授权错误终止。实际 dispatch 后不自动重发；pending保留原attempt，uncertain阻塞重复计划。取消与桌面提交共用事务线性化；渠道 dispatch CAS 赢门禁之后保留真实发送结果。

## 验收与风险

按已确认 seam 逐条红绿：公开 HTTP 主人创建→list→注入 clock 到时真实桌面投递→后续聊天可读→cancel；soft 在 spawn 关闭、父 run 已结束后经真实 TaskRuntime/SpawnExecutor 使用本地 file/web/provider fixture 并仅投递一次。真实 SQLite 测双 worker、重启、提交窗口、保留ACK、取消竞态及角色/来源/目标撤权；时间测试覆盖 DST、cron、count、接收延迟、时钟回拨。

所有模型、网络发送、凭据和数据库验收仅本地临时 fixture。风险集中在取消与跨库发送窗口，权威 receipt 优先，不能从 accepted 推断成功。运行相关 pytest、Ruff、类型检查（对固定基线比较）、compileall、前端 React/tsc/Vite 和文档链接检查；最终分别按仓库规范、需求规格两轴审查。此分支不推送、不合并、不关闭 Issue。
