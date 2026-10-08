# Agent Runtime Tool Result

状态：当前

事实来源：

- [`backend/app/agent_runtime/loop.py`](../../backend/app/agent_runtime/loop.py)
- [`backend/app/agent_runtime/tools.py`](../../backend/app/agent_runtime/tools.py)
- [`backend/app/agent_runtime/shell_tool.py`](../../backend/app/agent_runtime/shell_tool.py)
- [`tests/python/test_agent_runtime.py`](../../tests/python/test_agent_runtime.py)

## 运行时结果

Tool 实现返回 `AgentToolResult`，Runtime 在工具调用边界补齐诊断字段。发送给模型的内容仍然是有限的 `content`；结构化 `details` 用于 Runtime transcript、内部工具事件和审计。

```json
{
  "content": "有限的面向模型文本",
  "details": {
    "status": "succeeded",
    "durationSeconds": 0.012,
    "outputChars": 42,
    "truncated": false
  },
  "is_error": false
}
```

Tool 可以在 `details` 中提供领域字段，例如 Shell Tool 的 `exitCode`。Runtime 保留这些字段，并补齐或覆盖执行契约字段。`details` 不是映射时会放入 `value` 字段，以保证结果仍然是可记录的结构化对象。

## 状态

最终工具结果使用以下状态：

| 状态 | 触发条件 |
| --- | --- |
| `succeeded` | Tool 返回有效结果，且没有错误状态 |
| `unknown` | 模型调用了未注册的 Tool |
| `denied` | Tool 被当前能力快照、风险策略、审批策略或 Hook 拒绝 |
| `invalid_arguments` | 参数不符合 Tool 的 JSON Schema |
| `timed_out` | Tool 或 Runtime 的单 Tool 超时 |
| `cancelled` | 取消信号在 Tool 执行期间被观察到 |
| `failed` | Tool 抛出异常或返回了无效结果 |

工具更新事件使用 `running` 状态；它们不是最终 transcript 结果。取消、超时和单个 Tool 执行错误会作为 `is_error=true` 的 Tool Result 交给下一轮模型。Provider 超时和 Runtime 级别取消则终止整个运行。

## 数值字段

- `durationSeconds` 使用 Runtime 的单调时钟计算。Tool 已经提供可信耗时时保留 Tool 的值。
- `outputChars` 表示截断前的输出字符数。Tool 已经提供该值时保留它，否则由 Runtime 根据原始 `content` 计算。
- `truncated` 表示 Tool 或 Runtime 是否截断了输出。Runtime 截断时还记录 `outputLimit`。

Tool Result 的结构化字段写入 SQLite 的 `tool_result_json`；工具中间更新只通过运行时事件传输，不写入普通消息 transcript，也不提交给 MemoryWorker。
