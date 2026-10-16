# 主动来源作者流程本地验收

状态：当前 / #178 生产源0eb8ea4与后续ACK测试修正fb85d45均完成两轴复核；准确总分支的新CI另行核验。

基点：已验证aggregate `f7be50795a78f901b54a3efefa61db0317b00280`，独立`feat/178-proactive-source`；Shiori固定`4f266aeaf42fcd611362e2456417b91f659341cf`。最后核验：2026-10-16。事实来源：[HTTP/作者产物](../../tests/python/test_proactive_authoring_api.py)、[实际stdio快照](../../tests/python/test_proactive_snapshot.py)、[React](../../web/src/features/role-management/SourceArtifactPreview.test.tsx)、[操作手册](../handbook/proactive-source-authoring.md)。

## 实际闭环

真实`/messages`显式激活S08，scripted本地adapter实际取得Skill正文里的完整模板，由原FileTools写角色目录中的server/catalog/fixture/source-draft/manifest/README；后者记录Python 3.11+标准库依赖，无安装或Shell操作。支持reference只校验hash，不声称模型读取分发assets。

owner只读HTTP预览计算真实文件及清单SHA，修正manifest仍由原聊天FileTool写入。随后真实MCP配置HTTP保存固定解释器/入口/cwd和角色，预览仅准备disabled来源草稿，owner显式保存具体get/poll/ACK。原autonomy_verify通过真实owned stdio验证三通道，未启动正常state；不是直接置SQLite成功标志或替换执行器。

同一文件产物经三通道验证和显式proof/模型/许可保存后，真实宿主clock完成初始与周期poll、六步A01工具循环、关联正文读取、一条桌面canonical消息和两条真实来源ACK。content原expiry为decisionAt+168h，alert永久。关闭重建App及stdio后，原ACK仍抑制事件，context仍可读取；原ackId/decisionAt/expiry不变，无第二次模型/发送。

## 有效与失败的区别

- S08不存在时真实对话先assistant_failed，加入合法Skill后实际文件写入通过。
- 预览原404后补公开接口；绝对/父级/跨角色路径、hardlink、缺文件、超量、重复JSON字段和额外授权字段均拒绝。缺legacy workspace不会mkdir。只读期间同路径换成相同字节的新目录原200，真实RED后加入workspace/artifact目录身份复核，相关10例通过。
- v2三通道原先失败：旧profile不接受新产物，context还被原ACK限定拒绝。扩展同一个verifier后通过；v1不追认collection证明，context不取得ACK资格。
- 合法无event_id/kind的context原被新validator过严拒绝，真实RED后只冻结合成背景数据；flat context业务字段原被normalize丢弃，真实RED后保存在host context.data。真实stdio的dict/list两格式均保留背景数据，host身份和大小检查继续生效。
- 真正破坏的源码产物（吞掉读取错误、正文关联错误、ID不稳定、重启丢待处理数据）均实际运行后失败，无proof、无正常state副作用。
- 早轮闭环曾把宿主clock固定在2026年5月，而正常server正确使用当前UTC，有限ACK已过期；只将fixture初始clock对齐实际time.time，未修改生产TTL。
- 最终组合曾因测试helper直接query写事务撞合法50ms锁而30通过/1失败。改为同ID真实owner HTTP观察，503按原产品契约有限重读；不改数据库超时/恢复/准入、nonce或执行次数。
- 初轮扩大组218通过/4跳过/3 teardown错误，旧Skill fixture试图访问dashscope，inner guard实际阻断2/1/1次。保留原失败，不撤guard/清记录。此为#175已审同因隔离修复，在本票旧基点逐字复用其临时disabled记忆设置与显式guard，未改变生产记忆或原断言。

## 检查范围

主动、ACK/恢复、受管验证、TaskRuntime交接和Skills扩大组第二轮 **221 passed/4 skipped（320.96s），runner exit0**；该轮启动早于最后目录身份叶子修正和六文件说明补齐，不冒称整个最终版本重跑。最终作者、context实际stdio快照及旧Skill组合 **31 passed（69.79s），runner exit0**，覆盖这两项增量；各组重叠，不相加为唯一总数。跳过为Windows链接权限及未启用的旧公开CLI/网络验收，不算原生通过。

上述作者源9484156的同一作者/context/旧Skill组合在WSL现成Python 3.12环境 **31 passed/1条Starlette依赖弃用warning（54.03s），runner exit0**，无skip；不是新增31项唯一案例。Windows使用Python 3.13，三新根与内外guard两平台均至所有owned退出。WSL启动另提示宿主localhost代理未转发，未影响这些stdio/SQLite测试。

作者源9484156的完整前端 **190 passed/34文件（27.22s）**，TypeScript/Vite通过。20源类型检查准确参数为`--check-untyped-defs --ignore-missing-imports --follow-imports=silent`，覆盖主动模块、自主MCP与共享Agent/TaskRuntime；compileall、文档相对链接及diff检查通过。更宽的follow-imports模式追入其他模块报136项，含新context变量缺注解已修；不据此声称全仓严格类型干净。

每轮收集前隔离全部三个MEIDO数据根并移除PROJECT_ROOT，内外local-platform guard持续到App、MCP、Runtime、线程与进程退出。成功轮无非本地阻断；早轮被阻断的尝试不改写为零尝试。全部模型是scripted本地fixture，来源为合成stdio/SQLite，不使用真实账号、远端ACK、付费模型或外部发送。


## 独立审查与修正

固定源9484156的Standards发现P3：预览422错误用status呈现，未按前端规范使用可关闭Notice。真实独立DOM反例失败后，3d2bb76仅4+/3-替换提示；原反例未改加真实关闭操作独立2 passed/16.91s，关闭后原路径和按钮保留，没有导入trust/grant。相关React8项及TS/Vite另通过，不把旧190项冒称修正后重跑。

固定3d2bb76的Spec发现P2：同一产物只把返回键id改为合法event_id，真实stdio与运行时均能读，后续ACK verifier却只取id。独立alert/content两例2 failed/23.85s，proof为空，原Runtime ACK/cleanup正确完成。0eb8ea4将ACK观察统一走同一_normalize/remoteId并拒绝incomplete，保留v1 collectionVerified=false以及原ACK接受丢回执、固定expiry/原receipt、重启/永久记录。新增两个真实stdio→ownerHTTP回归。

原独立反例SHA256 16CD5C0F2188EEF25F62D907E749141B095552B2C4C520C3F1783205559B87A4不变，修后协调者2 passed/22.10s、Spec另独立2 passed/22.55s。准确修正输入的author/source-contract/source-verification/API/snapshot整组70 passed/166.99s；各runner exit0，保留1条已知conftest显式plugin重复导入warning，三个新数据根与内外guard持到统一all-owner collector结束，零非本地阻断。单源check-untyped-defs/ignore-missing-imports/follow-imports=silent、compile/diff通过。各组重叠不相加；没有为此重跑旧WSL31组或完整190项。

最终源0eb8ea4的Standards/Spec完整审查及各次增量复核均无遗留。主线同步c83bbc2另获双轴和完整前端204项/构建通过；源三提交增量进入总分支，仅总状态一处内容冲突按最新S07/A01与S08分别保留，旧builtin fixture逐字相同无重复更改。main相对c83仅新增自主路由workspace_getter，生成、Coding与owner关闭不重写。准确集成的行为组合、前端和两套完整CI另行记录，不以源验收或13c绿色替代。

审查外部夹具的公共祖先WinError1337、误指定不存在配置、查询不存在TaskRuntime方法、Settings可访问region查询错误和conftest收集重复等启动/断言装配失败均保留原日志；它们与上述真实P3/P2产品RED分开记录，未改生产或期望规避失败。真实账号/ACK/模型费用/平台收件仍未验收，总PR未获合并或剩余Issue关闭授权。

## ACK 测试时序修正

Windows组合实际运行ffd4108，其后04afa68只改文档；该组为523 passed/1 skipped/1 failed（806.66s），唯一失败在10秒内未取得业务ACK，保留的真实状态为sources_unavailable、没有业务call或消息。隔离原用例及只读跨度观测各通过一次，但没有首次失败的实时跨度，冷启动越过夹具3秒预算仍是推断。04afa68的[PR CI](https://github.com/sawaki-zexi/meido/actions/runs/38885939851)后端2261 passed/35 skipped/2 warnings；[push CI](https://github.com/sawaki-zexi/meido/actions/runs/38885931884)为2260 passed/35 skipped/1 failed/2 warnings，业务ACK和原业务断言已经通过，失败在固定三次recover后Runtime结果尚未ACK。两次失败发生在不同阶段，不能合称同一历史根因。

受控慢连接实验先完成真实MCP connect，再仅延迟首次返回3.05秒：原3秒夹具与持久回归各实际失败，单独将本ACK夹具设为10秒后，同一探针通过且完整业务断言保留。另一实验暂持真实AutonomyExecutor.run已经完成执行和资源收尾的原outcome，证明业务ACK已可见时，同一原生任务仍running/未写终态/retain=1；原三次recover和永久回归均实际在最后Runtime断言失败。独立释放后，原任务和提交key经原恢复链路复制结果、清dirty、ACK并释放retention/存储预留，没有重做RPC或消息。

fb85d45只修改[ACK测试](../../tests/python/test_proactive_ack.py)：成功等待在原10秒总预算内同时观察业务与Runtime ACK，原9条业务assert的AST逐条相同；新增两项真实启动/晚终态回归。生产超时、恢复、任务与清理实现均无改动，失败finally的有界drain不能将失败变为通过。源Windows三例3 passed/1 warning（20.71s）、ACK/来源契约/验证三模块40 passed/1 warning（120.91s）、WSL同三例3 passed/2 warnings（18.03s）；总分支f785f82同三例另为3 passed/1 warning（21.36s）。这些组重叠，不相加，也不替代新HEAD完整CI。三新数据根与内外guard保持至全部owner退出，原失败和warning均保留；两路独立delta审查无遗留。
