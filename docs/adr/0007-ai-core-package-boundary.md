# ADR-0007：独立 AI 核心包边界

## 状态

已接受

最后核验：2026-10-14

## 背景

Pi 将消息类型、transcript 转换、事件流和 provider API 实现集中在独立的
`ai` 包中。Meido 的等价实现最初放在 `backend/app` 根目录，虽然已有
`ModelAdapterRegistry` 和统一 rich message，但应用层仍可以直接导入任意
provider 实现，模型核心和 Web/存储模块的 seam 不够清楚。

## 决定

将模型调用核心放入 `backend/app/ai/`，由该包拥有：

- provider-neutral 的消息、内容块、usage 和流式事件类型；
- transcript 转换和通用工具 schema 校验；
- provider-specific adapters；
- adapter registry 和旧调用接口的核心实现。

应用层只依赖 `backend.app.ai` 的公开接口。模型连接目录、Pydantic 应用模型、
角色绑定、配置持久化和 HTTP API 仍属于 `backend/app`，以配置快照传入 AI 核心，
不由核心包读取数据库或访问 Web 状态。

旧的 `backend/app/model_*.py` 和 provider adapter 路径保留为模块别名，以便已有
插件和第三方调用方迁移；它们不包含第二份实现，也不应新增业务逻辑。

## 数据流

```text
应用/Agent Runtime
  -> app.ai public seam
  -> ModelMessage + ModelConfiguration snapshot
  -> transform_messages / normalize_tools
  -> selected provider adapter
  -> unified ModelStreamEvent
```

## 取舍

- 独立包收窄了应用依赖，provider 协议变化集中在 AI 核心内。
- 保留模块别名避免一次迁移破坏已有导入，但兼容路径会继续存在一段时间。
- `ModelConfiguration` 当前仍定义在应用模型模块，因为它同时是本地配置 API 和
  持久化记录；AI 核心只把它当作输入契约使用。

## 验证

- `tests/python/test_ai_package.py` 验证公开 seam 和兼容别名指向同一实现。
- 既有模型协议和 Runtime 测试继续从兼容路径运行，确保迁移不改变外部行为。
