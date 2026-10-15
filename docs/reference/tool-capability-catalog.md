# 角色工具配置与目录

状态：当前

最后核验：2026-10-15

事实来源：[目录模块](../../backend/app/agent_runtime/catalog.py)、[角色 HTTP API](../../backend/app/main.py)、[模型请求适配](../../backend/app/agent_runtime/meido_provider.py)、[角色操作回归](../../tests/python/test_role_capabilities.py)、[网页操作回归](../../web/src/features/role-management/RoleCapabilities.test.tsx)。对应 [Issue #138](https://github.com/sawaki-zexi/meido/issues/138)；完整复刻目标见[整体规格](../specs/shiori-tool-parity-spec.md)。

## 使用

打开已保存角色的“角色资料”。“允许执行 Shell 命令”控制本角色命令工具，白名单每行填写一个程序名或绝对路径；超时范围为大于 0、至多 300 秒，输出上限为 1–200000 字符。关闭或白名单为空时，下一次对话不提供 Shell，通用 `enabledTools` 不能覆盖专用开关。工作目录、最小环境和 argv 执行规则沿用 ADR-0004，不提供操作系统沙箱。

保存角色资料会保留 Shell 配置以及其他工具、插件和授权项。取消编辑恢复已保存值；创建草稿在当前浏览器标签页恢复。

点击“查看实际工具”可查询工具和插件目录。目录使用已保存配置，修改后需保存并刷新。它预览下一次桌面对话的能力；外部渠道还会应用当前发言者、线程和附件约束。正在执行的运行继续使用开始时冻结的能力快照。

## HTTP 契约

`GET /api/roles/{role_id}/tools` 返回 `roleId`、`sessionKey`、`preview: true`、`tools` 和 `plugins`。角色不存在返回 404。查看目录不调用模型、不执行工具、不初始化插件工厂、不创建角色工作区或浏览器进程。

每个工具包含名称、说明、来源、版本、风险、暴露级别，以及：

| 字段 | 含义 |
| --- | --- |
| `registered` | 宿主实现或插件声明是否包含该名称；配置引用的未知名称为 false |
| `enabled` | 与运行共用的角色能力策略是否包含该名称 |
| `authorized` | 当前配置是否通过工具和插件授权策略 |
| `modelVisible` | 已知内置接口在桌面对话中是否会提供 schema；动态插件尚未初始化时可为 null |
| `reason` | 关闭、缺少授权、未注册或等待运行确认的原因 |
| `dependency.status` | `ready` 表示静态检查通过；`unavailable` 表示已发现依赖问题；`unchecked` 表示未验证 |
| `dependency.reason` | 检查范围与未验证部分，例如组件存在但浏览器尚未启动 |

插件目录包含已注册 manifest 的 ID、版本、来源、启用状态、授权、请求的 capability 和诊断原因。它只读取声明；任意插件的实际工具接口、Hook 和依赖需在真实运行初始化后确认，不把未知状态显示为健康或模型可见。

浏览器检查仅核对现有运行时支持的操作系统和组件文件。Shell 检查仅定位白名单程序。网络连接、平台目标、工具行为、进程是否能成功启动仍以实际调用结果为准；工具可见和依赖检查分开表达。

## 本次模型能力说明

原生 rich 和 structured 模型请求根据当前请求的实际 schema 添加简短能力说明，工具名不从旧会话或静态文案复制。未启用工具时原生模型获得明确的空清单。已有角色设定、Skill 提示词、工具结果与冻结能力策略继续保留；仅支持旧式文本 `stream_reply` 的第三方适配器沿用原有文本协议。

此变更不实现 `tool_search`、动态工具激活、后台任务或通用调度，不代表整体 Shiori 复刻目标完成。
