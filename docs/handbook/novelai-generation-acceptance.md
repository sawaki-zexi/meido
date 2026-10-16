# 验收 NovelAI 前台生成与历史

状态：当前本地受控验证，真实 NovelAI 未验收。最后核验：2026-10-16。事实来源：[Reference](../reference/novelai-generation.md)、[公开消息用例](../../tests/python/test_novelai_main.py)、[真实渠道用例](../../tests/python/test_novelai_channel.py)、[真实断连](../../tests/python/test_novelai_cancellation.py)、[恢复](../../tests/python/test_novelai_recovery.py)。Parent #137，当前票 #171；#172/#173 尚未由本票交付。

用户已明确批准完整具体规格第3节的实现及本地无费用夹具；真实 token、账号、模型/生成费用和外部发布另批。本票从获准的已验证 aggregate `30002a9a8de551a12a44b2c2f5724d5640aca50d` 开始，分支 feat/171-novelai-generation，统一整合总 PR162，不新建堆叠 PR，不合并或关闭 Issue。

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

真实 main `/messages` → localhost OpenAI SSE provider → 宿主冻结 generate_image → 真实 TaskRuntime → 独立 localhost NovelAI HTTP201 ZIP → 工具安全引用 → 下一次 provider → 唯一完成回复附件/下载 → owned 历史闭环。provider 路径和生成路径为两个真实 HTTP 服务；不使用真实模型。

真实 OneBot WebSocket 主人入站、NapCatAdapter/ChannelHost、原QQ目标单次文本+PNG、真实 socket 回执；真实断线丢回执 uncertain 不重发。SQLite触发器分别使 receipt 和消息metadata写失败，恢复只同步已有 receipt/原消息，不降级已知 sent、不再次 send/POST、不改投桌面。

真实 Uvicorn HTTP socket 断开恰逢本地图片导入时，先封存发布资格，Runtime停止/join，迟到成功只留历史；recover 不能重新插入 UI 消息。真实角色许可撤销、关闭再开启 ABA、最后 INSERT 前撤权、角色删除及应用 close 都有行为证明。

HTTP200/201、401/402、408/429/500、坏 ZIP、严格 JSON base64、实际像素超限、17项 ZIP、实际解压超过64MiB、无 Content-Length 响应超过32MiB、Content-Encoding gzip、原始deadline结束，均不自动重发。输入secret、rawpath/base64不进正常工具结果/历史JSON；协议异常保留消费可能说明。HTTPX transport 明确 trust_env=false，测试用无效 SSL_CERT_FILE/本地代理证明不误读进程环境。

真实文件 metadata rename 失败、已导入图片后状态提交失败、Runtime交接 taskId 回写失败、canonical outbox 回滚/重复消费都先 RED 再 GREEN。恢复复用既有二进制、先持久 staged 引用再删除 spool；账本找回同一 Runtime handover并 ACK，真实 retention 唯一槽可以随后释放。有效先导入图+坏后续项保留历史并按实际占用结算。

React 覆盖真实 App 设置/角色/历史入口、有限消费显式确认、只打开不POST、网络失败保持原requestId重查、切角色 abort/忽略迟到图。正常界面不会展示凭据、base64或本地路径。

## 本轮记录与限制

首次专用整组70 passed（48.54s）；随后增加 receipt/消息故障后，专用72项与共享角色/Runtime/渠道回归89项联合161 passed（77.00s）。最终扩大到 NovelAI 专用77项、角色/Runtime/渠道及全部 schedule/spawn/desktop_push 调用者，合计 **489 passed（212.21s）**，完整三数据根与整组网络 guard，JUnit保存在协调者外部证据目录。共享回归最初88绿1失败为旧的精确 agentConfig 默认值断言未包含新增 novelai；按真实默认扩展期望后通过，未放宽生产校验。

最终全前端 **30文件179项通过（23.97s）**，TS noEmit/Vite、Python compileall、相对链接及diff check通过。mypy 以 `--follow-imports=silent --ignore-missing-imports --no-incremental` 检查11个新增模块源文件，首轮6项诊断；修正 ZIP/JSON 字节变量静态类型、容器声明并显式检查 owned path字符串后全绿，没有忽略诊断。489项覆盖该静态修正前的模块，修正后专用 **77 passed（56.42s）**，不能把两轮结果相加；最后纯UI提示文案修正后专项5项及TS/Vite再通过。

未声明本地全量 Python或总 PR 两套 CI已通过；独立两轴审查针对固定实现提交由协调者安排，本文不提前声称通过。

未验收：真实 NovelAI 账号/模型/图片尺寸兼容、真实 Anlas费用、取消后实际扣费，以及非本轮OneBot peer覆盖的实际平台媒体行为。受控成功不能称真实NovelAI已可用；本票没有取得真实费用许可，不索取或读取用户token。后台/重生成和CG仍按 #172→#173 的完整依赖推进，不拿前台成功替代这两票。
