# 主动决策本地配置与验收

状态：当前 / #177 实施中，最终固定提交与独立双轴尚待完成。

事实来源：[当前契约](../reference/owned-autonomy.md)、[已批准规格](../specs/proactive-drift-spec.md)、`tests/python/test_proactive_*.py`。最后核验：2026-10-16。

## 先准备独立配置

角色资料中打开自主行为设置。默认主动决策和Drift均关闭；首次准备来源、保存与刷新不需要调用模型。实际开启主动决策前，先保存一个明确的模型连接，选择该连接并勾选有限操作许可。每天次数和token上限不是服务商费用估算，本轮验收只用localhost模型夹具，不使用真实付费连接。

先在MCP设置保存并信任本地来源，只给当前角色需要的服务。将具体来源许可数组填写到自主设置，核对当前配置/grant修订、具体工具名与定义hash、事件唯一域、channel和ACK契约后保存。来源默认关闭；不要把未验证缓存、群资料或另一个角色的服务当成当前授权。

正常启动后首个判断等待30分钟，且主人回合结束至少10分钟。最近运行区展示实际状态；来源不可用和无新事件不同，Drift在后续实现前显示尚未就绪。主人输入会同步封印旧运行；停止后若资源或结果仍待结清，状态不会提前假报完成。

可在当前角色工作区放置 `PROACTIVE_CONTEXT.md` 作为专用行为文本，严格UTF-8，最多64KiB且16000字符。它按实际tick冻结，运行中修改不会替换本轮文本，也不能授予新工具或身份。目录或文件链接被拒绝；读取失败会标error，仅同角色实例和工作区可保留已读文本，缺失则清空。

## 明确运行来源契约测试

仅对已经信任的本地Python/Node来源进行测试。准备 `meido-source-contract-v1` 验证清单，声明固定入口和支持文件的SHA256；自行核对整个清单摘要。程序必须实现固定测试模式和 `meido_test_seed`/`meido_test_clock`，在测试数据中持久ACK，不接真实账号。

在来源ACK契约验证区选择已保存的来源，填写绝对清单路径和精确SHA256，勾选信任并运行该本地产物，再提交。普通配置保存不启动验证。验证运行独立TaskRuntime，最长120秒；网络断线重试保持原请求身份，未结清的旧职责不会换nonce绕过。

通过后点击使用验证结果，它只把proof引用填入来源草稿。再次保存自主设置后，后续新运行才能使用证明。单纯声明absolute-idempotent或粘贴任意proof ID不授予重试。验证失败、取消或清理未确认会显示真实状态；不要清空错误或删除未知记录来假装已经退出。

测试源码拥有同账号代码能力；最小环境和临时数据不是OS沙箱。artifact/state写入有预留及观测预算，观测前可能短暂超过。不能把本地夹具证明推广成任意远端来源都具有同一ACK契约。

## 隔离与核验

收集或导入main前，将 `MEIDO_DATA_DIR`、`MEIDO_ROLES_DIR`、`MEIDO_OWNER_KNOWLEDGE_DATA_DIR` 三项都指向本次新建临时根；不以PROJECT_ROOT代替数据隔离。对本地组强制 `-o usefixtures=local_platform_only`，保持guard至模型、MCP、Runtime、HTTP服务器和所有owned线程/进程完整退出。不得在中途撤guard或清其阻断记录。

主要行为文件分为配置/时钟准入、静态来源/正文、真实MCP权限、工具协议、实际记忆/网络依赖、投递/ACK、交接/恢复、存储保留、主人优先、原loop清理、来源契约和托管验证。最终固定提交需整组执行，再做共享Runtime/角色/删除回归、类型检查、前端及构建，以及独立Standards和Spec审查。前轮数量存在重叠，不相加充当唯一通过数。

## 已取得的分轮证据

来源契约模块完成实际双进程测试：接受ACK但丢回执、重启后取回原回执、原期限不延长、有限到期/永久保留及产物/权限变化拒绝。原先仅凭契约字符串允许第二请求的P1探针先RED，加入宿主持久proof后关闭；专用22 passed/46.13秒，相关24 passed/23.07秒，最后publication边界另3 passed。

托管验证的原loop清理发现真实RED：取消落在清理重试sleep会使loop提前退出。改为独立owned清理task并屏蔽取消至实际退出，7 passed/12.39秒；完整wrapper/contract组25 passed/26.13秒，ACK/恢复/准入交叉22 passed/38.03秒。首次RED保留，不冒称原设计本就通过。

网页信任、来源选择、摘要、显式使用proof和不自动保存的行为3项通过，另首次读取失败不能原地重试的RED补齐后，网页自主专项4项通过。API拒绝隐式信任/伪nonce/digest/未知字段/超长body共8项通过。真实owner HTTP配置→来源双stdio→TaskRuntime→proof/原请求重查→显式保存且零聊天消息，合计API9项通过。最新前端完整27文件178项（24.48秒）及TypeScript/Vite通过，取代前轮177项证据。

真实工具协议17项通过（35.33秒）；真实MemoryService/DefaultMemoryEngine、SQLite只读召回、当前角色/来源、SSRF拒绝、冻结与撤销许可13项，与协议联合30项通过（61.38秒）。初始依赖测试中的同步HTTP自等待造成fixture死锁，改为独立主人actor后通过，未修改生产；保留错误记录，不算生产修复。

配置替换也纳入同事务存储检查：新回归先RED，配置/保留22项通过（7.19秒）。24小时正文及7天transcript到期仍保护未知发送/ACK职责，终态恢复释放预留但不遗忘墓碑，相关真实SQLite故障回归保留。

MCP资源采用 #169 已双轴复核且两条完整CI通过的 `c052848` 两个共享文件，精确保留exit_confirmed与failed-close原loop重试。只合入资源接口，不开放桌面控制或普通owner权限。合入后的MCP/原loop/来源契约/托管验证/HTTP交叉43 passed/41.17秒。先前基线300无法重试底层failed-close的限制不被倒写为当时已支持。

固定上下文与决策/协议/配置/真实HTTP交叉36 passed/1 skipped（58.28秒），包含真实junction/hardlink拒绝、双tick固定版本、恶意shell指令不能扩权、16000中文触发总输入预算且provider零请求。skip仅为当前Windows无创建symlink权限，不计原生通过；首次参数ID/合法角色profile/Windows句柄时间语义夹具与边界失败均保留。

以上仍是分轮作者/协作证据，不是最终固定177提交的整组通过、两轴审查或总PR完成。无真实账号、模型/embedding费用、来源业务事务或外部发送验收。

## 固定提交前的共享检查

扩大到全部主动模块、Runtime交接、旧agent runtime、MCP来源/stdio/schema及删除回归时，首轮 **227 passed /1 skipped /1 teardown error（237.33秒）**。唯一error是旧HTTP/SSE兼容测试隐式触发无关dashscope向量连接，被整组guard阻断；原SSE/run/消息断言均通过，但该轮不报全绿。该用例现显式持有guard，使用临时关闭的自动记忆设置；保留原断言。它与poll、原loop/MCP及旧删除/取消相关 **14 passed /17.08秒**；固定 `1fecd40` 的完整同范围随后重跑为 **227 passed /1 skipped（244.66秒）**，整组guard持有至所有owned工作退出。

首次带check-untyped-defs的20源mypy发现7项新诊断。补齐存储和权限容器类型、快照允许布尔字段、返回实际owned关闭Task，并明确验证poll的非空定义后，20源全通过；compileall、5份Markdown19个相对链接及diff检查通过。上述14项也覆盖这次关闭/poll收窄，未放宽授权或预算。

该固定版本的独立规范审查发现排队渠道输入在解绑后仍会触发主人优先；规格审查发现最终消息INSERT缺少紧邻授权复核、首次clock延迟时判断早于首poll、同源配置保存绕过150秒间隔，共四项P2。真实排队配对与SQLite撤权回归先 **2 failed /1 passed（6.85秒）**；将配对检查移入获锁后实际接受回调，并保留与先行生图一致的最终INSERT授权检查后，相关主人优先/投递组 **6 passed（10.71秒）**。原RED记录不改写为通过。

poll的两个原stdio探针先 **2 failed（9.17秒）**，canonical首poll与150秒回归分别先RED。持久初始poll身份仅在真实执行、终态副本、owned清理和Runtime ACK之后打开当前代门禁；普通保存、来源变版、停启和重启都保留原150秒边界。首次clock迟到时先等该门禁，下一个真实clock准入决策，不额外延后30分钟。原外部首poll探针只recover不推进clock，保留原RED，作者副本增加真实on_clock后两探针 **2 passed（7.35秒）**。

检查同时发现失败poll+空get曾错误进入Driftpending，实际RED保留。现在按来源版本与generation持久并冻结poll健康状态；独立来源不短路，失败不删除已取得候选，其他成功来源仍可A01，后续真实成功poll才恢复可信空。实际两来源、共享poll一次、慢stdio、Runtime拒绝、旧schema迁移、真实SQLite回滚及交接等交叉 **41 passed（65.27秒）**。混合来源测试曾把既有四步模型循环误写为三请求而失败，修正测试计数后通过，没有放宽生产协议。

全部修正后的扩大原范围加新的宿主授权回归 **244 passed /1 skipped（275.15秒），进程exit0**。三MEIDO根在导入前隔离，整组guard保持至所有owned退出；skip仍只为Windows创建symlink权限。20源 `--check-untyped-defs` mypy、compileall与diff通过。前端未变，沿用此前178项及TS/Vite证据，没有声称本轮重复。当前仍需固定修正提交的独立双轴增量复核与总分支集成CI。
