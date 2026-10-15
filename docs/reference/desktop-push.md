# 普通对话桌面推送

状态：#167 实现，待总 PR #162 集成与合入。事实来源：[工具](../../backend/app/desktop_push.py)、[原子消息存储](../../backend/app/session_store.py)、[来源测试](../../tests/python/test_desktop_push_authority.py)和[公开 HTTP 测试](../../tests/python/test_desktop_push_api.py)。

`message_push` 是默认关闭的基础工具，独立于渠道账号。主人在网页角色资料勾选“桌面推送”并保存后，新回合可用；关闭后旧调用也会在提交前被拒绝。工具构造和目录预览不创建渠道 push 表、不改变提醒账本、不启动外发服务。

参数 `channel` 只接受 `desktop`，`chat_id` 只接受当前角色自己的 `owner`/`user`。`message` 最多 4000 字符，可为空；`image`、`file`、`attachments` 使用当前角色已保存的 32 位媒体 ID，去重后最多四项，文本和媒体至少一项。每个声明的媒体类型都须匹配，全部附件有效才写一条消息；链接重定向的附件目录拒绝，不读取模型给定的宿主路径。

来源以实际角色、原 run、持久 user 入站、宿主线程/账号和 `FrozenChannelSource` 为准。主人桌面及仍有效配对的主人私聊/群来源可以写当前角色桌面；访客、伪报身份、不同角色/会话、终态或无入站的 run 拒绝。解绑重绑、账号同 ID 重建、平台暂停、角色删除和工具关闭继续实时校验，不凭工具参数选目标。

SQLite `BEGIN IMMEDIATE` 取得写锁后再次校验来源、取消和媒体，再一次提交。角色/run/call 共同生成稳定消息 ID，重放及重启不会重复追加。等待写锁期间撤权或取消不产生晚写；重复取消仍等待实际写线程结束。提交完成后消息独立于父 run，后续父失败或停止不会撤回已投事实。

成功回执为 `status=sent` 与已持久化 `messageId`。消息为 assistant/`desktop_push`，没有新主人输入、正常 run 或记忆/待办维护；原 run/call 仅作来源元数据。公开会话只含安全附件元数据，图片和文件沿用当前角色附件端点及已有焦点/10 秒刷新；媒体可以没有正文。后续模型历史只保留正文和安全附件名称/ID，不加载附件字节或宿主路径。

对外发送使用 `account_send`。现有显式选择的待办 `ChannelPushService.deliver_reminder` 仍沿用原账本与 uncertain 不重试规则。此工具不承担主动决策草稿、Drift 单次发送或通用调度授权；这些运行须按各自协议接入内部交付，不能伪造活跃主人 run 调用普通工具。
