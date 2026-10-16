# 验收 NovelAI 生成、重生成与后台原来源交付

状态：当前本地受控验证，真实 NovelAI 未验收。最后核验：2026-10-16。事实来源：[Reference](../reference/novelai-generation.md)、[公开消息用例](../../tests/python/test_novelai_main.py)、[真实渠道用例](../../tests/python/test_novelai_channel.py)、[真实断连](../../tests/python/test_novelai_cancellation.py)、[恢复](../../tests/python/test_novelai_recovery.py)。Parent #137，当前本地实现覆盖 #171/#172；#173 CG 尚未交付。下方 #171 历史记录保留当时范围和失败事实，#172 增量单独记录。

用户已明确批准完整具体规格第3节的实现及本地无费用夹具；真实 token、账号、模型/生成费用和外部发布另批。其中 #171 从获准的已验证 aggregate `30002a9a8de551a12a44b2c2f5724d5640aca50d` 开始，分支 feat/171-novelai-generation，统一整合总 PR162，不新建堆叠 PR，不合并或关闭 Issue。

## 运行受控测试

所有平台/HTTP peer 都必须为 localhost；测试使用 fixture credentials，不读取用户 vault。pytest 收集前设置完整三数据根，PROJECT_ROOT 不能代替 owner knowledge 数据根：

```powershell
$testRoot = Join-Path $env:TEMP ('meido-novelai171-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$env:MEIDO_DATA_DIR=Join-Path $testRoot 'data'
$env:MEIDO_ROLES_DIR=Join-Path $testRoot 'roles'
$env:MEIDO_OWNER_KNOWLEDGE_DATA_DIR=Join-Path $testRoot 'owner-data'
Remove-Item Env:MEIDO_PROJECT_ROOT -ErrorAction SilentlyContinue
$files=Get-ChildItem -LiteralPath tests/python -Filter 'test_novelai*.py' | Select-Object -ExpandProperty FullName
python -m pytest @files -q --tb=short
npm run test:web
npm run build
python -m compileall -q backend tests
git diff --check
```

新用例依赖 novelai_environment/local_platform_only，允许 localhost socket、拒绝其他网络；三环境隔离和 guard 保持到 Runtime、HTTP server、真实OneBotWebSocket、Uvicorn、头像worker和维护任务全部结束，不在 yield 后提前撤销。peer 的 auth/header/base64 只在测试内存断言，不输出真实 secret。

## 已验证的公开行为

独立服务设置、默认关闭角色许可和旧客户端部分更新保留；普通配置保存不读 token。严格参数、冻结 seed 与完整 v4.5 请求、主人的当前附件 img2img、无源/内部run拒绝；同请求重查和 humanRun intent claim；持久账户/角色尝试及 Runtime 既有并发实际生效。

真实 main `/messages` → localhost OpenAI SSE provider → 宿主冻结 generate_image → 真实 TaskRuntime → 独立 localhost NovelAI HTTP201 ZIP → 工具安全引用 → 下一次 provider → 唯一完成回复附件/下载 → owned 历史闭环。txt2img 与 img2img 参数化覆盖；后者先通过公开附件上传和消息 attachments 字段提交不同颜色的底图，逐字节核对生成 POST 的原图，后续模型只保留当前用户输入图，不收到生成图 base64。provider 路径和生成路径为两个真实 HTTP 服务；不使用真实模型。

真实 OneBot WebSocket 主人入站、NapCatAdapter/ChannelHost、原QQ目标单次文本+PNG、真实 socket 回执；真实断线丢回执 uncertain 不重发。SQLite触发器分别使 receipt 和消息metadata写失败，恢复只同步已有 receipt/原消息，不降级已知 sent、不再次 send/POST、不改投桌面。

真实 Uvicorn HTTP socket 断开恰逢本地图片导入时，先封存发布资格，Runtime停止/join，迟到成功只留历史；recover 不能重新插入 UI 消息。真实角色许可撤销、关闭再开启 ABA、最后 INSERT 前撤权、角色删除及应用 close 都有行为证明。

HTTP200/201、401/402、408/429/500、坏 ZIP、严格 JSON base64、实际像素超限、17项 ZIP、实际解压超过64MiB、无 Content-Length 响应超过32MiB、Content-Encoding gzip、原始deadline结束，均不自动重发。输入secret、rawpath/base64不进正常工具结果/历史JSON；协议异常保留消费可能说明。HTTPX transport 明确 trust_env=false，测试用无效 SSL_CERT_FILE/本地代理证明不误读进程环境。

真实文件 metadata rename 失败、已导入图片后状态提交失败、Runtime交接 taskId 回写失败、canonical outbox 回滚/重复消费都先 RED 再 GREEN。恢复复用既有二进制、先持久 staged 引用再删除 spool；账本找回同一 Runtime handover并 ACK，真实 retention 唯一槽可以随后释放。有效先导入图+坏后续项保留历史并按实际占用结算。

React 覆盖真实 App 设置/角色/历史入口、有限消费显式确认、只打开不POST、网络失败保持原requestId重查、切角色 abort/忽略迟到图。正常界面不会展示凭据、base64或本地路径。

## 本轮记录与限制

### #172 本地增量

#172 从准确已验证 `0dee4e43a36bfdc39160cbf2c20c7a2e203198cf` 建立独立 `feat/172-novelai-regeneration-background`，仍统一总 PR162，不另建堆叠 PR。前置 #171 两次准确 CI 均为 1922 passed/25 skipped；本段不把此前 CI 当作 #172 验收。

历史和聊天生成图片提供精确选图入口，经服务器 lookup 核验当前消息与 canonical outbox。新操作预填参数、清空旧 Seed、重新确认消费，保持 requestId 重查；指定旧附件 CAS 只换该图，其他图、文件和 metadata 保留。真实 HTTP 覆盖新 attempt/parentAttemptId、不同 seed、参数冲突和重放不增加 POST；真实并发改槽/删消息后新图留历史，意外多图保留协议诊断。当前底图删除、跨角色底图和修改后的模型白名单在新的消费预占前拒绝。

真实主应用 lifespan、公开 `/messages`、localhost OpenAI SSE 与独立 NovelAI HTTP 证明：background=true 返回 accepted 后父回合已完成，供应商夹具仍阻塞；释放后原桌面唯一 internal_completion，无伪造 user/run。实际角色锁阻塞发布，解锁后只发布一次；两角色的已收图结果不因另一角色忙锁而饥饿。父 token 取消不撤回已受理后台，精确停止、真实角色 API 撤权仍 seal、stop 并 join。实际媒体能力关闭的 adapter 在生成 POST 前拒绝，真实主人解绑后重绑定不会恢复旧来源资格；两项追加协议检查 2 passed / 8.08s。后台交接 taskId 的 SQLite trigger 失败不假报 accepted；ACK trigger 失败保留真实 Runtime retention，恢复只修本地并释放保留。

真实 OneBot WebSocket 群主人输入先收到普通受理文本，之后仅一次媒体投递到原群，保留原 reply/mention；公开 HTTP 重生成通过同一 sender 发送新媒体，原消息只换所选图片，原 attempt receipt 保留。真实 socket 收到 media 后丢回执为 uncertain，不重发。receipt/消息 SQLite 故障后重建整个业务应用，sent journal 恢复原事实；只有 sending 而没有 receipt 的重启明确为 uncertain，原消息投影也为 uncertain，零补发。前台生成成功而最终模型回复失败，则仅在原角色锁释放后、同一未消费 outbox 内转原来源完成消息。

专项首次完整 **115 passed / 114.66s**，其后补充的当前授权检查 **3 passed**；不能把这两轮相加当作某一准确版本的完整统计。后台/receipt 重启与故障合组 **13 passed / 25.77s**，前台 fallback 与原消息/渠道/恢复组 **14 passed / 37.03s**。首次组装把私聊按群回复段断言是夹具错误；改为实际支持该上下文的群协议后验证原 reply/mention，未放宽生产安全校验。

React 新增选槽、再次消费、稳定请求重试、后台观察/精确停止、冲突/仅历史选择、A→B→A/卸载迟到隔离、停止后的旧查询与历史读取故障等行为，并用完整 App 证明后台完成实际刷新原会话。图片已生成但等待发布时也可以精确停止；此新增边界先 RED 再 GREEN。当前前端 **194 passed / 33 files / 28.24s**、TypeScript/Vite 通过，之前 193 项记录保留为先前检查而不相加。

旧 #171 请求按原参数指纹重查保持兼容；首次新指纹包装导致旧请求422的真实 RED 已修正。新动作选中已经被替换的旧槽在消费前409拒绝，首次会多发第三次生成POST的真实 RED 已修正；同 requestId 重查仍返回旧事实，出网后才发生的槽位竞争仍保留新图历史。兼容、槽位和取消/后台故障的定向组合 **18 passed / 30.65s**。

最终严格受管组合共51个文件 **579 passed / 345.81s**，其中 NovelAI 专项 **123项**，其余456项覆盖现有渠道发送/receipt/作用域、TaskRuntime交接、spawn/schedule/桌面推送和角色能力。完整三 MEIDO 临时根与整组/逐case网络 guard 均保持到 owned 资源退出。另一个后台命令旧HTTP夹具在全新进程单case重现默认自动记忆触发 dashscope 出网，被 guard 拒绝；仅给不测试记忆的 `api` fixture 注入临时 MemorySettingsStore(enabled=False)，保留全部原业务断言，Shell/Spawn API两文件 **39 passed / 45.62s**，不把该组合与579相加。

此前尝试整个 Python 目录时，共 **1931 passed / 14 skipped / 39 teardown errors / 0 failures，990.491s，退出1**；39项均由guard记录 dashscope.aliyuncs.com 尝试，分布于既有 HTTP 夹具，没有 NovelAI 用例错误。该轮是失败记录，不能称完整后端全绿，也没有清空blocked或修改记忆生产策略。只在本票修正上述后台命令夹具，其余共享隔离由总集成协调；真实网络均在连接前拒绝。

新模块 11 源 mypy `--follow-imports=silent --check-untyped-defs`、compileall、相对链接与 diff check 通过。独立两轴和准确总集成 CI 以固定提交后的记录为准；本段不预报 CI、真实 NovelAI 或 #173 已通过。生成/模型调用全部是 localhost 夹具，真实费用为零。

### #171 历史记录

首次专用整组70 passed（48.54s）；随后增加 receipt/消息故障后，专用72项与共享角色/Runtime/渠道回归89项联合161 passed（77.00s）。最终扩大到 NovelAI 专用77项、角色/Runtime/渠道及全部 schedule/spawn/desktop_push 调用者，合计 **489 passed（212.21s）**，完整三数据根与整组网络 guard，JUnit保存在协调者外部证据目录。共享回归最初88绿1失败为旧的精确 agentConfig 默认值断言未包含新增 novelai；按真实默认扩展期望后通过，未放宽生产校验。

最终全前端 **30文件179项通过（23.97s）**，TS noEmit/Vite、Python compileall、相对链接及diff check通过。mypy 以 `--follow-imports=silent --ignore-missing-imports --no-incremental` 检查11个新增模块源文件，首轮6项诊断；修正 ZIP/JSON 字节变量静态类型、容器声明并显式检查 owned path字符串后全绿，没有忽略诊断。489项覆盖该静态修正前的模块，修正后专用 **77 passed（56.42s）**，不能把两轮结果相加；最后纯UI提示文案修正后专项5项及TS/Vite再通过。

固定初版 `7cf50b7` 的独立两轴审查发现同一项恢复公平性 P2：100条取消墓碑占满固定批次，后续 local_pending 永久被挡。原审查者的真实SQLite/localhost复现先 RED，修正后 GREEN（1 passed，8.57s）。新增 suppressed 和 Runtime 暂时停用两种100条阻塞回归均先 RED（第101条仍为 local_pending），再以恢复资格收敛和持久游标轮转转为 GREEN；不把暂时失权误判为 Runtime 记录不存在，恢复不增加 POST或消息。公开两模式补证2项通过（8.61s）；早期补证夹具误用 attachmentIds 被API忽略，按真实 attachments 字段纠正，未放宽当前人类附件授权。

修正后完整专用 **80 passed（82.53s）**，三数据根和整组 guard 持续至所有资源 join；11源 mypy、compileall、两文档相对链接及 diff check 再通过。本次没有改动前端或既有共享消息/授权模块，未重复全前端或489项扩大回归。

## 总分支集成回归

源 `b786a867` 汇入已验证桌面控制集成 `c052848`，共享配置同时保留 computerUse 与 novelai，角色更新合并三个能力子配置，关闭、删除、工具门禁、真实主人来源与各工具期限均保留双方约束。新增删除时序测试确认：进入任何可能挂起的生成清理前，记忆写入准入门必须已关闭；取消仍由原 finally 恢复门禁。原顺序得到 `[False]` 的真实 RED，门禁移到清理前后，新增回归与两个既有删除/取消测试 **3 passed / 8.64s**。

首次新增测试因 monkeypatch 保留到 fixture.close，使 close 调用签名不兼容，出现额外 teardown error；已将补丁限定在 scenario 生命周期，确认原 pytest 进程身份后终止其遗留 HTTP 线程所在进程，未操作其他服务。随后干净 RED 独立重现门禁问题，修正后 fixture 与进程均正常收尾。该首次失败和清理不计为通过。

首次集成的 NovelAI、桌面控制、角色能力交叉 **108 passed / 10 deselected / 96.19s**（`-k 'not native'`，不计原生验收），收集前设置完整三 MEIDO 根，但该轮旧角色能力模块没有整组网络 guard，相关区间外网边界未知，不能计为零外联验收。独立审查给全组加 guard 后，四个旧聊天能力测试尝试连接无关 dashscope 向量服务，被 guard 阻断；该轮保留为验收隔离P2，不以缩小测试范围解决。角色能力夹具现显式持有 guard，并使用本地关闭的自动记忆设置，既有目录、schema、配置和回复断言未改。

修正后原交叉组加两个既有删除回归，在整组 `local_platform_only` guard 下 **110 passed / 10 deselected / 91.93s**，完整三 MEIDO 根且 guard 持续至全部 owned 资源退出；角色能力单独7 passed。前端 NovelAI/ComputerUse/角色能力 **9 passed / 6 files**，TypeScript/Vite、11源 mypy、compileall 通过。总第三方声明补入固定请求常数来源。完整集成 CI 仍以提交后的准确 HEAD 为准，未使用真实 NovelAI 服务。

未声明本地全量 Python或总 PR 两套 CI已通过；修正固定点仍由原独立两轴审查者作增量复核，本文不提前声称通过。

未验收：真实 NovelAI 账号/模型/图片尺寸兼容、真实 Anlas费用、取消后实际扣费，以及非本轮OneBot peer覆盖的实际平台媒体行为。受控成功不能称真实NovelAI已可用；本票没有取得真实费用许可，不索取或读取用户token。后台/重生成的 #172 本地验证见下方增量，CG 仍由 #173 依赖前票准确集成 CI 后推进；不拿受控成功替代真实费用验收。
