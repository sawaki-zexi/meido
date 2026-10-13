# ADR-0006：显式模型协议与 Provider Adapter

## 状态

已接受

最后核验：2026-10-13

## 背景

多个服务商都提供流式聊天和工具调用，但请求消息、工具 schema、图片、思考内容、
认证方式和响应事件并不统一。把它们都当作 OpenAI Chat Completions 会让协议格式
错误直接变成服务端 400，也可能在历史回放时丢失工具调用或结果内容。

## 决定

模型连接通过稳定的 `api` 字段选择 wire protocol。已有配置缺省为
`openai-completions`；服务商预设固定使用声明的协议，自定义连接可以从已注册
协议中选择。Provider Adapter 独立负责该协议的请求转换、流式响应解析和统一
模型事件映射。

共享 transcript 转换只处理协议无关的行为，包括消息配对、能力约束、工具 ID
规范化和通用工具 schema 结构校验。它不改写 provider 专属字段或假设所有模型
支持相同的 JSON Schema 子集。

当前注册协议为 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages、
Google Generative AI、Mistral Conversations、Amazon Bedrock Converse 和 Pi Messages。

## 取舍

- 显式协议选择可以避免把一种 API 的 payload 发给另一种 endpoint，但要求自定义
  连接选择正确协议。
- 独立 adapter 使 schema、历史和错误处理能够按协议测试；新增协议需要维护专属
  转换和行为测试。
- 通用预检可以拦截确定的结构错误，但不能代替服务端验证模型是否支持工具调用、
  特定 schema 关键字或上下文长度。
- 当前 API Key 配置不实现 OAuth、AWS SigV4 或 Vertex ADC。Bedrock 通过配置的
  bearer API Key 访问 endpoint，需其他认证时由前置代理处理。

## 后果

- 新增 API protocol 必须添加注册、配置校验、预设或自定义选择入口、协议行为测试，
  并更新模型接入规格。
- provider 返回的错误详情经过密钥脱敏后向用户展示；协议本地校验错误在发请求前
  报告。
- `api` 缺省值保持旧配置兼容；协议标识一旦保存，不应因 provider 显示名称变化而
  自动改变。

## 事实来源

- [模型接入架构方案](../specs/model-integration-spec.md)
- [Pi 模型层语义移植规格](../specs/pi-model-layer-port-spec.md)
- [`model_config.py`](../../backend/app/model_config.py)
- [`model_adapter_registry.py`](../../backend/app/model_adapter_registry.py)
- [协议 adapter 行为测试](../../tests/python/test_model_protocol_adapters.py)
