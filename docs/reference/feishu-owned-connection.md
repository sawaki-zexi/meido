# 飞书连接与停止资源

状态：已实施并完成两轴复核，已本地集成；未合入 main。最终CI以[总PR检查](https://github.com/sawaki-zexi/meido/pull/162/checks)为准。

最后核验：2026-10-16；对应 [#181](https://github.com/sawaki-zexi/meido/issues/181)。事实来源：[受管 SDK/Runner](../../backend/app/channels/feishu_connection.py)、[Transport](../../backend/app/channels/transports.py)、[账号生命周期](../../backend/app/main.py)、[真实 SDK 协议](../../tests/python/channels/test_feishu_owned_connection.py)、[HTTP 预算](../../tests/python/channels/test_feishu_handshake_budget.py)、[清理所有权](../../tests/python/channels/test_feishu_runner_cleanup.py)、[公开 HTTP 行为](../../tests/python/test_feishu_lifecycle_api.py)。

## 连接边界

飞书保留独立线程与事件循环。工厂、SDK Client 和缓存任务在该循环内创建；HTTP 握手改为异步请求，不调用 SDK 同步 requests.post。已有 AppID/AppSecret、事件处理器、WebSocket 协议和 SDK 接收/心跳被复用，没有新增工具、HTTP API、持久表或权限。

保存的 HTTPS endpoint 同时参与 REST 和既有 SDK discovery。Transport 先 rstrip('/')；REST 使用完整 API 根，discovery 保留 split('/open-apis', 1)[0] 后拼 /callback/ws/endpoint。默认根仍是飞书官方地址，已有主人自定义 HTTPS 地址继续可用；不新增 URL 入口、不暗加官方 hostname 白名单、不跟随重定向、不关闭 TLS，也不从环境继承新凭据。

## 预算与配置

| 范围 | 当前界限 |
| --- | --- |
| discovery HTTP | 整个请求及流读取总计20秒，另有阶段超时；原始响应正文64 KiB |
| 编码 | 请求 identity；解码前拒绝其他压缩编码；严格 UTF-8 |
| JSON | 深度8、节点256，未知扩展内容也计预算 |
| WebSocket URL | UTF-8 8 KiB；校验协议、host、端口、device_id/service_id，复用原 SDK 连接 |
| WebSocket 开放握手 | 固定 SDK/依赖的原10秒默认值 |
| disconnect | 5秒后标记 cleanup_pending，仍持有并等待同一执行，不创建替代连接 |
| stop | 10秒仍未退出则明确失败并保留拥有关系；后续显式 stop 可继续等待/重试 |

Content-Length 仅用于早拒，实际 raw 流累计也受限。成功、错误、缺少长度与慢速分块均受界限；不先完整物化或解压再检查。

discovery 与 PONG 都经过同一个配置校验，先验证完整补丁再改 SDK。缺省字段保留当前值；ReconnectCount 为整数，允许默认哨兵 -1，范围 -1–10000；ReconnectNonce/Interval 为有限数值，范围0–86400；PingInterval 为有限数值，范围0.01–86400秒。拒绝 bool、非法范围与非有限数值，不把缺省 None 写入心跳。默认心跳仍120秒。

## 停止与失败

Runner 持有单一 Connection、监督和清理任务。停止先封闭新派发，再取消握手/监督；已开始的 owned disconnect 被保护，调用方重复取消也等待同一清理。清理异常、超时或自行返回 CancelledError 均保留原对象，等待显式 stop 重试；清理未完成时不调用新 factory。

只有监督、SDK 子任务、异步生成器、事件循环完成清理，且原线程确实退出后，才报告 stopped 并清 runner 引用。join 超时不算成功。调用方取消在已持有清理完成后传播；整批渠道关闭也不因第一个账号的取消遗漏其他账号。

已获准 handler 继续由 Host pause_and_drain/event_scope 排空，不取消其他角色或已获准主人回合。Transport 停止 latch 拒绝新 SDK 派发和 webhook，配置停止失败后恢复准入也不会使旧 transport 再接新事件。

启动/保存失败且清理未收敛时，Host、adapter、transport 成组保留；账号安全停用，公开接口返回失败。已有保存配置及密钥恢复旧值，与内存中冻结新参数的 candidate 分开。显式重试先收回 candidate，再按当前保存配置新建连接，不能复用 candidate 新 endpoint/secret 或随失败丢失旧值。新建账号无旧配置时保留安全停用的 candidate，显式删除仍先清理再删除。

平台错误保留 SDK ClientException/ServerException 与 code 分类，正文换成安全原因。SDK 日志只保留安全事件/异常类型；WebSocket wire logger 为连接私有且不传播内容；已完成 SDK task 的异常也被读取，避免 asyncio 未取异常回显远端 close reason、URL 或正文。

## SDK 维护与限制

requirements 固定 lark-oapi==1.7.4。原 SDK _connect code object 在保留 defaults/closure 的私有 globals 中执行，只注入原连接 kwargs 加私有 wire logger；不全局修改 websockets.connect 或其他角色 logger，不复制整套 SDK 协议。关键接缝不匹配时失败，不回退同步网络。

私有接口需要固定版本及真实 SDK localhost HTTP/WebSocket 契约测试。上游 [1.7.4→1.7.5](https://github.com/larksuite/oapi-sdk-python/compare/v1.7.4...v1.7.5) 新增可选连接 host/headers，并改变私有 kwargs 接缝；Meido 未暴露这些可选参数，本票未声称1.7.5兼容。SDK要求 websockets<16，与项目范围联合为>=14,<16，未声称16/17支持。版权见[第三方声明](third-party-notices.md)。

namespace 入口在缺 pkg_resources 时有 pkgutil 回退，隔离子进程阻断后完整导入成功，因此没有新增 setuptools 依赖。58c基线 Linux CI 实际安装1.7.5，本票本地验证1.7.4，不混作同版本证据。

系统 resolver、首次同步依赖导入等步骤不能被 asyncio 强杀；stop 达到截止保留对象并明确失败，不声称任意 OS 故障下所有原生线程均有绝对墙钟硬限。localhost 夹具不代表真实飞书账号在线或真人收件，真实平台验收仍需具体授权。
