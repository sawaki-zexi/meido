# 聊天文档附件校验

状态：当前

最后核验：2026-11-02

事实来源：[附件保存与提取](../../backend/app/chat_attachments.py)、[DOCX API 回归](../../tests/python/test_docx_attachments.py)、[现有附件回归](../../tests/python/test_chat_attachments.py)。

## 入站与拒收

`POST /api/roles/{role_id}/attachments` 沿用单文件 10 MiB 上传限制。入站附件先完成格式检查和文本提取，再创建本次原件及元数据；提取失败不会留下这次上传的附件文件，也不会修改已有附件。这个顺序修复不清理历史文件，不是所有磁盘 I/O 失败的通用事务保障。

DOCX 只读取 ZIP 包内的 `word/document.xml`，不把成员解压到磁盘。正文缺失、CRC 错误、无效压缩数据、损坏 XML（包括未知编码声明）或非 ZIP 内容返回 415，提示重新导出文档或改用 TXT；响应不包含本地路径或异常栈。

正文成员展开字节上限为 **10 MiB**，与压缩后上传大小分别检查。先检查成员声明大小，再最多请求上限加一字节并核对实际结果；超过限额返回 413，提示拆分文档或改用 TXT。允许的正文存储方式为 STORED（0）和 DEFLATE（8），加密正文及其他压缩方法返回 415。

此限额约束正文成员读取，不代表整个 ZIP、XML 解析器总内存或并发响应时间保障。正常 DOCX 仍按原规则提取文字，最多保留 32000 字符；这是模型输入的文字上限，与正文展开字节上限不同。原件下载保持上传的完整原字节，公有附件字段不变。TXT、图片及原有 PDF 提取功能保持现状，本次没有增加 PDF/OCR 能力。

## 显式输出文件

`save_attachment(..., output=True)` 表示发送文件，不请求文档提取。输出 DOCX 不套用入站正文校验，保留原字节、稳定附件 ID 和空提取文本；既有输出文件大小、媒体类型及角色路径约束继续生效。

## 校验依据

- [ECMA-376 Part 2，第五版（2021 年 12 月）](https://ecma-international.org/wp-content/uploads/ECMA-376-2_5th_edition_december_2021.zip)：OPC ZIP 包不允许 DEFLATE 之外的压缩算法或 ZIP 加密；附录 B 的表 B.4 仅支持存储方式 0、8，表 B.5 不支持加密标志。
- [CPython 3.13.9 `zipfile` 读取实现](https://github.com/python/cpython/blob/v3.13.9/Lib/zipfile/__init__.py#L1072)：DEFLATE 解码传入输出长度边界，其他压缩器路径未传入同等限制。因此不能仅凭 `member.read(n)` 返回不超过 `n` 字节，就宣称 BZIP2/LZMA 的内部展开有界。这里按 OPC 的正常压缩边界处理，不另建解码框架。
- [Shiori `c14152e` 的 QQ 附件接收](https://github.com/YinFengWindy/Shiori-Agent/blob/c14152ed225954a2a50da775075fe64dbf7b8b50/plugins/qq/backend/channel/files.py#L103)：参考其先检查大小及内容、再写入文件的顺序。它没有内置 DOCX 解析契约，本实现不是移植其文档解析器。
