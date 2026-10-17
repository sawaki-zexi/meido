# ADR-0018：显式自主许可与 Drift

状态：已采纳，2026-10-16主人在本次对话明确批准具体规格第3节；#177–#179已通过准确312b7f1两套完整CI；#180已整合为源码头0acfb4f，源/修复/集成两轴无遗留，本地115项交叉、完整前端252项及构建通过，新准确HEAD两套CI另行核验。尚未合入main，真实账号、服务事务和费用另批。

## 背景与决定

无人输入时的来源读取、ACK 和发送具有独立权限与恢复语义，普通主人聊天身份不能授权这些动作。一个主动深模块拥有默认各自关闭的 A01/Drift、显式模型、来源 get/poll/ACK、工具和发送目标许可；宿主准入产生持久 AutonomyOrigin，复用现有 TaskRuntime 与 MCP stdio。

一个最多 600 秒的 tick 持有来源快照并只选一种模式。A01 使用单草稿/finish、post-guard、canonical outbox；Drift 每 run 只有一次发送机会，成功后实际 dispatch 与 schema 一同收窄。投递、固定 expiry 的 ACK 和 Runtime 结果交接分别持久恢复，未知效果不重放。主人输入先 seal 旧 generation，下一自主运行等全部 owned 资源回收。

## 取舍与授权

显式目标和聊天忙闲替代上游 presence/relationship fallback，增加配置和持久状态，但避免隐式身份或改投。S06/S08 产物须经相应信任与许可后由后续运行真实消费。批准覆盖实现和本地合成验收；真实账号、服务事务及费用另批。

事实来源：[具体规格](../specs/proactive-drift-spec.md)、[#177 实施计划](https://github.com/sawaki-zexi/meido/issues/177#issuecomment-6207491543)。批准不表示生产实现或真实自主事务验收完成。
