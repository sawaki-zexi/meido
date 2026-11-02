# 上下文与记忆契约

状态：当前。最后核验：2026-11-02。
事实来源：[ContextWindowProvider](../../backend/app/agent_runtime/context_provider.py)、[维护](../../backend/app/memory_maintenance.py)、[默认引擎适配](../../backend/app/memory_retrieval.py)、[优化器](../../backend/app/host_memory_optimizer.py)、[生命周期测试](../../tests/python/test_context_memory_parity.py)。

## 请求与控制

SELF/MEMORY 全文进入稳定 system，历史和已保存工作摘要随后排列。RECENT_CONTEXT 的摘要（不重复原文）、语义召回和当前状态位于当前输入前的动态 frame。工具调用与实际结果保留在当前尾部。前缀可由供应商自行缓存，不由 Meido 建立 KV 服务。

`GET /api/roles/{role_id}/context-status` 独立检查实际已保存输入，`canCompact` 表示存在可压缩的完整前缀，不取决于自动阈值。`POST /api/roles/{role_id}/context-compact` 返回新状态、`compacted` 与 `reason`。成功或失败后均重新准备持久状态，因为文档覆盖可能已提交而工作摘要失败。操作不接受草稿，不创建普通回合。桌面圆环和 `/compact`、获授权渠道命令使用同一控制器。

自动维护默认保留20条已完成消息，待整理部分至少10条才后台运行；渠道旁听沿既有50条/30分钟扫描。窗口需要覆盖时可强制处理选中的完整前缀。会话进度分别保存文档版本、事件发布版本和关系版本。未成功消费者重试，已确认部分不重复发布。RECENT_CONTEXT 只记录紧凑归属，来源语义使用 SHA-256 前缀印记，游标不复制整段历史。

## 文档与引擎

| 责任 | 配置/存储 | 行为 |
| --- | --- | --- |
| 宿主文档 | 角色 memory 目录；会话维护进度 | 规范 SELF/MEMORY、追加 HISTORY/PENDING、近期摘要；不依赖语义开关 |
| 语义引擎 | memory2.db；memory-embedding-config.json | 角色隔离的结构化检索和显式记忆工具 |
| 周期优化 | 高级设置的优化开关/间隔 | 合并 PENDING 到 MEMORY，独立更新 SELF |
| 主人资料 | 原 embedding-config.json 与 ownerEmbeddingEnabled | 知识库检索；可在主人资料设置编辑，不随语义引擎开关改变 |

引擎缺配置、关闭或 fingerprint 失配时，记忆工具不可执行，宿主文档仍进入输入。`/api/memory/embedding/configuration` 管理引擎配置；`/api/embedding/configuration` 保留给主人资料。两者分别支持 test、reveal-key。显式写入先取得并验证向量，失败不创建部分记录。读取不补嵌入、不更换空间。模型名与维度定义引擎空间；endpoint/provider/key 不参与其 fingerprint。

独立向量服务的 HTTP 超时为30秒、总请求预算40秒；查询外层仍限8秒，显式工具仍受现有15秒工具预算约束。外层取消会关闭异步 HTTP 请求并完成短回滚，不能在工具结束后迟到写入。主人资料原向量服务的行为不变。

## 固定检索规则

默认 top_k=8；向量阈值0.45，procedure=0.66，其余 preference/event/profile=0.5；相对分差0.2。注入最多6000字符、单行600字符、强制项3、procedure/preference共4、event/profile共4。采用固定 retriever 的向量/关键词 RRF、热度、procedure guard；兴趣查询只取 preference/profile；时间线必须有起止时间。answer 的两路 HyDE 每路3秒，查询 embedding 8秒；同步存储搜索在线程中执行。SQLite 向量索引使用单位向量距离换算余弦，失败走线程内评分，不在事件循环扫向量。

宿主用户层提取的截止时间为300秒，近期语境为180秒；这与检索向量的8秒边界独立。辅助调用对已支持厂商关闭 thinking，保留上游1024/512输出预算，并使用容错 JSON 解析，不强制 JSON mode。前台自身草稿可取消并关闭请求；等待独立后台草稿的前台取消不会取消后台工作，短提交开始后则等待提交结束。

已知整理失败只在当前积压达到门槛时阻止进入；更换实际模型配置或凭据后失效，旧配置迟到的成功或失败不能清除或覆盖新配置的失败。此状态不写入配置凭据，不撤销已合法提交的覆盖。

回合后隐式提取为 profile/preference/procedure；共同经历由已提交整理发布。工具已显式写入的 ID 在回合后处理期间受保护。来源与角色边界由宿主提供，模型不能自行扩大；群聊主人的事实和旁听使用各自进度，不推进私人近期上下文。外部成员资料与私人记忆保持原隔离。
