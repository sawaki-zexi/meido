# 第三方代码与设计引用

状态：当前

最后核验：2026-10-26

多端会话模块参考并改编 [Shiori-Agent](https://github.com/YinFengWindy/Shiori-Agent)，固定基线为 `1c241a2627bbcc99124aef71abaee237ac057499`。引用范围包括渠道账号和身份、入站暂停缓冲、聊天上下文隔离、群旁听以及平台传输行为；具体来源见[源码研究](../research/shiori-multichannel-integration.md)。上游采用 MIT 许可证，保留其版权及许可声明如下。

手机导航与消息展示进一步参考 `d149de7f052f77c5bb88d4d1393d48dce1870875`；
`web/src/features/channels/channelMessagePresentation.ts` 的 Markdown 摘要格式改编自该版本
[`apps/desktop/src/shared/chatPreviewText.ts`](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/src/shared/chatPreviewText.ts)。
来源与 Meido 适配边界见[消息源码研究](../research/shiori-channel-message-presentation.md)，适用同一 MIT 声明。

`web/src/ui/MessageMarkdown.tsx` 的 Markdown 插件组合、HTML 原文显示、链接限制与图片替代文字策略
参考同一基线的 [`ChatMarkdownContent.tsx`](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/desktop/renderer/src/chat/ChatMarkdownContent.tsx)。
Meido 将其用于桌面及渠道消息，并适配网页链接、现有附件入口和共享样式；适用同一 MIT 声明。

```text
MIT License

Copyright (c) 2026 YinFengWindy

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 飞书 SDK 连接桥

飞书受管连接固定使用 [larksuite/oapi-sdk-python](https://github.com/larksuite/oapi-sdk-python) `1.7.4`。
`backend/app/channels/feishu_connection.py` 复用已安装 SDK 的原 `_connect` code object，
在私有 globals 中注入连接日志 kwargs，并以局部子类改写端点预取、配置验证和安全日志；
未复制完整 SDK 协议。维护边界见[连接契约](feishu-owned-connection.md)。
分发 SDK 时保留其包内许可，Lark 版权独立于本页 Shiori MIT 声明。

```text
MIT License

Copyright (c) 2023 Lark Technologies Pte. Ltd.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Feed Skill

`backend/app/builtin_skills/feed-manage/SKILL.md` 改编自 Shiori-Agent
`4f266aeaf42fcd611362e2456417b91f659341cf` 的 `apps/backend/skills/feed-manage/SKILL.md`，
适用上文 MIT 版权与许可声明。改写范围包括 Meido frontmatter、实际 MCP 目录/授权和已验证的缓存、分页及删除语义。

协议匹配的 `kachofugetsu09/feed-mcp@210a79512c5cec008dd24c70f7c9c4e2616f5034`
只作为外部独立验收服务，未复制进本仓库；没有声称它就是 Shiori 实际部署服务。

## Owned Herdr

编程原生宿主固定 [herdrdev/herdr](https://github.com/herdrdev/herdr/tree/7e26bad94044eba205033111a0ce8033ede7e24f)
`7e26bad94044eba205033111a0ce8033ede7e24f`（0.9.4），适用 Apache-2.0。
完整上游许可、Meido owned-mode 补丁和固定构建/发行资源哈希保存在
[third_party/herdr-owned](../../third_party/herdr-owned/manifest.json)；补丁不改变上游版权。
运行接口与已验证边界见[Owned编程任务](owned-coding-tasks.md)。

## codex-delegate Skill

`backend/app/builtin_skills/codex-delegate/SKILL.md` 改编自 Shiori-Agent
`4f266aeaf42fcd611362e2456417b91f659341cf` 的 `apps/backend/skills/codex-delegate/SKILL.md`，
Copyright (c) 2026 YinFengWindy，适用本页保留的 MIT 许可全文。
Meido 改写为受控 CodingTask、JSONL 身份与原来源结果交接；未复制或再分发 Codex CLI。

构建依赖的 portable-pty 和 libghostty-vt 均保留其 MIT 上游许可；
实际包内证据见 [portable-pty-LICENSE.md](../../third_party/herdr-owned/portable-pty-LICENSE.md)
与 [libghostty-vt-LICENSE](../../third_party/herdr-owned/libghostty-vt-LICENSE)。
锁定 Cargo metadata 的224个包保留完整包内许可集合
[CARGO-NOTICES.txt](../../third_party/herdr-owned/CARGO-NOTICES.txt) 和
[许可清单](../../third_party/herdr-owned/cargo-license-inventory.json)，该清单包含构建/开发依赖，不能当作二进制SBOM。
部分拆分crate无独立许可文件，另保留其固定源码根的
[Ratatui MIT许可](../../third_party/herdr-owned/ratatui-LICENSE.txt)
及 [WezTerm MIT许可](../../third_party/herdr-owned/wezterm-LICENSE.md)。

Windows发行同时保留官方 Microsoft.Windows.Console.ConPTY `1.24.260710001` 包内
[LICENSE](../../third_party/herdr-owned/Microsoft.Windows.Console.ConPTY-LICENSE.txt) 与
[NOTICE](../../third_party/herdr-owned/Microsoft.Windows.Console.ConPTY-NOTICE.md)，包及三个原生文件的精确哈希见
[ConPTY锁](../../third_party/herdr-owned/conpty.lock.json)。不得仅复制herdr.exe而丢失这些资源和许可。

## 通用调度

通用调度的公开 tier/trigger、duration 和五/六字段 cron 语法参考固定 Shiori `4f266aeaf42fcd611362e2456417b91f659341cf` 的 `apps/backend/agent/tools/schedule.py`、`scheduler.py`、`scheduler_cron.py`。Meido 的 SQLite occurrence/outbox、批量日历计数、DST和宿主接收时间规则独立实现；没有复制上游 JSON store 或 schedule→message_push 路径。引用适用上文保留的 Shiori MIT 声明，当前契约见[角色通用时间任务](owned-schedules.md)。

## Windows Computer Use

八工具 Schema 与窗口引用行为改编自固定 Shiori `4f266aeaf42fcd611362e2456417b91f659341cf` 的 `plugins/computer_use/backend`，适用本页保留的 Shiori MIT 声明。原生驱动由 [trycua/cua](https://github.com/trycua/cua/tree/fc188250b4ca8549b8e61f937fdb1fb560770e86/libs/cua-driver/rust) 固定 commit 的 MIT 源码加仓库最小补丁构建；保留 [Cua AI, Inc. 2025 完整许可](../../native/computer-use/LICENSE-CUA.txt)，打包脚本将该许可随主驱动分发。没有复制或分发 UIAccess helper，当前构建与 hash 见[验收手册](../handbook/computer-use-native-acceptance.md)。

## QQ 托管 NapCat

托管 QQ 的安装、官方 QQ 运行时校验、私有目录、WebUI 与二维码流程改编自
[Shiori QQ 插件](https://github.com/YinFengWindy/Shiori-Agent/tree/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/qq/backend)，
适用上文保留的 MIT 版权及许可声明。进程生命周期复用 Meido 已有 Windows Job 和目录租约。

首次使用下载未修改的 [NapCat v4.18.28 官方 Windows Node 压缩包](https://github.com/NapNeko/NapCatQQ/releases/tag/v4.18.28)，
文件名 `NapCat.Shell.Windows.Node.zip`，SHA-256 为
`fb64fa3b036ad2df1a5d7c204c482694c20e4b763978c8a4968fd3474c05b4a8`。
组件版权属于 © 2024 Mlikiowa；保留包内文件与许可。以下为该固定版本的
[上游许可证](https://github.com/NapNeko/NapCatQQ/blob/v4.18.28/LICENSE)全文。
该组件有非商业使用等限制，其许可不替代 Meido 其余代码的许可。

Windows x64 托管模式始终使用固定运行包内的 QQ wrapper；首次从已安装官方 Tencent QQ
校验并复制 crypto.dll、ssl.dll 到本机私有缓存，已验证的依赖组合为 `9.9.31-49738`、`9.9.36-53644`。
之后启动重新校验缓存，不依赖桌面 QQ 当前版本。本仓库不包含或分发这些额外 DLL。
本机 OneBot/WebUI 的随机 IPC 令牌仅保存在运行内存及 NapCat 所需的运行配置中；
停止后清理配置，重启重新生成，不写入持久账号元数据。QQ 原生登录资料按账号隔离保留。

```text
Limited Redistribution License for NapCat

Copyright © 2024 Mlikiowa

1. Usage and Reproduction:
   - Unauthorized use, reproduction, modification, or distribution of this code is prohibited without explicit permission from the main author of the NapCat repository.

2. Redistribution:
   - Redistribution of this code is permitted, provided that the full text of this license is included, and the source and copyright information is clearly stated.
   - Minor modifications and extensions are allowed for redistribution purposes, but the modified code must not be publicly released.

3. Non-Commercial Use:
   - This code is not to be used for any commercial purposes.

4. Additional Permissions:
   - Any rights not explicitly addressed in this license must be requested from and granted by the main author of the NapCat repository.

5. Disclaimer:
   - This code is provided "as is," without any express or implied warranties, including but not limited to the implied warranties of merchantability and fitness for a particular purpose. In no event shall the author be liable for any damages or other liability arising from, out of, or in connection with the use or distribution of this code.
```

## NovelAI V4.5 请求适配

`backend/app/novelai_generation/parameters.py` 的固定 V4.5 请求常数改编自 Shiori-Agent
`4f266aeaf42fcd611362e2456417b91f659341cf`。Copyright (c) 2026 YinFengWindy，
适用本页 Shiori MIT 许可；完整声明也随模块 `SHIORI-LICENSE.txt` 分发。
NovelAI 为独立服务，其账号、消费与图片条款不由源码 MIT 许可授予。

## 渠道品牌图标

`web/public/assets/channels/` 内置以下官方原始资源，用于识别所连接的软件；不重绘或改变品牌配色。

- `qq.png`：[QQ 官网图标](https://static-res.qq.com/static-res/imqq/qq-logo.png)，来源为 [QQ 官网](https://im.qq.com/index)，用于 QQ 和官方 QQBot 的平台标识。
- `qq-group.png`：[腾讯默认群头像](https://p.qlogo.cn/gh/0/0/100)，仅在缺少实际群头像时使用；不包含用户的自定义群头像。
- `telegram.svg`：[Telegram 官方图标](https://telegram.org/img/t_logo.svg)。
- `feishu.ico`：[飞书官网图标](https://www.feishu.cn/favicon.ico)。

以上品牌图像与商标权利归各自权利人，不纳入前文 Shiori MIT 声明。用户账号、群聊和联系人实际头像仍通过后端缓存提供，不作为仓库静态资源提交。
