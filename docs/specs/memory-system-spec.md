# Meido 记忆系统规格

状态：当前

事实来源：[`shiori-agent-memory.md`](../research/shiori-agent-memory.md)、
[`shiori-agent-memory-maintenance.md`](../research/shiori-agent-memory-maintenance.md)。
Shiori-Agent 当前基线为 `main` 提交 `d32d98e7847a70a9461525bd33fd6b156ad7a2d0`；
维护链路的逐段源码记录来自提交 `ffe36d66d7fabd2a98c4fdb01069debc75f46b26`，
因此实现前须按当前基线复核细节。

最后核验：2026-10-01

本文定义 Meido 第一版记忆系统的用户可见行为、数据语义和模块边界。实现参考
Shiori 的角色级 Markdown、SQLite 结构化记忆、回合后异步维护、consolidation、
Optimizer、混合检索，以及当前版本按需查询和修改角色笔记/摘要的管理方式，但不
复制 Shiori 的插件、任务、工具和多用户运行时。

## 1. 目标

记忆系统让每个角色在持续对话中积累关于主人的事实、偏好、共同经历和关系认识，
在后续对话中按当前话题找回相关内容，并允许主人查看和维护这些记忆。

记忆系统不替代角色设定。角色设定由主人维护，决定角色的身份、性格、行为规则
和回复限制；记忆由系统根据对话和主人操作维护。`SELF.md` 保存角色对自身、主人
和关系的认识，允许自动更新，但不能自动改写角色设定中的核心人格字段。

## 2. 第一版范围

第一版闭环为：

```text
创建角色
  -> 角色唯一会话
  -> 流式对话
  -> 回合后异步提取
  -> Markdown 近期维护 / consolidation
  -> 结构化记忆写入
  -> 后续混合检索和上下文注入
  -> 定时 Optimizer 更新 MEMORY.md / SELF.md
```

包含：

- 角色级记忆隔离。
- 回合完成后的显式和隐式记忆提取。
- `RECENT_CONTEXT.md` 更新和按条件触发的 Markdown consolidation。
- `HISTORY.md`、`PENDING.md`、`MEMORY.md`、`SELF.md` 的维护。
- SQLite 结构化记忆、来源追踪、幂等去重、reinforcement 和 superseded。
- 关键词检索与 embedding 语义检索融合；embedding 不可用时关键词降级。
- 记忆上下文注入、查看、搜索、来源追溯、记住、忘记、编辑和删除。
- 角色删除时清理该角色的记忆数据和未完成维护工作。

不包含：

- 主人资料库导入、邮件、浏览器、任务、技能和其他外部事务能力。
- 多用户、跨设备同步、云端记忆服务和知识图谱数据库。
- 自动修改角色设定中的性格、行为规则或回复限制。
- 任意 Markdown 文件的自由编辑和双向自动同步。
- 流式回复断点续传。

## 3. 记忆文档

每个角色拥有独立目录 `roles/<role_id>/memory/`。文档由记忆维护服务写入，首版
通过受控的记忆管理操作修改；用户直接编辑 Markdown 不属于首版支持的输入路径。

| 文档 | 职责 | 注入方式 |
| --- | --- | --- |
| `SELF.md` | 角色对自身、主人和关系的持续认识 | 有长度上限的固定上下文 |
| `MEMORY.md` | 主人的长期事实、偏好和明确要求记住的内容 | 有长度上限的固定上下文 |
| `HISTORY.md` | 按时间记录的共同经历和重要事件 | 默认不全文注入，按需检索 |
| `PENDING.md` | 等待长期归并的候选记忆 | 不作为完整事实直接注入 |
| `RECENT_CONTEXT.md` | 近期对话压缩和正在延续的话题 | 有长度上限的近期上下文 |
| `journal/` | 按日期归档的整理事件 | 默认不注入 |

角色设定保存于角色资料中，与上述记忆文档分离。`SELF.md` 的自动更新只反映关系
和自我认识，不改变角色设定的性格与规则。

## 4. 数据语义和权威关系

三类数据各自承担明确职责：

1. 原始会话消息是记忆的证据来源，不能因记忆整理而被修改。
2. SQLite 是结构化记忆条目的权威存储，负责状态、去重、强化、替代、来源索引和
   检索；embedding 是其检索索引的一部分。
3. Markdown 是角色可读的记忆文档视图，由 consolidation 和 Optimizer 维护。
   首版不支持用户直接改文件后自动反向解析 SQLite，因此不存在双向同步冲突。

结构化记忆至少包含以下语义字段：

```text
MemoryItem
  id
  role_id
  memory_type       # profile | preference | procedure | event 等
  summary
  extra_json
  source_ref
  happened_at
  status            # active | superseded | forgotten | rejected 等
  created_at
  updated_at
  reinforcement
  content_hash
  embedding
```

`role_id` 是所有记忆操作的强制作用域。精确去重的逻辑键为：

```text
role_id + memory_type + content_hash
```

角色作用域同样适用于相似去重、reinforcement、supersede、关键词检索、向量检索、
来源查询和删除。不同角色即使内容相同，也不能互相合并或影响。

### 4.1 source_ref

每个自动产生的条目必须带可定位且稳定的来源引用。来源至少表达：

```text
kind: message | turn | consolidation
session_key
message_ids 或 message_range
stable_source_key
```

`stable_source_key` 用于幂等；用户查看来源时，后端能够跳转或定位到产生记忆的
消息，或定位到产生它的 consolidation 窗口。相同来源重复投递不得重复写入。

### 4.2 状态和时间

- `happened_at` 表示已知的事实或事件发生时间；`created_at`、`updated_at` 表示
  系统记录时间。
- `superseded` 表示被更新的旧记忆，保留来源和历史，不再作为正常结果注入。
- `forgotten` 表示主人明确要求忘记；不再被正常检索或注入，但原始会话消息默认
  保留。
- `rejected` 表示主人明确拒绝一条候选记忆；不再被正常检索、注入或归并进长期
  记忆文档，原始会话消息仍作为操作证据保留。
- 物理删除是独立的管理或角色生命周期操作，不与 `forget` 混同。

## 5. 维护流程

同一角色会话的维护操作串行执行。主对话只等待消息和回复保存，不等待记忆维护。

```text
TurnCommitted
  ├─ 回合后 worker
  │    ├─ 显式 memorize / forget
  │    ├─ 明确纠正和旧记忆 supersede
  │    └─ 隐式 profile / preference / procedure 提取
  │
  └─ Markdown maintenance
       ├─ 未达到条件：更新 RECENT_CONTEXT.md
       └─ 达到条件：consolidation
            ├─ HISTORY.md
            ├─ PENDING.md
            ├─ RECENT_CONTEXT.md
            └─ ConsolidationCommitted -> SQLite 结构化记忆

定时 Optimizer
  ├─ PENDING.md snapshot -> MEMORY.md
  └─ 根据可用记忆更新 SELF.md
```

### 5.1 回合后 worker

1. 完整用户消息和女仆回复保存后发出回合完成信号。
2. 后台 worker 先处理本轮显式记忆结果，再处理明确纠正或遗忘信号。
3. 在模型调用和 token 预算允许时提取隐式长期记忆；不可靠或无长期价值的内容
   可以不写入。
4. 写入时必须携带当前 `role_id` 和 `source_ref`。
5. worker 失败只影响记忆维护，不撤销已完成对话。

### 5.2 Markdown maintenance 和 consolidation

- 普通维护只刷新 `RECENT_CONTEXT.md` 的近期区。
- 超出保留窗口、达到最小新消息数或出现上下文压力时，选取完整消息窗口进行
  consolidation。
- consolidation 生成 `HISTORY.md` 事件、`PENDING.md` 长期候选和新的
  `RECENT_CONTEXT.md`，并按日期追加 `journal/`。
- 角色记忆管理页可只读查看五份记忆文档和按日期整理日志；未知文档名、路径穿越
  和越过角色目录的符号链接均被拒绝。
- 准备期间如果消息前缀或 consolidation cursor 变化，丢弃过期草稿，下次重新处理。
- Markdown durable write、cursor 更新和事件发布必须幂等；重复窗口不能重复追加。

### 5.3 Optimizer

Optimizer 按角色串行运行：

1. 原子取得 `PENDING.md` 快照；运行期间的新候选继续写入新的 `PENDING.md`。
2. 将快照与现有 `MEMORY.md` 合并、去重和精简。
3. 只有模型结果有效且 `MEMORY.md` 成功写入后才消费快照；失败则恢复快照。
4. 根据可用记忆更新 `SELF.md` 的固定章节。
5. 任何一步失败都留下可诊断状态，不覆盖已有有效文档。

### 5.4 后台队列决定

第一版采用 Shiori 风格的进程内后台队列和每角色串行锁，不增加持久化 job 表。
服务正常关闭时 drain 已入队工作；异常退出可能留下尚未执行的维护工作，下一次
启动会根据 durable `PENDING.md` 重新调度归并，后续对话也会继续维护。该取舍不影响
已经保存的消息和已提交记忆。

## 6. 检索和上下文组装

每次回复前：

1. 用当前角色 ID 确定唯一记忆作用域。
2. 读取角色设定、`SELF.md`、`MEMORY.md` 和 `RECENT_CONTEXT.md` 固定块。
3. 对当前消息并行执行关键词检索和 embedding 语义检索。
4. 使用融合排序合并结果，过滤其他角色、`superseded`、`forgotten` 和不适合
   当前上下文的条目。
5. 按类型、相关性和总长度预算选择有限条目。
6. 将记忆作为独立上下文块传给模型，并保留来源信息用于诊断和管理。

embedding 或向量索引不可用时，保留关键词检索；两者都失败时允许无记忆继续回复。
记忆检索失败不得阻塞正常对话。

允许使用记忆的模型调用仅限：当前角色正常对话、回合后记忆提取、consolidation、
Optimizer 和 embedding。模型连接测试、其他角色的调用和普通日志不得携带当前角色
的记忆内容。

## 7. 记忆管理

主人针对当前角色可以：

- 查看记忆文档、搜索结构化条目和查看来源。
- 明确要求记住一项内容。
- 编辑、拒绝或删除错误的结构化记忆。
- 明确要求忘记一项内容。

管理操作由后端执行并带 `role_id`，不能由前端直接写文件或数据库。编辑、拒绝、
忘记后的结构化状态与 Markdown 视图由记忆服务同步；编辑会使旧 embedding 失效并在
后续检索时重建；`forget` 不删除来源消息。

删除角色时必须一并清理或失效：角色 Markdown 目录、SQLite 结构化记忆、embedding
索引和该角色未完成的维护工作；其他角色数据保持不变。

## 8. 失败、恢复和安全

- 提取、embedding 或检索失败不能撤销已完成对话。
- 后台失败必须记录可诊断状态和错误，不能伪装成成功。
- `source_ref` 保证回合和 consolidation 重复投递幂等。
- Markdown 使用原子写入和快照恢复，Optimizer 失败不得丢失 pending 候选。
- SQLite 条目、状态和索引更新使用事务或等价的一致性边界。
- 后端是角色作用域、来源校验和记忆读写的权威边界；所有任务必须带明确
  `role_id`。
- 记忆和来源消息属于本地私人数据，不写入普通日志，不发送给无关模型调用。
- 首版备份、导出和加密遵循 Meido 整体数据安全方案；不新增跨设备同步规则。

## 9. 模块边界

### MemoryEngine

提供 ingest、query、显式 mutate、维护状态和管理操作的统一入口；对话运行时不
直接依赖 Markdown 格式或向量扩展。

### Post-response worker

消费已完成回合，处理显式记忆、纠正/遗忘和隐式提取。

### Markdown maintenance

管理角色文档、近期上下文、consolidation、cursor、来源幂等和 pending snapshot。

### Structured memory store

保存条目、状态、来源、embedding，提供精确/语义去重、替代、强化、关键词和向量
检索。

### Optimizer

低频合并候选、更新 `MEMORY.md` 和 `SELF.md`，不编辑角色设定。

### Retriever / Context assembler

按角色作用域检索、应用类型和长度预算，生成当前回合的记忆上下文块。

### Memory management UI

提供查看、搜索、来源追溯、记住、忘记、编辑和删除入口，不直接访问数据库或文件系统。

## 10. 验收场景

1. **记忆跨回合**：明确表达的稳定偏好在后续相关话题中可被召回，无关话题不会
   被强行注入。
2. **角色隔离**：角色 A 的记忆不会出现在角色 B 的检索、文档或提示词中。
3. **来源可追溯**：用户能从自动记忆定位到产生它的消息或整理窗口。
4. **偏好变化**：明确改变偏好后，新记忆生效，旧条目变为 `superseded`，重复
   投递不产生重复记录。
5. **整理链路**：达到条件后，事件进入 `HISTORY.md`，候选进入 `PENDING.md`，
   近期上下文更新，并最终可由 SQLite 检索。
6. **长期归并**：Optimizer 成功后候选进入 `MEMORY.md`，`SELF.md` 可自动更新，
   角色设定保持不变。
7. **明确忘记**：被忘记条目不再正常检索或注入，原始消息默认仍可在会话中查看。
8. **失败恢复**：模型失败、embedding 不可用或服务正常关闭时，已保存消息和已
   提交记忆不丢失，未提交候选保留或有明确未完成状态。
9. **上下文限额**：记忆数量增加后，模型仍只收到当前角色相关且在预算内的记忆。
10. **角色删除**：删除角色后其文档、条目、索引和维护工作不可访问，其他角色
    数据不受影响。
