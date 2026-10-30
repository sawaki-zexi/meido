---
id: feed-manage
version: 1.0.0
description: 管理 RSS/Atom 订阅并查询文章。用于询问信息来源、当前订阅、最新动态、订阅概况、搜索或文章目录。
tools: [tool_search, mcp_list, mcp_add]
activation: explicit
---
# Feed 订阅管理与查询

通过主人已配置并授权给当前角色的 MCP 服务完成操作。技能只提供指引，不授予工具、服务、Shell 或外部事务权限。

## 发现并选择实际服务

1. 先确认工具发现可用，用 `tool_search` 的 `select:mcp_list,mcp_add` 加载管理工具，再调用 `mcp_list({})` 查看当前来源真实获准的服务。
2. 使用返回的 `serverId`、显示名称、连接状态和工具目录确认目标。服务 ID 不必为 feed。多个服务可能符合需求时，先向主人列出可用选择并取得明确目标，不按名字猜选，也不批量连接试探。目录为空时说明当前角色没有获准服务，请主人在 MCP 设置中配置并授权。
3. 如目标未连接，调用 `mcp_add({"server_id":"实际返回的ID"})`。只传 server_id；command、cwd、env 和角色授权只能由主人通过现有设置管理。
4. 管理调用成功返回后，用 `tool_search` 加载该服务真实返回的 feed_manage、feed_query 工具。使用目录返回的完整名称和 schema；名称形式通常为 `mcp_<serverId>__<远端名>`，不要把占位符或固定 `mcp_feed__` 当作实际名称。
5. 只调用当前已可见或已成功解锁的工具，并遵循其当前 schema。连接和搜索成功后，同一次模型回复中的后续调用可按顺序使用新解锁工具；连接或搜索尚未成功时不能抢先调用。始终沿用当前来源真实获准且已确认的同一服务，不改用其他服务绕过失败或撤权。

缺少 tool_search、管理工具、服务 grant、连接或对应远端工具时，具体报告缺项和配置方式。工具关闭、角色无权、服务错误或中途撤权均不能声称执行成功；技能不会安装依赖、改命令/环境或重新授予权限。

## 七种操作

下面参数表示协议匹配服务的常见用法；以当前实际 schema、结果和主人请求为准。

| 意图 | 远端工具与参数 | 核验 |
| --- | --- | --- |
| 当前订阅/信息来源 | feed_manage：`{"action":"list"}` | 返回实际名称、订阅 URL、启用状态与备注；不得凭记忆列举 |
| 添加订阅 | feed_manage：`{"action":"subscribe","name":"已确认名称","url":"已提供RSS或Atom地址"}` | 检查返回结果，再 list 核对；保存订阅不等于已经采集或推送 |
| 取消订阅 | feed_manage：`{"action":"unsubscribe","name":"已确认名称"}` | 先新鲜 list 核对删除范围，再核对结果及后续 list；不要猜测目标 |
| 最近文章 | feed_query：`{"action":"latest","limit":10}` | 可按 schema 添加 `source`；保留实际文章标题、时间、来源与原文 URL |
| 概况 | feed_query：`{"action":"summary"}` | 按结果说明总量；原验收服务返回 sources/items 聚合和来源名称，不提供逐源计数或更新时间 |
| 关键词搜索 | feed_query：`{"action":"search","keyword":"已确认关键词"}` | 保留真实匹配；无匹配时如实说明 |
| 文章目录 | feed_query：`{"action":"catalog","page":1,"page_size":20}` | 原验收服务返回文章分页；按 has_more/next_page 获取必要后续页，不把它当订阅 URL 清单 |

主人每次询问“订阅了什么/信息来源是什么”时，本轮必须重新调用 list；即使此前对话已查过，也不能用历史结果代答。订阅 URL 清单来自 list。

原验收服务的 unsubscribe 按名称子串及 SQL LIKE 通配符匹配，可能一次删除多源并清理历史与 ACK。先说明实际匹配范围；同名、包含关系、`%`/`_` 等可能扩大范围或无法确定只删所需对象时，先向主人澄清范围，不承诺精确删除。其他服务若提供 source_id，应按其实际 schema 和已确认来源使用。

## 结果与失败

所有订阅、文章、页数和成功状态都必须来自本轮实际工具结果。连接失败、isError、超时、权限拒绝、返回错误或输出截断时，准确说明已验证的范围；取消/断连后的写入可能结果未知，先 list 确认再决定是否重试。

原验收服务 query 会尝试抓取，但抓取失败后可能仍返回旧缓存，且 isError=false、没有 stale 标记。因此只能说“服务返回的内容”，不能据此声称已刷新、这是刚抓取的最新内容或所有源均正常。服务显式提供错误/采集时间时才据其说明。无法确认新鲜度时直接告知；不要擅自读取服务日志或启用其他工具补权限。

若实际服务的查询仅读缓存（例如 kaze-feed），明确说明这一点；只有主人请求刷新、真实 schema 提供采集工具且既有授权允许时，才按该服务协议显式采集并检查结果。本技能不会自动轮询、开启主动通知、调用 proactive 事件或 ACK，也不代表主动决策或 Drift 已接入。

来源：Shiori-Agent 4f266aeaf42fcd611362e2456417b91f659341cf，apps/backend/skills/feed-manage/SKILL.md（MIT）；按 Meido 格式、动态 MCP 授权和真实协议改写。
行为核验依赖：kachofugetsu09/feed-mcp@210a79512c5cec008dd24c70f7c9c4e2616f5034，仅外部运行验收；不代表已证实 Shiori 的实际部署服务。
