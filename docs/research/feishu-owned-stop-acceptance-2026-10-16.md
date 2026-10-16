# 飞书受管握手与停止验收

状态：实施验证记录，待两轴独立复核及总集成；未合入 main。

最后核验：2026-10-16。Issue [#181](https://github.com/sawaki-zexi/meido/issues/181)，授权基线 `58c3fa16c5a3bb7df88ebfd4f188e33a20f68a7b`；源码及四份测试见[连接契约](../reference/feishu-owned-connection.md)。基线两套 CI 各1780 passed/16 skipped、前端174 passed/26 files及构建，不代替本票最终 CI。

## 缺陷与红绿

旧生产和4b1b3b8对照均证明：Runner.stop约10秒成功返回，线程仍活。实际SDK1.7.4端点发现是无 timeout 的同步 requests.post；受控成功回包在stop返回后才释放，SDK仍调用WebSocket连接步骤。只读探针把该连接器替换为记录后立即失败，不计真实平台连接成功。

首条实施RED使用真实SDK与真实localhost HTTP挂起响应：stop成功返回却thread alive。原始重跑1 failed/52.25秒，包含首次依赖加载，不能当作stop耗时。公开HTTP停用也在旧版返回200后线程仍活。后续RED证明启用失败遗失拥有映射、保存回滚遗失仍活candidate、停止超时后无法显式重试、重复取消shutdown遗漏另一账号。

修复后SDK在HTTP等待期间可取消并退出，已开始disconnect不被stop/重复调用方取消打断。disconnect异常、超时或自行返回CancelledError均保留同对象和重试责任，不创建替代factory；仅成功清理后报告stopped。已有账号失败保存恢复旧endpoint/secret，仍活candidate冻结新参数；POST enable或PUT显式重试先收回它，再按旧保存配置新建。新建账号无旧值时安全停用并保留candidate，显式delete仍先清理。

真实HTTP和WebSocket TCP准入观察证明：深12层、节点300、超长URL在应用边界拒绝，不借服务端解析失败冒充提前阻断。无长度70KiB原始响应和小gzip解压2MiB反例在raw/编码界限拒绝。慢分块每0.1秒16字节、计划50块，2秒总截止在真实origin已进入后有限失败，未等完整流。

真实SDK WebSocket交付event、回传协议响应并继续心跳；partial PONG保留握手值，ReconnectCount=-1可用，非法0/Infinity补丁不破坏原值。SDK/HTTP错误保留安全分类和code。DEBUG日志反例真实泄漏query和远端close reason后，私有kwargs/code-object接缝、SDK安全格式及完成task异常读取实现绿；成功和拒绝握手均验证，不依赖全局WARNING。

## 夹具修正与证据限制

以下保留为失败历史，不计生产通过：

- 协议夹具第一稿Frame缺SeqID/LogID/service，44.71秒失败发生在服务端序列化，未到partial-config断言。补齐后得到有效生产RED：event已交付但第二次心跳超时，41.83秒。
- 首次WS日志夹具未在计时前加载SDK，3秒等待和10秒stop在惰性导入期间超时，实际1 failed及1 teardown error。撤guard时仍有1连接线程；guard覆盖窗口内无非localhost尝试，撤除后未知，不能用隔离进程后来退出倒算全段线程0/网络0。修正版在guard内预加载，finally直到原线程真实join才撤guard，才取得有效日志RED（41.11秒）。
- 慢分块第一稿总截止0.2秒，HTTPClient初始化已耗完预算，没进本地handler，是夹具时限错误；改2秒后实际流通过。
- size拒绝初期出现HTTPX异步生成器aclose未await；补显式aclosing与loop.shutdown_asyncgens后最终专集不再出现该资源告警。
- 早期回滚测试只要求保留candidate新参数，未验证保存旧值，是测试契约遗漏。按ADR修正取得2个真实RED后，才验证旧保存值/冻结candidate分开和显式重试恢复。

有效运行在导入应用/构造SDK前设置完整绝对临时MEIDO_DATA_DIR、MEIDO_ROLES_DIR、MEIDO_OWNER_KNOWLEDGE_DATA_DIR。local_platform_only覆盖DNS、socket connect/connect_ex及asyncio create_connection；记录不清空，直到handler/socket关闭和线程join才撤。同步POST另计数，生产catch不能隐藏尝试。最终有效运行两种尝试计数0、自有连接线程0。

只有合成凭据和真实localhost HTTP/WebSocket，没有真实账号、模型请求或对外业务发送。API用实际Host/adapter/Transport/Runner，部分失败路径明确注入start后抛错或stop拒绝，属于所有权故障缝隙，不冒称真实平台故障。未启动的maintenance/托管QQ钩子显式替换，不声称覆盖应用其他生命周期服务。

## 检查结果

| 范围 | 实际结果 |
| --- | --- |
| 最终四文件专集 | 33 passed/6条SDK依赖弃用warning，51.11秒；含4条Runner失败/取消，14条公开生命周期及真实SDK协议/预算；无资源warning、未取task异常或teardown error |
| 更大渠道交叉 | 427 passed/6条SDK弃用warning，164.67秒；channels、API设置/目录/来源/身份和multichannel，完整三环境变量和整组guard。随后补disconnect自行CancelledError反例及窄修，再由上述最终专集验证 |
| 新模块静态 | mypy通过，Ruff、编译及diff通过 |
| 旧模块类型 | main/transports同旗标与准确58c按文件、限定函数、完整AST语句、错误码及全文比较：25→22，22匹配、新增0、移除3；旧模块仍退出1，不声称全仓mypy干净 |
| 前端 | 无输入变化，继承准确58c两套174项及TypeScript/Vite构建，不声称重跑 |

各组重叠，不相加为唯一覆盖数。原始命令/日志及夹具边界说明保存于仓库外D:/meido-artifacts/feishu181-*。公开复现：在仓库根设置三项完整临时变量后运行 `python -m pytest tests/python/channels tests/python/test_feishu_lifecycle_api.py tests/python/test_channel_api.py tests/python/test_channel_settings_api.py tests/python/test_channel_directory_api.py tests/python/test_channel_source_authority.py tests/python/test_channel_identity_parity.py tests/python/test_multichannel.py -q -ra -o usefixtures=local_platform_only`。

最初误用不存在的pytest --usefixtures参数被命令解析拒绝，未执行测试；改-o usefixtures=local_platform_only，没有绕过guard。SDK隔离核验还证明阻断pkg_resources后namespace回退及完整导入成功，未新增setuptools依赖。基线Linux实际SDK1.7.5与本票固定1.7.4有明确区别，见Reference。

系统resolver/同步导入不可被asyncio强杀，超时必须失败并保留归属；真实平台账号在线/真人收件尚未验证。总PR合并、Issue关闭及完整复刻不由本票替代。
