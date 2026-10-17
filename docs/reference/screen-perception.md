# 屏幕感知运行契约

状态：当前实现，独立分支待总集成审查。最后核验：2026-10-16。

事实来源：[观察模块](../../backend/app/screen_perception/__init__.py)、[固定 worker](../../backend/app/screen_perception/worker.py)、[HTTP 装配](../../backend/app/main.py)、[公开工具行为测试](../../tests/python/test_screen_perception.py)、[HTTP 行为测试](../../tests/python/test_screen_perception_api.py)、[具体规格](../specs/screen-perception-spec.md)、[ADR-0014](../adr/0014-explicit-screen-vision-binding.md)。

## 配置与授权

角色通过既有 POST/PUT roles 原子保存 `agentConfig.screenPerception`，字段为 `enabled`（默认 false）和 `visionModelConfigurationId`（默认 null）。关闭保留 ID；省略整个配置、旧客户端省略 screenPerception，或只修改 enabled 都保留未提供的屏幕字段。清空绑定需明确提供 null。开启须选择存在且完整的已保存连接，否则 HTTP 422。沿用模型连接的认证契约，本地服务可以没有 API Key。

选择与聊天相同的保存 ID 是明确授权；没有隐式 active/chat fallback。连接删除后角色保留失效 ID，网页显示“连接已不存在”。表单、目录及保存都不截图、不测试模型。画面发往所选视觉连接，本地不保存原图；摘要可进入当前对话及正常工具历史。

`observe_screen` 默认直接暴露，输入严格为 `{}`，read_only、external_allowed=false、source=builtin:screen_perception、timeout=20 秒。角色开关和宿主候选/执行检查都生效，enabledTools 或插件列表不能授予观察许可。仅正常桌面发送入口写入内部来源标记；观察模块同时检查真实 run、canonical role session、真实完成的 user 消息和当前配置。群、访客、配对私聊、定时和内部后台回合均不获得许可。

## 数据与资源

Windows 10/11 交互桌面采用独立 Python `-I -B` 进程：受控最小环境、不启动 shell、无用户命令参数、CREATE_NO_WINDOW，先检查 WTS active/unlocked、Default 输入桌面和主屏物理尺寸，再 Pillow ImageGrab 主屏采集。非 Windows 与未实现 frozen worker 分派明确不可用，没有系统 fallback。

父模块拥有长度前缀元数据/PNG 管道、有界 stderr、真实 PNG 解码、配置复核、独立 rich model 请求与清理。预算沿用规格第 8 节：原始 ≤16,777,216 像素，发送长边 ≤2048、≤4,194,304 像素、PNG ≤4 MiB；capture 5 秒、vision 12 秒、整体 18 秒；同时 1 次、无队列；消费文本含思考 ≤16 KiB。模型请求固定中性 system、一张图片、空工具、max_tokens=1200，不包含角色、历史、记忆或技能；不创建主聊天 snapshot。

响应只接受严格两字符串字段 JSON，拒绝重复 key、非有限常量、围栏、额外字段、动作、工具调用、空值、错误类型及原始字段超过 2000 字符。合并空白后 summary/activity 分别限 400/80 字符。工具成功只返回 `available/interface_summary/activity_key`，images 为空；details 只含固定状态、所选 ID、处理后尺寸、字节与耗时。provider/worker 原始异常不会直接进入结果或日志。

停止或取消会 kill/wait capture，或关闭视觉 async iterator/HTTP 上下文；重复取消仍等待真实资源收尾，才释放全局名额。角色设置、模型内容/密钥变更和删除都 invalidate 旧 generation，并在 HTTP 成功前等待。禁用后重开不能复活旧候选。shutdown 拒绝新观察并收尾。

同一次观察的撤权、signal、外层 Task 和三个 deadline 共享一次取消通知；后续通知只等待，不能在 provider 的 response/client finally 中再次注入取消。即使慢 HTTP 关闭超过后续 deadline，名额仍保留，直到真实连接关闭。

已有 rich adapter 的委托层逐层 await 其拥有的 iterator 关闭。文本预算或工具事件提前拒绝也必须等到底层 HTTP 上下文关闭，不依赖 asyncgen finalizer，才返回结果和归还观察名额。

保留既有删除规则：运行中 DELETE 返回 409；主人先 stop 等待收尾，再删除。成功删除在既有 gate 内 invalidate；回滚也不恢复旧候选。取消无法撤回已上传字节，也不承诺安全擦除内部副本或远端遗忘。

## 结果与限制

失败为 `available:false/error/message`，is_error=true、无附件，完整 JSON ≤4 KiB；错误码包括 permission_denied、configuration_missing、configuration_changed、vision_unsupported、capture_unavailable、frame_too_large、busy、timeout、invalid_observation、vision_failed。取消继续传播取消语义。

目录区分关闭、未绑定、失效绑定、协议图像编码和平台限制；配置/平台满足只标为调用时验证，不宣称远端模型已支持视觉。摘要是不可信观察，不能扩大其他工具权限，也不保证绝对准确或脱敏。

真实单屏主屏与取消已验收；双屏、混合 DPI、锁屏/远程断开/安全桌面原生拒绝、frozen 分发仍有明确限制，见[验收记录](../research/screen-perception-acceptance-2026-10-16.md)。原生测试只在显式 opt-in 且受控窗口已覆盖主屏后运行，不用普通用户桌面或真实付费服务作夹具。
