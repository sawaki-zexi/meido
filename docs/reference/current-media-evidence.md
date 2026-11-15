# 当前图片输入依据

状态：当前（#375 实现分支，尚未合入主线）。

事实来源：[图片视图与请求记录](../../backend/app/agent_runtime/image_evidence.py)、[模型请求边界](../../backend/app/agent_runtime/meido_provider.py)、[自然聊天媒体准备](../../backend/app/channels/natural_media.py)、[自然回合](../../backend/app/channels/natural_turn.py)、[QQ 适配器](../../backend/app/channels/napcat.py)、[请求证据测试](../../tests/python/test_current_image_evidence.py)、[渠道接收与公开历史测试](../../tests/python/channels/test_current_media_turn.py)、[网页消息展示测试](../../web/src/features/channels/ChannelImages.test.tsx)。下述行为和字段为已从源码及测试提取的事实；真实模型语义质量仍未核验。

最后核验：2026-11-15，提交 `b16ea44`。

适用：#375，增量父规格 #374。此文描述本实现的审计边界，不表示真实模型质量已经验收。

普通聊天和自然聊天从已授权来源准备图片。静态图保留内容 SHA-256；GIF、动画 WebP/APNG 使用首、中、末最多三帧 PNG 视图，记录原件 SHA-256、实际帧位置、总帧数和每个视图 SHA-256。超过三帧的动画明确标记覆盖不完整。旧的仅校验文件签名、不能解析动画信息的静态附件保留原输入兼容性，覆盖记为 unknown，不冒充完整读取。准备成功不等于发出请求，更不等于模型正确理解。

模型输入包含图片来源和动画覆盖说明。自然聊天的逐项 `imageInputs` 是准备状态：`prepared`、`not_provided` 或 `unreadable`；不能把旧的 `imageReadable` 字段当作已提交证据。无视觉能力时不给模型像素，保留明确的文字说明。

每次通过既有来源、取消、快照和额度检查后，才在适配器调用边界记录 `mediaRequests`。生产 rich 适配器从同一 `prepare_input` 序列化投影提取实际图片，边界为 `provider_payload`；不提供投影的兼容适配器记录其实际输入，边界为 `model_adapter`。记录只证明提交尝试，不证明网络传输完成、识图正确或消息发送成功。

记录位于现有 AgentRun 的 `modelSnapshot.mediaRequests`，没有新增素材数据库。每个运行最多保留最近 8 次含图片结果的请求，每次最多 64 项，另有 `totalImages`、`submittedImages`、`omittedImages` 显示截断。每项保留来源、内容版本、视图版本、覆盖和状态；请求有独立 `requestId`。没有路径、下载 URL、base64 或可再次使用的读图权限。`sourceMessageIds` 标识当前请求中的消息；`sourceKind` 与 `originalMessageId` 区分当前原件和引用原件。相同像素来自不同消息时也分别记录。

`pixels_submitted` 表示本请求提供了像素；`unsupported` 表示模型不支持；`not_submitted` 表示已准备但最终投影未包含；`invalid_encoding` 不构成有效像素证据。所有记录都标记 `understanding: quality-unverified`。后续失败不抹掉已提交事实；后续请求必须重新经过权限和生命周期检查。自然缓存关闭、取消、撤权或过期后不能凭旧视图再请求。

QQ `face`、`mface` 通过既有消息文字展示类型、允许的短平台标识和“像素读取不支持、原生发送不支持”。未验证原生适配器能力前不推测图意、不请求隐藏 URL，也不伪造原生发送。每条消息最多展示 16 个原生段；异常 ID 显示无效，省略 URL、密钥和任意说明字段。引用原生段经过同样处理。

[固定样本集](../../tests/fixtures/current-media-quality.json)含 20 个确定性合成图片/GIF场景及期望语义标注。自动测试运行真实文件加载、采样、请求序列化和摘要比对；QQ/自然聊天测试走真实接收与运行存储，网页测试走现有消息组件。远端模型和 QQ 使用替身，语义标注尚未交由真实模型评分；这些结果不能报告为 OCR、图片理解或表情选择质量通过。
