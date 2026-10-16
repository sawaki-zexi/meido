# 构建与验收 Computer Use

状态：当前，已通过本文列明的单机受控范围，扩展环境未验收。最后核验：2026-10-16。事实来源：[build/prepare](../../scripts/computer-use/)、[原生 fixture 测试](../../tests/python/test_computer_use_native.py)、[实际 HTTP/provider 测试](../../tests/python/test_computer_use_api.py)、[固定资源锁](../../backend/app/computer_use/runtime.json)。

本票从用户批准的准确 aggregate `30002a9a8de551a12a44b2c2f5724d5640aca50d` 建立本地 `feat/169-computer-use`；复用已集成 #159 transport，与 #160 仅共享角色配置装配。统一进入 PR #162，不需要等待旧 main，也不另建堆叠 PR。

构建机需已有 Windows x64、Git、VS2022 MSVC 14.42.34433 和 SDK 10.0.22621.0。脚本不安装系统编译器、不改全局 PATH/策略；Rust 1.97.1、Cargo 和 Spectre CRT 放调用者指定的独立缓存。Rustup 1.29.1 的固定官方归档与 SHA、微软 VSIX 和 SHA 记录在 [source.json](../../native/computer-use/source.json)，Rustup 归档格式见[官方安装文档](https://rust-lang.github.io/rustup/installation/other.html)。Spectre 包取自该版本 VS 安装目录 catalog；保留 regorus 默认 Spectre/安全构建，没有关闭其特性绕过工具链缺项。

```powershell
./scripts/computer-use/prepare.ps1 -CacheRoot C:/build-cache/meido-computer-use
./scripts/computer-use/build.ps1 -CacheRoot C:/build-cache/meido-computer-use -ResourceRoot C:/build-resources/meido
```

prepare 固定源码 commit/tag，拒绝覆盖其他 commit；所有下载先校验固定 SHA。build 校验唯一最小补丁以及源树只有该补丁，使用 `cargo build --release --locked -p cua-driver`，只产出 direct 主驱动。只分发 `native/computer-use/cua-driver.exe`、manifest 和 CUA MIT 许可证，不构建/分发 uiAccess helper。实际 PE 用 SDK mt.exe 提取验证 asInvoker/uiAccess=false；启动后再次检查真实 token。

接受的构建：CUA tag `cua-driver-rs-v0.28.2`，commit `fc188250b4ca8549b8e61f937fdb1fb560770e86`；补丁 SHA256 `12c9612f6ca5f832731bbebaae1a0f78d509ee21224950911246b81f912a9788`；Rust `1.97.1 (8bab26f4f)`、MSVC `14.42.34433`、SDK `10.0.22621.0`；主驱动 SHA256 `a3b4fb309d9ab4e8d91ae5f70ec2661e44b828abbb733a1f8b58bdb479cfebb5`。旧上游 exe SHA 仅溯源，不用于执行校验。release lock、资源 manifest、实际 exe 必须全部匹配。新环境产生不同 hash 时脚本拒绝打包；维护者须先重跑原生证据并明确更新 release lock，不自动信任自签 manifest。当前未宣称跨构建路径二进制完全可复现。构建保留上游 unused imports 和缺少静态 CRT PDB 的 warning；release 成功，不隐藏为零 warning。

开发时设置 `MEIDO_COMPUTER_USE_RESOURCES` 指向资源根；应用默认查 `backend/app/native/computer-use`。不从 PATH、现成 daemon 或网络回退。普通用户使用已准备资源即可，无需 Rust。运行 cwd/TEMP/配置根位于宿主持有的每次驱动私有目录，telemetry/update 环境关闭，进程 CREATE_NO_WINDOW，并由命名 Job 独占。运行不下载/升级。

自动验收必须指定 3 项独立应用根，并只使用新建 fixture：

```powershell
$env:MEIDO_DATA_DIR='C:/test-data/meido169/data'
$env:MEIDO_ROLES_DIR='C:/test-data/meido169/roles'
$env:MEIDO_PROJECT_ROOT=(Get-Location).Path
$env:MEIDO_COMPUTER_USE_NATIVE_RESOURCES='C:/build-resources/meido'
python -m pytest tests/python/test_computer_use.py tests/python/test_computer_use_resources.py tests/python/test_computer_use_native.py tests/python/test_computer_use_api.py -q
```

fixture 使用已安装 .NET Framework csc 生成自有 WinForms、随机标题和新 PID，只有该窗口可被测试观察/输入；测试只结束自己的 fixture/driver/owner 进程。Python local_platform_only guard 允许 localhost 可控模型 HTTP，拒绝非本地平台连接。原生 driver 的 telemetry/update 同时显式关闭。fixture 通过控件事件/state.json 证明真实效果，不把驱动“接受”当效果。PNG 从有界管道进入内存，不写截图；state.json 只有测试窗口的文字/事件/几何。

已实测 Windows 11 家庭版 10.0.26200：固定 direct driver 通过 MCP 2025-06-18 握手，无通用协议放宽。原生独立套件包含可信来源截图/UIA、主屏最大化、8 工具全部动作、缩小到 200 像素的真实映射命中、同 run release 拒绝、owner 子进程强杀后 Job 清空和 mutex 接管，以及阻塞 UIA Invoke 时取消，验证目标 enabled/exstyle 与基线一致、只点击一次、目标仍存活。background click/type_text/press_key 在已经前台的 fixture 上通过；非前台 B 窗口 background click 和上游不支持的 WinForms background scroll 明确拒绝。四方向滚动使用重新 state 后的显式 foreground，没有生产自动重试。HTTP 原生模式经真实 `/messages`、SQLite、SSE、本地 OpenAI 兼容 provider 完成 20 次请求；UIA 点击、中文 `你好 HTTP`、End、Ctrl+A、上/下/左/右滚动由窗口事件核实；下一模型请求含图像，再下请求不含；SSE/SQLite/audit 无 base64。最终同轮原生/API联合检查为 7 passed / 48.45s。

广泛交叉检查曾为 89 passed / 2 skipped / 1 failed；失败发生在 fixture 自有 state.json 写入时 Windows 共享占用，已修复读取重试与构造失败清理，精确路径确认后的自有遗留 fixture 已退出，7项原生/API复核通过。全量 Python 为 1844 passed / 14 skipped / 1 failed；唯一失败是原有 MCP output_mismatch 用例的隔离 schema worker 超时，预期输出 schema 错误；未放宽安全 deadline，定向重跑同一完整结果参数范围为 9 passed / 17 deselected（22.34s）。最新公开边界与资源锁 26 passed（1.61s）。全量前端为 27 files / 175 tests，通过 TS/Vite build 和 Python compileall。跳过包括需真实平台/可选环境的既有验收和默认未启用的原生 fixture；开启本票资源变量后的原生套件没有跳过。独立 Standards/Spec 审查由协调者针对固定交付 SHA 安排，此文不先声称通过。

公开工具测试证明 A state→B state 以及 B 失败/未发现尝试均撤销 A、WGC 未映射只允许 token、停止 join、TTL/移动/前台活动/错误 token/跨角色/像素越界拒绝；无效来源、origin、ratio、尺寸在图像授权前拒绝。资源测试证明同时换 exe+manifest 也不能绕过仓库 release lock。React 真实 App 流程证明默认关闭、保存/重开和其他能力配置保留。

实测当前系统 125% DPI（窗口 dpi=120），主屏最大化 PrintWindow 为 2558×1378，压缩帧 1568×845，physical_origin=[1,1]、resize_ratio=1.6313775510204085；只读 DWM 检查通过。未完成的环境验收必须保留：真实 100%/150% 两种系统 DPI、混合 DPI/多屏、真实锁屏/断连/Session0/不同用户/提权目标、强制 PrintWindow 与 WGC 双失败并同时遮挡场景、所有应用类型的 background 路由、foreground 辅助 VK_NONAME 实际触发。当前测试窗口 DPI/采集尺寸作为原生 JUnit 属性记录；不能拿注入端口用例替代这些环境实测。首版只支持完全位于主屏的普通窗口；background 只容许目标已经是精确 foreground 的保守子集。UAC、UIAccess/helper、提权、非主屏/跨屏、远程 console 均不支持。未观察目标应用实际效果的输入始终保留 unknown；不能回滚。
