# 素材说明与收藏范围

状态：当前
最后核验：2026-11-15
事实来源：[语义投影](../../backend/app/channels/meme_semantics.py)、[收藏与候选](../../backend/app/channels/meme_library.py)、[管理接口](../../backend/app/material_api.py)、[行为验收](../../tests/python/test_material_semantics.py)、[渠道验收](../../tests/python/channels/test_material_message_scope.py)。

## 权威数据

原件继续位于角色 `workspace/memes/<分类>/`；分类、别名与分类开关继续由 `manifest.json` 管理。逐图记录沿用 `workspace/.meido-memes/chat/<原件 SHA-256>.json`，版本为 2。这些记录是持久业务数据，不能当作搜索缓存清理。

| 字段 | 含义 |
| --- | --- |
| `assetId`、`roleId`、`sha256`、`paths` | 角色内稳定身份、内容版本及已知原件位置 |
| `name`、`description`、`enabled` | 名称、说明、逐图聊天开关；仍受分类开关限制 |
| `revision`、`history` | 修订号及旧内容摘要、说明、作者和依据快照 |
| `basis`、`author` | 主人确认、模型观察、合格旧说明或未核验；模型依据包含实际请求 ID、原件摘要与覆盖信息 |
| `sources` | 多个实际收藏来源；角色范围或会话范围及冻结授权。重复收藏的不同建议保留在来源记录，不覆盖生效的主人说明 |
| `semanticStatus` | `ready` 可自动推荐；`pending` 待补充；`content_changed` 原件已替换，等待重新确认 |

旧 chat 记录、匹配摘要与路径的 Drift metadata、分类说明按旧说明兼容读取；不伪造新增视觉观察。已经存在版本 2 记录时，旧 metadata 或类别说明不能覆盖它。新记录空说明不回退为文件名或类别描述。已知位置内容改变时保留稳定身份和旧说明历史，新的字节不沿用旧视觉依据。

记录最多 512 KiB，最多 128 条来源、128 条已存修订；超过上限明确失败并保留旧记录。当前路径匹配使用有界扫描，尚未引入新搜索索引。

## 写入与使用

普通 `chat_meme_save` 同时接收可选说明；省略分类时使用“收藏”。宿主从真实主人本轮文本确认说明，或从当前 run 的 T1 `mediaRequests` 中匹配原件摘要及 `pixels_submitted` 记录。工具参数不能自称 `owner_confirmed`。宿主当前附件选择与实际权限仍是收藏前提。

`POST /api/roles/{role_id}/materials/from-message` 接收实际 `messageId`、`attachmentId` 和可选 `description`，只从该角色真实消息和附件登记解析来源；渠道来源保留实际会话、账号及授权代际。`PATCH /api/roles/{role_id}/materials/{id}` 接收 `expectedRevision` 及 `name`、`description`、`enabled` 的非空修改集合。旧列表版本返回 409，不自动重试。

所有修改复用现有 owner publication journal、文件锁、受管配额与恢复机制。修订后已有候选失效；模型的新建议不能覆盖主人确认。直接上传是主人明确添加角色级来源，公开管理中的改名、说明和开关不扩大来源范围。

已收藏原件不依赖输入附件或在线连接继续存在。每次选用、准备引用和发送仍检查冻结账号、配对、群回复代际及原会话；断网与撤权不同。待补充素材只允许主人明确选定后发送，普通自动回复和自然参与候选均排除它。移出收藏沿用本地恢复区，不删除已有消息附件。

公开响应提供依据、作者、历史和来源数量/范围标签，不暴露私有来源授权字段或路径。名称、说明及其他元数据始终是资料，不成为工具权限或系统指令。
