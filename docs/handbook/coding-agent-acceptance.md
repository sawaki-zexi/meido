# 验收 Owned Herdr #174

状态：当前本地受控验证；真实CLI账户、模型和权限边界未验收。最后核验：2026-10-16。事实来源：[公开HTTP原生闭环](../../tests/python/test_coding_api_native.py)、[原生宿主与两CLI夹具](../../tests/python/test_coding_herdr_native.py)、[真实OneBot来源](../../tests/python/test_coding_channel.py)、[资源所有权](../../tests/python/test_coding_owned_process.py)、[应用恢复](../../tests/python/test_coding_application.py)、[中断恢复和最终授权](../../tests/python/test_coding_recovery.py)。Parent #137；#175/#176 尚未由本票交付。

主人明确批准具体规格§3的实现、隔离构建与无账号费用夹具。本票以准确aggregate `30002a9a8de551a12a44b2c2f5724d5640aca50d` 为基线，使用已批准的本地任务分支流程，统一进入总draft PR162；不另建堆叠PR，不合并、不关闭Issue，不读取真实认证、不调用实际模型或发布。

## 准备隔离环境

在pytest收集/import前设置三个独立数据根，持续至应用/Runtime、原生Job、provider HTTP、OneBot WebSocket、avatar worker、日志读取线程和维护任务全部join。PROJECT_ROOT仅指源码，不替代owner knowledge根；新Coding测试启用local_platform_only，原生fixture脚本只执行固定本地动作。

```powershell
$testRoot=Join-Path $env:TEMP ('meido-coding174-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$env:MEIDO_DATA_DIR=Join-Path $testRoot 'data'
$env:MEIDO_ROLES_DIR=Join-Path $testRoot 'roles'
$env:MEIDO_OWNER_KNOWLEDGE_DATA_DIR=Join-Path $testRoot 'knowledge'
$env:MEIDO_PROJECT_ROOT=(Get-Location).Path
$files=rg --files tests/python | Where-Object { $_ -match 'test_coding.*\.py$' }
python -m pytest @files -q --tb=short -o faulthandler_timeout=60
python -m mypy --follow-imports=silent --ignore-missing-imports --no-incremental backend/app/coding_tasks
python -m compileall -q backend tests scripts
npm run test:web
npm run build
git diff --check
```

未显式提供原生fixture时，七项Herdr测试跳过，不能据普通CI声称原生通过。`faulthandler_timeout`只输出长用例堆栈，不是运行预算或跳过条件。

## 运行实际Windows原生夹具

先按[构建手册](coding-native-build.md)获得完整固定bundle。用私有rustc编译 [interactive_launcher.rs](../../tests/python/coding_fixtures/interactive_launcher.rs)，只输出到自己的工具链/fixture目录；同一产物复制为该目录的 codex.exe 和 claude.exe 供Herdr识别前台进程。它们运行 [interactive_cli.py](../../tests/python/coding_fixtures/interactive_cli.py)，不是真实Codex/Claude、没有真实登录或模型，原生拒绝JSONL是明确的合成证据。

```powershell
$env:MEIDO_TEST_HERDR='D:/isolated-herdr-bundle/herdr.exe'
$env:MEIDO_TEST_CODING_CLI='D:/isolated-tools/interactive-fixture.exe'
$nativeRoot='D:/isolated-tests/c174-'+[guid]::NewGuid().ToString('N').Substring(0,8)
if (Test-Path -LiteralPath $nativeRoot) { throw 'Choose a fresh native test root' }
python -m pytest @files -q --basetemp=$nativeRoot -o faulthandler_timeout=60 --tb=short
```

保持测试根简短，Windows原生session/worktree路径会继续增加层级；早期长临时路径曾真实失败，后使用全新短路径通过，不能据短路径成功宣称任意长路径兼容。测试不操作用户其他仓库或窗口。原生fixture每次使用新任务/Job身份，避免并发测试争用固定named Job。

## 已验证行为

- 独立Rust1.96.1/Zig0.16.0/MSVC工具链实际构建固定owned Herdr；固定补丁、二进制、ConPTY三个文件和完整notices逐一核验。首次Zig std失败原始日志保留，隔离cache和-j2后编译成功。没有全局安装或修改全局PATH。
- 网页保存/重开角色许可，默认关闭、保留其它配置及未知工具名，显式启用发现；主人配置固定宿主、专用profile、仓库后，只按明确按钮许可启动核验。程序不接受外部自报verified，未知版本/缺证据保持blocked。
- 真实公开/messages→localhost OpenAI SSE provider→tool_search同运行解锁→五个Herdr工具→实际私有server、worktree、CLI夹具、宿主命令与SQLite/TaskRuntime→原会话完成事件。首轮和继续使用同一native UUID/branch/worktree/base，主checkout未被改写；第三次受管hold取消后全部Job清空。六个主人回合、18次本地provider HTTP调用，没有额外付费摘要模型。
- 两种CLI分别走首次和精确resume的真实Herdr进程/集成hook，宿主核验两次实际修改及合成原生拒绝记录；证明宿主运输和资格流程，**不证明真实CLI权限、认证隔离或账号可用**。Codex的本地canary健康请求显式关闭环境代理。
- 实际OneBot WebSocket主人入站冻结来源；父回合结束后只向原QQ目标发一次完成消息并保存真实receipt。解绑后抑制交付，不投桌面。完成发送等待原角色锁，最后写入在事务内重验授权。
- 同调用返回原attempt，继续后重放旧调用仍返回旧attempt；真实SQLite证明Coding全局2/角色1/仓库1、持久日额度、任务/尝试界限。冻结检查到事务准入之间撤权不能借新代际复活旧schema。
- ACK已确认但dirty写失败且Runtime结果被prune，只对同一持久副本恢复ACK。实际MultichannelStore的sent回执丢返回后，本地uncertain投影恢复sent，发送次数保持1。
- 实际Windows进程在cleanup失败时仍被业务强引用拥有，后续准入冻结，终态副本/ACK仍可完成，随后重试确认退出；核验任务采用同样责任。另以未嵌套外层Job的真实宿主os._exit证明private named Job关闭、晚写后代没有逃出。
- 原始结果严格JSON、task/attempt/native binding和固定验证；迟到修改使验证失效，继续时HEAD偏离原base或复制的认证摘要变化均在prompt前拒绝。secret字符串包括短值会从摘要和Runtime尾部清除。受限CLI额外写入范围仅当前handoff；Claude dontAsk显式Edit规则限制worktree与该目录，不能把工具表面当写入授权。

## 验证记录

原生早期完整组27 passed（156.19秒），之后增加真实OneBot、许可事务/锁和收尾修正的整组31 passed（157.64秒）；追加当前handoff写许可及Claude scoped Edit模板后32 passed（163.28秒）。这些是连续版本的里程碑，不能相加，也不等于真实账户验收。补充继续HEAD和认证副本重验后的最终整组 **36 passed（166.11秒）**；最后仅将canary健康请求改为显式无代理，原生Codex核验在故意无效代理环境下独立 **1 passed/5 deselected（19.56秒）**。外部JUnit为 coding174-native-final-r4.xml 与 coding174-native-proxy.xml。

首次全Python在旧记忆删除顺序用例卡住，定位为新增Coding preflight过早yield，在原memory删除门禁前让迟到写入等待。只将Coding preflight移到memory begin之后、任何删除之前；相关2 passed（3.59秒），保留首次未完成记录。第二次全量 **1837 passed、16 skipped、2 failed（787.67秒）**：项目扩展扫描误将用户临时目录祖先的全局.codex当项目，以及旧精确AgentConfig期望未包含新增coding。修正为止于实际Git根并扩展准确默认期望，相关8 passed（14.12秒），未放宽配置校验。

第三次全Python **1844 passed、16 skipped、9 warnings（768.15秒）**，外部JUnit coding174-full-suite-r3.xml。该轮启动后又收紧Coding内部的当前handoff写许可、Claude scoped Edit、继续HEAD与认证副本校验；最终原生整组36项以及无代理核验定向覆盖这些后续改动，不能把较早启动的全量冒称最终HEAD全量。warnings包含依赖弃用及既有MemoryRecallTool coroutine未awaited告警，未据成功状态隐去。第一方共享入口在第三轮中已包含删除顺序与准确默认值修正；总集成仍须对准确新HEAD运行CI。

前端完整 **27文件176项通过（24.68秒）**，TypeScript noEmit/Vite通过；14个新增模块以以上准确mypy命令通过，compileall通过。mypy默认不会检查未注解函数体，不能据此称严格全类型覆盖。原生补丁及许可按固定字节保留；所有新增文档相对链接和diff检查另行核验。

独立规范/规格两轴审查由协调者针对固定实现提交安排，本手册不提前声称审查或总PR CI通过。全量、定向、原生各轮的覆盖不同，不能将失败轮与成功重跑相加成一次无失败全量。

## 独立审查后的恢复修正

固定首版87314b0的Spec审查发现三项P2：业务终态而真实named Job仍活时释放准入槽、预启动快照的第二事务失败留下永久accepted、最终消息INSERT前撤权仍写入。Standards另发现一次CleanupFailed查询错误会杀死全局worker并令close重抛。原始外部负向探针和两轴报告均保留；以下是作者修正证据，仍待独立增量复核。

资源未确认的所有attempt继续占全局/角色/仓库槽；终态资源义务冻结profile，并持续恢复扫描至真实Job消失。nativeBuild/授权代际现在与初始admit同事务持久化；同宿主准入、launch交接、取消和恢复串行，旧未交付记录仅收尾为interrupted或cancelled，禁止恢复重放和迟到launch。取消封住实际执行权限，完成投影仍独立按原来源授权。共享SessionStore只补最终INSERT前重验，与先行171修正相同，集成须保留该检查。

CleanupFailed按记录隔离后继续worker轮询；close消费已失败worker的可恢复异常，仍实际确认所有资源。持续无法确认Job时close保留数据库/清理责任并报错，恢复查询后可重试，不把错误视为已退出。

每项均先独立RED再GREEN：资源槽1f→1p；快照事务及旧未交付恢复/两种取消4f→相关组12p；最终授权1f→1p；worker瞬态1f→8p；持续未知close1f→8p；已有failed worker1f→恢复组9p。对应作者JUnit使用coding174-resource-slot-author、prelaunch-author、finalauth-author、worker-author、close-author、failed-worker-author前缀。外部仍适用的liveJob/最终授权/瞬态worker三探针 **3 passed（3.35秒）**；原prelaunch探针专门在已删除的第二UPDATE上注入故障，修后不再适用，改用初始INSERT/旧UPDATE双触发器验证完整回滚，再验证旧部分记录不执行即收尾，未把旧RED改写为成功。

修正首轮完整原生专用 **44 passed（169.48秒）**，coding174-recovery-native-final.xml；覆盖此前36项和新增8项，固定旧bundle、实际Herdr/两合成CLI/公开HTTP/原会话与渠道回执。该轮收集后又补close消费既有worker故障、closed-admission重验及claimed/unclaimed恢复参数，后续准确最终检查单独记录，不冒称该44项涵盖后补行为。14源mypy与compileall通过，类型检查仍沿用默认未注解函数体限制。独立审查本身首轮33p3f、中路径4p3f、极短路径三项3p已见两轴报告；长路径不是任意目录兼容的证明，仍保持前述原生路径限制，不据短目录GREEN消除这些失败。

全部后补行为后的最终原生专用 **48 passed（176.63秒）**，coding174-recovery-native-exact.xml，完整三MEIDO根与整组guard直至全部资源join；最终恢复专项12 passed（6.07秒），coding174-recovery-author-final-r2.xml。没有在此后修改生产代码；新增真实named Job恢复用例按与现有crash用例相同方式仅Windows执行，非Windows明确跳过该原生能力。旧fixed补丁SHA256仍为2a2df5e1626a1b37a89663ccb64bc11d3f8513bf6e44513041d9c29f8b98806f，bundle仍为445b03a54b314fffc6720b1229b3f6dda43bc42c28ddb4ec91c0f5144d25ce67，未借修复替换或跳过宿主验真。前端未改变，未重跑全Python/前端，不把先前全量/CI冒称修正头结果。

## 未验收与后续票

未使用真实CLI认证，未验证实际Codex/Claude模型首次/精确resume、原生权限拒绝、扩展配置/全局keychain不继承、费用或代码质量。生产profile在取得相应真实证据前保持不可用；当前产品已提供明确配置→主人核验→具体缺项/可用的流程，不用fixture标志绕过。后续真实核验需明确批准精确profile/版本/模型/宿主hash、两个native task turns及其中可能多次模型请求和可接受费用。

#175再接spawn coding/codex-delegate，#176再接确切成果批准的commit/发布；本票没有伪造这两项。没有执行真实认证、费用、push或PR发布，也没有取得总PR合并/关闭Issue授权。
