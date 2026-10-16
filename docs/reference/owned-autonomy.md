# 主动决策与来源验证契约

状态：当前 / #177–#179已进入准确312并通过两套完整CI；#180外部目标整合为源码头0acfb4f，源/修复/总集成两轴无遗留，本地115项交叉、完整前端252项及构建通过。新准确HEAD两套CI另行核验，仍未合入main。

事实来源：`backend/app/proactive_runtime/`、`backend/app/mcp_autonomy.py`、`tests/python/test_proactive_*.py`；架构见 [ADR-0018](../adr/0018-explicit-autonomy-and-drift-grants.md) 与[规格](../specs/proactive-drift-spec.md)。最后核验：2026-10-17。

## 配置与实际身份

`GET/PUT /api/roles/{roleId}/autonomy` 管理当前角色的独立配置，PUT 必须给 `expectedRevision`。默认主动决策和 Drift 均关闭，没有继承全局模型、历史主人身份或普通聊天工具许可。开启主动决策必须指定已保存的 `decisionModelConfigurationId` 与 `consentRevision>=1`。A01和Drift均已实现，默认目标仍为角色桌面；#180支持从真实目录显式选择本角色已绑定主人私聊及至多四个许可目标。模型只见原run冻结的最小目标投影，完整二元组只能选原许可；配置、投递与真实验收边界见[目标手册](../handbook/autonomy-channel-targets.md)。

`sourceGrants` 最多8项，冻结 sourceId、revision、channel、serverId、MCP配置及grant修订、get/poll/ACK工具名和完整定义hash、eventDomain、ACK契约、可选proof引用。来源变更、删除重建生成新版本；只填写新的 `ackProofId` 不改变来源版本，但改变角色许可代际。保存配置只写本地，不能连接来源或测试发送。

时钟在真实准入事务创建独立 `AutonomyOrigin`，绑定 role/tick/config revision/generation/allowedModes；它不创建用户消息，不继承任何聊天run或owner_context。自主MCP只使用冻结的具体操作，不能打开普通owner入口或获得其他服务的连接意愿。

每30分钟判断一次，主人回合结束至少10分钟后才允许准入；启动/重启等待一个间隔，没有补跑。每角色每天最多24次、全应用120次。真实宿主接受主人输入时同步封印原代际，普通聊天不等待网络收尾；原自主资源仍由宿主持有并回收，未确认结束则阻止下一自主准入。Stop不追溯撤回已提交的远端事务。

## 快照与决策

poll使用单独受管任务，每角色间隔至少150秒、每天最多576次、每任务120秒；同一server/poll组合一次任务只调一次。硬间隔按持久准入时刻计算，同源保存、来源变版、停启和重启均不提前，旧库从实际poll历史补齐边界。新poll准入的同一事务立即以原tickId将本次来源健康标为incomplete，上一次成功不能证明这次刷新已完成。

若当前角色有已准入而未完成结果ACK的poll，到期判断保留原资格，等待该poll实际终态、owned收尾与Runtime ACK，再由下次真实clock准入；不会额外推迟30分钟或重放poll。启用或重启后的首判断还绑定当前代实际执行的初始poll；仅submit或被拒绝不算首次完成。失败终态允许继续读取其它来源，但失败/未完成健康不能证明可信空；无poll来源不增加此门禁。

tick只get一次形成静态快照，候选/正文重读不再访问来源。poll保留每来源有界健康状态，独立来源不因另一来源失败而跳过；当前代及来源版本匹配的失败/未完成状态随tick冻结，后续真实成功poll才清除。失败或不完整不视为可信空集合，不能据此进入Drift；仍保留已经取得的候选供A01判断。正文失败同样保留候选，不能隐式判为不感兴趣。

主动决策固定12项专用工具：get_alert_events、get_content_events、get_context_data、recall_memory、get_content、web_fetch、web_search、get_recent_chat、message_push、mark_interesting、mark_not_interesting、finish_turn。这些不是向普通聊天额外开放的通用工具。一次provider回复多个tool calls时没有任何一个被执行，纠错仍消费预算。

原角色资料与明确模型连接被版本冻结。记忆使用真实引擎只读效果、当前角色桌面来源、最多2项；web同时受被冻结角色许可、当前许可和现有网络/SSRF规则约束。最近聊天只含最后20条普通已完成桌面消息，排除自主、内部完成和旁听。

当前角色工作区的固定 `PROACTIVE_CONTEXT.md` 由宿主只读，最多64KiB/16000字符，拒绝路径链接、junction、reparse及hardlink。文本、状态和digest在准入时冻结；中途修改只影响下一tick，读取失败仅可沿用同角色/同工作区/同角色实例的已读文本并标error，缺失清空缓存。角色删除清除其缓存。预览和默认关闭时不读文件，文本不授予任何身份、工具或发送许可。

message_push只能创建一份不可改写草稿。finish_turn(reply)需要草稿，skip需要没有草稿且理由合法；同参finish返回原收据，冲突拒绝。取消、缺finish或预算耗尽不会自动发送或推断不感兴趣。去重检查包含24小时归一文本/URL与最近5条主动消息的有界语义判断，检查不可用时不发送。

桌面消息、投递intent/收据、事件占用、ACK决定写入同一原会话事务。它是内部完成消息，不伪造主人输入。业务幂等键不含callId、发送模式或重试序号，查询、重复finish和本地恢复不会追加第二条消息。

## ACK 与真实契约证明

每条ACK固定原来源/事件、ackId、decisionAt、绝对expiresAt或永久标记以及理由。引用content为168小时、未引用但interesting为24小时、明确discarded为720小时；引用alert永久，context不ACK。skip和确定失败只处理明确discarded；不确定发送不作成功ACK。

仅声明 `absolute-idempotent` 不授予重试。没有有效宿主proof的来源只发一次，回执不明记 `ack_uncertain`，本地抑制仍保留。有效proof绑定完整来源、MCP配置/grant/工具hash、事件域、固定本地产物与解释器digest、验证器版本；任一变化均失效，旧tick不能从后补proof扩权。通过证明才允许固定三次ordinal：立即、1分钟、5分钟，最晚原决定后10分钟且未过原expiry；最多512个ACK请求/角色/UTC日。每次保持原ackId/expiry，不重做已消费的ordinal。

旧 `legacy-ttl` 接口最多调用一次；有限期限按剩余完整小时向下取整，少于一小时不发，永久仍为ttl_hours=0。未知回执不视为服务已处理。

## 主人本地产物验证

`POST /api/roles/{roleId}/autonomy/source-verifications` 只接受真实桌面主人操作：requestNonce（UUID）、sourceId、expectedRevision、manifestPath（绝对路径）、manifestSha256（精确SHA256）、trustLocalCode=true。它不作为模型工具或渠道能力提供，不创建聊天消息。同角色同nonce参数不变只查询原attempt，冲突拒绝。

`POST .../source-artifact-preview`只接受当前角色workspace内相对manifestPath和可选已保存serverId，请求最多4KiB。只读返回实际文件size/SHA、当前清单SHA、manifestReady及可复制修正清单；不创建目录、写文件、执行、读取vault、注册或授权。选择服务后提供当前config/grant和声明catalog绑定的disabled来源草稿，实际握手仍必须匹配。修正清单由既有文件工具或主人保存后再次预览。作者流程见[手册](../handbook/proactive-source-authoring.md)。

新增`meido-source-author-v2`/version2使用同一owned verifier，按实际channel验证稳定ID、未ACK重读与重启、合法empty和明确读取失败；content检查正文关联，context检查背景数据。proof分别返回collectionVerified与ackVerified；纯context可验证且不需无关ACK，也不能取得ACK重试资格。旧v1继续仅表示原ACK契约，不追认完整collection验证。v2事件来源仍经过以下全部原ACK及资源/权限证明。

旧ACK manifest使用profile `meido-source-contract-v1`/version1，新作者清单使用`meido-source-author-v2`/version2；均声明entrypoint和相对支持文件的SHA256。最多32文件、合计8MiB、清单64KiB；拒绝链接、junction、hardlink、逃逸、重复字段或未声明入口。入口必须精确对应既有已信任MCP命令的Python/Node脚本，仅保留有限安全解释器前缀。验证丢弃生产后缀参数、环境和vault，复制固定文件到独立临时artifact/state/home，以 `--meido-contract-test <stateRoot>` 启动。

宿主随机种子事件与ACK ID、控制测试时钟；真实接受ACK后模拟丢回执，再重启来源验证抑制和原回执持久、重复不延长、有限期到期与永久记录保留。两个进程及stdio完整退出、临时根清理成功后，才在最终事务复验许可/产物/期限并发布proof。自报“verified”不算证明。

验证使用独立 `autonomy_verify` TaskOrigin/原请求key、120秒Runtime、16KiB日志、4000字符输出，保留结果直到业务ACK。持久接受记录先于submit；返回或taskId保存丢失只查回原提交，不重启验证。结果副本与dirty同事务，Runtime ACK后才释放预留。清理失败保留原async loop与资源所有权；无可信退出证据的重启interrupted仍为待处理，不能伪造已回收。

`GET .../source-verifications` 返回当前角色最多100条记录；`POST .../{attemptId}/cancel`取消原操作。查询包含status/proof/runtimeAcknowledged/cleanupPending/error；cleanupPending可能表示资源或本地结果/ACK义务未结清，不能据此断言仍有活进程。网页将成功proof填入草稿，仍须显式保存配置后才生效。

本地测试会运行主人信任的代码，不是OS沙箱。宿主预留16MiB+64KiB，artifact最多8MiB、state/home观测上限8MiB/1024项，超限取消并收尾；监测存在采样间隔，不声称阻止同账号代码的所有副作用或瞬时超量。

## 配额与恢复

tick外层600秒、快照120秒、A01原始startAt后300秒；每provider与工具30秒。最多35步，每请求max_tokens<=1024、累计预留12288 token，单输入48KiB、累计512KiB。取消或缺usage不退已出网预留。这些是本地请求参数上限，不是费用估算。

本地逻辑存储全局1GiB，每角色最多20000条/16MiB元数据；新工作和配置在原事务检查配额。来源正文24小时、内部transcript7天、已解决详情90天；未解决投递、ACK、原接受和业务幂等墓碑不为腾空间删除。满时拒绝新工作并显示原因。

恢复只处理原任务结果、原投递与新的合格ACK ordinal，不重放旧模型、poll、发送或本地验证。跨库交接用权威 `find_submission`，disabled或不可读不是“不存在”。结果副本与待ACK标记同事务后才ACK，ACK成功而清标记失败可在原结果prune后重ACK，不重新query或执行原任务。
