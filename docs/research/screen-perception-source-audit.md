# 屏幕感知源码核验

状态：当前源码核验；建议为推断，尚未实施。

最后核验：2026-10-16。Shiori 固定提交 `4f266aeaf42fcd611362e2456417b91f659341cf`；Meido 固定提交 `17c2b28af5d643f3f90667973d72e4ca7f9fa1d5`。

事实来源：下列固定源码、Pillow/Python 官方资料。核验只读取源码与文档；未调用截屏、未访问用户视觉连接。

## 已提取：Shiori 的实际契约

| 事项 | 固定源码直接证明的行为 |
| --- | --- |
| 工具输入 | `observe_screen` 的参数为 `{"type":"object","properties":{},"additionalProperties":false}`；没有模型可填的 role、显示器、坐标或模型参数。宿主 `role_id` 具有 context precedence。 |
| 工具输出 | 成功返回 JSON 字符串，恰为 `available: true`、`interface_summary: string`、`activity_key: string`；工具层不返回原图、targets、risks、bubble 或 experience。缺身份、停用、采集/分析错误向上抛异常，没有上游统一的 `available:false` 错误对象。 |
| 注册 | always_on、read-only；注册点没有打开 external_allowed，因此仍受 SDK 默认外部发起者限制。插件自带 active task 集合与 close 取消。 |
| 截图 | `ImageGrab.grab(all_screens=False)`，同步执行；RGB PNG 写到 BytesIO 后 base64，没有截图文件路径。空尺寸报错。没有截图阶段的独立 deadline、降采样或子进程。 |
| 帧校验 | base64 字符串最多 `12*1024*1024` 字符；严格 base64 解码、检查 PNG 文件头、正宽高/scale、role/frame/time 元数据。它没有完整解码 PNG 核对真实尺寸。 |
| 模型调用 | `activate(role_id,"vision")`，专用中性 prompt，两个 messages，PNG data URL，`tools=[]`、`max_tokens=1200`、`payload_snapshot_enabled=False`。拒绝响应 tool_calls，以及 JSON 顶层 action/actions/computer_action。 |
| JSON 容错 | 使用 SDK `load_json_object_loose`：去 Markdown fence，允许 json_repair；并非严格 JSON parser。 |
| 文本 | summary 压缩空白后截断 400 字符，activity_key 80，bubble 120，experience_candidate 280；为空时有默认值。`safe_observation_text` 的 docstring 明确说不脱敏屏幕内容。 |
| 视觉绑定回退 | 插件选择 vision purpose，但宿主 model_runtime 允许 visual_model_registration_id 缺省时回退 dialogue_model_registration_id。不能把 purpose 隔离误写成上游绝不复用聊天绑定。 |

上述行为定位：[tool.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/tool.py#L24)、[plugin.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/plugin.py#L19)、[capture.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/capture.py#L14)、[contract.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/contract.py#L27)、[model.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/model.py#L41)、[safety.py](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/backend/safety.py#L4)、[SDK JSON parser](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/packages/sdk/python/shiori_sdk/json.py#L19)、[vision fallback](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/backend/core/roles/model_runtime.py#L126)。

上游内部模型对象包含以下字段，并不是角色工具的返回 Schema：

| 字段 | 上游实际校验/归一化 |
| --- | --- |
| interface_summary、activity_key | 可被 str 转换；分别限 400/80 字符，缺省使用固定默认文案。 |
| targets | 必须是数组，最多 30 个对象；label 非空且限 80 字符；x/y/confidence 为有限数，0≤x≤frame.width，0≤y≤frame.height，0≤confidence≤1。 |
| risks | 非数组变为空数组；保留非空字符串、去重；源码未给总数/单项长度上限。 |
| bubble、experience_candidate | 可选，字符串归一化，分别限 120/280 字符；工具最终不输出。 |

上游 [README](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/README.md#L3) 明确当前是按需观察，持续采集另案；[依赖](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/screen_perception/pyproject.toml#L5) 是 SDK 与 Pillow，没有 Electron/Computer Use 驱动要求。

## 已提取：Meido 当前可复用部分

| 已有能力 | 固定源码位置与限制 |
| --- | --- |
| 稳定模型连接 | [ModelConfigurationStore](https://github.com/sawaki-zexi/meido/blob/17c2b28af5d643f3f90667973d72e4ca7f9fa1d5/backend/app/model_config.py#L196) 管理 ID、协议、endpoint、模型和后端密钥；`get(None)` 会取 active 配置。因此屏幕模块必须先拒绝空 ID，不能依靠 get(None) 的默认行为。 |
| 角色配置 | [AgentToolConfig/Role](https://github.com/sawaki-zexi/meido/blob/17c2b28af5d643f3f90667973d72e4ca7f9fa1d5/backend/app/models.py#L42) 没有屏幕感知配置；[RoleStore.update](https://github.com/sawaki-zexi/meido/blob/17c2b28af5d643f3f90667973d72e4ca7f9fa1d5/backend/app/role_store.py#L74) 以旧角色为基底更新；新配置需要专门覆盖省略字段保留的情况。 |
| 图像/工具协议 | [ModelImageBlock](https://github.com/sawaki-zexi/meido/blob/17c2b28af5d643f3f90667973d72e4ca7f9fa1d5/backend/app/ai/model_types.py#L30) 已支持内存 base64 图像；[registry](https://github.com/sawaki-zexi/meido/blob/17c2b28af5d643f3f90667973d72e4ca7f9fa1d5/backend/app/ai/model_adapter_registry.py#L31) 有十种协议；[rich stream](https://github.com/sawaki-zexi/meido/blob/17c2b28af5d643f3f90667973d72e4ca7f9fa1d5/backend/app/ai/model_adapter_registry.py#L112) 可传 tools/max_tokens/cancel_check，可观察工具和错误事件。协议会编码图像不等于某个远端模型一定支持视觉。 |
| 请求隔离 | 直接调用 rich adapter 不必经过聊天 Runtime/SessionStore；现有 adapter 不在此入口持久化请求。OpenAI 适配器 tools 为空时通常省略 wire tools；“无工具”不能只断言 wire 一定有 `tools:[]`。其错误可能含远端正文，屏幕模块不能原样输出错误。见 [stream transport](https://github.com/sawaki-zexi/meido/blob/17c2b28af5d643f3f90667973d72e4ca7f9fa1d5/backend/app/ai/pi_openai_adapter.py#L523)。 |
| 停止 | [RuntimeManager](https://github.com/sawaki-zexi/meido/blob/17c2b28af5d643f3f90667973d72e4ca7f9fa1d5/backend/app/agent_runtime/runtime.py#L93) 已有 token/Task 取消和 stop 等待；[HTTP stop](https://github.com/sawaki-zexi/meido/blob/17c2b28af5d643f3f90667973d72e4ca7f9fa1d5/backend/app/main.py#L4623) 检查 role/run/session。 |
| UI/目录 | [RoleEditor](https://github.com/sawaki-zexi/meido/blob/17c2b28af5d643f3f90667973d72e4ca7f9fa1d5/web/src/features/role-management/RoleEditor.tsx#L93)、[draft 保留字段](https://github.com/sawaki-zexi/meido/blob/17c2b28af5d643f3f90667973d72e4ca7f9fa1d5/web/src/features/role-management/draft.ts#L8)、[只读工具目录](https://github.com/sawaki-zexi/meido/blob/17c2b28af5d643f3f90667973d72e4ca7f9fa1d5/backend/app/main.py#L1481) 可扩展，目录构造不能顺便截屏或调用模型。 |
| 依赖 | requirements 已有 `Pillow>=12,<13`、httpx；没有必要为了本票引入 Electron、PySide6、Computer Use 驱动或新 AI SDK。 |

## 官方约束与推断

Pillow 文档将 `all_screens` 标为 Windows 参数，并说明 Linux 的默认采集可退到外部 screenshot 工具；macOS Retina 的尺寸也有独立规则。**推断**：仅复制 all_screens=False 不能证明每个平台都“只截主屏”；建议本票先明确 Windows 支持，其他平台报告不可用，而非静默换采集方式。[Pillow ImageGrab](https://pillow.readthedocs.io/en/stable/reference/ImageGrab.html)

运行中的 executor Future 不能通过 cancel 强制停止其线程。**推断**：同步 ImageGrab 放到线程只能避免阻塞事件循环，无法保证采集 deadline 后没有继续持有原图的工作；短命进程会增加启动和发行成本，但可以终止并等待真实 OS 进程退出。[Python Future.cancel](https://docs.python.org/3.13/library/concurrent.futures.html#concurrent.futures.Future.cancel)、[asyncio Process.kill/wait](https://docs.python.org/3.13/library/asyncio-subprocess.html#asyncio.subprocess.Process.kill)

Windows 的 OpenInputDesktop 成功也不能证明当前会话已连接：官方说明断开会话时仍可能返回重连后会激活的 desktop handle。**推断**：建议 worker 同时检查当前 WTS 会话处于 active/unlocked、输入桌面可访问，查询失败一律不可用；不能以一次打开 handle 代替锁屏/断开判断。[OpenInputDesktop](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-openinputdesktop)、[WTS session flags](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_w)

具体待批准方案见 [屏幕感知草案](../specs/screen-perception-spec.md)。这两项推断均未经过本机采集实验；文档提交不表示平台、驱动或真实视觉服务已经验收。
