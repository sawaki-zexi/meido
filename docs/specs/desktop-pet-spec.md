# Electron 桌宠

状态：当前实施规格；主人于 2026-10-17 明确选择“Electron 独立宿主（推荐）”，并要求按此继续实施。实现和当前 Windows 本地原生验收见[验收记录](../research/desktop-pet-acceptance-2026-10-17.md)；准确总提交 CI 及交付检查以 PR 为准。真实模型、账号、外部发布与总 PR 合并许可没有扩大。

实施票：[#195](https://github.com/sawaki-zexi/meido/issues/195)。Parent：[#137](https://github.com/sawaki-zexi/meido/issues/137)，统一交付[总 PR #162](https://github.com/sawaki-zexi/meido/pull/162)。事实来源：[总规格 P05/D04](shiori-tool-parity-spec.md)、[固定上游调研](../research/pet-host-primary-sources-2026-10-17.md)、[ADR-0020](../adr/0020-owned-electron-desktop-pet.md)。

## 1. 用户结果

主人在角色设置导入 Shiori `codex-sprite@1` ZIP，选择并显式开启后，真实 Windows 桌面显示透明置顶桌宠。角色在当前主人桌面对话中调用 `pet_action`，能够移动到五个语义位置或播放当前包声明的动作。网页聊天和 Python 后端继续作为原主入口。

网页显示绑定、可用动作、宿主真实状态和错误，提供停止、重启及删除包入口。关闭、切换角色、删除角色与后端重启保持一致；旧动作不会落到新角色或重播。维护者可以从本地发行目录启动宿主，不依赖 Vite 开发服务器。

## 2. 范围与批准

本票完成 P05 的包、角色状态、工具、Electron 窗口与原生闭环；先交付当前 Windows 桌面。其他平台明确显示未支持，不声称跨平台实证。

主人本次批准的选项包含保留现有主入口、适配 React 播放器、真实透明置顶窗口、导入/绑定、move/play、停用和重启。以下是该路线内的接口与资源收尾细化。语音、直播、气泡聊天、全局输入、任意插件窗口和外部发布不随桌宠引入。

## 3. 实施合同

### 3.1 包与角色状态

- 使用固定 Shiori `4f266aeaf42fcd611362e2456417b91f659341cf` 的 manifest、动作映射、atlas 格式与节拍，保留版权。接入 Meido 角色身份，不复制另一套角色数据库。
- 接受根目录或单层 wrapper 的 `pet.json`，必填 id/displayName/description/spritesheetPath；previewPath 与 actions 可选。动作名符合 `[a-z][a-z0-9_-]{0,63}`，映射仅为 idle/running-right/running-left/waving/jumping。
- atlas 是单帧 1536×1872 WebP，格子192×208，九行已用格数6/8/8/4/5/8/6/6/6；必需格有内容、未用格透明。预览仅 PNG/WebP，边长64–2048。
- ZIP 输入最多32MiB、128个成员、声明展开量合计64MiB；实际读取另按上界截断。拒绝重复/不安全路径、链接、加密和超额资源。只保存校验后的固定数据和图片，包不能导入脚本。
- 包存放于角色私有插件目录，位于普通聊天 workspace 外；每角色最多8包、持久文件合计256MiB，应用合计1GiB。失败导入清理仅属于本次的暂存，不删除主人其他素材。
- 插件状态仅保存包与选择，角色存在性始终由 RoleStore 决定。默认关闭，同时只有一个角色显示；开启另一个角色时使旧绑定失效。状态有修订/代际，选择和动作均关联不可变角色与包身份。

### 3.2 窄接口与授权

- 主人网页提供当前角色的包列表、ZIP 导入、选包启停、重启、删除及图片预览；不能提交任意本机路径或 Electron 命令。
- `pet_action` 参数保留 action=move/play；move.target 为 top_left/top_right/center/bottom_left/bottom_right，animation 为空/idle/run；play.name 的 enum 来自冻结包动作。
- 接入原 ToolDefinition、能力目录和同 run tool_search。关闭、未绑定、平台不支持、构建缺失和宿主未 ready 有可解释诊断；通用 enabledTools 不能绕过独立许可。
- 仅真实主人当前角色桌面对话可用。宿主从原 SessionStore/run/user message 核对身份，不信模型自报角色、来源或回合。渠道、访客、子任务、Coding、通用调度、A01/Drift 不继承桌宠控制权。
- 开始运行冻结角色/包/绑定代际，执行前重验；切包、撤权、删除、桥重建拒绝旧调用。每角色3秒冷却，每真实 run 最多一次动作；并发调用也不能突破。派发不明时保留该动作责任，不自动重发。
- 主人输入准入将桌宠代际写入原 run 快照；工具版本同时带绑定代际，不能仅靠内存对象冻结。后端重建恢复已启用绑定时旋转代际，因此旧 run 即使仍有记录，也不能继承新桥控制资格。

### 3.3 Electron 窗口与受管桥

- 新增可选 Electron sidecar；主聊天不迁入 Electron。后端持有独立原生进程及进程树生命周期，复用现有 WindowsJob 收尾能力；长期窗口不伪装成600秒模型任务。
- 固定构建入口通过受管 stdio 交换有界 JSON，不开放通用 Shell、任意本机端口或远程网页。绑定握手与每条动作携带宿主代际、角色/包身份、请求ID；每帧最多16KiB，队列与诊断有限。
- Electron main/preload/renderer 是固定应用代码。启用 contextIsolation、sandbox，禁用 renderer Node；IPC 核对原 webContents/frame，本地图片只来自已批准包的固定素材映射，禁止导航、新窗口与远程代码。
- 窗口透明、置顶、不抢聊天焦点；真实移动只作用于本宿主窗口。使用屏幕工作区和 Electron 坐标，五位置落在可用显示器内；重启/拔屏后夹紧保存的位置。不能把透明像素等同自动点击穿透。
- 适配原 React atlas 播放器，保留 idle节拍×6、非idle三轮后回idle。动作标识区分 accepted、renderer-started、finished、失败或不明；accepted 不声称移动/播放已经完成。
- 停用先撤销新动作资格，再等待原窗口、stdio、timer/listener、主/renderer/helper进程退出。原资源未收尾时保留 cleanup_pending，不能清空 owner 后启动第二宿主。
- 窗口关闭、renderer失败或桥断开不自动重播动作；主人可显式重启。应用重启可恢复原有效绑定和位置，恢复失败保持诊断；已关闭的配置不自行开启。

### 3.4 构建与集成边界

- 计划固定 Electron 44.8.0（官方 npm registry 于2026-10-17核验，Node要求≥22.12）；版本和依赖落入 lockfile，新增宿主构建与本地 Windows 发行目录命令，保留 Electron/Chromium/Node 与上游许可；不调用签名服务或外部发布。
- 普通前端构建覆盖桌宠 renderer 类型和产物；包导入与模型工具使用同一后端服务，网页展示真实配置与状态，不维护第二份授权。
- main.py 仅装配服务、路由、工具和原生命周期；包校验、绑定规则、原生桥、动作策略分别由其拥有模块处理。保留已有 #148/#169/#191/#192 接缝。

## 4. 必要验收

1. 隔离 HTTP 导入自制合法 ZIP、绑定角色并启用，发现动态动作；坏包/越权拒绝。普通角色配置与聊天入口保持。
2. 真实模型循环经 tool_search 或冻结 schema 调用 move/play，下一响应拿到原动作回执；错角色/渠道、关闭、旧包、无效动作、冷却和同 run 第二次拒绝。
3. 明确验证桥失败、renderer 未 ready、切包/停用后的迟到动作和原宿主收尾；不重播旧动作，不以超时虚构 terminal。
4. Windows 实机运行真实 Electron，在自己的透明窗口执行五位置移动和一个包动作；核窗口可见、实际 bounds、renderer帧/完成事件，采集仅限自己的窗口。证据分别报告 accepted/started/finished。
5. 停用、重启、关窗和应用重建后原进程树收尾；从本地发行目录再次完成绑定恢复和动作。无法实际验收的其他DPI/多屏环境单列，不冒充已测。

遵循最新 AGENTS：开发只跑最小相关集；保留一组有意义的包/HTTP/协议回归与一次原生闭环，改动后仅补受影响检查。最终集中构建、独立两轴审查和准确提交 CI；复用未变的已有证据，不为提交、审查或文档反复全跑。
