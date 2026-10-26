# 连接 Grok Bot

状态：当前实现；随本 PR 集成。

最后核验：2026-10-27。

事实来源：[账号页面](../../web/src/features/channels/AccountDetail.tsx)、[Grok 适配器](../../backend/app/channels/grok/adapter.py)、[回程客户端](../../backend/app/channels/grok/client.ps1)。

1. 在全局渠道设置启用 **Grok Bot**，再进入角色的账号列表添加 Grok Bot。
2. 填写固定 Bot 的 HTTPS Webhook、密钥及单次交流上限。上限是双方合计，默认 12 条；更换 Webhook 会建立新会话，旧历史保留。
3. 保存并连接后，状态为 **本机入口已启用**。这只表示本机回程就绪；“检查本机入口”不会向 Bot 外发任务。
4. 复制页面中的 **Routine 配置** 到 Grok 的任务说明。Grok 必须已经获准在运行 Meido 的同一台 Windows 电脑执行其中的 PowerShell 客户端。路径由当前安装与数据位置生成，勿手工复制连接文件内容或密钥。
5. 让角色通过账号目录向唯一 Grok 目标发文字，或按 Routine 从本机客户端主动发起。角色普通回复自动回到同一 Bot，不需要再次用工具发送。

消息页的“已提交 Bot”表示 Webhook 接收任务，不代表 Bot 已处理或回复。超时、断线显示结果不确定，不能自动换消息 ID 重发。客户端回执 `accepted` 表示本机已准入，`duplicate` 表示此前已收到；实际角色回复经 Webhook 到达。

仅支持文字私聊。正文最多 16,000 字符，消息及父消息 ID 最多 128 字符，请求最多 64 KiB。每次新消息使用新 ID；回复带原消息 ID，真正首发才不带父 ID。未知父消息、冲突 ID、达到额度或收据容量满都会明确拒绝。

暂停时本机入口关闭；恢复后使用当前生成的连接文件。更换对端后旧凭据与旧父消息失效。外部 Bot 不参与主人配对，不能读取主人私有会话。
