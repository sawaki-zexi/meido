# Windows 桌宠

状态：当前操作手册；最后核验：2026-10-17。事实来源：[桌宠规格](../specs/desktop-pet-spec.md)、[后端服务](../../backend/app/desktop_pet/application.py)、[固定宿主](../../desktop-pet/native/main.cts)。只支持当前 Windows 桌面；其他平台可以构建 renderer，不能据此称原生可用。

## 准备宿主

使用 Node ≥22.12，在项目根目录执行 `npm ci`，再执行 `npm run setup:pet` 和 `npm run build:pet`。Electron 44 的 npm 包按需安装可执行文件；仅有 TypeScript 类型和 renderer 产物还不能启动桌宠。首次 setup 从官方发行下载固定版本，不使用账户或模型。

要生成本地 Windows 发行目录，执行 `npm run package:pet`。输出为 `desktop-pet/release/`，包含 Electron、固定 main/preload、静态 renderer 和许可文件。保留整个目录；后端优先启动其中的 `MeidoPet.exe`，没有发行目录时使用 node_modules 中的 Electron 与已构建入口。已有发行目录不会被打包命令覆盖，避免替换正在运行的宿主。

照常启动原 Python 后端和网页。桌宠由后端持有和启动，不需要 Vite 桌宠页面，也不以双击裸 exe 代替角色绑定和握手。

## 导入、绑定和动作

1. 保存一个角色，在其资料中的“桌宠”区域选择 ZIP。包必须包含 `pet.json` 和规定的 WebP atlas，可以位于一个外层文件夹内。最大32MiB；每角色最多8包，包中的脚本不会导入。
2. 选择包，确认页面展示的包名和动作，再打开“开启角色桌宠”。这些独立配置立即生效；默认关闭。同时只能显示一个角色，开启另一个角色会撤销旧绑定。
3. 看到“窗口已就绪”后，在该角色的主人桌面对话中要求移动或播放包动作。启用工具发现时，模型可用 `tool_search(query="select:pet_action")` 加载 schema；play.name 只列当前包声明的动作。
4. move.target 支持 top_left、top_right、center、bottom_left、bottom_right；animation 为省略/空、idle 或 run。play 仅携带 action 和 name。每个真实回合最多一次，动作间隔至少3秒。
5. 页面中的最近回执分别展示已接收、开始播放、已完成或失败。没有完成回执时不能声称动作完成；结果不明不自动重发。

窗口不会抢聊天焦点。拖动区域仅移动本桌宠窗口；悬停关闭按钮可关闭窗口，随后通过网页“重启桌宠”恢复。透明区域并不表示自动点击穿透。

## 停用、恢复和诊断

停用立即撤销新动作资格，后端等待原进程树退出。选中且启用的包不能删除；先停用，再删除即可。删除角色也会撤销绑定并清理该角色包。换角、重启和停用都使旧工具调用失效。

后端重启可恢复已启用、仍有效的绑定和位置；未启用的配置不会自行开启。窗口关闭或桥断开不会自动重播动作；显式重启会创建新代际。`cleanup_pending` 表示原宿主未确认退出，不能将它当作已停止并启动第二个宿主。

“尚未构建”时执行 setup/build；“未就绪”时先检查状态，再显式重启。包损坏或配置修订冲突时重新加载配置，不自动覆盖其他变更。只有当前主人真实桌面输入回合有桌宠动作权限，渠道、后台、调度与自主回合不能继承。

## 维护者验收

普通 `pytest` 运行包/HTTP/权限回归，原生窗口用例默认跳过。受控 Windows 环境显式设 `MEIDO_PET_NATIVE_TEST=1` 后，`tests/python/test_desktop_pet_api.py` 包含真实发行窗口、五位置及动作回执、重启/断开/停用，以及两个不同完整后端进程的绑定恢复。使用隔离数据/HOME/临时目录与空凭据夹具；不读取真实账户或调用收费模型。

保持测试产生的原始任务、ASGI 生命周期、Windows Job 与线程收尾；超时不能记成成功退出。其他 DPI、多屏拔插和锁屏没有本机实证时单列限制，不以计算逻辑或跳过用例代替。
