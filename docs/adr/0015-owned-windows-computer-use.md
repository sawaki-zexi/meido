# ADR-0015：固定普通权限驱动与有界桌面输入拥有权

状态：已接受，第 3 节方案已获用户批准。最后核验：2026-10-16。事实来源：[Computer Use Spec](../specs/computer-use-spec.md)、[当前契约](../reference/computer-use.md)、[固定 release lock](../../backend/app/computer_use/runtime.json)。

Computer Use 要使当前聊天模型直接观察真实窗口并输入。P04 的独立视觉连接只返回屏幕摘要，不能表达这个用户结果；通用 MCP 可配置服务器也不能替代角色专用输入授权。固定上游驱动还存在 PID 范围缩放状态、外部窗口 EnableWindow/exstyle guard、桌面区域截图回退三个不可直接接受的行为。

选择独立默认关闭的角色许可，只接受宿主持久化证明的主人主动桌面 run，并联合验证 Windows console、登录 SID、解锁状态和普通权限。固定 MIT CUA commit 加最小补丁，仅分发 direct 主驱动；没有 helper/UIAccess/提权或全局 daemon。复用现有 ctypes/Pillow、McpStdioClient 和 WindowsJob；保持通用 transport 默认协议与既有消费者行为。

所有进程/角色/run 共用 session 范围 mutex 与一个命名 Job，应用拥有整棵输入进程树。观察和动作串行，跨模型等待仍保有 lease；清理失败保留拥有权，确认进程树和管道 tasks 退出后才能交还。全 run 只接受一张最新动作观察，任何 state 尝试和一次输入都会使旧引用失效。原生补丁禁外部窗口启用/样式变更；首版 background 仅保留目标已经是精确 foreground 的子集。显式 foreground 保留上游有界辅助激活，不重发未知效果业务输入。

截图只能来自具备准确 HWND provenance 的 PrintWindow/WGC；没有映射时禁止像素动作。PNG 通过既有临时 ImageContent 给当前聊天下一请求，既有 loop 负责过期，app wrapper 在上传前重核授权；不改变 AI 核心或复用 P04 的许可。UIA 与正常文字历史继续保留。输入/上传已发生后无法撤回，Windows 也没有跨用户输入事务隔离。

代价是维护固定 Rust/MSVC/SDK、最小 patch、许可证、新 exe SHA 和实机回归。不同 hash 的重构建不会自动进入 release lock。多屏、其他 DPI、受保护目标和未验收应用类型不会因 mock 或部分 fixture 通过而自动声明支持。每次升级需要重新取得真实来源/几何、取消、owner 崩溃和输入效果证据；具体范围保存在验收手册。

批准边界：2026-10-16 主人在本次对话明确批准具体规格第3节和专用测试窗口原生验收；未授予任意真实用户应用操作、外部事务、PR合并或Issue关闭。[实施计划](https://github.com/sawaki-zexi/meido/issues/169#issuecomment-6207489899)。
