# 记忆召回与回合后提取配置

## 召回诊断

`MemoryEngine.query()` 的 `MemoryQueryResult` 同时返回注入文本、结构化 `records` 和 `trace`。每条 record 包含来源 `evidence`、查询 lane/rank、lane score、RRF 分数、hotness、是否注入和选择原因。`trace` 记录各 lane 的候选与过滤数量、排序阈值、类型配额、预算裁剪、向量后端和降级信息。

角色记忆接口带 `q` 查询参数时，在原有 `memories` 列表之外返回：

- `hits`：结构化命中记录，适合诊断和管理界面；
- `trace`：本次查询的排序、过滤和注入诊断。

不带 `q` 时不会触发额外的 embedding 查询，`hits` 为空、`trace` 为 `null`。

## 隐式记忆提取

回合后隐式提取默认开启。设置环境变量即可关闭：

```text
MEIDO_MEMORY_IMPLICIT_EXTRACTION_ENABLED=false
```

关闭后，post-response 不调用隐式提取模型，也不会从普通闲聊创建候选；用户明确要求“记住”、纠正、忘记和拒绝记忆的处理仍然执行。模型超时、异常或非法 JSON 时，开启状态下继续使用现有规则提取回退，并通过 `MemoryWritten`/`TurnIngested` 记录错误和处理状态。
