# 通用媒体输出闭环（#267，Parent #265）

基线：Meido `6103b55`；Shiori `d3acaed186b172a78a5892e0307e9affd89759f0`。

## 实际差距

Shiori 的通用发送不是 `read_file`：`apps/backend/agent/tools/message_push.py` 接收桌面文件路径、图片路径或 URL；`account_delivery.py` 的 `account_send.media` 接收图片路径或 URL。`ToolResult.content_blocks` 是模型观察内容，不能据此推断用户已收到文件。QQ/NapCat 的 `accounts_actions.qq_image_segment` 和 QQBot 的 `outbound.send_image` 只负责图片；上游不提供同等通用 QQ 文件发送工具。

Meido 已有 owned 附件、桌面 push 幂等提交、渠道分段投递账本及图片/文件 renderer，缺口是生产者与发送工具之间：文件工具返回路径，而发送工具只认附件 ID；桌面入站附件在模型上下文里也不显示 ID。桌面 push 已提交后没有 SSE 消息事件，工具后的新回复 placeholder ID 也没有通知前端，造成文件/最终文字延迟出现或不出现。

## 本次闭环

- `prepare_output_attachment` 只在显式发送时将已有附件引用、workspace 文件或公共图片 URL 保存为原角色 owned 附件，复用现有 metadata 契约。相同文件名/字节复用稳定引用，不新增媒体账本。
- `message_push.file/image/attachments` 接受文件路径或已有 ID；`image` 可接受 URL。桌面通用文件可下载，不要求先上传或让模型猜 ID。
- `account_send.media` 按 Shiori 接收图片路径、引用、URL，后续仍走现有 ChannelHost/DeliveryAttempt 与平台 adapter。QQ 文件仍在发送文本前被拒绝，平台不支持的图片格式不伪装成功。
- 桌面当前回合的 push 提交后立即发送已存消息事件；前端去重插入。工具后的新文本 placeholder 使用真实 ID 通知前端，保留独立媒体消息与最终正文。非当前 HTTP 回合的后台/渠道发往桌面仍沿用已有桌面刷新机制。
- 桌面入站附件在模型上下文明确给出可用引用。`read_file` 的图像观察结果不自动发给用户；NovelAI 原有 outbox、来源与真实 receipt 不变。

## 保留的边界

Shiori 可接收本机路径；Meido 仍仅允许当前角色 workspace 的授权文件，不通过路径、链接或 URL 读取其它角色文件/配置密钥。访客不能用 `media` 读取角色本地文件；旧 ID 仍须属于当前线程可见附件，只有本次显式下载的公共图片获得内部准备引用。这个引用不是模型参数，也不扩大目标权限。

公共图片下载复用 `network_tools` 的 DNS 固定、逐跳公共地址校验、无环境代理/凭据、限时及 5 MiB 下载预算；本地附件沿用 10 MiB 上限。HTML/未知 URL 正文不作为图片保存。非图片文件始终以 `Content-Disposition: attachment` 和 `X-Content-Type-Options: nosniff` 提供，不在应用源下执行 HTML/SVG。

发送成功仍来自真实桌面提交或平台 receipt，而不是生成成功、文件存在、URL 可下载或工具观察成功。结果不确定不重发。

## 验证范围

新增三个后端行为用例覆盖：真实文件工具→发送→持久消息/SSE/下载；真实工具/Host/NapCat adapter 的本地与公共 URL 图片负载及 receipt；越界、链接、伪图片拒绝。前端实际 App 的 SSE 用例检查模型尚未完成时文件已经可见，以及更换 placeholder ID 后最终文字不丢。全部使用临时 workspace、DB 和 HTTP/平台替身，不调用真实渠道或付费模型。
