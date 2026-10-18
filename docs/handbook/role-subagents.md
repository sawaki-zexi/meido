# 配置角色独立子任务

状态：当前。最后核验：2026-10-16。

事实来源：[子任务契约](../reference/owned-subagents.md)、[SpawnSettings](../../web/src/features/role-management/SpawnSettings.tsx)。

1. 独立子任务默认提供。按需要在“设置 → 工具与插件”选择角色、进入“高级运行设置”并调整有限超时、模型请求数和输出预算后保存。
2. 研究 profile 使用冻结的读取/网络工具，脚本 profile 使用任务目录文件工具；命令执行须在“受限任务的命令许可”中单独授予开关与白名单，不继承普通默认 Shell。
3. 让角色把完整、独立的目标交给 `spawn`。默认同步等待结果；显式后台任务返回 ID 后可以继续对话，完成结果回原会话。
4. 在原来源使用 `spawn_manage` 的 list/get 查询或 cancel 停止。其他角色、群、发言者或新绑定无法继承旧任务。
5. 查看完成消息中的真实执行状态和投递状态。`interrupted` 表示重启前未完成；`uncertain` 表示平台可能收到，系统不会自动再次发送。

停止已有子任务使用原来源的 spawn_manage(cancel)；删除角色、来源失效与取消仍会停止任务。旧 spawn.enabled 不再关闭普通委托，Shell/文件默认提供不取消受限 profile 边界。
