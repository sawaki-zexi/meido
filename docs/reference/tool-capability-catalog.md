# 角色工具配置与目录

状态：当前

最后核验：2026-10-17；默认内置方案见 [ADR-0021](../adr/0021-default-builtin-tools.md)。

事实来源：[目录模块](../../backend/app/agent_runtime/catalog.py)、[角色 HTTP API](../../backend/app/main.py)、[模型请求适配](../../backend/app/agent_runtime/meido_provider.py)、[角色操作回归](../../tests/python/test_role_capabilities.py)、[网页操作回归](../../web/src/features/role-management/RoleCapabilities.test.tsx)。对应 [Issue #138](https://github.com/sawaki-zexi/meido/issues/138)；完整复刻目标见[整体规格](../specs/shiori-tool-parity-spec.md)。

## 使用

基础工具由宿主默认提供，新旧角色无需逐项勾选。旧 `enabledTools` 空列表或部分列表不隐藏默认工具，仍保留扩展工具选择。普通主人 Shell 在 Windows 使用 PowerShell 7、其他平台使用 /bin/sh；无需开关或命令白名单。工作目录固定为角色 workspace，环境有限；超时大于0、至多300秒，输出上限1–200000字符，不提供操作系统沙箱。受限子任务和自主运行继续校验各自的独立许可。

在“设置 → 工具与插件”选择角色，启用或暂停浏览器、屏幕、桌面控制、生图、Coding、桌宠和自主功能。Shell、子任务预算和实际工具目录也集中在这里。服务凭据及依赖使用各自设置，停用不清空已有配置。插件保存同步当前角色，普通资料编辑不会恢复旧开关。

角色资料只保存身份和设定，不提交工具配置；已有 Shell 配置以及其他工具、插件和授权项保持原值。取消编辑恢复已保存值；创建草稿在当前浏览器标签页恢复。

点击“查看实际工具”可查询工具和插件目录。目录使用已保存配置，修改后需保存并刷新。它预览下一次桌面对话的初始能力；外部渠道还会应用当前发言者、线程和附件约束。运行开始冻结授权候选，开启发现后可追加不可变的可见性版本；撤权可以阻止执行。

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

浏览器检查仅核对现有运行时支持的操作系统和组件文件。Shell 检查仅定位固定平台 Shell。网络连接、平台目标、工具行为、进程是否能成功启动仍以实际调用结果为准；工具可见和依赖检查分开表达。

## 本次模型能力说明

原生 rich 和 structured 模型请求根据当前请求的实际 schema 添加简短能力说明，工具名不从旧会话或静态文案复制。未启用工具时原生模型获得明确的空清单。已有角色设定、Skill 提示词、工具结果与冻结能力策略继续保留；仅支持旧式文本 `stream_reply` 的第三方适配器沿用原有文本协议。

`tool_search` 默认提供。已获授权的延迟工具可以显示 `authorized=true`、`modelVisible=false`，发现后在同次运行的下一次请求提供 schema；不会由发现自动启用特殊插件或授予 MCP 服务。配置和版本语义见[工具发现契约](tool-discovery.md)。后台任务和[通用调度](owned-schedules.md)使用独立宿主生命周期；目录或发现功能不代表整体 Shiori 复刻目标完成。
