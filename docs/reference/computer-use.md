# Windows Computer Use 契约

状态：当前。最后核验：2026-10-16。事实来源：[宿主](../../backend/app/computer_use/host.py)、[八工具 Schema](../../backend/app/computer_use/tools.json)、[固定资源锁](../../backend/app/computer_use/runtime.json)、[公开工具测试](../../tests/python/test_computer_use.py)、[HTTP/provider 验收](../../tests/python/test_computer_use_api.py)。目标与已批准取舍见 [Spec](../specs/computer-use-spec.md)，原生证据及未验收环境见[验收手册](../handbook/computer-use-native-acceptance.md)。

角色 `agentConfig.computerUse.enabled` 默认 false。旧客户端更新时省略 `computerUse` 保留原值；关闭后写 `enabledTools`、插件名称或通用 capability 不能绕过专用开关。工具目录能读取关闭、聊天连接不支持图像、固定驱动缺失或不匹配的依赖状态，不启动原生进程。Windows 身份与解锁状态只能在调用时动态验证。

八名称为 `computer_list_windows`、`computer_get_window_state`、`computer_click`、`computer_type_text`、`computer_press_key`、`computer_hotkey`、`computer_scroll`、`computer_release`。模型不能指定原生命令、文件输出、daemon、session 或额外参数。工具全部 `external_allowed=false`；同一角色的渠道、spawn、定时、主动消息也不获得桌面输入权限。

`POST /api/roles/{id}/messages` 的可信宿主在真实用户 run 的 SQLite modelSnapshot 写入 `computerUseSource=desktop_owner` 和窄通用标记 `humanInputSource=desktop_owner`。请求体字段不能覆盖它们。原生调用核对精确 role/session/run、单条普通 user text、当前开关和代际、聊天绑定与完整配置。模型或配置改变使旧 run 失效。仅物理 active console、WinSta0/Default、WTSActive 且 unlocked、相同用户 SID/session、普通完整性且无 UIAccess 的宿主/驱动/目标可用。WinSta0 的 UOI_USER_SID 可是当前令牌的 logon SID，按 TokenUser 或 TokenGroups 中 SE_GROUP_LOGON_ID 核验；目标进程仍要求精确 TokenUser 相等。[Microsoft TokenGroups](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-token_groups)

宿主独占一段桌面输入 lease：`Local\Meido.ComputerUse.Desktop` mutex 由专用线程持有；命名 Job 为 `Local\Meido.ComputerUse.InputJob`。先非阻塞取 mutex，再确认同名旧 Job 没有活动进程。其他实例/角色/run 得到 busy，不排队。lease 持续跨模型等待；绝对 300 秒、空闲 90 秒撤销，不能自动续租。

list 每次最多保存本次发现的 100 个身份，30 秒过期。身份包含 PID 创建时间、HWND、线程、类、用户/session、DWM 物理边界和 DPI；标题只作描述。state 仅接受已发现的精确窗口；任意 state 尝试先清掉全 run 唯一活动观察，包括失败、未发现窗口和同 PID 的其他窗口。state 给出原生 snapshot_id、原始 element_token 和随机 observation_id；动作必须携带 pid/window_id/snapshot_id/observation_id 四元组。一次输入消费观察，下一动作需重新 state。无 token 的 element_index、token/像素混用、过期、窗口移动或身份不符均拒绝。

截图只由补丁 PrintWindow 或 WGC 产生；禁 desktop-region 和窗口 DC BitBlt 回退。每帧绑定准确 HWND、provenance、source_dimensions、image_dimensions、physical_origin、resize_ratio。宿主验证 PNG、尺寸、比例和预算，不二次缩放。WGC 无已证明映射时原点/比例为 null，可观察并使用 UIA token，携带 x/y 的输入失败。像素为图片内坐标，有限且在图片边界内。动作前比较观察时的 foreground HWND、LastInputInfo 和当前按住的修饰键/鼠标按钮；发生变化返回 foreground_conflict。

background 不自动升级。补丁首版只保留目标已经是精确 foreground HWND 的 background 路由；否则原生输入前 background_unavailable。hotkey 以及带 modifiers/modifier 的组合输入始终要求显式 foreground。外部窗口的 EnableWindow/exstyle 不会由该 profile 修改。foreground 保留上游 AttachThreadInput、必要时一次 VK_NONAME down/up、最多 3 次后续辅助激活；业务动作不因未知效果重发。结果仅表示 dispatched/unknown，不能证明应用效果；必须观察或由应用证据核实。已进入 OS/应用队列的输入不能撤回。

state 的 PNG 只放 `AgentToolResult.images`，进入当前聊天模型紧接着的一次请求；运行时在请求结束后清除，宿主在上传前重核权限。SQLite、SSE、audit 和重放历史无原图。UIA、标题、正常工具参数与输入文本仍遵循现有文字历史契约。独立 screenPerception/P04 摘要路径不参与、不授予此能力。

| 预算 | 行为 |
| --- | --- |
| 单操作 | 含启动 30 秒；工具外层 35/36 秒；超时终止自有驱动树，不补发 |
| 当前回合 | 实际提供 Computer Use 工具时最多 32 次模型调用，仍受 300 秒 lease；其他回合保持原 8 次 |
| state | depth 默认 6/最大 12；elements 默认 200/最大 500；query 最大 256 字符 |
| 图像 | PNG≤2 MiB、边≤1600、解码≤2,560,000 像素；采集前目标面积≤16,777,216 |
| 文本 | ≤4000 字符且 UTF-8≤16 KiB；delay_ms×字符数≤20 秒；UIA结果≤64 KiB，超出拒绝 |
| 键与管道 | keys≤5、key≤32；JSON-RPC 单帧≤4 MiB；持续消费 stderr，诊断有界 |

stop、release、撤权、配置改变、删除、运行结束或关闭应用先撤销，取消请求，等待自有 driver 及子进程退出并 join stdio，再清观察/配置并释放 mutex；不终止目标应用。cleanup 失败保留 owner、Job 句柄和 lease，后续清理可以重试，不能宣称 released。release 后同一 run 永久拒绝再次操作；只有宿主完成整个 runtime 才丢弃内存撤销记录。owner 进程强杀时 Job 的 KILL_ON_JOB_CLOSE 清理旧驱动，继任者在 Job 未清空前拒绝 abandoned mutex。

常见错误是 permission_denied、vision_unsupported、busy、stale_window、stale_observation、stale_element、coordinates_out_of_bounds、pixel_mapping_unavailable、foreground_conflict、background_unavailable、image_budget_exceeded、observation_budget_exceeded、timeout、cleanup_unconfirmed。原生可能已收到请求后的错误标为 effect=unknown，不承诺回滚。原生错误的结构化数据保留在工具文字中。
