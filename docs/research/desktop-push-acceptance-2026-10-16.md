# 桌面文本与媒体推送验收

状态：#167 实现，独立审查尚未完成。日期：2026-10-16。父目标 #137，总交付 PR #162。

固定参照 Shiori `4f266aeaf42fcd611362e2456417b91f659341cf` 与[总规格 C06/第 4 节](../specs/shiori-tool-parity-spec.md)。#121 原普通 push 外发行为已在对应 Issue 说明改由 `account_send`，显式待办渠道提醒不变。

第一条公开 HTTP 用例先证明未配置账号的角色目录不能使用 push，再通过显式授权、真实 Runtime 工具循环、SessionStore 和下一 provider 请求形成桌面文本闭环。图片/文件均走真实上传端点、角色附件目录、SQLite 与公开会话/下载端点；未调用真实平台账号或付费模型。

补充故障先红后绿：实际宿主渠道 candidate 被换入无关桌面 context，以及 user 关联到了别的会话，均曾错误写入；修复后实际 run、user、宿主来源一致才可写。锁等待期间删除媒体曾产生假成功；现在提交时再次解析和比对附件。重复 Task.cancel 后线程抛授权异常曾掩盖取消；修复后等待真实线程并保留取消。实际 Windows 目录 junction 指向另一角色附件及同 ID 同时声明 image/file 的类型矛盾也先红后绿。

实际 SQLite/Runtime 取消包括 10ms 取消、20ms 解锁的窄窗口及重复 Task.cancel；原存活/写入断言未放宽。重启重放按角色/run/call 去重，父模型失败及公开 Stop 返回成功后的取消均保留已提交消息。Stop 用例中 HTTPX 内存 ASGI transport 对被停止的原流可能返回自身 AssertionError，因此以实际 Stop 200、持久 run=cancelled、已投消息仍 completed 为证，不伪造该流完成事件。

来源专项使用真实 RoleStore、AgentRun、入站、绑定和 FrozenChannelSource，共 62 项；含旧绑定/账号代际、首次执行前账号重建、平台暂停、角色/工具撤销及目录预览保持已有 pending 提醒账本。网页新增第十二项基础开关，保存/重开和默认关闭经过用户操作红绿验证；焦点刷新能显示无正文图片和文件并链接本角色附件端点。

同步屏幕集成后，桌面推送/来源、提醒/发现、屏幕 HTTP、子任务与群 HTTP 交叉 **131 passed**；网页基础工具、屏幕/子任务配置、草稿及桌面媒体 **15 passed**，TypeScript/Vite 通过。新增工具 mypy、三个相关模块检查和 Ruff/compileall 通过；SessionStore 仍有 4 项既有 mypy 错误，使用固定基线 shadow-file 比对错误类别与文本逐项一致，无新增，并未声称全仓类型检查通过。

独立 Standards 初审发现两个 P2：事务内先检查权限再读附件留下取消窗口，且独立消息插在 tool_call/result 之间导致下一轮重放误报中断。三个媒体解析中取消/撤权用例和一/两调用组的真实 HTTP→新 SessionStore→下一主人回合共 **5 项先红后绿**。最终权限校验移到媒体解析后；重放扫描只暂存组内独立消息、按原 run/call 保留全部结果后再放回。修复后的桌面/真实 HTTP、核心 Runtime 和 Google 调用身份交叉 **62 passed**；最终两轴复核尚未完成。

现有账号在线状态、目录种类/群成员分页和真实平台收件仍是 C06 的后续差距；此票不代表 C06 整体完成。
