# 配置和验收 MCP 服务

状态：当前

最后核验：2026-10-16

事实来源：[设置 API](../../backend/app/mcp_api.py)、[MCP 契约](../reference/mcp-services.md)、[stdio fixture](../../tests/fixtures/mcp_stdio_server.py)、[HTTP 验收](../../tests/python/test_mcp_http.py)。

## 主人配置

1. 在设置的 MCP 页面创建服务。填写稳定服务 ID、名称、可执行文件绝对路径及固定参数、存在的工作目录；逐项填写 env 变量，凭据放 env。
2. 选择授权角色及服务风险。远端宣称只读不会降低这里的风险。保留“启用工具发现”可同时给所选角色启用 tool_search 和 MCP 管理工具。
3. 保存。此时仅保存配置和凭据，不启动程序。重新打开设置只能看见 env 变量名；编辑时留空保留已有值，删除变量后保存会撤去该变量。
4. 在获授权角色的对话要求连接该服务 ID。角色先搜索 mcp_add、连接，再搜索远端名称，可在本次对话继续调用。
5. 要停止该来源后续自动连接，让角色调用 mcp_remove。关闭/修改/删除主人配置会撤销所有来源旧配置的权限及连接意愿。角色授权列表中移除角色后，该角色不能再连接。

成功 add 后，下一次同来源对话会重新握手发现；前一次 run 的进程已释放。后端重启同样只恢复握手与发现，不自动重放任何业务调用。设置中的连接意愿数与活动进程数因此可能不同。

## 本地验收

无需第三方服务、网络账号或付费 API。fixture 是真实 Python 子进程；模型与平台传输使用测试替身。执行：

```powershell
python -m pytest tests/python/test_mcp_stdio.py tests/python/test_mcp_http.py tests/python/test_mcp_origin.py tests/python/test_mcp_schema.py -q
python -m compileall -q backend tests
python -m mypy backend/app/mcp_config.py backend/app/mcp_api.py backend/app/mcp_runtime.py backend/app/mcp_stdio.py --follow-imports=silent
```

用例覆盖主人保存不连接、无密钥目录、两页握手发现、同 run 搜索/执行、真实文件效果、宿主/SQLite 重建恢复意愿、移除和重新添加版本链、跨角色/线程/绑定隔离、初始握手与在途撤权、重复取消及真实子孙清理。协议用例覆盖坏消息、输出/目录/页数/工具数/名字预算、超时、schema 深度和离线引用。恶意正则/引用膨胀用例自身运行在有整树所有权的隔离测试进程中，15 秒外层超时防止错误实现卡死测试运行器。

## 故障处理

| 现象 | 处理 |
| --- | --- |
| 配置 422 | 检查固定 argv、绝对文件/目录、角色、env 名称及预算；新变量需要值 |
| 配置 503 | 检查本机 CredentialStore 与配置目录是否可写；错误不包含提交的密钥 |
| mcp_add 未授权 | 在设置中授权当前角色，并在新 run 重试；旧 run 不接受后来新增的 grant |
| 握手/目录失败 | 检查服务是否实现 2025-06-18 tools 协议、stdout 是否只写逐行 JSON-RPC，以及目录/schema/名称预算 |
| schema validation exceeded time budget | 服务 schema 的验证成本超限；调整正则/引用结构后更新服务或开启新 run |
| 服务退出、坏结果或断连 | 用 mcp_list 看安全错误；add 可以重新握手。同 run 声明变化时开启新 run |
| Stop/超时/断连后的外部效果不明确 | 先独立确认远端事务状态；宿主不会自动补发 tools/call |
| 平台恢复后旧 run 仍被拒绝 | 已观察的来源撤权不会在旧 run 内恢复；从仍有有效主人绑定的新入站消息继续 |
