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

## 桌宠格式与播放器

`backend/app/desktop_pet/packages.py` 适配同一固定提交的 `plugins/desktop_pet/backend/package_archive.py`、`package_images.py`、`pet_packages.py` 中的包格式、图片和动作规则；`desktop-pet/renderer/spriteContract.ts`、`main.tsx` 适配 `plugins/desktop_pet/surface/spriteContract.ts`、`CodexSpritePetRenderer.tsx` 的 atlas、节拍及播放逻辑。Copyright (c) 2026 YinFengWindy，MIT 许可见本目录 LICENSE。

Meido 自行实现角色授权、受管 stdio/Windows Job、Electron 主进程与 preload、动作回执和配置界面；不包含上游语音、气泡或通用 surface 宿主。Windows 本地发行目录保留本声明与 Electron 自带的 LICENSE、LICENSES.chromium.html。

## Grok Bot 回程与交流协议

`backend/app/channels/grok/client.ps1` 和 `protocol.py` 改写自 Shiori-Agent 固定提交
`d3acaed186b172a78a5892e0307e9affd89759f0` 的 `plugins/grok_bot/client.ps1` 和
`plugins/grok_bot/backend/protocol.py`。同目录 `adapter.py`、`exchange.py`、`config.py`
适配该提交的 `plugins/grok_bot/backend/connection.py`、`exchange.py`、`config.py`、
`transport.py` 中固定 Bot、回程、交流预算与接收回执行为。
Copyright (c) 2026 YinFengWindy，MIT 许可见本目录 [LICENSE](LICENSE)。

Meido 使用自身账号生命周期、SQLite 收据和渠道投递账本；未知父消息拒绝、访客权限及
`transport_acceptance` 的宿主边界见 [ADR-0030](../../docs/adr/0030-fixed-grok-bot-exchanges.md)，
不引入上游插件宿主或默认授予完整工具权限。
