# 工具搜索相关性与访客可见性修正验收

状态：当前 / 已实现并进入总分支；新集成提交的完整 CI 待核，不构成总目标完成或合并许可。

最后核验：2026-10-17。固定基线 `f4ac973a04cdcd8327f80f20bd58d5088fdf8b77`，独立受审源 `6d04af6129358c5e1fe70daefbde2bd8a8dd93d6`，整合提交 `2d843e29e6fe1949bd22c06874e180b2ec01f4bc`。

事实来源：[发现契约](../reference/tool-discovery.md)、[搜索实现](../../backend/app/agent_runtime/discovery.py)、[公开搜索行为](../../tests/python/test_tool_search_relevance.py)、[HTTP 同轮验证](../../tests/python/test_tool_discovery_api.py)、[Issue #148](https://github.com/sawaki-zexi/meido/issues/148)。固定上游为 [Shiori 4f266 的搜索后端](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/agent/tools/search_backend.py)，MIT 许可保留。

## 已提取的行为变化

- 自然查询“查群笔记”和仅出现在生产提示中的“是谁”可命中相应已授权工具；中英文混合、中文单字/双字、名称片段及反向部分匹配按固定上游规则检索。
- 名称、MCP、提示词和说明分别按契约计分；完整名称优先、同分按名称稳定排序。23处生产构造迁入真实上游注册提示，包含浏览器、桌面和 Coding 动态工具族。
- `search_hint` 是冻结宿主元信息，非空进入目录身份与恢复比较，空值维持旧 payload；不进入 provider 的参数 schema。匹配解释有显式截断标记，完整结果仍受16000字符的有效 JSON 预算约束。
- 访客不再收到主人专用 `message_push` 的 schema，关键词、精确选择及伪造调用不能解锁或写入桌面。原桌面主人和配对渠道主人文本/媒体正向保持通过。
- 授权与风险过滤仍先于排名；结果持久化后，同一 run 下一次 provider 请求才获得新 schema。同一回复提前伪造调用、撤权与恢复的原边界保持。

## 原始验证与独立复核

原件位于实施环境的 `D:/meido-artifacts/`，下列数量属于各自范围，不相加。协调者只读复算和审查没有冒称再次执行测试。

| 证据 | 实际结果 |
| --- | --- |
| `discovery148-tdd-cycles-1-8-v1.json` | 8对原始 RED→GREEN。第三轮4失败→4通过，其余每轮1失败→1通过；RED 包括行为断言及缺少 `why_matched` / `searchHint` 的 KeyError，均不是收集或环境错误。每对测试源SHA相同。 |
| `discovery148-integration-v1-36d327078b03` | 134 passed，0 failure/error/skip/warning，70.97秒；XML有134个唯一用例。包括真实 HTTP/SessionStore、混合查询同轮 schema 与持久审计、撤权及主人/访客桌面推送。浏览器和模型为既有协议夹具。 |
| 同组环境与收尾 | import/collection前设置4个fresh绝对数据根和首次不存在的绝对basetemp；内外localhost守卫保持至原memory/channel关闭返回及线程退出。无未完成渠道任务、新线程或非本地连接记录。未运行原生窗口、驱动、真实账号、模型消费、平台发送或字幕CLI。 |
| `discovery148-compile-types-v1-b162954087c5` | backend/tests编译通过；准确f4ac的完整232文件backend归档、19实际修改源、相同mypy2.1.0参数，原始诊断输出逐字节相同：21→21、0新增。两次mypy自身exit1，**不是全类型通过**。 |
| 固定源复核 | 22交付文件Git blob/raw SHA、最终两组496输入、13份固定上游primary及23构造提示均独立核对。Windows Git归档的230文本为CRLF，与fresh固定Git归档字节相同；仅转换CRLF后对应存储Git对象，没有拼入混合基线。 |

协调者首个静态归档校验曾误把归档原字节当作LF存储对象，按实际行尾转换修正后全部232文件匹配；这不是产品或测试失败，也未重跑应用。作者交付摘要曾把全部RED笼统称为断言失败，规范报告v2及本文保留KeyError的真实分类，原件没有覆盖。

| 原始工件 | SHA256 |
| --- | --- |
| 8轮索引 | `562e733478ef2ff45b86200da191202ba8e4f72b78fd3baabd14a12eec6e90f2` |
| 134项原manifest | `442d8129ec6820e419f6b30ec1cb8641ff527771bbe04ec576e374db7d8a2839` |
| 134项原XML | `2de70c05a1eaf940a0d9acf55840cb44dbcb3190a0c55cfd77ff0fc1727a809b` |
| 类型原manifest | `6b6b67e919966aa96fa4e2c778bb37f63a86a938c578d11a7d46535f96dfe13f` |
| root独立核验 `discovery148-6d04af6-root-source-evidence-verification-v1.json` | `3585d2f6c1e459e7163112a5d4af83fee9dfaf922157a457ddd27719a9387658` |

## 两轴审查与边界

规范轴固定6d04af6：**0 findings**，报告 `discovery148-standards-full-6d04af6-v2.md`，SHA256 `fc6bb2d6678a4d30642166fa14585efde5d593213eaebcfc4be060df45f2998d`。

规格轴固定同一源：**0 findings**，报告 `tool-discovery148-final-spec-review-6d04af6-v1.md`，SHA256 `0270c0922d19489c0c29c832e68f8b2caa9afa049c07cfe7e187888c9576dbbc`。

没有前端改动，未为本修正重复前端本地构建。新总分支准确HEAD仍需完整CI；旧f4ac绿色不作为新提交已通过的证据。[固定f4ac全工具审计](shiori-tool-coverage-audit-2026-10-17.md)保留修正前的G01/G03，不改写历史快照。桌宠、#191及其他真实服务/原生环境缺口见[总状态](../reference/shiori-tool-parity-status.md)，本次未获得新的权限或合并批准。
