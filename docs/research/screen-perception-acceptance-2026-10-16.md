# 屏幕感知公开与原生验收

状态：独立分支实现验收，待总集成两轴审查。日期：2026-10-16（Asia/Shanghai）。

事实来源：[批准规格](../specs/screen-perception-spec.md)、[AgentTool 用例](../../tests/python/test_screen_perception.py)、[HTTP 用例](../../tests/python/test_screen_perception_api.py)、[受控原生用例](../../tests/python/test_screen_perception_native.py)、[网页用例](../../web/src/features/role-management/ScreenPerceptionSettings.test.tsx)。基点 `814c57cd396684503ab40b313891e6343fb00dd8`，UI 提交 `4e9f94a3dacce139161bb3b8bfc3c82a819f539b`。

## 已验证

- 默认关闭，显式稳定连接 ID，角色保存与部分字段省略、只改 enabled、重载保留；无 active/chat fallback，本地无 Key 连接沿用既有认证行为。
- 真实受控 PNG 子进程经过现有 OpenAI Completions 和 Anthropic Messages 两协议，固定独立视觉请求无角色/历史/工具，只返回三字段摘要。HTTP 主模型请求工具后，下一主模型请求、SessionStore 历史、SSE 和审计只有摘要，没有 data URL/base64 原图，受控目录无 PNG 文件。
- 额外 role/model/bbox/source 参数、错误角色/会话/run、停止 run、内部/定时/配对渠道、关闭、空/失效绑定和不支持图像的 Mistral 模型都在采集前拒绝。
- 坏元数据/PNG/过大长度/过大 stderr、采集阻塞 deadline、JSON 围栏/重复 key/非有限值/额外动作/错误类型/空值/超字段或总文本预算、provider 工具事件和请求回显错误均得到安全失败。合法文本按 400/80 归一化。
- 真实阻塞采集、视觉长连接、全局 busy、token/Task/重复取消、role/config/shutdown invalidate；角色/模型 HTTP 修改及删除等真实收尾后才成功。stop 等待 child terminal，DELETE 运行中为 409，stop 后为 204。

## 审查发现与取消修正

Standards 审查在 `bbe41e1` 发现：多个 role/config invalidate、signal 和外层 Task 取消会重复取消观察任务，打断 provider 的 `__anext__` 内部 response/client finally。仅在后面 shield `iterator.aclose()` 不能恢复已被中断的 HTTP 清理，旧实现在真实 peer 仍连接时就归还名额。

修正用同一观察对象持有一次性取消 latch。撤权、signal、外层停止和 capture/vision/overall deadline 都只能首次通知操作取消；后续调用等待原收尾。三个宿主 deadline 使用相同取消入口，避免嵌套 timeout 在慢关闭期间再次注入取消。

新增公开 AgentTool/invalidate 回归保留真实 PNG 子进程、RoutedModelAdapter 和 localhost SSE，只延迟外部 httpcore 的真实连接关闭，以稳定观察慢清理。旧提交出现“observation returned before real HTTP close”红例；修正后连续 role→config→role、signal、重复 Task 取消均等待，另一合法角色保持 busy。另一个用例实际等过 12 秒视觉和 18 秒整体预算，确认 deadline 叠加也不打断 close；释放后 peer 真实断开、首个 worker 终止，下一合法观察能重新采集并成功。此次修正不重复原生桌面采集，不调用外部模型。

修正后的 screen/HTTP、Agent Runtime、工具发现 HTTP 综合命令为 `python -m pytest tests/python/test_screen_perception.py tests/python/test_screen_perception_api.py tests/python/test_screen_perception_native.py tests/python/test_agent_runtime.py tests/python/test_tool_discovery_api.py -q`：99 passed、1 个默认原生 skip。随后独立补充/复跑慢 close 的两种触发和普通视觉 deadline：3 passed。新模块 Ruff、mypy、compileall 通过；这些数量包含重叠用例，不相加。两轴仍待最终修复复核。

Spec 完整审查随后在 `52bccec` 发现另一条提前释放路径：文本预算或工具事件提前拒绝时，关闭最外层 iterator 未向内层 `async for/yield` adapter 包装传播，HTTP 清理落给 asyncgen finalizer。修正在已有 rich adapter 每一层以 `contextlib.aclosing` 传播并等待关闭；不新增 gateway/SDK。Responses 的 Azure/Codex 子类和 Google 的 Vertex 子类沿同一实现继承；legacy→rich 委托也明确收尾其拥有的 iterator。

真实 PNG 子进程与 localhost 持续响应验证十种已注册协议的文本/工具事件提前拒绝，共 20 项：旧 OpenAI/Anthropic 四个最小红例均在真实 close 前返回；修正后全部在底层关闭期间保持 pending、第二调用为 busy、只启动一个采集 worker，释放后生产路径先断开 peer 才交付 invalid_observation。夹具只在失败时回收逃逸 socket，不参与通过条件。AI 模块 mypy 的 51 项旧错误与 `52bccec` shadow 基线逐条一致，无新增；同样未重复原生采集或调用远端服务。

包含上述矩阵的完整命令 `python -m pytest tests/python/test_model_protocol_adapters.py tests/python/test_model_layer.py tests/python/test_model_tools.py tests/python/test_model_event_stream.py tests/python/test_screen_perception.py tests/python/test_screen_perception_api.py tests/python/test_screen_perception_native.py -q`：196 passed、1 个默认原生 skip。Ruff、screen 模块 mypy、compileall 和 diff 检查通过。该批包含先前用例，不与前面的通过数相加。

## Windows 受控原生

明确开启 `MEIDO_SCREEN_NATIVE=1` 后单独执行原生用例。测试先建立不透明、置顶、覆盖完整主屏的 Tk 四色窗口，确认尺寸及 5×5 网格点的实际顶层窗口归属；在启动真实采集前再确认覆盖。生产 worker 的图像只发到本机临时 HTTP fixture，fixture 收到的 PNG 在内存解码后精确匹配四个象限标记；不保存截图/base64。finally 销毁窗口、恢复线程 DPI。

2026-10-16 的实际结果：

| 项目 | 实际证据 |
| --- | --- |
| OS / Python / Pillow | Windows 11 `10.0.26200` / `3.13.9` / `12.0.0` |
| 显示器 / 主屏 / DPI | 1 / 2560×1440 / 120（125%） |
| 处理后帧 | 2048×1152，保持长宽比，PNG 9710 字节 |
| 工具 / 视觉 | available=true，固定 fixture editor/writing；仅 localhost fixture，无用户或付费视觉连接 |
| 总观察耗时 | 1022 ms（含进程启动、真实 ImageGrab、编码和本地模型协议） |
| 原生取消 | 另一次真实 worker 启动后重复取消，2 ms 收尾；无第二次图片上传 |
| 资源终态 | 成功 worker exit 0；取消 worker terminal；HTTP 上下文关闭；窗口 finally 关闭 |
| 命令结果 | `MEIDO_SCREEN_NATIVE=1 python -m pytest tests/python/test_screen_perception_native.py -q -s`：1 passed；普通测试默认 skip |

没有截取任意用户桌面，也没有保存原图或将画面发到外部视觉服务。该证据验证生产采集和已有协议路径；本地 fixture 的固定摘要不等于真实远端模型识别能力验收。

## 尚未验证

此机器只有一个显示器，因此没有双屏副屏排除与混合 DPI 证据。没有实际锁屏、远程断开或安全桌面实验，也没有 frozen executable 分发；这些场景保持保守拒绝/不可用设计，不能宣称全部实机验收。通用 adapter 的单个 wire 事件字节上限仍是既有边界，16 KiB 仅是模块消费的解码文本预算。

总 PR 集成回归、两轴独立审查和合并批准另行完成；不关闭 Issue #160。
