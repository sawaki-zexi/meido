# 群资料完整切片验收记录

状态：当前，#166 本地实现，等待总 PR #162 独立两轴审查。

最后核验：2026-10-16。实施基点：`af61ad33c50599ccbd101d05f76689abfd1eab96`（已集成 #165）。

事实来源：[Issue #166](https://github.com/sawaki-zexi/meido/issues/166)、固定 Shiori `4f266aeaf42fcd611362e2456417b91f659341cf` 的 `group_context.py/group_context_update.py/group_listening.py`、[当前接口契约](../reference/group-context.md)、[工具行为测试](../../tests/python/channels/test_group_tools.py)、[公开 HTTP/provider 测试](../../tests/python/test_group_context_api.py)。

## 已验证行为

- 红例：在旧群之后插入 501 个私聊和 21 个群，原 `list_threads(limit=500)` 过滤方案按群名查询旧群得到 `total=0`。改为数据库按角色/群筛选、线程 ID 稳定排序后，完整目录 22 群分为 20+2，名字查询可找到旧群。
- 工具直接执行拒绝错误参数、额外角色/主人字段、伪造来源和已取消调用；原代码接受额外身份字段和伪造来源的红例已记录于本轮测试输出。
- 群笔记由真实主人桌面、主人私聊和主人群回合跨当前角色的群维护；访客拒绝。主人群旁听只限当前群。成员工具仍是群/访客当前线程、主人桌面/有效私聊角色范围。
- 四工具逐项覆盖平台暂停、账号关闭、屏蔽、会话范围关闭、解绑、重绑、同 ID 账号重建和取消；初次捕获旧入站时也拒绝新账号代际或新绑定。
- 群笔记事务及既有旁听存储入口在 `BEGIN IMMEDIATE` 后消费来源检查。真实 SQLite 锁等待时取消或删除角色均拒绝写入，不产生笔记、设置或 toggle；两个并发 patch 保留彼此未提供字段。
- 公开 HTTP/SSE 从空白角色无用工具不可见，走到保存资料后账号离线仍能列群、读笔记并清空摘要；三个工具结果进入下一次模型请求，目录/schema 一致。原聊天与旁听记录逐项保持不变。
- 私聊及其他角色群 ID 不能读写；账号删除后，主人桌面仍可读取并维护保存的结构化群资料。

## 检查

针对群工具与公开 HTTP 的独立测试：68 passed。渠道、multichannel、来源、成员 HTTP、群 HTTP、工具发现、角色能力的最终组合回归：311 passed。随后只加强锁等待测试的等待断言，重跑该组 4 passed。组合命令：

```powershell
python -m pytest tests/python/channels tests/python/test_multichannel.py tests/python/test_channel_source_authority.py tests/python/test_member_lookup_api.py tests/python/test_group_context_api.py tests/python/test_tool_discovery.py tests/python/test_tool_discovery_api.py tests/python/test_role_capabilities.py -q
```

类型检查使用同一 Python/mypy，对基点和当前分别运行 `group_tools.py/multichannel.py/napcat.py --follow-imports=silent --ignore-missing-imports`，当前额外纳入 `group_access.py`。按文件、错误文本与类型码去除行号后逐条比对：基点 40 项，当前 39 项，无新增；仅消除原 `GroupRepository.update_context(**dict[str, object])` 错误。余下 Telegram 20 项、NapCat 19 项均为原有错误。新群访问/工具两文件单独检查为 0 错误。

变更群模块与测试完整 Ruff 通过；`main.py/multichannel.py` 的 E9/F63/F7/F82 检查通过。`compileall backend tests`、`git diff --check` 和三个关联 Reference 的相对文档链接检查通过。

## 范围限制

外部模型和渠道传输只使用夹具；没有连接真实渠道账号、读取系统凭据或外部发送。群/成员工具的本地数据能力不扩大账号启停、网络发送、原聊天共享或访客权限。未推送单票 PR、合并 main 或关闭 Issue；最终审查及集成由总 PR 流程继续完成。
