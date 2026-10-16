# Shiori memory tool attribution

状态：当前实现分支；最后核验：2026-10-15。

`backend/app/agent_runtime/memory_write_tools.py`、`memory_tool.py` 的业务参数、
结果字段与时间过滤规则改写自 Shiori-Agent 固定提交
`4f266aeaf42fcd611362e2456417b91f659341cf`：

- `apps/backend/agent/tools/memorize.py`
- `apps/backend/agent/tools/forget_memory.py`
- `apps/backend/agent/tools/recall_memory.py`
- `plugins/default_memory/backend/engine/prompts.py` 的默认工具 profile

上游 [Shiori-Agent](https://github.com/YinFengWindy/Shiori-Agent) 的 MIT 许可见 [LICENSE](LICENSE)，
Copyright (c) 2026 YinFengWindy。Meido 保留自己的角色硬隔离、主人授权、真实来源、MemoryEngine、
SQLite 和记忆文档同步流程，不复制上游插件宿主或直接读取其数据库。未来更新人工核对行为测试与上游版本。
