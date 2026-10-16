# 主动来源作者流程本地验收

状态：当前 / #178 本地实施；独立两轴复核、总分支集成和准确HEAD CI另行记录。

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

同一最终作者/context/旧Skill组合在WSL现成Python 3.12环境 **31 passed/1条Starlette依赖弃用warning（54.03s），runner exit0**，无skip；不是新增31项唯一案例。Windows使用Python 3.13，三新根与内外guard两平台均至所有owned退出。WSL启动另提示宿主localhost代理未转发，未影响这些stdio/SQLite测试。

最终完整前端 **190 passed/34文件（27.22s）**，TypeScript/Vite通过。20源类型检查准确参数为`--check-untyped-defs --ignore-missing-imports --follow-imports=silent`，覆盖主动模块、自主MCP与共享Agent/TaskRuntime；compileall、文档相对链接及diff检查通过。更宽的follow-imports模式追入其他模块报136项，含新context变量缺注解已修；不据此声称全仓严格类型干净。

每轮收集前隔离全部三个MEIDO数据根并移除PROJECT_ROOT，内外local-platform guard持续到App、MCP、Runtime、线程与进程退出。成功轮无非本地阻断；早轮被阻断的尝试不改写为零尝试。全部模型是scripted本地fixture，来源为合成stdio/SQLite，不使用真实账号、远端ACK、付费模型或外部发送。
