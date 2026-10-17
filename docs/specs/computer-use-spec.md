# Computer Use：Windows 原生窗口控制规格

状态：当前目标规格。第 3 节已获用户明确批准，#169 已实施并通过单机专用窗口的八工具与 HTTP/provider 原生验收；其余环境和独立审查状态见[验收记录](../handbook/computer-use-native-acceptance.md)。最后核验：2026-10-16。未勾选的复合验收项包含尚未验证的环境，不代表再次等待架构许可。

最后核验：2026-10-16（Asia/Shanghai）。范围：完整复刻规格 P02 / T17，[实施票 #169](https://github.com/sawaki-zexi/meido/issues/169)，Parent [#137](https://github.com/sawaki-zexi/meido/issues/137)。

事实来源：固定 Shiori `4f266aeaf42fcd611362e2456417b91f659341cf`；CUA 固定 MIT 源码 `fc188250b4ca8549b8e61f937fdb1fb560770e86`；[当前宿主与资源锁](../../backend/app/computer_use/)、[最小补丁](../../native/computer-use/patches/0001-meido-windows-profile.patch)、[HTTP/provider测试](../../tests/python/test_computer_use_api.py)、Microsoft 官方 Win32/UIA 文档。本票从根确认已验证 aggregate `30002a9a8de551a12a44b2c2f5724d5640aca50d` 建本地独立分支，已包含 #159 stdio 和 #160。用户在 [#137 comment6198017236](https://github.com/sawaki-zexi/meido/issues/137#issuecomment-6198017236) 批准从已验证 aggregate 开发并统一整合 [PR #162](https://github.com/sawaki-zexi/meido/pull/162)，不要求等待 main 合并。最初只读调研已完成；原生实施仅操作本任务新建的专用 fixture，不观察或输入其他用户窗口。

标记：**已提取**表示源码直接证明；**计划**是拟议契约；**推断**是基于证据的判断。上游 README 的实机描述不是 Meido 已通过的证据。

## 1. 要交付的用户结果

计划：主人为一个角色单独开启 Computer Use 后，在当前主人桌面聊天中让角色发现实际窗口、观察该窗口的控件及画面，使用本次观察引用点击/输入/按键/滚动，重新观察确认效果并释放控制。默认关闭；停止回复、撤权或关闭应用后不再接收新动作，清完本应用拥有的输入进程才交还控制权。

P02 的八个工具都属于本票。P04 `observe_screen` 仍独立：它把主屏发给明确绑定的独立视觉连接，只回摘要；本票的窗口截图直接作为当前聊天模型下一次请求的临时图像，不复用 P04 的授权或摘要路径。不增加新 Agent，不依赖 P04 的行为、桌宠或 spawn；本实现复用的 #159 stdio transport 是真实代码依赖，已在获准的 aggregate 中集成，不重新实现通用 MCP 管理。

## 2. 固定上游的完整契约核验

**已提取：**注册名为 `computer_` 加上下面的本机名称，八项都来自固定 [tools.json](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/tools.json)。所有对象都 `additionalProperties=false`。宿主而非模型提供角色和运行身份；[tool.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/tool.py)额外拒绝 session、scope、target、screenshot_out_file、from_zoom、socket、command、extraArgs。

下表中的 Q 是必填四元组 `pid:integer, window_id:integer, snapshot_id:string(^s[0-9a-f]{8}$), observation_id:string`；T 是可选 `element_token:string, element_index:integer, x:number, y:number`；D 是 `delivery_mode:background|foreground`，默认 background。

| 实际注册名 | 必填参数 | 其余参数和已提取行为 |
| --- | --- | --- |
| `computer_list_windows` | 无 | `on_screen_only:boolean, pid:integer`；列出 PID、HWND、标题和边界。Schema 未写 on_screen_only 默认值，不把猜测写成事实。 |
| `computer_get_window_state` | `pid, window_id` | `query:string, max_depth:integer≥1, max_elements:integer≥1, max_dimension:integer≥1`；返回 UIA 树、原始截图、snapshot_id、element_token；宿主追加随机 observation_id。query 保留匹配祖先和原始索引。Schema 未声明上述可选参数默认值。 |
| `computer_click` | Q | T、D、`button:left|right|middle`（默认 left）、`count:integer 1..3`（默认1）、`modifier:string[]`。注意其单数字段名为 modifier。 |
| `computer_type_text` | Q、`text:string` | T、D、`delay_ms:integer 0..200`；插入中文等文本，不保证替换已有内容。 |
| `computer_press_key` | Q、`key:string` | T、D、`modifiers:string[]`；有 modifiers 时仅支持显式 foreground。 |
| `computer_hotkey` | Q、`keys:string[]`（至少2项） | T、D；仅显式 foreground 可以执行；后台模式在输入前拒绝。 |
| `computer_scroll` | Q、`direction:up|down|left|right` | D、`x,y`、`amount:integer 1..50`（默认3）、`by:line|page`（默认line）；没有 element_token/index。 |
| `computer_release` | 无 | 空对象；释放并永久撤销本回合的后续 Computer Use，正常回合结束也自动释放。 |

**已提取：**上游 [targets.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/targets.py) 校验四元组、原始 token 集合、PID 创建时间、DPI、窗口物理边界、截图范围；只给 element_index 而无 token 会失败；x/y 必须一起提供。新的观察替换同一窗口的旧观察。它没有墙钟 TTL；因此“快照自然过期”是本提案加强，不能写成上游现状。`get_window_state` 的原生返回必须是带准确窗口身份的结构化内容；上游从真实 image block 读尺寸，未把 element_index 重建成 token。

**已提取：**[windows.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/windows.py) 用 ctypes 校验 IsWindow、GetWindowThreadProcessId、DWM 可见边框、GetDpiForWindow 和 psutil 创建时间；拒绝最小化、跨屏和非主屏窗口。窗口标题不是选择器，失效 HWND 不重新按名字查找。

**已提取：**[README](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/README.md) 明确窗口图像进入当前聊天模型，不切换视觉模型；本机后台组合键曾在受控 WinForms 窗口只插入普通字母，因此 background hotkey/press_key modifiers 被拒绝。`effect=unverifiable` 不等于成功。插件注册八项时统一标为 external-side-effect；Meido 的风险枚举不同，见 §3。

**已提取：**上游 [Desktop 生命周期](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/backend/desktop.py) 允许被动、后台、Drift 等宿主回合持有 lease；本提案仅允许主人主动桌面回合，是有意收紧的 Meido 兼容差异。

### 2.1 固定 CUA 源码揭示的四项适配问题

**已提取：**以下直接核对 [CUA 固定源码 fc188250…](https://github.com/trycua/cua/tree/fc188250b4ca8549b8e61f937fdb1fb560770e86/libs/cua-driver)，不能从 Shiori wrapper 的检查推断原封驱动满足本稿全部目标：

1. [tools/impl_.rs](https://github.com/trycua/cua/blob/fc188250b4ca8549b8e61f937fdb1fb560770e86/libs/cua-driver/rust/crates/platform-windows/src/tools/impl_.rs) 的 resize_registry 按 PID 保存缩放比例，而非按窗口/观察。同进程 A 大窗 state 后再观察 B 小窗会覆盖比例；仅让每窗口保留最新 snapshot 不够。计划在宿主收紧为全 run 一张可动作观察，每次 state 开始前即撤销前一张，无论窗口相同或不同、state 最后是否成功。
2. [input/inject.rs](https://github.com/trycua/cua/blob/fc188250b4ca8549b8e61f937fdb1fb560770e86/libs/cua-driver/rust/crates/platform-windows/src/input/inject.rs) 的 force_foreground_assisted 会先用 AttachThreadInput 辅助激活，失败后注入一次 VK_NONAME down/up，并至多再尝试3次辅助激活。foreground 不是“只调用一次且不重试”；本稿保留并明确披露此有界辅助激活，业务点击/文本/按键不因未知效果自动重发。
3. 同文件 NoActivateGuard 给外部窗口设置 WS_EX_NOACTIVATE，并依赖 Drop 还原；[uia/fg_bypass.rs](https://github.com/trycua/cua/blob/fc188250b4ca8549b8e61f937fdb1fb560770e86/libs/cua-driver/rust/crates/platform-windows/src/uia/fg_bypass.rs) 用 EnableWindow(false) 暂时禁用目标，也依赖 Drop。Job 强杀或驱动崩溃不保证运行 Drop，可能留下目标禁用或样式变化。计划最小源码补丁禁止对外部 HWND 改 EnableWindow/exstyle，依赖这类 guard 的后台路由在任何输入前返回 background_unavailable；不加 host watchdog 或事后强行修用户窗口。
4. [capture.rs](https://github.com/trycua/cua/blob/fc188250b4ca8549b8e61f937fdb1fb560770e86/libs/cua-driver/rust/crates/platform-windows/src/capture.rs) 在 PrintWindow/WGC 失败时允许桌面区域或窗口 DC BitBlt；state 的 screenshot_window_bytes 调用没有把来源/遮挡/映射证明完整交给宿主。桌面区域可能是覆盖者的画面，不能因 HWND 正确就称为目标图像。计划补丁限制 state 仅用 PrintWindow/WGC，并返回来源和坐标证明；无法证明 WGC 屏幕映射时只允许 UIA token 动作，拒绝像素动作。

## 3. 建议供人工一次批准的具体选择

本节整体拟议为 T17 的明确架构选择；D01–D03 和屏幕 §3 / ADR-0014 未覆盖这些决定。批准仅允许按本节实现及在专门测试窗口中原生验收，不代表可操作用户任意真实应用、发送外部事务或合并 PR。

| 决定 | 计划采用 | 取舍及边界 |
| --- | --- | --- |
| 配置和授权 | 新增角色 `agentConfig.computerUse.enabled=false`；一个开关包含窗口画面向当前聊天连接发送和窗口输入。角色编辑保存、目录和执行重检同一字段。 | 不借用 screenPerception、browserUse、enabledTools、MCP grant 或 Shell 开关。无需逐次操作弹窗；一般外部事务仍遵守主人原有授权范围。开关 UI 必须写清“可操作本机窗口，窗口截图发送至当前聊天模型”。 |
| 工具目录 | 八项统一 `risk=external`、`external_allowed=false`、`source=plugin:computer_use`、`exposure=direct`；开关由现有角色策略映射到该插件及 `desktop.control`，不另要求主人勾第二个同义 grant。 | 沿用上游整组高风险分类；开启后的工具直接可见是明确适配，避免人为依赖延迟搜索票。目录仍保留关闭/不可用原因，执行时重检。 |
| 原生实现与维护 | 以 **CUA MIT 固定 commit `fc188250b4ca8549b8e61f937fdb1fb560770e86`** 为源，维护 Meido 最小 Windows 补丁并仅构建本票 direct 模式实际使用的 `cua-driver.exe`；现有 ctypes 做身份/桌面校验，Pillow 验证图像，不新增 Python 包。 | 明确不直接运行原封 0.28.2 发布 exe：它的外部窗口 guard 与截图回退不满足本稿边界。补丁仅覆盖后台 guard、窗口 capture/metadata 与普通权限 manifest，八个公开工具和显式 foreground 激活保持上游接口。代价是固定 Rust/MSVC/SDK 构建及每次升级的补丁重放、许可和原生回归，见 §3.1。 |
| 后台原生边界 | Meido 构建禁止对外部 HWND 调 EnableWindow 或修改 exstyle；依赖 NoActivateGuard/fg_bypass 的路由在输入前返回 background_unavailable，保留无需这些变更的后台路由。 | 不承诺所有应用后台可控；每个保留路由须受控原生验证，不把删除 guard 后仍强行注入算作修复。后台组合键和 click modifier 仍输入前拒绝；不加外部窗口修复 watchdog。 |
| 窗口采集与坐标 | state 仅允许 PrintWindow 或按精确 HWND 的 WGC，禁桌面区域及窗口 DC 的 BitBlt 回退；补丁返回 provenance、source HWND、physical origin、缩放 ratio/尺寸和 pixel_coordinates_available。 | 来源不明不上传图片；PrintWindow/WGC 均失败可返回明确截图不可用的 UIA 结果。WGC 若只有可信目标帧但 origin/ratio 不可证明，字段为 null、pixel_coordinates_available=false：可观察及使用 UIA token，所有像素动作输入前拒绝。不按 DWM 边框猜 WGC 原点。 |
| 驱动协议 | 固定 `mcp --direct`，私有 stdio，会话属于当前 run；离线八项 Schema 是唯一模型表面。 | 复用已集成到获准 aggregate 的 #159 MCP stdio framing/限流/清理能力（真实代码依赖），不走 mcp_add、服务目录、动态 grant 或 MCP 工具搜索，不允许模型决定 command/env/cwd/socket。现有通用客户端固定协议为 2025-06-18；必须以固定 driver 的 legacy initialize 实测确认可复用，不能声称现在已兼容。若不兼容，在现有 transport 增加明确的私有 pinned-driver profile，通用 MCP 的版本/信任规则不放宽，不重新写第二套 MCP 管理。 |
| 来源证明 | 只接受宿主记录为 `computerUseSource=desktop_owner` 的当前桌面 owner run，结合真实 SQLite run、唯一 user 消息、当前角色配置与 OS 身份再次验证。 | 渠道、配对主人私聊、群、后台、spawn、定时、常规主动、Drift 全部拒绝。模型参数/metadata 中的 owner、role、session 字样不授予权限。 |
| 驱动权限 | 主驱动嵌入 `asInvoker/uiAccess=false`；检查实际 PE manifest、运行令牌 `TokenUIAccess=0` 与非高完整性。仅 `mcp --direct` 的进程内 Windows 工具，不构建、分发、启动或连接 UIAccess helper/特权管道。 | 上游 helper 的 manifest 为 uiAccess=true，不直接复用；backend 自身为高完整性时也拒绝，不使用 runas、签名/安全目录绕行或系统策略修改。普通权限无法完成的操作明确不可用，不新增 helper 启动路径。 |
| 桌面范围 | Windows 10/11 x64、当前物理 console 的 active + unlocked、WinSta0/Default、与后端同一交互登录 SID/session。 | 首版明确不支持远端后端、Session 0、断开的 RDP、其他用户桌面、锁屏/安全桌面或高权限目标。不申请 uiAccess、不提权、不通过系统设置解除隔离。 |
| 引用 | 公开保留上游四元组/原始 token；内部建立不可猜测、可过期 `opaqueWindowRef`；**全 run 只保留一张可动作观察**，每次 state 开始前撤销前一张，动作后也撤销。 | 避免上游按 PID 的缩放记录被另一窗口 state 覆盖后旧坐标仍可用。opaqueWindowRef 不增加必填参数；角色/run/generation/来源/TTL 或坐标证明不符即拒绝。新观察失败也不恢复旧引用，不自动换目标。 |
| 画面用途 | `computer_get_window_state` 的窗口 PNG 进入 **当前主聊天连接下一次视觉请求**，之后丢弃；UIA/文字结果正常进入工具历史。 | 与 T16 摘要管线分开；不会把独立视觉连接回退成聊天连接。聊天协议不能编码图像时目录显示不可用并拒绝 state，不静默转摘要。远端聊天模型收到的帧不能撤回；关闭能力只阻止尚未上传的帧。 |
| 并发和 lease | 一个 desktop 的所有 Meido 进程/角色/run 共享一个 OS named mutex，拥有者是专用 lease 线程；run 内串行，模型等待期间继续占用；不排队竞争者。 | `Local\Meido.ComputerUse.Desktop` 不包含角色、workspace 或插件代号，避免多实例绕过。Local 是 Windows session 范围，正好对应本方案唯一被允许的 console desktop。丢弃上游 semaphore 方案，防止持有进程崩溃时计数不能自动恢复。 |
| 前台交付 | background 默认且不自动升级；foreground 必须显式参数。保留上游 AttachThreadInput 辅助激活、必要时一次 VK_NONAME down/up 及至多3次后续激活尝试，并受单次调用 deadline 限制。 | 这是可见的有界焦点切换及辅助合成输入，不承诺“一次激活/无重试”。没有业务输入效果确认时不重发点击、文本或按键；激活失败/冲突/超时如实报告，不能再扩展尝试次数或默默 foreground。 |
| 结束边界 | stop/revoke/release/正常结束/断连/应用关闭先封住输入和结果发布，终止并等待所有自有 driver 子进程，清帧/引用，最后释放 mutex。 | 不终止用户目标应用，不回滚已经发生的点击、文本或应用事务，不把 timeout 当作“未执行”。只有确认旧输入进程全部退出才可向新 run 授权。 |

### 3.1 固定源码、最小补丁和新资源清单

**已提取，溯源而非本方案的执行校验值：**Shiori [native-runtime.json](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/computer_use/native-runtime.json) 的原版归档 SHA256 是 `1f4bfceeab64cb7f56be7aad774c3dc2d2910d1427e4be1d79939c706e8029ba`，原版主驱动是 `dbbd52d75759900155fbf3d5f0a13c759a12d06ef17338b88b3f2b8b9c1ef8dc`，原版 UIA helper 是 `2aa7bde5774c12771b994ba5500e1b948e90a3129fd1182a486e462606bde7f8`。这些值仅记录上游原件，**不能作为 Meido 补丁构建的 hash**，也不允许以原版资产回退代替补丁。

**计划：**固定 [CUA commit fc188250…](https://github.com/trycua/cua/tree/fc188250b4ca8549b8e61f937fdb1fb560770e86/libs/cua-driver) 的源码及 Cargo.lock，以提交在仓库的最小 patch set 表达以下改动：私有 Meido Windows profile 禁外部窗口 EnableWindow/exstyle 变更，并对依赖该 guard 的路由提前拒绝；窗口 state capture 禁 desktop-region/窗口 DC BitBlt 回退，透传 provenance/geometry；为唯一分发的 direct 主驱动嵌入普通权限 manifest，不新增 helper 启动/转发。其余平台、工具、后台可证明不依赖 guard 的路由和显式 foreground 辅助激活不改写。profile 由受控构建启用，模型和环境变量不能关闭这些约束。宿主全 run 单观察规则解决 resize_registry 覆盖，不在本票重写通用 registry。

已构建并核验固定源码自带 [Rust toolchain 1.97.1](https://github.com/trycua/cua/blob/fc188250b4ca8549b8e61f937fdb1fb560770e86/libs/cua-driver/rust/rust-toolchain.toml)、MSVC 14.42.34433、SDK 10.0.22621.0，`cargo build --release --locked -p cua-driver` 只产出 direct 主驱动。upstreamCommit、补丁 SHA、target、权限与**构建后实际计算的 exe SHA256** 固定在 [runtime.json](../../backend/app/computer_use/runtime.json)，命令与实机范围见[验收记录](../handbook/computer-use-native-acceptance.md)。新构建不得自动更新 release lock 或沿用上游原 exe hash。

应用 prepare/package 只接收上述固定 manifest 匹配的 Meido 构建产物，复制到应用 `native/computer-use` 并验证 hash；运行时只查固定资源目录并验证 profile/version/hash，不找 PATH、全局 driver 或现成 daemon。缺失、原版替代、profile不符或 hash不符均不可用。准备可由开发环境或受控构建产物供应，最终用户无需安装 Rust；运行不自动下载、安装或升级。上游 helper 的 [manifest](https://github.com/trycua/cua/blob/fc188250b4ca8549b8e61f937fdb1fb560770e86/libs/cua-driver/rust/crates/cua-driver-uia/cua-driver-uia.manifest#L27) 与 [build.rs](https://github.com/trycua/cua/blob/fc188250b4ca8549b8e61f937fdb1fb560770e86/libs/cua-driver/rust/crates/cua-driver-uia/build.rs#L4) 会嵌入 uiAccess=true；本票不构建或分发该 helper。固定源码 [serve.rs](https://github.com/trycua/cua/blob/fc188250b4ca8549b8e61f937fdb1fb560770e86/libs/cua-driver/rust/crates/cua-driver/src/serve.rs#L369) 只证明 helper 转发并非现有路径；direct 的进程内 Windows 工具注册见 [main.rs](https://github.com/trycua/cua/blob/fc188250b4ca8549b8e61f937fdb1fb560770e86/libs/cua-driver/rust/crates/cua-driver/src/main.rs#L342) 和 [SDK runtime](https://github.com/trycua/cua/blob/fc188250b4ca8549b8e61f937fdb1fb560770e86/libs/cua-driver/rust/crates/cua-driver-sdk/src/runtime.rs#L564)。不为凑两个进程新增它。主驱动必须显式嵌入普通权限 manifest 并核验实际令牌，符合 [Microsoft UIAccess 边界](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-securityoverview#uiaccess-requirements-for-assistive-technology-applications)。

保留 Shiori MIT 归属、[CUA 固定提交许可证](https://github.com/trycua/cua/blob/fc188250b4ca8549b8e61f937fdb1fb560770e86/LICENSE.md) 及第三方 notices，明确标识 Meido modified build。未来更新需要显式改变 upstreamCommit、审阅/重放最小补丁、重新构建和生成新 hash，再跑 §7 的受控原生验证；不能自动追随 tag/latest。本方案由此承担小型 Rust 补丁和发行资源维护成本，而非宣称仅复用未修改 exe 即可。

## 4. 数据流、真实 owner 证明与模块边界

计划：现有桌面 `POST /api/roles/{id}/messages` 接受真实 user 消息并创建 run 后，宿主写入不可由请求体覆盖的 `computerUseSource`。工具调用时必须同时满足：

1. ToolContext 的 role/session/run/call 与当前 capability revision 一致；SQLite run 确实存在且归属 `role:{role_id}`，状态为 created/running；原始 source 是 desktop_owner。
2. 本 run 恰有一个已完成的普通 user text 消息；其宿主来源为 desktop，排除 scheduled、background、taskId、proactive、trigger、senderId/channel 及内部完成消息。检查不能仅依赖字段“没有写”，必须要求宿主正向来源声明。
3. 角色仍存在、未删除/撤权中、Computer Use 仍开启；声明对应的配置代际和初始聊天模型配置不变。手工把八个名称写进 enabledTools 不得绕过专用开关。新开关关闭或模型绑定变更使旧 run 的 Computer Use 终止，开启后需要新 run。
4. OS 后端令牌 session id 等于当前 console，WinSta0 的 UOI_USER_SID 必须匹配该 TokenUser 或其 TokenGroups 中 SE_GROUP_LOGON_ID；实机的 station SID 是当前 logon SID，不能错误要求它必定等于账户 User SID。目标 PID 仍要求精确相同 TokenUser/session，且完整性在允许范围；取不到身份不猜测。此检查证明 Windows 登录身份，不宣称 local HTTP 本身提供新的账户认证。

随后进入 `ComputerUseHost`：它拥有准入、当前 owner/run/generation、lease、deadline、窗口收据/坐标校验、撤权和最终清理；`PinnedDriverSession` 拥有固定原生进程、串行协议和响应预算。窗口收据直接封装在宿主内，不新增只有转发职责的 WindowTargets 层。工具只是八项参数/结果适配器。应用装配与 HTTP/SQLite 仍在 app；AI 核心仅消费现有 ImageContent，不读数据库或执行 Win32。

```text
真实桌面 user 消息 → 已持久 run/source → capability revision + Computer Use 开关
  → owner/OS 桌面证明 → 单个 lease → 固定 driver 的 list/state
  → 宿主登记窗口身份/图像/观察收据 → 下一次当前聊天视觉请求
  → 带四元组的动作 → 执行前重检 → 原生结果 → 重新 state 验证
  → run 结束/stop/revoke/release → 封门 → 驱动树退出 → 清帧引用 → 交还 lease
```

**已提取，复用点：**当前 `backend/app/agent_runtime/tools.py` 已有 ToolContext 和 `AgentToolResult.images`；`types.py` 的 ImageContent 注释为下一次请求临时图像；`loop.py` 在 provider 请求结束的 finally 调 `_expire_ephemeral_images`；`session_store.py` 重放工具结果时 images 为空。`mcp_stdio.py` 已有有界 framing、关闭与 owned WindowsJob；`browser_use/process.py` 已有 suspended 启动再装入 Job 的进程树拥有权。它们不是本票已经接入的证明。

**计划：**持久消息、details、audit、SSE 工具结果都不放 data URL/base64/图像路径。原图只给模型内存请求；UI 不自动额外保存截图。工具文本可能含窗口标题、UIA 文本及输入状态并进入正常历史，不声称这些文字绝不包含私密信息；日志不得额外复制原文、输入 text 或截图。当前 loop 仍会为工具调用保留正常参数记录，若 text 的持久策略与现有浏览器/输入工具不同，须在实现计划明确做脱敏或保持既有契约，不能只靠 audit 字段摘要误称所有输入文本均未持久。

## 5. 桌面、窗口、坐标和交付检查

### 5.1 active / unlocked 不是同义词

计划：准入、每次原生请求前、state 发布前都重检以下联合条件；持有 lease 时以有界轮询监测 session/desktop 改变，发现后撤销本 run。

- `ProcessIdToSessionId` 非0且等于 `WTSGetActiveConsoleSessionId`，后者不能是 0xFFFFFFFF。此选择有意只承诺物理 console，不把已连接 RDP 自动视为支持。
- `WTSQuerySessionInformation(WTSSessionInfoEx)` Level1 的 session id 一致、SessionState=WTSActive 且 SessionFlags=UNLOCK；失败/unknown/lock/disconnected 一律不可用。Windows 7 标志相反，不在支持范围。
- 当前进程 window station 为 WinSta0，`OpenInputDesktop` 成功，`GetUserObjectInformation(UOI_NAME)` 为 Default，且与 worker 所属 desktop 一致；用只读身份查询，不 SwitchDesktop、不设置其他 window station。UOI_USER_SID 匹配进程 TokenUser 或其 SE_GROUP_LOGON_ID；所有自有 handle 均关闭。[Microsoft logon SID 属性](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-token_groups)

上述来源：[WTS console](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-wtsgetactiveconsolesessionid)、[WTS state](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ne-wtsapi32-wts_connectstate_class)、[WTSINFOEX lock flag](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_a)、[OpenInputDesktop](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-openinputdesktop)、[User object name/SID](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getuserobjectinformationw)。OpenInputDesktop 在断开的 session 也可能返回将来恢复时的 desktop，故单查成功或 Default 不能证明可以输入。

### 5.2 opaqueWindowRef 与四元组

计划：list 返回的每个项目先经宿主重新核准，内部登记随机 128-bit opaqueWindowRef，并绑定 `{role_id, run_id, source_message_id, capability_generation, driver_generation, pid, process_creation_time, hwnd, window_thread_id, class_name, process_sid, session_id, bounds, dpi, observed_at}`。它不是可反解析地址，也不存 SQLite；最多保存本 run 已发现的100个窗口。state 仅可指向本 run 已发现的精确窗口实例，不能直接猜 PID/HWND。

每次 state 开始前，先撤销全 run 上一张可动作观察；成功后追加原生 snapshot_id + host observation_id + 原始 token 集合 + image_width/height + 采集 provenance/origin/ratio + 单调时钟截止，它们指向同一内部收据。全 run 仅有一张可动作观察，绝不同时保留同一 PID 不同 HWND 的活动快照；state 失败也不恢复旧观察。动作验证这张收据后才传给固定驱动；原生不理解的 observation_id 不透传。像素/无token按键路径如同上游移除原生不支持的 snapshot_id，但宿主验证义务不移除。

窗口销毁重建、PID创建时间、HWND线程/class、session/SID、可见边界、DPI、driver generation、TTL 或 role/run 改变都会失效。重复 HWND 无完全可证明的 OS incarnation 编号；线程/class/创建时间结合短 TTL 是保守约束，任何矛盾拒绝，不能宣称能识别同进程瞬间复用同类 HWND 的全部情况。窗口标题只是描述；不以原标题搜索替代。发生一次动作后失效全 run 的可动作观察，必须 state 再动作，避免重复输入与不新鲜状态。

### 5.3 物理坐标

计划：用每线程 PMv2 DPI awareness 和 DWM `DWMWA_EXTENDED_FRAME_BOUNDS` 获取可见物理边界；拒绝最小化/不可见、cloaked、空矩形、任何超出主屏边界的窗口。GetWindowRect 可有不可见边框并受 DPI 虚拟化，不能直接当截图坐标。[官方坐标说明](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowrect)

**已提取：**固定 CUA 的 PrintWindow 路径存在裁边与 inset，WGC 返回可能没有可证明的屏幕原点；不能只用 DWM 左上角代替两条路径的真实 frame geometry。原版 state 丢失的来源/geometry 信息必须通过 §3 的最小补丁显式返回。

计划：窗口采集只保留 `provenance=print_window|wgc` 两种受控来源，绑定实际 HWND 和采集时身份。禁 desktop DC/region 或窗口 DC 的 BitBlt 回退；不是把 occluded 标志吞掉后照常返回。PrintWindow 失败或缺乏可信帧时可试 WGC；两者均不可用时 state 明确 `screenshot_available=false` 并保留已验证 UIA 树，像素动作不可用，不把别的窗口区域作为替代图像。[PrintWindow 官方行为](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-printwindow)、[WGC capture item](https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture.graphicscaptureitem)

可信帧返回 provenance、source_window_id、source_dimensions、image_dimensions、physical_origin、resize_ratio 与 pixel_coordinates_available。origin/ratio 若不能证明则为 null、标记 false；特别是 WGC 有可信 HWND 帧而原点映射未证时允许当前图像观察及原始 UIA token 路径，所有携带 x/y 的动作在输入前拒绝。不因“图片看起来正确”就开放坐标。具有映射时需由同一次采集的明确 crop/origin/ratio 推导，动作前重检 bounds/DPI；宿主不自行缩放或裁剪。受控fixture须验证实际驱动消费的 ratio/origin 与 metadata 一致。

可用的坐标是实际 PNG 内像素：x/y 为有限数，`0≤x<image_width, 0≤y<image_height`；拒绝 bool、NaN、Inf、缺半轴、绝对屏幕坐标、无映射截图和 token/像素混用。token 必须出自唯一活动 snapshot，不依赖截图坐标；带 element_index 时核对它对应原始 token。新 state 全局失效旧观察保证同 PID 的另一窗口不会借 resize_registry 覆盖旧坐标。来源/尺寸/几何校验不符则不上传该帧、不允许像素输入，并保持相关路径未验收。

### 5.4 foreground 冲突和输入不可回滚

计划：记录观察完成时的 foreground HWND、session LastInputInfo 样本；原生动作发送前，如前台已被第三方切走、用户输入样本发生变化、用户正在按修饰键/鼠标按钮，返回 `foreground_conflict`，要求新观察。检查 LastInputInfo 只作干扰信号，不能当“真人正在操作”的证明，因为它也可能受 SendInput 影响。[GetLastInputInfo](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getlastinputinfo)

background 从不自动改为 foreground。补丁对依赖 NoActivateGuard（WS_EX_NOACTIVATE）或 fg_bypass（EnableWindow）的后台路由，在更改外部窗口状态或发送输入之前返回 background_unavailable；不简单删掉 guard 后照发输入。其余后台路径保留上游实现并逐一原生验收，不承诺支持未验证应用。hotkey、press_key modifiers 及 click modifier 的后台请求均先拒绝。将来某条后台路径要新增支持，应在该补丁的现有边界内审查与验证，不能为“兼容性”恢复外部窗口状态变更。[EnableWindow 状态影响](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-enablewindow)、[SetWindowLongPtr exstyle 影响](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowlongptrw)

foreground 必须显式选择；如实保留上游 force_foreground_assisted：先 AttachThreadInput 辅助激活，失败后一次 VK_NONAME down/up，再至多3次辅助激活，全部受同一次调用 deadline 限制。辅助键本身也是合成输入，不能承诺激活失败时完全无输入。驱动确认精确目标/上游明确验证的同进程归属窗口后才发送业务输入；本方案不新增按标题或当前active窗口的隐式换目标，不扩大上游验证的目标归属。业务动作只发送本次请求规定的 count/amount/text，未知效果后不自动再发。

宿主核对前后对象与交付报告；激活失败、焦点竞争、超时或 unverifiable 均如实返回，需要重新观察。已注入辅助激活键时可报告 `business_effect=not_started`，但不能把整体 effect 声称完全未输入；在途或不确定统一保留 effect=unknown/unverifiable。用户可在预检与输入间操作，Windows 无跨用户输入事务锁，本方案不宣称完全隔离。原版恢复先前焦点也是可见的最佳努力，退出/强杀不保证恢复，不能在 §6 的资源清理里暗示焦点可回滚。[SetForegroundWindow 约束](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setforegroundwindow)

SendInput 只允许注入同等或更低完整性进程，且返回值/错误不能完整判断 UIPI 阻断；UIA 也不自动跨越受保护 UI。本票拒绝高权限、uiAccess、UAC/登录/安全桌面；保留的有界辅助激活不赋予这些权限，不通过提权或扩大尝试次数绕过。[SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)、[UIA 安全边界](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-securityoverview)

动作可能已经点击按钮、插入部分文本、触发保存或应用外部事务，停止无法撤回。原生调用前且未注入辅助键时已拒绝的返回 `effect=not_started`；已开始后超时、断连、取消或验证不足返回 `effect=unknown`，不得显示“未操作”、自动补发或伪称成功。本票专门测试窗口不包含发送、购买、删除用户资料等真实外部事务。

## 6. 资源所有权、lease 与预算

### 6.1 资源顺序

计划：首次 list/state 前通过来源与 OS 校验，非阻塞取得同一 named mutex；当前 lease 记录 role/run/driver generation。只有专用 lease 线程获取和 ReleaseMutex，同进程第二角色也不能靠同一线程 mutex 可重入绕过。先有 lease，才允许 suspended 启动 driver、加入独立 Job（不允许 breakaway）、恢复执行并 initialize。本票 direct 模式不启动 UIAccess helper；若发现非预期自有子进程则拒绝结果并按整个 Job 清理，目标应用不加入 Job。

上游使用 named semaphore；本方案选 mutex 是为了支持 OS abandoned 检测。`WAIT_ABANDONED` 绝不继承旧引用或报告旧任务完成：清空全部 generation，并确认旧自有输入 Job 已无活动进程后才开始新 driver；如果无法证明，不授予控制，返回 cleanup_unconfirmed。实现需保存足够的原生 Job handle/固定命名 Job 的拥有关系来完成这项确认，不能靠 PID 存在与否猜测，也不能杀任意同名 exe。[Mutex 官方语义](https://learn.microsoft.com/en-us/windows/win32/sync/mutex-objects)

stop/revoke/release/timeout 流程：同步关闭准入并记录本 run revoked → 取消/停止在途请求并禁止新 provider 图像发布 → disconnect并终止本 run 的 driver 自有 Job → wait root和活动进程数归零 → 清 stdout/帧/receipt/私有 state → lease 线程 ReleaseMutex/CloseHandle → 停止接口完成。清理任务被宿主持有，反复取消只 join，不丢弃；收尾中竞争者 busy。无法确认退出时保留输入禁用和 lease 隔离，返回清理失败，不以 finally 盲目交还。[Job Objects 进程树/kill-on-close](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)

实施时已协调调整共享 WindowsJob/McpStdioClient：可选 named Job、exit_confirmed 和 failed-close 重试；失败保留句柄、stdio tasks 也完整 join 后才确认退出。现有默认未命名消费者和协议保持原行为，新增真实故障测试见 [owned cleanup](../../tests/python/test_owned_job_cleanup.py)。宿主没有收到退出确认就保留 lease，不返回 released。

补丁在原生层禁止外部 HWND 的 EnableWindow/exstyle 写入，不把 Drop 或 host 清理当作恢复这些外部状态的手段；不新增 watchdog、修复用户窗口进程或还原任意样式。这样消除已知 guard 强杀遗留状态的来源，但不保证回滚焦点、光标或已发生的业务输入。

驱动 child 的工作/临时目录每 generation 独立；不继承 CUA_*、用户 driver 配置、更新检查或遥测。使用 child 最小环境保留 Windows 所需变量，私有路径仅传给 child；父环境不修改。进程用 CREATE_NO_WINDOW。工具没有截图文件参数，图像只经有界管道；如受控实测发现固定驱动仍写轨迹/截图/clipboard持久副本，不隐瞒，先改明确驱动配置或报告该方案未满足内存契约。只终止自己创建的 driver及任何自有子进程，不 attach 已有 daemon，不关闭目标应用。

### 6.2 首版固定预算（计划，非上游已有默认）

| 对象 | 固定上限/行为 |
| --- | --- |
| 并发 | 同一个 console desktop 1 lease；run 内1个原生请求；不等待其他run排队。 |
| lease | 绝对5分钟，最近一次Computer Use调用后空闲90秒；期间模型等待仍持有，触顶整段run撤销，不自行续租。需要继续由主人发起新回合。 |
| 观察引用 | list 收据与 state snapshot 最长30秒；state更新、一次动作、release或generation改变即失效。最多100个发现收据，全run只1张最新可动作观察；任意窗口state开始即撤销前一张。 |
| 单次调用 | 总30秒含启动和观察/输入；初始化10秒。到期终止驱动树，效果边界按§5.4报告。 |
| 对话预算 | 当前run实际提供Computer Use工具时32次模型调用，容纳完整观察/动作配对；其他对话仍8次。独立5分钟lease不变。 |
| UIA和文本 | max_depth缺省6/最多12；max_elements缺省200/最多500；query≤256字符；UIA文本结果≤64KiB，返回截断标志而不把部分树冒充完整树。 |
| 图片 | driver max_dimension缺省1600/最多1600；有可信PrintWindow/WGC来源时返回单张PNG，解码≤2,560,000像素、PNG≤2MiB；WGC映射未证禁像素动作，两种采集都失败可只回UIA和截图不可用原因。窗口物理面积≤16,777,216像素；超限在capture前拒绝。宿主不自行重缩放，不落盘。 |
| 输入 | text≤4000 Unicode字符且 UTF-8≤16KiB；delay_ms仍0..200，但预估输入时长>20秒提前拒绝；键名/修饰键受固定driver支持集合限制，keys总数≤5；count、amount、by保留上游范围。 |
| 通道 | JSON-RPC单message≤4MiB（兼容现有MCP边界）、stderr持续消费且仅保留有界脱敏诊断；不能把含图像的完整响应打印到日志。 |
| 清理 | 正常5秒清理预算；超预算转为cleanup_unconfirmed并保持禁止再授权，继续宿主拥有的收尾，不返回“已释放”。 |

这些预算是明确兼容差异，模型Schema表达新增最大值，运行时必须同样执行；不能只改description。超预算是可演示失败路径，不把长期不可用或过小输出上限当作完整复刻。

## 7. 公开验收与最小垂直 Ticket

不为目录数量或 mock 绿灯宣称 P02 完成。先用公开 `AgentTool.execute` + HTTP/SSE 和可控 provider 验证行为；原生验收只运行专门创建的本机 fixture 窗口，使用其 PID/随机窗口标记和进程创建时间限定操作，不对当前用户其他应用截屏/输入。此文写作期间不执行这些测试。

拟议单个 T17 Ticket；不把窗口枚举、API、前端分层拆票。

### Parent

Parent: [#137](https://github.com/sawaki-zexi/meido/issues/137)

### What to build

主人为角色开启 Windows Computer Use 后，在自己的当前桌面聊天中让角色发现专门测试窗口、看真实控件与截图，完成点击、中文输入、按键、组合键、四方向滚动，再观察验证，并显式或随回合结束释放控制；关闭许可或停止后，旧回合无法继续操作。

### Acceptance criteria

- [ ] 角色设置默认关闭；开启、保存其他资料和重载不丢配置；关闭后手工enabledTools、tool_search或旧schema均不能重新授予八个工具；目录显示缺驱动/hash不符/平台/聊天不支持图像的准确原因。
- [ ] 实际HTTP对话中 list→state→四元组+真实token点击→state→中文type→state、press_key、显式foreground hotkey、四方向scroll、release全部通过；fixture控件内容/事件证明效果，而非只看driver返回。
- [ ] 当前聊天下一次provider请求得到同一次state的真实图像及UIA结果；请求后/后续run/SQLite/SSE/普通audit无原图；P04独立视觉adapter不被调用，P04开关不能授予本票。
- [ ] 旧run/跨角色/猜测HWND/旧token/错observation_id/driver重启复用snapshot编号/窗口重建、移动、DPI改变/过TTL/越界或NaN坐标均在输入前失败；主屏最大化、100%与150%DPI、缩小截图坐标映射有受控原生证据，非主屏/跨屏/最小化明确拒绝。
- [ ] 渠道与配对主人私聊、后台spawn、定时、主动/Drift、合成内部消息以及伪source字段均不能取得lease；active但locked、Default但disconnected、不同SID/session、Session0、UAC或高完整性目标明确拒绝。
- [ ] 两角色/两回合/两个Meido进程竞争时只有一个有输入权，模型等待期间仍互斥；显式release后同run永久不可重开；用户输入/焦点冲突、background热键在原生输入前失败，不自动foreground重试。
- [ ] 超时/取消/撤权/聊天绑定改变/角色删除/应用关闭，均等待自有driver及任何自有子进程退出才释放lease；未知效果不补发、不伪成功；未确认cleanup保持隔离。强制终止owner后的mutex abandoned与Job收尾故障有专用子进程证据，目标fixture应用仍存活。
- [ ] 文本/树/PNG/JSON预算都可通过公开工具得到可理解失败；不落截图或driver遥测轨迹；未执行的多屏、混合DPI、真实锁屏/断连项逐项列未验收，不拿fake测试替代。
- [ ] 同一进程的 A 大窗口 state→B 小窗口 state 后，携旧 A 引用的像素或 token 动作均在原生输入前拒绝；B state失败也不复活A。重新观察A后，缩小截图像素准确命中fixture记录点。
- [ ] 受控fixture遮挡目标并使PrintWindow/WGC不可用时，绝不返回覆盖者/桌面区域画面；来源为PrintWindow或WGC、精确HWND、origin/ratio/尺寸随同一帧校验；WGC映射缺失时UIAtoken可用、像素请求拒绝，失败不是伪造图像成功。
- [ ] 对所有保留后台路由记录受控原生证据；触发原guard路径时在任何外部HWND状态变更和输入前background_unavailable。正常/取消/Job强杀/host崩溃后fixture启用状态和exstyle没有被本驱动改变；不以正常Drop测试代替崩溃场景。
- [ ] 显式foreground辅助激活的VK_NONAME与有界尝试次数如实记录；deadline/冲突失败不重发业务动作，不声称辅助键绝无副作用。fixture记录文本/点击次数证明无未知效果补发。
- [ ] 固定CUA源码+Meido最小补丁通过Rust检查和受控fixture，仅构建direct主驱动后登记新SHA256；prepare/package和运行均拒绝原版/错误profile/hash替代。没有新产物及原生证据时保持未验收。
- [ ] 相关Python、前端、类型和构建检查通过，并由独立agent分别按仓库规范和本Spec两轴审查；原生报告记录upstreamCommit、补丁hash、Rust/MSVC/SDK、新主驱动的实际hash和PE manifest/令牌权限、OS/DPI、fixture身份、命令/结果与未验证限制。

### Blocked by

- 本稿 §3 人工架构批准已由用户明确给出，不再阻塞实施。
- **#159 为实际代码依赖：**复用其 McpStdioClient/有界stdio与拥有进程树的能力，已在准确基点 `30002a9a8de551a12a44b2c2f5724d5640aca50d` 集成。补丁driver与现有2025-06-18协议的真实握手已通过，不放宽通用client。
- T01角色配置/能力目录、T06版本化能力seam以同一已验证aggregate复用；本票不重建它们。**#160/T16不是行为依赖**；它已串行集成，与本票只共享RoleEditor/models/main装配配置区域，应协调局部修改而非等待其独立能力或再建视觉管线。桌宠、spawn不构成本票依赖。
- 用户在 [#137 comment6198017236](https://github.com/sawaki-zexi/meido/issues/137#issuecomment-6198017236) 明确批准剩余任务从已验证总集成建本地任务分支、统一 [PR #162](https://github.com/sawaki-zexi/meido/pull/162)。因此按该会话授权执行，不要求先等main合并、不另开堆叠PR；开始实现仍核对开放工作和aggregate最新状态，登记本票真实依赖及边界。

实施计划已发布到 [#169](https://github.com/sawaki-zexi/meido/issues/169#issuecomment-6207489899)：独立本地任务分支交付宿主、最小补丁、构建/prepare、角色开关、完整 HTTP/provider 和受控fixture证据，协调者整合回单一 PR #162。与 #160 共享配置区已经串行，不重建采集/视觉模型管线。当前源码契约见[Reference](../reference/computer-use.md)。

## 8. 仍须诚实保留的不确定性

- 固定源码最小补丁、实际新 hash、direct 握手、PrintWindow 几何、中文/全部八工具和保守 background 子集已有受控窗口证据；多屏、两种真实系统 DPI、真实锁屏/断连、双采集故障遮挡等环境未验收，逐项保留在[验收记录](../handbook/computer-use-native-acceptance.md)，不拿端口注入测试替代。
- OS接口不是事务：句柄复用、在检查与实际输入间的用户竞争，以及已进入应用队列的事件都不能通过stop撤回。文档和产品错误不得承诺桌面沙箱或无副作用回滚。
- `OpenInputDesktop(Default)`和API session前缀单独都不能证明当前主人许可。本提案同时要求宿主SQLite来源/当前run和OS会话/SID；不新增遥控登录系统，也不把单一主人部署假设说成强认证。
- 截图发送至聊天连接是本票最需要明确批准的可见区别。若维护者选择仅摘要，必须承认那不符合此P02固定上游图像契约，并另改目标和验收；不能在实现时静默改成P04。
- 第 3 节已获人工批准；实际外部事务、最终合并与关闭Issue仍遵守已有人工权限。
