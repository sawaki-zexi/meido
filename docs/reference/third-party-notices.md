# 第三方代码与设计引用

状态：当前

最后核验：2026-10-15

多端会话模块参考并改编 [Shiori-Agent](https://github.com/YinFengWindy/Shiori-Agent)，固定基线为 `1c241a2627bbcc99124aef71abaee237ac057499`。引用范围包括渠道账号和身份、入站暂停缓冲、聊天上下文隔离、群旁听以及平台传输行为；具体来源见[源码研究](../research/shiori-multichannel-integration.md)。上游采用 MIT 许可证，保留其版权及许可声明如下。

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

## 通用调度

通用调度的公开 tier/trigger、duration 和五/六字段 cron 语法参考固定 Shiori `4f266aeaf42fcd611362e2456417b91f659341cf` 的 `apps/backend/agent/tools/schedule.py`、`scheduler.py`、`scheduler_cron.py`。Meido 的 SQLite occurrence/outbox、批量日历计数、DST和宿主接收时间规则独立实现；没有复制上游 JSON store 或 schedule→message_push 路径。引用适用上文保留的 Shiori MIT 声明，当前契约见[角色通用时间任务](owned-schedules.md)。

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

Windows x64 托管模式还要求本机已安装官方 Tencent QQ `9.9.31-49738`。
程序核验并使用安装目录的运行组件，不复制或分发 Tencent DLL。
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
