# Electron 桌宠本地验收

状态：当前；最后核验：2026-10-17。生产主体 `6fd9485`，撤权/恢复修正 `9592f8b`，尺寸与持久代际修正 `04a135e`。事实来源：[规格](../specs/desktop-pet-spec.md)、[行为测试](../../tests/python/test_desktop_pet_api.py)、[完整后端重建夹具](../../tests/fixtures/desktop_pet_rebuild.py)。Issue #195 通过总草稿 PR #162 交付，尚未合入 main。

## 已实际完成

- 合法 Shiori `codex-sprite@1` ZIP 导入、默认关闭、角色绑定、原始图片读取；坏路径、跨角色包、重复 ID、过期配置拒绝。普通角色 agentConfig 保持原接口。
- 原聊天模型循环通过 `tool_search` 加载当前包动作 enum，调用真实 `pet_action`，消费和保存原 actionId 回执。当前主人、角色、session、run、人类输入来源与持久绑定代际共同核对；跨角色/伪渠道/自主来源、重启前工具、每 run 第二次和三秒冷却均有回归。
- 原派发线程已进入、停用已提交而原 close 仍等待的 barrier 回归：放行后实际 send=0、工具返回错误，原宿主退出。派发与撤权使用同一短门，回执等待在门外；没有把取消或超时当作退出证明。
- 固定 Electron44.8.0、本地发行 `MeidoPet.exe`、真实 Windows 透明置顶且不抢焦点窗口；网页和 Python 主入口保留。五位置移动、包动作三轮播放、关闭/重启/断连/停用及原 WindowsJob/管道线程收尾有实机证据。
- 两个不同的完整 Python 后端进程，从同一持久库恢复同角色/包，第二进程使用新绑定代际和新 actionId 实际挥手；每个原应用与 native 树都已退出。发行启动不依赖 Vite 开发服务器。
- 截图仅来自本宿主 `webContents.capturePage`，测试显式启用，固定写入私有 runtime。根会话已查看：125%DPI 下240×260 RGBA，角像素 `(0,0,0,0)`，测试精灵中心 `(80,130,170,255)`；没有采集其他窗口或桌面。

## 版本、命令与结果

本地原件均在 `D:/meido-artifacts/`；使用隔离 DATA/角色/HOME/临时目录、导入前空凭据和 localhost 守卫，保留到原应用退出。合成模型不访问远端服务。各结果重叠，不相加为总验收数量。

| 检查 | 结果 | 日志目录 |
| --- | --- | --- |
| 包/配置首个 HTTP 红绿 | 404 RED；修正装配与 Path 后1 passed | `pet195-package-binding-green-fixed-fcc0e9e4d1` |
| 初次完整发行与重建行为组 | 5 passed，73.82秒；当时尚未断言所有 bounds/置顶 | `pet195-native-release-and-rebuild-c2a72e03aa` |
| 最终五位置、尺寸、工作区、置顶、回执、断开与原树退出 | 1 passed，50.91秒 | `pet195-native-dpi-bounds-2816d1cfc2` |
| 持久代际、每 run/冷却/来源、原派发撤权、损坏素材与真实两进程重建 | 4 passed，30.33秒 | `pet195-native-durable-generation-5f80d6694c` |
| React桌宠设置及原角色能力保存回归 | 4 passed / 2文件 | `vitest run src/features/role-management/DesktopPetSettings.test.tsx src/features/role-management/RoleCapabilities.test.tsx` |
| 根 `npm run build` | TypeScript、网页111模块、桌宠renderer22模块通过 | 根会话原构建输出；后续仅改 native main 的 tsc 已补齐 |
| 新增 Python 服务及夹具编译 | `compileall -q` 通过 | 根会话原命令输出 |

实际工作区为2048×1104 DIP。五位置最终 bounds 分别为 `(0,0)`、`(1856,0)`、`(928,448)`、`(0,896)`、`(1856,896)`，始终192×208 DIP；每次 move24帧、wave12帧，均依原 actionId 收到 accepted → started → finished。

## 修正与证据边界

原生启动先后暴露可执行文件尚未按需下载、Python无 CREATE_SUSPENDED 常量、Windows启动CRLF和 Electron 替换 stdin 为 EOF。已固定 setup 命令、Windows flag、有界空行处理与继承fd0读取；[固定 Electron stdin 源码](https://github.com/electron/electron/blob/v44.8.0/lib/common/init.ts)支持该适配。

独立两轴审查在6fd定位实际派发竞态，Spec另定位缺失素材阻断主后端；9592修正，新增差异复核无遗留。原始报告保留，不追认旧源已通过。随后核验实际几何发现125%DPI下 `setPosition` 尺寸累积，改显式 `setBounds`；默认 floating 层可能把窗口移至任务栏之后，明确置顶层级后核实实际状态，依据[固定原生实现](https://github.com/electron/electron/blob/v44.8.0/shell/browser/native_window_views.cc)。

早期 Win32 PrintWindow 抓到黑图，不能作为渲染成功证据；最终只采用自身 compositor 截帧。旧 native 组虽 passed，未断言尺寸/置顶的局限已列出，不以旧回执覆盖后续发现。04a最后小段两轴复核无阻塞；Standards提出一项非阻塞的奇数工作区中心舍入夹具差异，已按宿主 Math.round 改为整数公式，本机偶数尺寸原断言结果不变，没有追加实机重跑。准确总提交 CI 以 PR 检查为准。

本次是当前机器 Windows/125%DPI/自制几何动作包/合成聊天 Provider；未声称其他DPI、多屏拔插、锁屏、真实聊天模型或艺术素材质量通过。真实账户、费用、外部发布和总 PR 合并许可没有扩大。
