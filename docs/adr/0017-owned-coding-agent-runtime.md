# ADR-0017：受管 Coding Agent 原生宿主

状态：已采纳，2026-10-16 主人在本次对话明确批准具体规格第 3 节；实施进行中。

## 背景与决定

Coding 需要跨对话存活、准确原生 session 和可信 Git 成果，不能以通用 Shell 或提示词续写代替。Windows 采用固定 Herdr 源码的 owned-mode 补丁、每任务专用 server、固定 CLI/repo profile 和每 attempt 最多 600 秒的 TaskRuntime；终态回收全部自有进程。继续必须精确恢复原生 session。

共享业务账本拥有任务、attempt、预算、结果与原来源交付；codex-delegate 通过既有 spawn coding profile 接入。原生 agent 不获取发布凭据，外部发布由独立 broker 在具体 HEAD 一次性批准后执行。

## 取舍与授权

固定原生协议及补丁需要维护构建和进程证据，但提供可检查的终态与恢复。只支持主人信任的仓库及 CLI，不承诺同 OS 账号的安全沙箱。批准覆盖实现、隔离构建及无费用 fixture；真实认证、模型消费与外部发布另批。

事实来源：[具体规格](../specs/coding-agent-spec.md)、[#174 实施计划](https://github.com/sawaki-zexi/meido/issues/174#issuecomment-6207491034)。批准不表示原生构建、真实 CLI 或发布验收完成。
