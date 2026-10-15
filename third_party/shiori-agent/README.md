# Shiori-Agent attribution

状态：当前。最后核验：2026-10-16。

本项目的 `backend/app/network_tools.py`、`backend/app/network_extract.py` 改写了
[Shiori-Agent](https://github.com/YinFengWindy/Shiori-Agent) 提交
`4f266aeaf42fcd611362e2456417b91f659341cf` 中的
`apps/backend/agent/tools/web_fetch.py` 和 `web_search.py`：业务参数、默认值、
Exa MCP 请求结构和 HTML 转换选项。上游版权和 MIT 许可见 [LICENSE](LICENSE)。

Meido 自行适配 AgentTool、角色授权、宿主搜索配置、DNS 固定连接、逐跳校验、
流式容量控制、可终止解析进程和取消协议；不引入上游工具宿主、数据库或身份系统。
`backend/app/agent_runtime/discovery.py` 另遵循同版 `tool_search.py` 的查询、精确选择、
风险筛选和下一次请求加载行为；冻结目录、不可变版本、来源撤权及持久化协议由 Meido 实现。
更新以人工核对固定上游版本和公开行为测试为准，不自动同步。

## Skill 指令适配

`backend/app/builtin_skills/` 的 weather、summarize、meme-manage、skill-creater 改写自同一固定上游的 `apps/backend/skills/`，保留 MIT 授权。Meido 的 frontmatter、角色 workspace、CLI 版本和权限行为差异见 `docs/reference/shiori-builtin-skills.md`。
