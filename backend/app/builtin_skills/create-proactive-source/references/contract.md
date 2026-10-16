# 来源作者契约参考

版本：meido-source-author-v2 / 2。此文件参与 Skill 支持资源 hash 校验；当前模型只收到 SKILL.md 正文，完整模板已放在正文，不要求读取分发目录。

固定入口与最多32份相对文件合计8MiB，清单64KiB。owner预览只读当前角色workspace，计算真实hash；修正清单由既有文件工具或主人保存。预览、草稿、受信命令、来源grant、合成证明和启用是分别可见的事实。

ACK使用event_ids、ack_id、expires_at和兼容ttl_hours；同ackId持久保存原回执与绝对期限，永久为null/0。不因查询、重启、重试或测试清理而删除业务记录。v2按当前channel检查稳定身份、未ACK重读与重启、空集合和明确读取失败；content检查关联正文，context检查背景数据。context没有ACK；旧v1不追认完整来源检查。

来源：Shiori-Agent 4f266aeaf42fcd611362e2456417b91f659341cf / apps/backend/skills/create-proactive-source/SKILL.md，MIT。上游相对TTL、全局注册、明文env、旧severity/HyDE文案按Meido规格改写。
