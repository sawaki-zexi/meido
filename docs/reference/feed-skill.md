# Feed Skill 契约

状态：当前
最后核验：2026-10-16
事实来源：[Skill 正文](../../backend/app/builtin_skills/feed-manage/SKILL.md)、[SkillRegistry](../../backend/app/agent_runtime/skills.py)、[MCP 契约](mcp-services.md)、[HTTP 行为测试](../../tests/python/test_feed_skill.py)、[真实依赖 HTTP 测试](../../tests/python/test_feed_skill_native.py)。对应 #168 / S03。

## 激活与授权

内置 `feed-manage@1.0.0` 使用 explicit 激活。聊天 `$feed-manage` 或 HTTP `skillIds:["feed-manage"]` 选择同一份指引；角色/项目的同名覆盖仍遵守现有 Skill 优先级、信任与 hash 校验。

frontmatter 仅声明 `tool_search`、`mcp_list`、`mcp_add`。它不声明固定远端名字、不修改角色开关、不增加服务 grant，也不传 Shell、bin 或环境凭据。服务在独立进程中运行，其 Python 不需要位于 Meido PATH；当前 SkillRegistry 没有 `requires.mcp` 字段，服务依赖通过实际管理结果解释。

主人通过已有 MCP 设置保存 argv/cwd/env 和授权角色。模型只可 `mcp_add({server_id})`，不能提交启动参数或增授角色。没有获准服务、工具关闭、错误、撤权等执行边界由现有 MCP 宿主校验；Skill 的自然语言规则本身不是权限校验。

## 目录和数据流

`tool_search` 加载管理工具 → `mcp_list` 获取当前来源可用服务 → 明确选择 → 必要时 `mcp_add` → 管理结果持久化 → 下一模型请求搜索远端工具 → 后续请求按实际 schema 调用 → 工具结果进入下一模型请求和对话记录。

显示名称和 ID 来自目录。ID 可以是 `reading_room` 等任意合法值，实际工具名为 `mcp_<serverId>__<remoteName>`。多个候选服务时，指引要求先让主人选择；没有新增自动识别 Feed、批量试连或选源 UI。测试用脚本化模型边界验证真实目录/结果可达，不证明任意模型都会遵守文字指令。

## 七动作和兼容差异

| 用户意图 | 工具动作 | 固定验收服务的实际语义 |
| --- | --- | --- |
| 当前来源 | feed_manage/list | 订阅名称、URL、状态、备注；每次询问必须重新调用 |
| 添加 | feed_manage/subscribe | 保存订阅；不等于抓取或推送完成 |
| 删除 | feed_manage/unsubscribe | 名称子串和 SQL LIKE 通配符匹配，可能批量删除并清历史；先明确范围，再 list 核对 |
| 文章 | feed_query/latest | 返回已有或抓取后的文章、来源、时间与原文 URL |
| 概况 | feed_query/summary | sources/items 总量和来源名称，不是逐源统计 |
| 搜索 | feed_query/search | 按关键词筛选；可用实际 schema 的 source 限定来源 |
| 目录 | feed_query/catalog | 文章分页，含 page/page_size/has_more/next_page；订阅 URL 清单应使用 list |

固定验收依赖是外部 `kachofugetsu09/feed-mcp@210a79512c5cec008dd24c70f7c9c4e2616f5034`。它与 Shiori 的工具协议吻合，未证实为 Shiori 实际部署服务，也没有把其源码或依赖加入 Meido。

原服务 query 会尝试刷新，但 HTTP 503 后可以返回旧缓存且 `isError=false`，结果没有 stale 标记。指引要求称“服务返回的内容”，无法确认新鲜度时说明限制，不能推断刷新成功。其他服务若采用只读缓存，须依据其实际 schema 和显式采集结果说明，不沿用原服务的刷新承诺。

连接错误、isError、超时、拒绝、截断或返回错误都不能冒充成功。写入取消/断连后可能结果未知，先独立查询状态再决定是否重试。Skill 不擅自读取服务日志或切换服务扩大授权。

本票不自动轮询、主动发通知、消费主动事件或 ACK；Feed 管理可用不代表主动决策、主动来源或 Drift 已实现。准备与复验见[手册](../handbook/feed-skill.md)，实证与限制见[验收记录](../research/feed-skill-native-acceptance-2026-10-16.md)。
