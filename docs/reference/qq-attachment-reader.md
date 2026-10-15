# QQ 文本附件读取契约

状态：当前。最后核验：2026-10-16。

事实来源：[工具实现](../../backend/app/channels/attachment_tool.py)、[入站装配](../../backend/app/main.py)、[真实文件测试](../../tests/python/channels/test_attachment_reader.py)、[QQ 入站到模型往返](../../tests/python/channels/test_attachment_turn.py)。对应 [#164](https://github.com/sawaki-zexi/meido/issues/164) 及完整规格 P07。

`read_attachment` 仅读取宿主在当前 QQ 回合提供的附件 ID，包括本条消息与经过实际 QQ 引用解析的附件。参数使用 `attachment_id`，不接受任意路径。角色与渠道 session 必须相符；后续回合和其他群即使知道旧附件 ID，也不能因此读取文件。此工具不要求通用文件或 Shell 授权。

| 参数 | 行为 |
| --- | --- |
| `attachment_id` | 必填，本回合展示的附件 ID。只支持 TXT/MD、UTF-8（可有 BOM）、非 NUL 文本，文件上限 2 MiB。 |
| `offset` | 跳过的行数，默认 0，非负整数。 |
| `limit` | 最多读取行数，默认 50，范围 1–400；普通行页正文最多 10000 UTF-8 字节，超出时要求减少行数或显式字符分页。 |
| `character_offset` | 从此 Unicode 字符位置读取，每页最多 2000 字符。不能与非默认 `offset`/`limit` 混用。 |

原始 UTF-8 字节中任一行超过 8000 字节时，默认自动切换字符分页。阈值按字节计算，页位置按解码后的字符计算，中文与四字节字符不会被按字节截断。字符页保留正文中的 CRLF；UTF-8 BOM 不进入正文。

尚有内容时，结果给出下一页的 `character_offset`；尾页明确“已读到文件末尾”，不再给出相同位置的续读指令。超过文件末尾的字符起点返回空正文和文件末尾位置。长行附件不能用非零行 `offset` 定位，应改为字符位置。

示例：4000 个中文字符为 12000 UTF-8 字节，首次只给 `attachment_id` 即返回前 2000 字符及 `character_offset=2000`；下一次返回剩余 2000 字符与结束标记。

入站测试使用真实 NapCat 解析/引用解析、角色附件文件、SQLite、能力解析和共享模型循环，验证当前/引用结果和续页进入下一 provider 请求、旧回合与其他群 ID 被拒绝。仅远端模型、QQ RPC、媒体下载和头像下载使用可控传输夹具；没有连接真实 QQ 账号或向第三方发送测试消息，这些测试不等于实际 QQ 平台收件验收。
