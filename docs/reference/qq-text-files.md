# QQ 文本文件接收

状态：当前分支实现，合入后生效。

最后核验：2026-10-26。

事实来源：[接收实现](../../backend/app/channels/qq_files.py)、[地址与下载边界](../../backend/app/channels/media.py)、[接收验收](../../tests/python/channels/test_qq_file_receiving.py)、[实际回合读取](../../tests/python/channels/test_attachment_turn.py)。关联 [Issue #236](https://github.com/sawaki-zexi/meido/issues/236)。

## 接收契约

文件描述保留名称（`name`/`file` 的 basename）、文件编号（`file_id`/`id`）、大小（`size`/`file_size`）和已准入消息所属聊天。CQ 字符串按实体转义解码一次，不解码编号中的百分号，也不把平台路径当作本机路径。

仅接收 `.txt`、`.md`，每个至多 2 MiB；读取前检查声明大小，下载中检查实际字节，拒绝二进制控制字符和非 UTF-8 文本。每条消息最多下载 4 个附件，引用使用同样规则。地址查询和下载共用 20 秒总预算。来源许可在异步边界后再次检查，账号/平台停用、账号替换、配对撤销或代次变化不能通过重新启用恢复旧下载。

成功文件保存为角色附件，由当前回合的 `read_attachment` 分页读取；后续回合或其他聊天不能借旧 ID 读取。手机沿用现有附件链接和引用附件展示。失败保留文件名和原因：格式不支持、超限、无地址、地址不可信、超时、下载失败、编码错误或保存失败；失败不会产生可读取的附件 ID。普通旁听保留名称，不查询地址或下载。

## NapCat 固定版本契约

Meido 当前受管版本为 `v4.18.28`。依据运行代码而非示例字段：

| 情况 | 调用 | 参数 |
| --- | --- | --- |
| 群文件没有直链 | `get_group_file_url` | 当前聊天 `group_id` 字符串、`file_id` 字符串 |
| 私聊文件没有直链 | `get_private_file_url` | `file_id` 字符串 |
| 已有可信直链 | 直接下载 | 不重复查询 |

返回值中的 `url` 可为空。接口实现见 [群文件](https://github.com/NapNeko/NapCatQQ/blob/v4.18.28/packages/napcat-onebot/action/file/GetGroupFileUrl.ts)、[私聊文件](https://github.com/NapNeko/NapCatQQ/blob/v4.18.28/packages/napcat-onebot/action/file/GetPrivateFileUrl.ts)。该版本群文件地址构造为 HTTPS，私聊地址使用平台返回的 HTTP server/port，见 [operationContext](https://github.com/NapNeko/NapCatQQ/blob/v4.18.28/packages/napcat-core/packet/context/operationContext.ts#L265-L278)。因此文件专用下载器接受 QQ 官方域名的 HTTP/HTTPS；只有通过文件编号查询得到的地址可以使用公网 IP。拒绝本机、内网地址、任意外部域名、用户密码、重定向和本机文件路径。图片下载范围保持原约束。

## Shiori 对照与验证范围

对照 [Shiori `d149de7f` 文件接收](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/plugins/qq/backend/channel/files.py) 的文本格式、大小、UTF-8、文件名占位和失败呈现；Meido 复用既有附件存储与回合权限，不新增独立文件读取工具。Meido 的地址范围和重定向策略较 Shiori 严格。

自动化通过真实解析、持久存储、Runtime 和模型工具读取验证，外部平台与 HTTP 用受控传输替身；覆盖群/私聊、当前/引用文件、地址/编号、失败呈现、普通旁听和下载期间撤权。真实 QQ 账号收发尚未执行，部署时需另行授权验收；此处不把替身测试标为真实平台验证。
