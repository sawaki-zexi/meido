# Feed Skill 真实依赖与 HTTP 验收

状态：实施中
最后核验：2026-10-16（Windows）
事实来源：[Skill](../../backend/app/builtin_skills/feed-manage/SKILL.md)、[公开 HTTP 回归](../../tests/python/test_feed_skill.py)、[原生 HTTP 用例](../../tests/python/test_feed_skill_native.py)、下列固定上游与外部实证目录。

## 固定来源与依赖

- Shiori-Agent：`4f266aeaf42fcd611362e2456417b91f659341cf`，`apps/backend/skills/feed-manage/SKILL.md`，MIT。只适配其 Skill 指令并保留归属。
- 协议匹配验收服务：[kachofugetsu09/feed-mcp@210a79512c5cec008dd24c70f7c9c4e2616f5034](https://github.com/kachofugetsu09/feed-mcp/tree/210a79512c5cec008dd24c70f7c9c4e2616f5034)。未证实就是 Shiori 实际部署服务；顶层未见 LICENSE，服务源码只在外部 checkout/临时验收副本运行，未 vendoring。
- Python 3.12.10；`mcp==1.30.0`、`requests==2.34.2`，仅独立 venv 的 wheel。原服务依赖未锁；当前 mcp 2.3.0 wheel 无 `mcp.server.fastmcp`，不能盲装最新版。1.30.0 支持 Meido 的 `2025-06-18`。
- 官方 mcp wheel SHA256：`666edb5009503e1047c9d60346a756f94b261f05cc2625f23d41c728ffc484d0`；requests：`2a0d60c172f83ac6ab31e4554906c0f3b3588d37b5cb939b1c061f4907e278e0`。安装 report 保存 files.pythonhosted.org URL 和所有二级包哈希，pip freeze 保存完整安装环境。
- Windows 新 venv 导入 Meido 时首次缺 Asia/Shanghai 时区数据，显式补项目已有依赖 `tzdata==2026.5`；其 wheel SHA256 `b683bd1b6659ddcd810ff02ad09ba821d4bf1065072805063eb35c49617905ac`。

## 已提取的协议差异

原服务公开五工具，前两个覆盖七动作。manage 的 list/subscribe/unsubscribe，query 的 latest/summary/search/catalog；其余主动工具本票不调用。catalog 实际是文章目录，summary 为 sources/items 总量，unsubscribe 使用名称子串及 SQL LIKE 通配符并清历史。query 强制抓取，但失败时可能返回旧缓存而不标 stale。Skill 与[契约](../reference/feed-skill.md)按这些实际结果说明，未照搬旧 Skill 的逐源统计或订阅目录承诺。

对比来源 [kaze-feed@d8261c05f61c1c2c9de24cbe5fea18cd9b6c424f](https://github.com/jackeyfaker77/kaze-feed/tree/d8261c05f61c1c2c9de24cbe5fea18cd9b6c424f)：六工具、mcp 2.3.0，query 只读缓存、显式 poll，新增精确删除/暂停/恢复和 consumer ACK。未将它当作原服务的直接替代，也未执行其 prepare.py。

## 已运行与证据边界

首次公开 HTTP 测试在未添加 Skill 时失败，模型系统上下文缺少 Feed 指引；加入内置 Skill 后通过，未授予 MCP、工具发现或 Shell。后续 HTTP 负例覆盖无配置、无 grant、服务停用、工具关闭、多候选服务以及握手错误。

此前固定 MCP `cef144a8afabf8155fb655296566fac0f3be6426` 的默认环境协议探针完成 49 checks、24 次 tools/call、21 次 localhost 请求，含七动作和重启；它没有覆盖 Skill HTTP。早期 f05 环境遗漏 SYSTEMROOT 的失败与显式控制组保留，cef 修复后默认环境才通过。

本轮真实 HTTP 已走到实际 Feed 订阅/查询，并暴露独立协议探针未覆盖的 schema 子进程中文 JSON 解码问题：合法 catalog 直接 jsonschema 校验通过，隔离 validate_instance 却拒绝。共享修复由集成任务处理；完整 HTTP 七动作必须待修复后重跑，不能以旧 49 checks 代替。

外部证据根：`C:/Users/Elysia/.codex/visualizations/2026/10/15/01a13e82-f9da-7eb1-8b64-ae6ab21c48d0/`。`feed-168-native/default-env-cef144a8/` 保存旧协议探针及 hash；`feed-168-http/` 保存本轮 HTTP 原始模型请求/工具结果（完整验收后更新）。运行服务、数据库、pip 产物和大体积证据未提交仓库。

测试模型是脚本化外部边界，不是付费模型；其断言确认实际 Skill 上下文、目录、schema 和工具返回进入下一模型请求，不能证明任意模型均遵守自然语言指令。未调用真实账号、主动通知或 ACK，本票不能计作 A01/S08/Drift 完成。
