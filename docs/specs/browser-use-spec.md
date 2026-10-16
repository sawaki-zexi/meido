# Browser Use 网页操作规格

状态：当前

最后核验：2026-10-15

事实来源：

- 已提取：GitHub Issue #99、`backend/app/browser_use/`、角色配置与 Agent Runtime 测试
- 已提取：Shiori-Agent `plugins/browser_use/README.md`、`manifest.yaml`、`backend/tools.json`
- 已提取：`docs/specs/agent-capability-extension-spec.md`、`docs/specs/agent-runtime-tau-integration-spec.md`
- 已提取：`backend/app/browser_use/native-runtime.json` 中固定版本、官方来源和 SHA-256
- 已提取：[2026-10-15 真实原生浏览器验收](../research/browser-use-native-acceptance-2026-10-15.md)

## 问题与目标

已提取：Meido 的角色对话需要在同一 Agent Runtime 回合中观察网页、执行交互并依据结果继续生成回复。浏览器必须由宿主管理，避免模型获得任意进程、profile 或路径控制权。

已提取：角色在一次对话中使用独立的运行代浏览器会话和可持久化的角色 profile，复刻 Shiori Browser Use core 工具的可演示闭环：打开页面、读取文本、获取可访问性快照、使用元素引用操作页面、管理标签页和获取截图。

## 用户故事

- 主人可以为单个角色打开或关闭浏览器授权；关闭后该角色的 provider schema 不包含浏览器工具。
- 主人要求角色访问网页时，角色可以导航、读取页面、重新观察快照，并按快照引用点击、填写、选择或发送按键。
- 角色可以查看、切换、新建和关闭标签页，并将受大小限制的截图用于当前回合的下一次模型请求。
- 角色在取消、超时、停用或宿主关闭后不会留下占用中的浏览器进程或 profile 锁，下一次回合可以重新打开。

## 范围

已提取：首版固定支持 Windows x64 `agent-browser 0.38.2` 和 Chrome for Testing `153.0.8010.52`。资源版本和来源见 `backend/app/browser_use/native-runtime.json`；资源由宿主解析，模型不能指定可执行文件、工作目录、profile、session、namespace、启动参数或截图路径。

已提取：工具清单由 `backend/app/browser_use/tools.json` 提供，共 28 个 Shiori core 工具。启用角色授权后，工具才进入能力快照；首次实际调用才启动浏览器。

已提取：启用授权代表主人允许当前角色执行完整网页交互，包括可能改变网页外部状态的点击、填写、键盘和选择操作，不增加逐动作审批。

已提取：浏览器 profile 按角色身份隔离并持久化站点状态；每个 run 使用随机 session，run 结束后关闭原生会话；同一角色动作串行执行。截图只作为受大小限制的临时图像传给当前运行的下一次 provider 请求，不写入普通消息、长期记忆或完整工具日志。

不在范围内：移动端 Host、浏览器资源下载器、任意外部进程控制、跨角色共享 profile、自动审批和第二个浏览器 Agent。

## 架构决定

- 推断：`CapabilityRegistry` 是浏览器授权边界，因为能力快照会冻结工具集合、策略和运行身份。
- 已提取：宿主通过 `browser.session` capability 提供按角色管理的 `BrowserSessions`；插件只获得该宿主服务，不直接获得核心存储或全局配置。
- 已提取：每个角色使用独立 profile 路径；每个 run 使用独立 MCP stdio client、随机 session 和运行锁；取消、停用、删除角色和宿主关闭都会关闭当前 session 并回收原生进程与锁。
- 已提取：MCP 输出转换为有界文本和图像；资源缺失、取消、超时、网页失败和插件停用返回带状态的结构化 tool result，不伪造成功。

## 数据流和生命周期

角色配置 -> `CapabilityRegistry` -> 浏览器工具 schema -> 模型 tool call -> 角色浏览器 session -> MCP `tools/call` -> 有界文本/图像结果 -> 当前运行的下一次 provider 请求。

会话在第一次实际工具调用时创建，并绑定当前 `run_id`；角色动作获得串行锁后发送。停用或取消先阻止新动作，再取消活动和排队任务，最后关闭 MCP/Chrome 并释放 profile 锁。正常运行结束保留 profile 目录，以便后续角色回合复用站点状态，但不复用上一个 run 的 session 或标签页。

## 核验状态

- 已提取：角色授权的创建和持久化、停用时 schema 隐藏及伪造调用拒绝有行为测试。
- 已提取：完整 Runtime 工具回合有行为测试；截图进入下一次 provider 请求，且不写入普通会话响应。
- 已提取：角色范围 profile、串行锁、取消后不执行排队动作、资源缺失和角色数据删除有专项测试。
- 已提取：MCP 和 Chrome 使用独立于模型运行时的原生进程；Windows Job 所有权保证进程树随宿主清理。
- 已提取：Windows x64 原生资源验收覆盖导航、snapshot/ref 交互、标签页、截图、profile 状态复用、角色隔离、取消后恢复及进程清理。
- 已提取：2026-10-15 使用哈希一致的固定资源，真实调用全部 28 个 AgentTool，并从公开 HTTP 完成实际 Chrome 截图往返与活动角色删除；宿主重建后 Cookie/localStorage 保留，进程、IPC 和 profile 锁清理通过。模型选择使用测试适配器，不声称已验收真实模型服务或外部网站账号。

已提取：当前自动化边界分三组：角色设置启停、一次对话内的完整工具往返、会话隔离与取消清理。真实原生验收仅在 Windows x64 并配置固定资源时运行；其他平台跳过该项，不伪造原生通过。

## 验收标准

- [x] 关闭授权时浏览器工具不进入 provider schema，伪造调用得到确定性拒绝；角色授权写入存储后可重新载入。
- [x] 开启授权后，Runtime 可以完成工具调用和下一次 provider 请求往返；截图作为临时图像传入且不出现在普通会话响应中。
- [x] 同角色动作串行；每个 run 使用独立 session，角色 profile 和截图目录使用不同的角色身份路径。
- [x] 取消、超时、停用、角色删除和宿主关闭后，Windows 原生浏览器进程、IPC 资源和 profile 锁均被回收。
- [x] 工具结果不把 API key、完整 cookie、敏感表单值或不必要的完整页面全文写入日志、普通消息或长期记忆；浏览器输入、URL 敏感查询参数、页面结果和 daemon 诊断均经过脱敏或有界处理。
- [x] Windows x64 原生资源可用时完成真实页面验收；缺少资源或平台不支持时返回结构化不可用错误。
