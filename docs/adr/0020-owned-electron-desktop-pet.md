# ADR-0020：受管 Electron 桌宠宿主

状态：已采纳宿主路线；2026-10-17 主人在本次对话选择“Electron 独立宿主（推荐）”，明确要求按此继续实施。实施和原生验收尚未完成。

## 决定

为 Meido 增加可选 Electron sidecar，保留现有 React 网页和 Python 主后端。适配固定 Shiori 的 atlas 播放合同与 React renderer；后端继续拥有角色绑定、工具授权、单回合/冷却和原资源生命周期。窗口在当前 Windows 桌面运行，后续平台支持分别验收。

包是经过校验的数据和图片，不能带入执行代码。后端拥有固定入口的 stdio 桥和原进程树，Electron 使用固定本地 renderer、隔离 preload 和受限素材映射；accepted 与实际播放完成分开。停用、换角、删除和退出必须回到原 owner 收尾，旧动作不重播。

## 取舍

Electron 能适配复用 Shiori 的 React/CSS 播放器及几何逻辑，避免先重写 Qt 播放器；代价是新增 Electron/Node/Chromium 的构建、分发与多进程收尾。不能据此声称已测得更小包体或更低资源占用。PySide6 原生与 QtWebEngine 方案保留为比较资料，本次不实施。

长期桌宠是应用拥有的本机资源，不强行套入单次模型任务的600秒预算。原生宿主不获得通用桌面输入、任意文件或外部账号权限；不扩大已批准 Computer Use、屏幕感知或模型费用范围。

依据：[已批准宿主范围与实施规格](../specs/desktop-pet-spec.md)、[固定源码与官方资料研究](../research/pet-host-primary-sources-2026-10-17.md)、[总规格 D04](../specs/shiori-tool-parity-spec.md#d04-桌面宿主与桌宠原生窗口)。本决定不授予合并总 PR、关闭 Issue、签名或外部发布权限。
