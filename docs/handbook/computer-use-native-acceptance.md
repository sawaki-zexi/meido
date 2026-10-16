# 构建与验收 Computer Use

状态：当前，已通过本文列明的单机受控范围，扩展环境未验收。最后核验：2026-10-17。事实来源：[build/prepare](../../scripts/computer-use/)、[原生 fixture 测试](../../tests/python/test_computer_use_native.py)、[实际 HTTP/provider 测试](../../tests/python/test_computer_use_api.py)、[固定资源锁](../../backend/app/computer_use/runtime.json)。

本票从用户批准的准确 aggregate `30002a9a8de551a12a44b2c2f5724d5640aca50d` 建立本地 `feat/169-computer-use`；复用已集成 #159 transport，与 #160 仅共享角色配置装配。统一进入 PR #162，不需要等待旧 main，也不另建堆叠 PR。

构建机需已有 Windows x64、Git、VS2022 MSVC 14.42.34433 和 SDK 10.0.22621.0。脚本不安装系统编译器、不改全局 PATH/策略；Rust 1.97.1、Cargo 和 Spectre CRT 放调用者指定的独立缓存。Rustup 1.29.1 的固定官方归档与 SHA、微软 VSIX 和 SHA 记录在 [source.json](../../native/computer-use/source.json)，Rustup 归档格式见[官方安装文档](https://rust-lang.github.io/rustup/installation/other.html)。Spectre 包取自该版本 VS 安装目录 catalog；保留 regorus 默认 Spectre/安全构建，没有关闭其特性绕过工具链缺项。

```powershell
./scripts/computer-use/prepare.ps1 -CacheRoot C:/build-cache/meido-computer-use
./scripts/computer-use/build.ps1 -CacheRoot C:/build-cache/meido-computer-use -ResourceRoot C:/build-resources/meido
```

prepare 固定源码 commit/tag，拒绝覆盖其他 commit；所有下载先校验固定 SHA。build 校验唯一最小补丁以及源树只有该补丁，使用 `cargo build --release --locked -p cua-driver`，只产出 direct 主驱动。只分发 `native/computer-use/cua-driver.exe`、manifest 和 CUA MIT 许可证，不构建/分发 uiAccess helper。实际 PE 用 SDK mt.exe 提取验证 asInvoker/uiAccess=false；启动后再次检查真实 token。

原功能验收使用的旧构建（历史记录）：CUA tag `cua-driver-rs-v0.28.2`，commit `fc188250b4ca8549b8e61f937fdb1fb560770e86`；补丁 SHA256 `12c9612f6ca5f832731bbebaae1a0f78d509ee21224950911246b81f912a9788`；Rust `1.97.1 (8bab26f4f)`、MSVC `14.42.34433`、SDK `10.0.22621.0`；主驱动 SHA256 `a3b4fb309d9ab4e8d91ae5f70ec2661e44b828abbb733a1f8b58bdb479cfebb5`。旧上游 exe SHA 仅溯源，不用于执行校验。release lock、资源 manifest、实际 exe 必须全部匹配。新环境产生不同 hash 时脚本拒绝打包；维护者须先重跑原生证据并明确更新 release lock，不自动信任自签 manifest。当前未宣称跨构建路径二进制完全可复现。构建保留上游 unused imports 和缺少静态 CRT PDB 的 warning；release 成功，不隐藏为零 warning。

开发时设置 `MEIDO_COMPUTER_USE_RESOURCES` 指向资源根；应用默认查 `backend/app/native/computer-use`。不从 PATH、现成 daemon 或网络回退。普通用户使用已准备资源即可，无需 Rust。运行 cwd/TEMP/配置根位于宿主持有的每次驱动私有目录，telemetry/update 环境关闭，进程 CREATE_NO_WINDOW，并由命名 Job 独占。运行不下载/升级。

自动验收通过受审阅的隔离 runner 启动；在 pytest 或应用导入前，为每次运行设置从未使用的 `MEIDO_DATA_DIR`、`MEIDO_ROLES_DIR`、`MEIDO_OWNER_KNOWLEDGE_DATA_DIR`、`MEIDO_BROWSER_USE_DATA_DIR` 四个根，并使用不存在的绝对 basetemp。PROJECT_ROOT 是代码/资源根，不能代替 owner knowledge 数据根；本次 runner 清除继承的 PROJECT_ROOT、资源位置、模型/平台/CLI/NovelAI/搜索凭据变量，只在原生组显式注入已核验的本次 Computer Use 资源。不要直接在日常数据目录执行旧命令。

外层 localhost guard 必须先于 pytest/应用导入启用，测试继续使用 `local_platform_only`。请求结束、原 memory/channel owner 和各测试自有 host/client/provider thread/driver/window 自然收尾后才恢复 guard；原 Job 未退出时不能只以无 Python 线程宣称清理完成。不删除 XML、log、SQLite 或中途失败工件。通用 runtime 中 finish_run 的错误可能被收集，HTTP 夹具只在原公共 finish_run 已真正返回后执行空 host.close，不跨新 loop 接管失败 owner。

原生组固定选择 [native 文件](../../tests/python/test_computer_use_native.py)的四例、[HTTP](../../tests/python/test_computer_use_api.py)的 `owned-native-window` 参数，以及同字节的外部单击回执用例。非原生组选择公开 AgentTool、资源锁、API 的 protocol 参数/角色设置、独立随机 mutex 子进程和[回执 HTTP](../../tests/python/test_computer_use_receipt_api.py)。随机 mutex 子进程没有模型、网络、GUI 或 driver；不得把它说成真实桌面输入。外部 runner 是本次验收工件，不是仓库提供的通用启动器。

fixture 使用已安装 .NET Framework csc 生成自有 WinForms、随机标题和新 PID，只有该窗口可被测试观察/输入；测试只结束自己的 fixture/driver/owner 进程。Python local_platform_only guard 允许 localhost 可控模型 HTTP，拒绝非本地平台连接。原生 driver 的 telemetry/update 同时显式关闭。fixture 通过控件事件/state.json 证明真实效果，不把驱动“接受”当效果。PNG 从有界管道进入内存，不写截图；state.json 只有测试窗口的文字/事件/几何。

已实测 Windows 11 家庭版 10.0.26200：固定 direct driver 通过 MCP 2025-06-18 握手，无通用协议放宽。原生独立套件包含可信来源截图/UIA、主屏最大化、8 工具全部动作、缩小到 200 像素的真实映射命中、同 run release 拒绝、owner 子进程强杀后 Job 清空和 mutex 接管，以及阻塞 UIA Invoke 时取消，验证目标 enabled/exstyle 与基线一致、只点击一次、目标仍存活。background click/type_text/press_key 在已经前台的 fixture 上通过；非前台 B 窗口 background click 和上游不支持的 WinForms background scroll 明确拒绝。四方向滚动使用重新 state 后的显式 foreground，没有生产自动重试。HTTP 原生模式经真实 `/messages`、SQLite、SSE、本地 OpenAI 兼容 provider 完成 20 次请求；UIA 点击、中文 `你好 HTTP`、End、Ctrl+A、上/下/左/右滚动由窗口事件核实；下一模型请求含图像，再下请求不含；SSE/SQLite/audit 无 base64。最终同轮原生/API联合检查为 7 passed / 48.45s。

广泛交叉检查曾为 89 passed / 2 skipped / 1 failed；失败发生在 fixture 自有 state.json 写入时 Windows 共享占用，已修复读取重试与构造失败清理，精确路径确认后的自有遗留 fixture 已退出，7项原生/API复核通过。全量 Python 为 1844 passed / 14 skipped / 1 failed；唯一失败是原有 MCP output_mismatch 用例的隔离 schema worker 超时，预期输出 schema 错误；未放宽安全 deadline，定向重跑同一完整结果参数范围为 9 passed / 17 deselected（22.34s）。最新公开边界与资源锁 26 passed（1.61s）。全量前端为 27 files / 175 tests，通过 TS/Vite build 和 Python compileall。**这些原始作者命令只显式设置 DATA_DIR、ROLES_DIR 和 PROJECT_ROOT，未显式设置 OWNER_KNOWLEDGE_DATA_DIR；不能声称三数据根隔离。**缺省解析可打开该工作树的 `owner-knowledge-data/owner-knowledge.db`；当时完整继承环境未记录，不倒推为全隔离。独立审查后所有修正复核命令显式设置上列三数据根，外部 ledger 保留原始事实。跳过包括需真实平台/可选环境的既有验收和默认未启用的原生 fixture；开启本票资源变量后的原生套件没有跳过。独立 Standards/Spec 审查由协调者针对固定交付 SHA 安排，此文不先声称通过。

公开工具测试证明 A state→B state 以及 B 失败/未发现尝试均撤销 A、WGC 未映射只允许 token、停止 join、TTL/移动/前台活动/错误 token/跨角色/像素越界拒绝；无效来源、origin、ratio、尺寸在图像授权前拒绝。资源测试证明同时换 exe+manifest 也不能绕过仓库 release lock。React 真实 App 流程证明默认关闭、保存/重开和其他能力配置保留。

Standards 审查修正：readiness 5秒超时后，构造器会先设置 release 并 join 专用 lease 线程，再传播准入失败；不会遗忘稍后才取得 mutex 的 owner。自有子进程/随机真实 Windows mutex 的 RED 为 `Empty / remaining_threads=1 / next=busy`，修复后无遗留线程且下一准入成功。修正范围 30 passed（11.31s），专用窗口 stop 和 owner 强杀的原生资源回归 2 passed（6.47s）。这些复核明确隔离 data/roles/owner knowledge 三数据根，保持旧全量和新定向检查的事实分开。若 OS 查询尚未返回，构造器继续拥有并等待该线程，不能先宣称清理完成。

实测当前系统 125% DPI（窗口 dpi=120），主屏最大化 PrintWindow 为 2558×1378，压缩帧 1568×845，physical_origin=[1,1]、resize_ratio=1.6313775510204085；只读 DWM 检查通过。未完成的环境验收必须保留：真实 100%/150% 两种系统 DPI、混合 DPI/多屏、真实锁屏/断连/Session0/不同用户/提权目标、强制 PrintWindow 与 WGC 双失败并同时遮挡场景、所有应用类型的 background 路由、foreground 辅助 VK_NONAME 实际触发。当前测试窗口 DPI/采集尺寸作为原生 JUnit 属性记录；不能拿注入端口用例替代这些环境实测。首版只支持完全位于主屏的普通窗口；background 只容许目标已经是精确 foreground 的保守子集。UAC、UIAccess/helper、提权、非主屏/跨屏、远程 console 均不支持。未观察目标应用实际效果的输入始终保留 unknown；不能回滚。

## 2026-10-17 实际调用回执修正

本次 `fix/169-foreground-input-receipts` 从已核验双 CI 的准确集成基点 `6751bcc425ad1c0fc4823691fbdda155a89c7bdf` 开始，仍只修改已批准的普通权限 direct profile。固定 CUA commit、Rust/MSVC/SDK 不变。当前补丁 SHA256 为 `9ca2222bad0ef99a8011add6a07310290522c76b958127b3b0a9c33c73e70a0c`，实际主驱动 SHA256 为 `f700163b2a053dbadee45339d877abe88b425aa2974ceffd29b5d9ce2b08e587`，资源 manifest 与 runtime 字节 SHA256 均为 `f670a6ba55a6abd512be8d1ebcd5c728673755f94112cb088b27c46250468037`。实际 PE 再次提取为 asInvoker/uiAccess=false。离线 `cargo build --release --locked --offline -p cua-driver` 成功（38.92秒），874 个原生输入前后同字节，原15条主驱动 warning 保留。

原固定驱动的同字节原生回执用例先真实失败；中间 candidate `698572...` 虽已编译，但原生结果暴露上游闭合 ActionResult 投影覆盖回执，未当作成功验收。最终补丁将 call-local 五字段回执放进专用有限 text carrier；宿主严格提取，不放宽上游全局 schema/SDK。17 个公开协议 TDD 周期保留各自红/绿完整测试字节和工件：包括 nested focus/恢复、撤权后保留事实、丢响应、generic error 的有限原因、唯一 carrier、重复/歧义/畸形/深 JSON、无结构化数据时不泄露载体，以及禁用目标仍可观察但五种输入在驱动调用前拒绝。第13周期原6失败/1通过到7通过，第15周期4失败到4通过，第16周期5失败到5通过，第17周期2失败到2通过；其他周期各1失败到1通过。第17周期来自固定ec22的规格审查：转义marker的重复键或尾逗号错误原会被忽略，继而接受后续合法载体；现对严格解析失败的JSON对象候选直接拒绝，不改变合法单份转义载体或普通前台错误。协议中的计数是声明值，不当作原生次数。

准确源 freeze4 的实际 `f700` 专用窗口六项为 **6 passed / 44.22秒 / 0 skip**，只有2条 record_property/xunit2 warning，覆盖同字节首例、原四例及原生 HTTP 参数。首例实际 A 点击1次、B0次，enabled/exstyle 不变；回执 `checks=1`、`already_foreground_checks=1`、`set_foreground_calls=0`、`assisted_attempts=0`、两个 VK API 标记 false，业务 API true/effect unknown，原异步恢复为 pending/null。该例证明已前台分支，不证明辅助键触发。原窗口/Job、memory/channel owner 全部退出，495个输入及3个资源前后稳定，0新线程/非本地连接；PNG不写盘。后续宿主修正未重建驱动，但这六项未在 freeze6/7 整组重跑。

最终宿主准确源 freeze7 的相关非原生组 **61 passed / 29.624秒 / 0 skip**，1条 record_property warning，覆盖两条真实 localhost HTTP 流程、资源替换拒绝、配置、租约及全部公开协议行为。回执 HTTP 的20次实际 provider 请求中8个动作回执由下一请求消费，SSE与SessionStore工具记录保留安全回执，PNG仅紧接着一次请求可见，随后清除，原图不在公开/session、SSE、SQLite或audit。finish_run真实返回、各close返回、生产输入Job为空、server/handler线程与原owner自然收尾均检查；audit仍只记录既有终态类别，不新存回执列。原 freeze5 的54项与 freeze6 的59项通过保留为历史工件，不与本组重复相加。

准确源 freeze7 的 Python 3.13 backend/tests compileall 通过；mypy2.1.0 用同一命令检查全部7个 Computer Use Python源，并对准确6751基点的232份backend归档做独立基线。当前SOURCE_HEAD为ec22并加本次宿主/测试修正，BASE仍为6751，没有把基线改成已含回执修正的ec22。两边均为 **9 diagnostics / checked7**，原始消息和所属函数的完整 multiset相同，0新增；Found9与实际提取9条逐边核对，现有Windows3、lease1、host5条保留，不能称全量类型通过。495个当前输入全程稳定，静态检查无app模块导入。CI的Python3.12与本地3.13不同；准确最终提交的两轴审查/CI另核，不沿用6751的旧绿色。

禁用目标准入修正前的准确源 freeze5 另执行同字节首例与一次自有窗口候选，整组为 **1 passed / 1 failed / 8.25秒**，2条 record_property warning；首例通过，候选因 `assisted_attempts=0` 未进入辅助分支而失败。候选前提A disabled/B foreground均实证，单次实际回执 `checks=1`、`already_foreground_checks=0`、`set_foreground_calls=1`、`activated=true`，两个VK API标记false，实际0xFC消息0；业务API已调用、effect unknown，恢复pending/null。最终A0/B0次点击、flags不变、前台B，这些事实不证明其他窗口没有受影响，也不证明输入已撤回。本机结果否定“disabled目标必然阻止业务输入/保证辅助分支”的假设；根因不唯一，不再重试该候选或改系统/算法制造VK。辅助键API实际触发及到件仍未证。两个host/Job/fixture和原owner全部收尾，495输入与3资源前后稳定，无新线程/非本地连接，guard恢复。原始工件 `computer169-final-native-helper-v1-13ab42f23a06` 保留失败，不并入原六项的成功计数。

据此在原宿主输入边界新增 IsWindowEnabled 前置检查；list/state不拒绝禁用目标。freeze6 的独立真实 Windows 验收为 **1 passed / 4.532秒 / 1条 record_property warning**：实际 WindowsDesktop 与 `f700` 只执行 list/state 两个只读 RPC，公开 click 返回 `target_disabled`、`effect=not_started`，没有 input_receipt。测试包装器对全部五种输入 RPC 均在转发前抛错，实际 input/unknown RPC 尝试均为空；A/B 点击和 flags不变、前台B，host/driver/Job/fixture全退出。此项证明禁用目标被宿主拒绝，不是另一轮辅助分支探测，也不证明0xFC触发或到件。原始工件 `computer169-disabled-admission-native-v1-c99b5faefb39` 与原六项、最终61组、类型检查各自保留准确快照，freeze7没有重跑原生；检查与OS输入之间仍可能发生窗口状态变化，不宣称原子输入事务。双采集失败并同时遮挡、其他DPI/多屏及真实应用环境仍保持前文未验收状态。
