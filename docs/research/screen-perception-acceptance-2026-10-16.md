# 屏幕感知公开与原生验收

状态：独立分支实现验收，待总集成两轴审查。日期：2026-10-16（Asia/Shanghai）。

事实来源：[批准规格](../specs/screen-perception-spec.md)、[AgentTool 用例](../../tests/python/test_screen_perception.py)、[HTTP 用例](../../tests/python/test_screen_perception_api.py)、[受控原生用例](../../tests/python/test_screen_perception_native.py)、[网页用例](../../web/src/features/role-management/ScreenPerceptionSettings.test.tsx)。基点 `814c57cd396684503ab40b313891e6343fb00dd8`，UI 提交 `4e9f94a3dacce139161bb3b8bfc3c82a819f539b`。

## 已验证

- 默认关闭，显式稳定连接 ID，角色保存与部分字段省略、只改 enabled、重载保留；无 active/chat fallback，本地无 Key 连接沿用既有认证行为。
- 真实受控 PNG 子进程经过现有 OpenAI Completions 和 Anthropic Messages 两协议，固定独立视觉请求无角色/历史/工具，只返回三字段摘要。HTTP 主模型请求工具后，下一主模型请求、SessionStore 历史、SSE 和审计只有摘要，没有 data URL/base64 原图，受控目录无 PNG 文件。
- 额外 role/model/bbox/source 参数、错误角色/会话/run、停止 run、内部/定时/配对渠道、关闭、空/失效绑定和不支持图像的 Mistral 模型都在采集前拒绝。
- 坏元数据/PNG/过大长度/过大 stderr、采集阻塞 deadline、JSON 围栏/重复 key/非有限值/额外动作/错误类型/空值/超字段或总文本预算、provider 工具事件和请求回显错误均得到安全失败。合法文本按 400/80 归一化。
- 真实阻塞采集、视觉长连接、全局 busy、token/Task/重复取消、role/config/shutdown invalidate；角色/模型 HTTP 修改及删除等真实收尾后才成功。stop 等待 child terminal，DELETE 运行中为 409，stop 后为 204。

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
