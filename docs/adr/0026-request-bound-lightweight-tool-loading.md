# ADR-0026：请求固定绑定与轻量工具恢复

状态：执行固定绑定与选择缓存决定已被 [ADR-0032](0032-current-tool-registry-and-raw-window.md) 替代（2026-10-30）；历史审计与真实来源、任务事实继续有效。以下保留原决定。

原状态：已采纳；2026-10-26 维护者明确要求“根据文档，完成开发；过程中时刻调研 pi agent，不要偏离轨道，简化实现；完成后提交一个总 PR”。批准覆盖 #229/#234 已确认边界内开发，不含物理清理旧历史、外部事务、自动合并。

## 决定与取舍

替代 [ADR-0011](0011-versioned-tool-activation.md) 的**持久完整工具目录版本链**，不撤销冻结授权、请求固定定义/执行器、来源绑定与及时撤权。

1. 原 CapabilityRegistry 仍是唯一准入入口。运行开始冻结候选、角色、来源和 service grant 上界；DiscoveryState 只持有当前发布值和待提交选择。模型请求持有原 CapabilityResolution 与捕获的执行方法，不保存历史 resolution 列表，不按最新同名对象分派。
2. 搜索普通消息/结果保存成功后才发布内存选择；同一回复不能抢先调用新工具。取消、保存失败不发布，待处理选择释放。MCP 重新连接只能为同一预授权 grant 的完全相同声明建立下一请求的新连接绑定；旧请求仍指向旧连接，不能借重连换执行对象。
3. 已正常完成运行在既有 model snapshot 中记录一次 `toolSelection`：有序 name/source/完整安全声明指纹及真实 source scope；无候选目录、schema 链或独立缓存表。恢复重新走当前准入、live authorization、完整指纹与累计声明预算。缺失工具本次丢弃，不等待以后注册自动加载。历史/角色删除使恢复失效；来源代际变化不命中旧缓存。
4. 既有 tool_audits 增加有限 `capability_binding_json`：当前调用所用的一个定义、其指纹、request/run/source 标识与 builtin 作者技能摘要。不得复制未使用候选、参数或结果。宿主 call ID 重用拒绝，不覆盖原不确定结果，也不触发自动重放。
5. 普通 Drift 作者验证原用户输入、当前 author scope、仍活动的真实 write/edit audit 及 builtin 内容 hash；自主作者验证既有 tick/active_skill/bundle/call。authored_files 原 writer_snapshot 列的新行保存窄 `drift-author-v1` provenance；旧内容不重写。文件/根目录 identity、publication digest、CAS 和执行许可保持原职责。
6. spawn 只持有当次请求已选择且 profile 允许的绑定；soft schedule 保留跨重启必需的有限定义、模型/角色/来源。TaskOrigin 严格兼容旧字段与位置参数，旧 capability_version 仅是历史元信息；自主/来源验证采用显式 grant_domain/grant_revision。

Pi 基线 `d5b3de22` 的启发是缩小状态与稳定请求引用，不移植其 durable harness、name-only 恢复、ASCII tokenizer 或通用事务重放。Meido 保留更严格的完整安全定义与实际渠道代际校验。

## 兼容迁移与回滚

兼容 reader 先支持旧 chain、新 call/cache 与旧/新 TaskOrigin；双格式夹具比较实际选中定义，再将普通/自主作者、委托、审计和恢复消费者切到窄事实。生产 Runtime/Drift 不再新增完整链。旧列/表与历史 JSON 保留；旧写方法已移除；兼容夹具直接构造旧数据，不保留平行生产写路径。

停写后，任意旧二进制不保证理解新行。回滚目标必须是保留本 ADR 新格式 reader、来源校验和窄作者 proof 的 bridge 构建，并可停用新准入路径；不能回滚数据库快照。异常时先停止新准入，只对照既有 receipt/outbox/claim；unknown 不能改成 failed 后重发。物理清理另行批准。

没有新增 registry、授权服务、效果 ledger、scheduler 或迁移框架。代价是旧目录审计改为逐调用事实，而不提供未使用候选的历史全目录重放；这是明确的存储取舍，不是授权放宽。

## 事实来源

[能力绑定](../../backend/app/agent_runtime/capabilities.py)、[发现](../../backend/app/agent_runtime/discovery.py)、[存储](../../backend/app/session_store.py)、[作者证明](../../backend/app/proactive_runtime/drift_skills.py)、[兼容 codec](../../backend/app/task_runtime/runtime.py)、[迁移回归](../../tests/python/test_lightweight_tool_loading.py)、[codec 回归](../../tests/python/test_task_origin_codec.py)。

离线存储与延迟范围见[固定夹具测量](../research/tool-loading-benchmark-2026-10-26.md)，不作真实模型性能承诺。

## 2026-10-30：工具可见性对齐的兼容说明

#277 按 Shiori `21e68d99` 对齐已有工具的常驻/按需分类、风险元数据和调用说明，详细清单及必要差异见[核验报告](../research/shiori-tool-exposure-and-guidance-2026-10-30.md)。这只改变模型的可见声明与指引，不替代本 ADR 的准入、请求绑定或撤权检查。关闭按需加载时保留核心搜索，搜索已可见工具不能增加授权。未加载名称目录每请求检查当前许可，与命名空间共享有限字节预算。

宿主绑定初始化遍历本次已准入候选，使按需 soft schedule 在稍后加载后仍取得原 provider；它不发布工具、不授予执行权限。更新后的描述/risk 会改变完整安全声明指纹，旧选择失配可丢弃并重新搜索；既有请求的执行器绑定不随同名最新定义变化。没有新增能力目录、授权服务或缓存链。
