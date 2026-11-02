# 角色卡导出契约

状态：当前

最后核验：2026-11-02

事实来源：[导出服务](../../backend/app/role_card_export.py)、[导入解析](../../backend/app/role_card_import.py)、[API 与边界测试](../../tests/python/test_role_card_export.py)、[下载界面](../../web/src/features/role-management/RoleCardExport.tsx)。

## 已保存资料与下载

资料页提供 JSON、PNG、CHARX 三种格式。创建中的角色、未保存编辑、图片裁剪和保存过程须完成保存后导出；不会自动保存或默默下载旧草稿。切换角色、资料更新、开始编辑或离开页面会取消并丢弃未完成的导出结果。

`GET /api/roles/{role_id}/export?format=json|png|charx` 返回一个附件，默认 JSON。同步路由在线程池内完成磁盘读取和图片处理，不阻塞异步事件循环。响应带 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff` 及安全、有界的 UTF-8 文件名；不持久化导出缓存。不存在的角色返回 404，不支持的格式、素材问题、超过限制或导出期间角色改变返回可重试的 422，错误不暴露本地路径。

`X-Meido-Export-Summary` 是与同一附件一起生成的 UTF-8 JSON 的 Base64，字段为 `format`、`fields`、`includedAssets`、`omittedAssets`、`notes`。除格式外均为字符串数组，内容只有固定字段标签、素材标签和转换说明，不放角色原文或本地路径。跨域配置仅额外暴露这个响应头和 `Content-Disposition`。前端验证摘要后再下载，并释放 Blob URL。

## 字段白名单

输出为 CCv3 `spec: chara_card_v3`、`spec_version: 3.0`。所有规范要求的字符串、数组和 `extensions` 对象均存在；Meido 未使用的标准必需字段取空值。没有运行时间戳或身份标识。

| Meido | CCv3 data |
| --- | --- |
| 名称 | `name` |
| 角色设定 | `description` |
| 性格 | `personality` |
| 行为规则 | `system_prompt` |
| 回复约束 | `post_history_instructions` |
| 简介 | `extensions.meido.description` |
| 对用户的称呼 | `extensions.meido.nickname` |

CCv3 的 `nickname` 表示角色别名，不能承载对用户的称呼；因此导出不设置它。Meido 导入只识别上述扩展中两个字符串，优先于旧的 `extensions.shiori.description`、顶层 `nickname/user_name/userName`；错误类型忽略，显式空字符串保留。旧卡仍兼容。其他客户端可能忽略 Meido 简介和称呼，摘要会告知。文字沿用导入器已有的首尾空白清理，不扩展模板语义。

仅显式构造这些角色数据。不导出模型连接、密钥、代理和权限配置、历史、记忆、主人资料、工作区、运行记录、素材来源或删除状态，也不从扩展恢复运行配置。

## 格式与素材

| 格式 | 内容及差异 |
| --- | --- |
| JSON | 设定，显式 `assets: []`，完全不读取图片文件。 |
| PNG | 当前卡片图优先，其次当前头像，无图时使用 1×1 透明占位。只有单张封面；另一张图片省略。`ccv3` tEXt 保存 Base64 JSON；声明 icon/main 的 `ccdefault:`。Meido 预览可将封面用作头像。 |
| CHARX | ZIP 根 `card.json`，按存在情况包含当前头像和卡片图。固定成员 `assets/icon/images/avatar.png`、`assets/icon/images/card-image.png`；URI 使用规范拼写 `embeded://`。头像为 icon/main，卡片图为 icon/card-image，避免把立绘误标为聊天背景。无图时 `assets: []`。预览中可分别选择图片导入位置。 |

只访问角色目录内固定的 `avatar`、`card-image` 当前槽位，不访问 `avatar-original`、图库、URL 或通配路径。校验角色 ID、目录与终点归属，拒绝符号链接、Windows reparse/junction 和多硬链接文件；打开后核对文件身份，使用有界读取，再检查读取期间文件是否改变。图片只接受声明类型相符的 PNG/JPEG/WebP/GIF，取第一帧解码、按 EXIF 方向转正后从新像素对象重新编码 PNG，剥离 EXIF、旧卡片文本及其他附带信息，再只写新白名单 metadata。动画及原编码不会保留，摘要明确说明。

输入单图最多 10 MiB、1600 万像素；转换后的每图最多 10 MiB，CHARX 图片合计最多 16 MiB。最多两张图片，远低于导入器 24 项素材限制。PNG 的完整文件限制在 10 MiB，以确保其封面可由现有导入器预览。另遵守导入器现有限额：源文件 50 MiB、JSON 10 MiB、PNG metadata 4 MiB、CHARX 128 个成员/单成员 25 MiB/解压合计 200 MiB。超限失败而非静默删除素材，仍可选择 JSON。

## 固定外部参考

- [CCv3 SPEC_V3.md，f3a86af](https://github.com/kwaroran/character-card-spec-v3/blob/f3a86af019fbd99f788f7a1155f399655b34ab35/SPEC_V3.md)：必需字段、nickname、assets 缺省语义和容器格式依据。
- [Shiori card_export/service.py，c14152e](https://github.com/YinFengWindy/Shiori-Agent/blob/c14152ed225954a2a50da775075fe64dbf7b8b50/apps/backend/core/roles/card_export/service.py) 和 [images.py](https://github.com/YinFengWindy/Shiori-Agent/blob/c14152ed225954a2a50da775075fe64dbf7b8b50/apps/backend/core/roles/card_export/images.py)：三格式导出和用户可见差异摘要的行为参考。Meido 实现自行编写，没有复制源码。

Pi 不提供本需求的角色卡格式实现，不作为容器规范依据。
