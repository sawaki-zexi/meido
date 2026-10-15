# 渠道分类目录与连接事实验收

状态：当前验收记录；#170 已实施、复核并进入总 PR #162 集成分支，未合入 main。

最后核验：2026-10-16。生产固定版本 `d0894c3b000e7bb9714287e4fac9dcd5832cee8f`，最终夹具 `8d2ff2a430bb7a8e14c199019088ee34d714a446`，前置审查 `73f08037e36185ca772bd8757ba238121e8a6d10`，放行基线 `4b1b3b8858896b907a147b65612de0bb02896305`；最终集成 `3feeb215773813030ce3fd76d9a086c985f6cfdc`。

事实来源：[Issue #170](https://github.com/sawaki-zexi/meido/issues/170)、[接口契约](../reference/channel-directory.md)、[查询与快照](../../backend/app/channels/directory.py)、[宿主](../../backend/app/channels/host.py)、[公开 HTTP/model 纵切](../../tests/python/test_channel_directory_api.py)、[传输](../../tests/python/channels/test_directory_transport.py)、[轮询代际](../../tests/python/channels/test_telegram_polling_generation.py)。固定提交的最终 CI 见[总 PR 检查](https://github.com/sawaki-zexi/meido/pull/162/checks)；下列数字仅对应明确标注的实际运行。

## 行为与协议

公开聊天 HTTP 实际执行 account_list、分类查询、继续分页或 Telegram 单成员查询；下一次 provider 请求读取真实 ChannelHost、SQLite 与本地平台协议结果。QQ friends/groups/members 各调用一个对应 RPC，入站只保存当前会话和发送者，不遍历全部群成员，也不重新导入无关旧缓存成员。Telegram getChatMember 结果仅为群范围资料，不创建私聊发送目标；QQBot、飞书首期只读 known 缓存。

连接状态取当前 transport/service 观测。账号启用、任务运行或 handler 存在不单独证明在线；网页与工具共用事实投影。测试覆盖 QQ 当前连接观测、QQBot READY/RESUMED、飞书长连接和 Telegram 真实 polling 控制流，不将协议夹具当成真人账号在线证据。

快照固定顺序、时间和总数；每页最多50项，完整观察最多5000条及1MiB，单平台查询最多1 RPC/10秒，查询和快照均有并发与TTL上限。cursor 绑定真实角色、来源、查询与账号配置代际；切换再恢复、重新配对、删号重建或替换 adapter 不复活旧凭证。SQLite 写锁等待中取消会在提交前复核并回滚。已取消 RPC 必须结束后才返回。

真实 localhost WebSocket 验证接收/JSON解析前的字节上限。Telegram 新接口只接受 identity 编码，按 raw body 限量读取；小 gzip 解压成大响应的反例先红后绿，拒绝发生在解压前。HTTP401/403、其他平台拒绝、超时、不支持、空目录分别处理，不回显凭据、请求URL、平台正文或原始异常。

## 独立审查与轮询修复

原版本 `73f0803` 的两位审查者独立确认同一 P1：在途 getUpdates 返回后比较整个 ChannelAccount，把普通 updated_at/last_error/display_name 写入误判为配置替换。连接测试或前次回复失败会让账号仍 enabled，却丢弃本批更新并停止后续轮询。

修复复用账号设置的单调 revision，只有实际凭据、收件策略和启停变化推进代际；诊断和显示更新不推进。poll 与连接快照绑定创建身份和真实配置代际，成功、异常返回都复核 token、代际与停止信号。真正切换后，旧失败也不能给新配置写诊断或重试。唯一生产入口在 test_account 完成身份登记后启动轮询。

两份原独立探针未经改写重新执行：连接测试后 pollTaskFinished 从 true 变 false，getUpdatesCalls 从1变2且 online=true；实际回复异常和清错误后仍交付 update1 并继续轮询；真实 token 替换仍丢弃旧响应并结束。最终两轴修复复核均为0项遗留。

## 固定范围检查

| 检查 | 实际结果与范围 |
| --- | --- |
| 原切片目录/协议专集 | `73f0803`：48 passed；包括公开HTTP到下一provider的Telegram成员纵切 |
| 修复后的相关交叉 | `d0894c3`：148 passed、6条已有SDK弃用警告，73.84秒；含20项轮询代际、目录/传输、Telegram、渠道HTTP/设置/来源与成员 |
| 修复后的旧消费者 | `d0894c3`：62 passed、26.62秒；含角色删除恢复、host发送/权限、历史成员、群取消与身份 |
| Standards 独立复核 | 原探针通过，新增20项代际矩阵20 passed、6.70秒 |
| Spec 独立复核 | 原探针通过，20项矩阵及48项目录/传输共68 passed、26.98秒 |
| 前端 | 174 passed /26 files，TypeScript/Vite通过；修复未改变前端输入，未声称再次全量运行 |
| 静态检查 | 变更Ruff、compileall、diff与相对链接通过；新base/directory/host三源mypy无错误 |
| 既有类型诊断 | 同旗标与基线逐文件、限定函数、完整AST语句、错误码和诊断全文匹配：47项原有错误，新增0、移除0；这些旧模块mypy仍退出1 |
| 最终正确隔离并集 | `8d2ff2a`：210 passed /73.85秒，退出0，无warning/error；完整三项临时环境变量、全部14文件网络guard及两份原探针均通过 |
| 集成核对 | `3feeb21` 无冲突；生产与经复核 `d0894c3` 相同，最终测试/前端输入与经复核 `8d2ff2a` 逐文件一致 |

各测试组有重叠，不相加为唯一覆盖数。作者复核实际命令后确认：早期作者运行设置了 MEIDO_DATA_DIR，但将另外两项误写为 ROLES_DIR 和 OWNER_KNOWLEDGE_DATA_DIR，不能声称 main 导入时的角色及主人知识库全局路径已由这些短名隔离。测试夹具显式 tmp_path/内存凭据仍有实际证据，不据此推断所有导入状态。正确复现须在导入前设置完整的 MEIDO_DATA_DIR、MEIDO_ROLES_DIR 和 MEIDO_OWNER_KNOWLEDGE_DATA_DIR。两位独立审查者的运行使用完整变量名；作者纠正后的最终并集也使用完整变量名，不覆盖之前的事实。

## 纠正隔离后的测试边界复核

第一次使用完整三项环境变量，并将已有 local_platform_only guard 扩展到相关14文件全部210项及清理的运行，实际为210 passed、6 warnings、7 teardown errors，pytest退出1。guard捕获旧渠道设置夹具的q1.qlogo.cn头像请求，以及open.feishu.cn SDK后台连接尝试，均被拒绝；不能只取210 passed宣称成功，也不能据此反推此前无整组guard的运行没有出网。

`2c3f779` 先只修改渠道设置夹具，原7项定向通过，但整组仍210 passed、1 teardown error，退出1：之前两个旧webhook测试的默认SDK连接延续到了后续测试。没有删除guard或隐藏拒绝记录。

最终 `8d2ff2a` 将受控头像读取和本地long_connection_factory放入[共享API夹具](../../tests/python/test_channel_api.py)，真实FeishuWebhookTransport使用子类，保留webhook的isinstance、验签和路由逻辑；[设置模块](../../tests/python/test_channel_settings_api.py)继续显式启用guard。退出时stop并断言线程结束、连接断开，原业务断言没有移除或放宽，生产输入与d0894c3相同。

作者在该准确HEAD使用完整三项变量、14文件全部210项及清理都启用guard，最终210 passed /73.85秒，pytest退出0，没有warning或清理错误；两份原探针同环境退出0。运行前后环境路径、HEAD及clean状态均留证并由根与Spec独立读取核对。最终夹具Standards独立23 passed /12.47秒，Spec独立23 passed /12.54秒；两轴窄复核均无finding，未冒领为两次独立210项。

## 网络隔离与未覆盖项

初期一个不完整 Telegram 测试 transport 缺 get_me，使既有构造器回落到默认 TelegramBotApi，该次运行已终止。可以确认构造回落，但没有当时连接或请求记录，因此初期是否真实出网未知；平台夹具所用值为内存合成凭据。结合前述变量名错误，不把夹具使用临时存储扩大为整次应用导入未访问默认路径的证明。

修正后，新目录、传输和轮询矩阵均在构造服务前启用 local_platform_only，覆盖DNS、直接IP socket connect/connect_ex 和 asyncio后台连接，持续至依赖清理及线程join。普通测试结束断言无非localhost尝试；guard自测单独明确预期拒绝。真实localhost HTTP/WebSocket正常完成，正常目录协议仅调用目录RPC/getChatMember，发送endpoint次数为0。

旧交付回归使用自己的内存发送夹具，验证原账本、跨角色、目录外、配置代际与uncertain不重试，不能将它们描述为真人收件。未取得真实账号在线或真人收件证据，付费模型验收未进行；完整复刻的其他未实现范围不由本票替代。

## 已确认的存量飞书停止问题

额外只读审计在d0894c3和基线4b1b3b8均复现：Runner.stop约10秒成功返回时，受控连接线程仍存活。真实lark-oapi1.7.4同步端点POST未传timeout；让受控成功响应在stop返回后才到达，SDK仍继续调用WebSocket连接器。后者被替换为只记录并立即失败，没有实际socket请求；最终释放/join后线程0，非localhost尝试0。

停止标志阻止新的外层重连和探针中的业务dispatch；直接Transport.stop不等待已派发handler，但Host另有pause_and_drain/event_scope，本审计未证明正常账号切换绕过Host排空。生产资源问题单列[#181](https://github.com/sawaki-zexi/meido/issues/181)，等待#170总CI验证后独立修复；本票的夹具修改没有解决它。
