# ADR-0028：主人跨端历史使用冻结授权视图

状态：已接受；替代 ADR-0009 中用近期文本片段补充主人历史的策略，其线程与权限隔离继续有效。

最后核验：2026-10-26

事实来源：[Issue #238](https://github.com/sawaki-zexi/meido/issues/238)、[统一历史实现](../../backend/app/conversation_history.py)、[真实请求测试](../../tests/python/test_owner_history_view_api.py)、[预算测试](../../tests/python/test_owner_history_budget.py)。

## 背景

桌面和主人私聊各自回放当前线程，再补入最近 24 条跨端文本，会丢失较早轮次、图片和对话角色。直接物理合并线程则会改变消息、运行和网页归属。查询工具与自动历史各自计算权限，还可能在等待途中获得新配对权限。

## 决定

在原始线程上构造每回合的 `ConversationHistoryView`。主人桌面与主人私聊共享当前角色桌面和获准私聊；群聊与访客保持当前线程。冻结账号实例、配置代次、身份及配对代次、每个聊天的历史起点和消息边界，后续新配对不扩大旧回合。

入站媒体等待前固定来源，接收后只加入本次消息。排队结束时允许更早合法回合已经确认送达的完整回复进入首次读取；首次读取之后固定消息集合。查询和媒体读取返回、摘要提交、实际模型请求前重新核验权限。解绑后重配建立新历史起点；未解绑重复配对保留原起点，但撤销旧运行许可。

复用结构化 transcript 解码与 ADR-0024 的完整回合裁剪、持久摘要和记忆覆盖证明。摘要来源指纹覆盖所有原始 session 的消息；来源删除、修改或撤权使摘要失效。原始消息、ID、工具归属和网页投影不改写。

附件按当前角色的附件 ID 重新解析，不信任历史 metadata 中的文件路径；引用只能回溯同一来源内可见的原文。失效来源保留缺失提示。自动历史不再重复拼入动态 `RECENT_CONTEXT` 原文，主人长期事实仍按既有记忆规则处理。

## 取舍

每回合读取与核验成本增加，但避免了复制消息、跨线程数据迁移和第二套摘要账本。当前仍以角色锁串行运行模型；投递可以晚于模型结束，尚未确认送达的助手文本不会进入另一个回合。未知模型容量沿用有限完整回合窗口，已配置容量则沿用预算与摘要机制，不承诺无限历史。

对照 [Shiori `context_scope.py`](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/conversation/context_scope.py#L250) 的主人授权视图和 [passive history window](https://github.com/YinFengWindy/Shiori-Agent/blob/d149de7f052f77c5bb88d4d1393d48dce1870875/apps/backend/agent/core/passive_turn/helpers.py#L30)，保留 Meido 独立 transcript 和运行记录，通过投影实现同等跨端接续。
