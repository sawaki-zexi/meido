---
title: QQ 扫码确认阶段与登录错误的区分
kind: 源码研究
status: 当前
last_verified: 2026-10-25
napcat_version: v4.18.28
napcat_baseline: 2049e64260d378e9f1f1f318ae033347d46ab994
meido_baseline: a3f837ab7aa12d2343f1e208920b2c8755113968
---

# QQ 扫码确认阶段与登录错误的区分

状态：当前。最后核验：2026-10-25。事实来源：[Meido Issue #227][issue]、Meido 固定基线 `a3f837ab7aa12d2343f1e208920b2c8755113968`，以及 NapCat 官方 `v4.18.28`。通过 GitHub API 解引用 annotated tag：tag 对象为 `bd121320f604731393e1c3200f6301a4bd76ec47`，源码提交为 `2049e64260d378e9f1f1f318ae033347d46ab994`；以下源码链接均固定到后者。[标签对象][tag]

**已提取**表示源码或日志直接证明；**推断**表示证据组合后的解释；**计划**表示本次 Meido 的建议处理，不冒充 QQ 原生协议保证。本研究没有启动、重启或操作任何 QQ 实例，没有读取真实账号登录日志。

## 结论与证据边界

**已提取。** Meido 基线将任意非空 `loginError` 统一显示成“QQ 登录未完成，请刷新二维码后重试”，但仍返回 `phase=login_required`，后台循环继续读取登录状态。仅凭这条提示，不能认定后台已经停止轮询，也不能认定二维码真的失效。[状态映射][meido-read]、[进程存活期间的循环][meido-loop]

**已提取。** NapCat 的 `loginPhase` 与 `loginError` 独立保存。扫码回调只把阶段设为 `qrcode_scanned`；随后发生的二维码过期或登录失败都可能保留这个阶段。因此，不能按 `qrcode_scanned`／`initializing` 一律吞掉错误。[状态存储][runtime]、[shell 回调][shell]

**未证实。** 没有找到固定版本源码、类型契约或本次检索的一手报告，证明“正常等待手机确认”必然触发 `onLoginFailed`，或某个 `result=0`／`serverErrorCode=0` 就代表正常等待。不能把产品上的等待容错写成已验证的原生错误码协议。[回调类型][listener]、[错误解析][parse]

## 固定版本提供了哪些可靠信号

| 信号 | 源码直接支持的含义 | 不能据此断言 |
| --- | --- | --- |
| `loginPhase=qrcode_scanned` | 已收到扫码事件，界面应提示等待手机确认 | 所有后续错误均可忽略、二维码永远有效 |
| `loginPhase=initializing` | QQ 登录已成功，NapCat Core 仍在初始化 | 已完成 Core 在线与真实身份校验 |
| `isLogin=true` | QQ 登录标记为真，且 `core.selfInfo.online===true` | 可以跳过 Meido 的账号归属校验 |
| `qrLoginAccepted` | QQ 登录标记为真，并处于扫码／初始化阶段 | 单纯扫码事件就一定会使该字段为真 |
| `coreReady` | QQ 登录标记为真，Core 明确在线 | 是一个独立于 `isLogin` 的额外成功门槛 |
| 非空 `loginError` | 曾写入一条尚未清除的错误文本 | QR 必定过期、必须立即刷新或取消会话 |

表中阶段来自 [shell 登录／扫码回调][shell]；字段公式来自 [CheckLoginStatus 第 91–113 行][status]。该接口明确说明阶段只用于进度，不应成为成功后的额外跳转门槛。

**已提取。** `onQRCodeSessionFailed(errType, errCode)` 只有在未登录且参数为 `(1,3)` 时写入“二维码已过期，请刷新”，并请求下一张二维码；此分支没有修改阶段。这个二元参数属于二维码会话回调，不能与登录错误 JSON 中的 `serverErrorCode` 混为同一码空间。新二维码回调及登录成功回调都会清空错误。[shell 第 142–151、185–227 行][shell]

## `onLoginFailed` 与 `parseLoginError` 的实际结构

**已提取。** `NodeIKernelLoginListener.onLoginFailed` 的参数类型仅为 `...any[]`。shell 对整个参数数组执行 `JSON.stringify(args)`，再存成 `登录失败: <JSON数组>`；不读取参数 0 的含义，不修改阶段，也不按错误码判断可重试／终结。[回调声明第 27–31 行][listener]、[shell 第 224–227 行][shell]

**已提取。** 官方前端的 `parseLoginError` 仅执行以下展示解析：识别 `登录失败: ` 前缀，解析后缀 JSON；若结果是数组且第二项存在，就显示第二项的 `message`，并在 `serverErrorCode` 为 truthy 时附错误码。解析失败则原样返回。它不读取 `args[0]` 或 `result`，也没有把零错误码定义成成功／等待。`serverErrorCode=0` 仅意味着这段展示代码不附加码值。[parseLoginError 第 29–48 行][parse]

**已提取。** 官方界面虽然对 `qrcode_scanned`／`initializing` 显示等待覆盖层并每 500ms 继续轮询，但收到新的非空错误时仍会弹“登录失败”；它只在 `reconnecting` 恢复流程中抑制此类弹窗。过渡界面写“最长可能需要 3 分钟”，但页面中的 180 秒定时器针对登录服务恢复，不是已扫码确认阶段的统一终结期限。[过渡反馈第 15–34 行][qr-ui]、[错误处理第 233–252 行及轮询第 342–363 行][login-page]

**已提取，一手报告而非规范码表。** 官方仓库的报告 [#1825][upstream-1825] 展示过 `[1,{serverErrorCode:45,message:版本过低提示,...}]`，[#804][upstream-804] 展示过 `168` 与账号受限提示。这些证明第二项可能承载明确失败原因；不证明每个码跨版本恒定，也不支持把“需要在手机处理”泛化为正常等待。未找到正常确认过程的零码依据，已按任务要求停止扩大检索。

## 最小修复边界

以下均为 **计划**，按证据支持的优先级处理：

1. `isLogin===true` 优先进入已有真实身份读取与归属校验。不要再额外等待 `loginPhase=ready`，也不要让残留的错误文本覆盖已验证成功。
2. 保留明确过期与明确失败。二维码过期／失效文案、结构化非零失败码等不能因 pending 阶段而消失；过期时保留会话引用与轮询，允许获取新图／手动刷新，不继续把已明确失效的图当成有效二维码。
3. `qrcode_scanned` 且无明确失败时显示“已扫码，等待手机确认”；`initializing` 显示“登录已确认，正在连接”。两者保留当前会话，不自动刷新、取消或重启进程。
4. 若需兼容“等待手机确认”出现在错误文本中的情况，只对 pending 阶段下明确、窄范围的等待措辞作进度处理；明确过期、非零失败码和其他失败原因优先。这是依据 #227 情境的产品容错，尚非原生协议事实。
5. 其他未知非空错误仍给出可恢复反馈，但不要统一宣称二维码失效或强制引导刷新。保留状态检查及取消／刷新能力；进程退出、网络异常和账号身份不匹配沿用原有独立边界。

**推断。** 当前最小问题是“错误文本被过度解释成统一刷新动作”，以及界面没有充分表达扫码后的阶段。修复不需要改 NapCat／QQ 固定版本、下载校验、账号归属或受管进程所有权。[Meido 映射][meido-read]、[现有轮询与归属检查][meido-loop]、[Issue 范围][issue]

## 回归与未证实项

- 主会话提供的边界回归红日志位于 `.data/qq-login-review/confirmation-red.log`。本次只读核验到两种 pending 阶段都因统一刷新错误提示而断言失败；没有从该日志得到“后台停止”或“原二维码真的过期”的证据。
- 原始测试文本“登录失败: 等待手机确认”是代表性合成状态，不是 shell 的 JSON 数组形状。后续可改为“前缀＋参数数组、第二项含 message”的合成响应，但应明确没有复现真实原生零码事件。
- 回归应分开覆盖：未扫码→扫码等待→初始化→成功；pending 中明确过期；pending 中结构化真实失败；未知错误；刷新／取消；进程退出／网络错误。尤其保留“带 pending 阶段也可能真的失败”的反例。
- 本次没有执行新的测试或 QQ 登录实验。该文只能支持状态映射和用户反馈修复，不能证明某个真实账号一定会在手机确认后恢复成功。
- 只读对照了旧预览固定发行包 `D:\meido-219-qq-phone\.data\development\managed-napcat\v4.18.28\napcat\napcat.mjs` 第 82121–82144 行，其扫码、过期、失败及新图清错回调与上述 shell 源码一致；这不是运行实例事件时序的证明。

[issue]: https://github.com/sawaki-zexi/meido/issues/227
[tag]: https://api.github.com/repos/NapNeko/NapCatQQ/git/tags/bd121320f604731393e1c3200f6301a4bd76ec47
[meido-read]: https://github.com/sawaki-zexi/meido/blob/a3f837ab7aa12d2343f1e208920b2c8755113968/backend/app/channels/napcat_managed.py#L297-L308
[meido-loop]: https://github.com/sawaki-zexi/meido/blob/a3f837ab7aa12d2343f1e208920b2c8755113968/backend/app/channels/napcat_managed.py#L200-L215
[runtime]: https://github.com/NapNeko/NapCatQQ/blob/2049e64260d378e9f1f1f318ae033347d46ab994/packages/napcat-webui-backend/src/helper/Data.ts#L101-L239
[shell]: https://github.com/NapNeko/NapCatQQ/blob/2049e64260d378e9f1f1f318ae033347d46ab994/packages/napcat-shell/base.ts#L136-L228
[listener]: https://github.com/NapNeko/NapCatQQ/blob/2049e64260d378e9f1f1f318ae033347d46ab994/packages/napcat-core/listeners/NodeIKernelLoginListener.ts#L21-L31
[status]: https://github.com/NapNeko/NapCatQQ/blob/2049e64260d378e9f1f1f318ae033347d46ab994/packages/napcat-webui-backend/src/api/QQLogin.ts#L91-L113
[parse]: https://github.com/NapNeko/NapCatQQ/blob/2049e64260d378e9f1f1f318ae033347d46ab994/packages/napcat-webui-frontend/src/pages/qq_login.tsx#L29-L48
[qr-ui]: https://github.com/NapNeko/NapCatQQ/blob/2049e64260d378e9f1f1f318ae033347d46ab994/packages/napcat-webui-frontend/src/components/qr_code_login.tsx#L15-L34
[login-page]: https://github.com/NapNeko/NapCatQQ/blob/2049e64260d378e9f1f1f318ae033347d46ab994/packages/napcat-webui-frontend/src/pages/qq_login.tsx#L206-L363
[upstream-1825]: https://github.com/NapNeko/NapCatQQ/issues/1825
[upstream-804]: https://github.com/NapNeko/NapCatQQ/issues/804
