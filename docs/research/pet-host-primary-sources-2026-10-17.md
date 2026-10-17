# P05 桌宠原生宿主：固定上游与迁移接缝调研

状态：当前研究／比较完成后主人于 2026-10-17 明确选择 Electron 独立宿主并要求继续实施；原比较事实保留，具体见[实施规格](../specs/desktop-pet-spec.md)、[ADR-0020](../adr/0020-owned-electron-desktop-pet.md)及后续[实现验收](desktop-pet-acceptance-2026-10-17.md)。下文的原缺口属于调研时点，不代表新实现状态。最后核验：2026-10-17（Asia/Shanghai）。

事实来源：Shiori 固定 `4f266aeaf42fcd611362e2456417b91f659341cf` 的 Git blob（仅 `git show`／`git ls-tree`，没有切换或执行其工作树）；Meido 固定 `4dcfd8f8723c96dd36bd6cb30e0933d74d56ed6a` 的总规格和发行依赖；2026-10-17 实际查询的 Electron／Qt／Pillow 官方资料。下文明确标出已提取、推断与计划。动态文档不等于版本锁；Qt 部分采用明确6.10文档，其余 live 文档不可据此推定已选发行版本。

## 1. 目标与当前缺口

**已提取：** Meido 总规格 P05／D04 要求角色导入并绑定动作包，在真实透明置顶桌面窗口执行 move/play，动态枚举动作，3秒冷却、每回合一次、当前角色桌面限制、停用／重启和桥断连一致；accepted 不等于播放完成。两种宿主仍待选择，语音、Bilibili、presence 或全局输入不因桌宠导入。主项目根依赖只有 React／ReactDOM，开发使用 Vite＋Python uvicorn，Python requirements 未声明 PySide6；没有现成 Electron 发行入口。[总规格](../specs/shiori-tool-parity-spec.md#L124)、[验收表](../specs/shiori-tool-parity-spec.md#L205)、[package.json](../../package.json#L1)、[requirements](../../requirements.txt#L1)。

本次只读 `gh issue list --repo sawaki-zexi/meido --state open --limit 200 --json number,title,url` 返回的开放标题列表未见独立桌宠票；目标仍在 [#137](https://github.com/sawaki-zexi/meido/issues/137)／总规格，不能据“未见标题”断言不存在任何相关正文、评论或他人未发布工作。本次未开票或发布消息。

## 2. 固定上游实际合同

**已提取：** `pet_action` 只有 move/play；move 目标为五个语义方位，animation 为空／idle／run；play.name 来自当前角色包 actions 的动态 enum。执行检查真实工具上下文 channel=desktop、role/session、角色开启和包绑定；同角色 asyncio.Lock 内用 monotonic 实施3秒冷却，用 `session_key:current_timestamp` 限一回合一次。成功条件只是 `rpc.emit("action", payload)` 返回真，才扣冷却并返回 accepted/action_id；此处没有等待 native 窗口或动画结束回执。[工具 L18–63、82–170][tool]

**已提取：** 后端角色状态默认关闭，选择只能来自该角色已导入包；启用某角色会在同次命名空间更新中关闭其他角色。`binding.get` 返回当前唯一启用角色／包／动作；素材必须实际存在。不能把这套 Shiori Roles／extension 原样挂到 Meido 作为第二个角色数据库。[状态 L64–99][state]、[RPC L88–120][rpc]

**已提取：** 包格式标识为 `codex-sprite@1`，ZIP 可为根目录或单层 wrapper；必填 pet.json 的 id/displayName/description/spritesheetPath，可有 previewPath 和 actions。动作名须匹配 `[a-z][a-z0-9_-]{0,63}`，模型动作只能映射 idle、running-right、running-left、waving、jumping；failed/waiting/running/review 是渲染内部状态，不能自动暴露为包动作。精灵为1536×1872 WebP、每格192×208、九行已用格数6/8/8/4/5/8/6/6/6，必需格有内容、未用格透明。[包服务 L20–24、48–108][packages]、[ZIP/manifest/actions L22–104][archive]、[图片校验 L8–38][images]

**已提取：** 原导入 RPC 只接原生文件选择暂存根里的普通 ZIP，压缩文件≤32MiB，拒绝明显穿越／链接；服务只写 pet.json、spritesheet.webp 和可选 preview，通过 staging→os.replace 后更新角色记录。这不是任意JS插件包，也不是当前其他产品的新版动作包格式。[导入边界 L5–25][import]、[写入 L94–127][packages]

**已提取：** 播放器按 CSS background-position 裁格，逐帧 timer，idle 帧时长乘6，非 idle 行循环3次后回 idle；单次 transient 有明确超时回落。原组件同时引入交互、bubble、voice 类型和图标，原 background/index 又启动语音／直播，故“复制整个插件”会扩大 P05 范围。[帧合同 L1–64][sprite]、[播放器 L1–74、96–104][renderer]、[装配 L69–100][background]

## 3. 最小可移植源码清单（候选，不是可直接构建的移植包）

下表是**推断：** 根据实际 import／调用链划出的最小行为来源。采用后须逐份记录来源与修改，而不是安装全部 Shiori SDK／插件宿主。

| 边界 | 固定源码候选与应保留行为 | Meido 接缝／不直接搬运的依赖 |
| --- | --- | --- |
| 工具 | `backend/tool.py`；动态 schema、五方位、合法动作、3秒／一回合限制。[tool][tool] | 接 Meido ToolTurn／冻结角色授权与真实 run 身份；不信模型自报 role/session/timestamp。 |
| 包与状态 | `backend/{models,package_archive,package_images,pet_packages,pet_state,import_source,storage,reconcile,rpc}.py`。[models][models]、[包服务][packages]、[状态][state]、[存储][storage] | 替换 SDK Roles／PrivateStorage／RPC 为既有角色与资产接缝；复用 Pillow；不复制原角色迁移数据库。 |
| 后台协议 | `background/{types,binding,settings,controller}.ts`。[类型][types]、[绑定][binding]、[控制器][controller] | 摘取唯一显示角色、保留状态、短动作、方位换算与退出；controller 的 bubble 等依赖须删减或适配，不直接导入 index 的 voice/live。 |
| 渲染 | `surface/{spriteContract,surfaceState,CodexSpritePetRenderer,DesktopPetSurface,index,styles}` 与必要的包选择UI。[sprite][sprite]、[状态解析][surface-state]、[入口][surface-entry] | Electron/QtWebEngine 可复用裁格与节拍；Qt原生绘制需重写等价播放器。拖拽、bubble、voice 非 P05 必需，不能顺带启全局输入。 |
| 原生窗口与桥 | `apps/desktop/src/surface/{contract,geometry,host,window,ipc}.ts`、`assets/localAssetPolicy.ts`、`windowSecurity.ts` 及 SDK surface 类型。[窗口][window]、[host][host]、[IPC][ipc]、[素材策略][asset] | 只保留窄窗口／本地素材协议，不搬通用插件任意窗口注册、托盘、热键和整个preload。 |
| 行为 oracle | `plugins/desktop_pet/tests/{test_tool,test_pet_packages,test_pet_state,test_import_source}.py`，`surface/{spriteContract,DesktopPetSurface}.test.*`，`background/{controller,binding}.test.ts`，host/geometry/ipc tests。[工具反例][tool-tests]、[包反例][package-tests] | 重写为 Meido 公开HTTP／实际tool调用／原生host闭环；上游测试通过不能冒充本机验收。 |

## 4. 原生窗口、IPC 与退出：已有的和仍缺的

**已提取：** BrowserWindow 使用 transparent、frameless、alwaysOnTop、resizable=false、skipTaskbar；contextIsolation=true、nodeIntegration=false，**但 sandbox=false**。Windows 额外设 topmost normal level。renderer ready 与首次 paint 都就绪才 showInactive；真实窗口关闭／renderer unavailable 时停止交互，host 持有 motion timer，destroy 会 forget／stop 再销毁窗口。[窗口选项 L46–64][window-options]、[窗口适配 L30–90][window]、[host L180–223、257–289、394–410][host]

**已提取：** background 通过 epoch/disposed 在异步绑定和建窗后再检查，terminate 经原队列等待后 destroy；保留 load/state 在 renderer ready 后重放，一次 play 则不保留，重载不重播旧动作。moveTo 实际运行约900ms tween，由 host 发 settle；这不是每帧由模型发位置。[controller L177–269、278–335、381–390][controller]、[host L369–410、464–477、515–533][host]

**已提取的缺口：** 上游 IPC 明说主窗口UI的plugin id来自payload，仅是全信插件之间的约定，不能做权限隔离；只有 surface 自指操作由发送窗口身份确定。上游工具的 action_id没有回到播放器的相关完成回执，隐藏时controller可以忽略已emit动作；accepted不能证明可见、已移动、已画帧或已播放。controller 的isRunning也承认可与OS销毁事实短暂失配。[IPC L24–35、79–108][ipc]、[tool L141–170][tool]、[controller L129–138、235–259][controller]

**推断／后续计划：** 两方案都需 Meido 持有受认证的本机桥、固定role/package身份与代际、严格消息上界及发送方校验，动作只准窄enum；同一action_id区分 accepted／renderer-started／finished或失效，不把进程超时当播放失败可自动重试。具体IPC传输、nonce/代际及持久位置的合同应在宿主选择后写入具体方案，不在本研究定案。退出要核原窗口、原timer/listener、原IPC读写、原host/renderer子进程终态，不能只将UI隐藏或返回destroy成功；Electron和QtWebEngine都存在多进程职责。[Electron进程模型](https://www.electronjs.org/docs/latest/tutorial/process-model)、[QtWebEngine发行](https://doc.qt.io/qt-6.10/qtwebengine-deploying.html#deploying-qt-webengine-processes)。

**推断／后续计划：** 32MiB ZIP 上界不等于解压／内存预算。当前archive_names遍历entry、read直接载入sprite／preview，没有在该接缝逐entry累计展开总字节或entry数量；移植须另列压缩比／展开量、帧数、像素、导入并发、角色删除与失败恢复的有限规则，不把上游路径校验说成完整资源或同账号OS沙箱。[archive L37–64][archive]、[packages L80–108][packages]、[images L13–38][images]

## 5. Electron 与 PySide6 的实际取舍

以下成本为**推断**，不是工时估计、体积实测或宿主推荐。两案都可保留现有浏览器主入口，增加可选sidecar；主聊天后端继续拥有授权和动作规则。[总规格D04](../specs/shiori-tool-parity-spec.md#L124)

| 项目 | Electron sidecar | PySide6 原生绘制 | PySide6＋QtWebEngine |
| --- | --- | --- | --- |
| 可复用 | 原TS几何／host思想、React/CSS atlas播放器；适配现有React18与上游React19差异。 | Python包校验／状态＋atlas帧合同；需以Qt image/painter/timer重写TS/CSS播放和几何。 | 可留React播放器，但要替换Electron preload/IPC为Qt桥；仍非原样运行Shiori插件。 |
| 必须新增 | Electron main/preload、只加载固定本地renderer、进程owner、构建和发行清单。 | PySide6/Shiboken/Qt库、GUI事件循环与Python后端边界、Windows透明置顶与WebP图像插件发行。 | 左列Qt依赖＋WebEngineCore/Widgets、WebChannel、本地页面与素材策略、Chromium资源／helper进程。 |
| 原生限制 | 官方透明窗口不能仅靠透明像素自动点击穿透；不可假定resizable透明窗口在各平台可靠；Wayland不支持setPosition／alwaysOnTop等本目标方法。 | QWidget透明需合成；Windows/macOS还需FramelessWindowHint；置顶／不抢焦点受窗口系统支持限制。 | 还须把QWebEnginePage背景设透明，不能只将HTML/CSS透明就断言native透明。 |
| 发行代价 | Electron官方要求package/rebrand，提供Forge或手工发行；Shiori固定源码已有独立Electron43范围与builder/NSIS x64配置，但Meido没有这条链。 | pyside6-deploy是Nuitka包装器，需编译与Qt插件收集；不能把“已有Python”推成“无需新发行”。 | 除Qt库还要QtWebEngineProcess、pak／ICU／locale；不等于没有浏览器引擎的轻量Python方案。 |

表中已提取依据：[Shiori依赖/发行 L27–61、63–109][desktop-package]、[播放器imports][renderer]、[Electron透明限制](https://www.electronjs.org/docs/latest/tutorial/custom-window-styles#transparent-windows)、[Electron位置/置顶](https://www.electronjs.org/docs/latest/api/browser-window#winsetalwaysontopflag-level-relativelevel)、[Electron打包](https://www.electronjs.org/docs/latest/tutorial/application-distribution)、[QWidget透明](https://doc.qt.io/qt-6/qwidget.html#creating-translucent-windows)、[Qt窗口flags](https://doc.qt.io/qt-6.10/qt.html#WindowType-enum)、[Qt透明页面](https://doc.qt.io/qt-6.10/qwebenginepage.html#backgroundColor-prop)、[Qt WebChannel JS桥](https://doc.qt.io/qtforpython-6/overviews/qtwebchannel-javascript.html)、[pyside6-deploy](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html)、[WebEngine分发](https://doc.qt.io/qt-6.10/qtwebengine-deploying.html)。Qt image reader／裁图API可作为原生实现接缝，实际WebP插件与帧一致性须用所选wheel验证：[QImage](https://doc.qt.io/qt-6/qimage.html)、[QImageReader](https://doc.qt.io/qt-6/qimagereader.html)。

**推断：** Electron复用路径减少播放器重写，却增加Node/Chromium侧发行与IPC维护；PySide原生保留Python方向但重写播放器，嵌WebEngine则用浏览器资源成本换React复用。没有下载／打包／运行实测，不能写“Electron大X MiB／Qt小Y MiB”或性能优劣。所选版本、最低Windows版本／CPU架构、是否只首发Windows、签名与更新策略均还未确定；官方现有API不是Meido跨平台验收。[Electron发行](https://www.electronjs.org/docs/latest/tutorial/application-distribution)、[Electron签名](https://www.electronjs.org/docs/latest/tutorial/code-signing)、[Qt部署](https://doc.qt.io/qtforpython-6/deployment/deployment-pyside6-deploy.html)

**已提取／计划：** Electron官方要求contextIsolation、renderer sandbox、验证IPC sender并限制导航／新窗口；所以上游sandbox:false不能直接当成推荐安全基线。P05不需要用户导入JS，只需数据manifest＋图片；固定本地renderer应禁止远程代码和任意文件访问。采用怎样的具体IPC授权仍须方案确认。[Electron安全官方指引](https://www.electronjs.org/docs/latest/tutorial/security)、[上游窗口选项][window-options]、[包实际写入][packages]

## 6. 许可证、署名与素材边界

| 范围 | 已核许可／署名 | 后续落地要求与尚未证明 |
| --- | --- | --- |
| Shiori上述原创源码／SDK | 固定根LICENSE及SDK/LICENSE均MIT，Copyright (c) 2026 YinFengWindy；允许改写／分发但保留版权与许可。 | 当前Meido已有`third_party/shiori-agent/LICENSE`与来源说明；真正移植时扩大准确文件清单／改写说明，不只保留Meido自身MIT。[Shiori LICENSE][license]、[SDK LICENSE][sdk-license]、[现署名](../../third_party/shiori-agent/README.md#L1) |
| Electron／React | Electron LICENSE为MIT，署名Electron contributors、2013–2020 GitHub Inc.；React为MIT、Meta Platforms, Inc. and affiliates。 | 固定实际采用版本后从发行包保留原LICENSE及第三方notice，不把Chromium/Node全部宣称只有MIT。[Electron LICENSE](https://github.com/electron/electron/blob/main/LICENSE)、[React LICENSE](https://github.com/react/react/blob/main/LICENSE)、[Electron组成](https://www.electronjs.org/docs/latest/tutorial/security#general-guidelines) |
| Pillow | 官方许可名是MIT-CMU；不应简单写成通用MIT。 | 按实际已有Pillow wheel／编解码组件保留许可证；本研究没有增装或更改项目Pillow范围。[Pillow许可](https://pillow.readthedocs.io/en/stable/about.html#license)、[现requirements](../../requirements.txt#L8) |
| PySide6／Qt／WebEngine | Qt for Python社区版LGPLv3/GPLv3，另有商业版；不是MIT。WebEngine的Qt部分有商业／LGPLv3等许可，Chromium还带多种第三方许可。 | 选择实际模块和发行方式后核相应许可；LGPL的告知、许可证、可替换／重新链接及必要源代码／安装信息义务不可用“Python import”一句抹去。不据此说项目自身必然转GPL，也不据根MIT保证所有组合已合规。[Qt for Python官方](https://doc.qt.io/qtforpython-6.10/commercial/index.html)、[LGPL正文§4](https://doc.qt.io/qt-6/lgpl.html)、[Qt义务](https://www.qt.io/development/open-source-lgpl-obligations)、[WebEngine许可](https://doc.qt.io/qt-6.10/qtwebengine-licensing.html) |
| pet ZIP里的原画／角色 | 当前导入schema不要求作者／license字段；读取固定pet插件树未发现随附可分发pet.json／精灵ZIP。`codex-sprite@1`只是格式标识，不授予第三方角色／图片的权利。 | 只用自制或有明确授权的验收atlas；用户导入不等于授予产品再分发权，不复制其他产品内置宠物、商标或上游站点宣传图片。资产license/provenance必须逐包记录，缺许可不纳入默认发行。[manifest字段 L72–104][archive]、[导入只保留三类文件 L98–107][packages] |
| Shiori其他UI资产／组件 | 树内React Bits目录另有 **MIT＋Commons Clause** NOTICE，不是普通MIT。 | 它不在最小P05必需链，不能整拷上游renderer目录时忽略该限制；上游mascot和站点图片也不是本研究授权的默认pet包。[NOTICE L1–21][reactbits] |

上述是核验到的许可事实和待落实责任，不是对尚未选版本／打包产物的合规完成声明。两种宿主均须在发布前按实际依赖与资产形成notice清单；本次不授权外部发布。

## 7. 两案都必须补的用户闭环（计划，未执行）

以下逐项来自总规格P05／D04及上述静态缺口，不是已批准宿主方案或已通过验收：[总规格P05](../specs/shiori-tool-parity-spec.md#L205)。

1. 主人导入自制、有许可的真实完整包→选角色→显式开启，重载配置仍绑定原角色／包；取消选择、非法ZIP、缺帧、穿越／link、超限／并发失败保留既有库和其他角色。
2. 真实桌面对话发现`pet_action`及包动作enum→执行move五目标／play声明动作；真实native透明置顶窗口可见，测窗口bounds／帧状态并人工视觉确认，不能以网页预览或accepted替代。
3. 同角色3秒内请求、同回合第二次、错角色／渠道／后台自主来源、停用、未绑定、非法动作必须在真实dispatch拒绝；测试并发同轮与撤权／包切换后迟到动作。
4. renderer未ready／重载／崩溃，backend桥断开／重连，关闭窗口／禁用角色／删除包／退出应用均保持准确可见性与错误；重连可恢复已选包／位置，不能重播旧一次性动作或让旧角色动作落到新角色。
5. owned退出核原进程／窗口／IPC／timer／监听和helper终态；异常后可显式重启，不能混入其他已有桌面程序；启动失败有可操作诊断。
6. Windows不同DPI、负坐标多屏／拔屏、工作区和置顶、不抢当前聊天焦点、透明背景与停止入口均实机验收；若声称更多OS，分别证明，Wayland API限制不得被包装成通过。
7. 在无开发机环境的发行目录复验导入、开启、move/play、停用和重启；保留所选native依赖／许可证／自制资产，测包大小和资源占用后才作产品承诺。

## 8. 剩余决定与执行边界

**后续决定已收到：** 主人重新了解两案后，明确选择“Electron 独立宿主（推荐）”并要求按此继续实施。原研究不替主人选择；当前实施按[具体规格](../specs/desktop-pet-spec.md)、[ADR-0020](../adr/0020-owned-electron-desktop-pet.md)及[#195](https://github.com/sawaki-zexi/meido/issues/195)推进，保持原主入口和 Windows 透明窗口/角色绑定/move/play/停用重启范围。尚需完成代码、必要检查和真实窗口闭环，不能把研究或选择本身算作 P05 交付。

原只读研究期间未改aggregate／Shiori工作树，未读取动态#191实现，未安装依赖、运行app/tests/Node/native、访问真实账号、创建Issue、发送外部消息或发布代码。浏览仅查询官方技术／许可主来源；read-only gh只列开放标题。

## 固定源码身份抽样

下面SHA256针对Git LF blob字节，不冒充执行文件或原生验收记录；所有引用均锁定同一40位上游提交。 表中 `backend/` 和 `surface/` 相对 `plugins/desktop_pet/`，`desktop/src/` 相对 `apps/`；完整路径见各固定源码链接。

| 文件 | Git blob OID | SHA256 |
| --- | --- | --- |
| LICENSE | d7c6dc853b686f04269cb90d6afd1dafe24ba2c1 | 494cb07b5a33467721d6fb1a3583b54097d9f841bb14ba90145d896c9ba34237 |
| backend/tool.py | 2f26a78d39afa42c2ff49fc44a8cc77cd5d45020 | 6134804677688b070c4a8d2fcba9b90a0495f1857f293aba9eefa2a3f04e2a14 |
| backend/package_archive.py | da55d3927a74902493689432efac8063eb1c9b04 | 097709f8a4b51132219cfb423ecf86f35f284a58a72d01781f3c6dc94022a398 |
| backend/package_images.py | c7b1c4d831ab37e76547fbdb546298b2f5b2dc92 | 58108883467014434ab1dd093e9851a37ee77aa544b5e4b2b7ad6586c25250bb |
| surface/spriteContract.ts | 04e02fa35974cc170e0fb6140be8455c295b543c | 62b4d6ab3b69b780cd622dd8769b57803774c359ef9709ce8d80bbe9956282fb |
| desktop/src/surface/contract.ts | d47ad0dac0bd35c3b9d062a6efdc975b727ae17c | ab85f7ff74d445176445ed364e2d63ee14146da714930d64d40464f3a3d0c591 |
| desktop/src/surface/ipc.ts | 43232e026f46802311354f4f18c67c00ab9e213f | 323956f4f7a14f381e22a67b64a057b4ae0c7019aef19915d3d65f43e6c54b9a |

[tool]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/tool.py#L18
[state]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/pet_state.py#L64
[rpc]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/rpc.py#L88
[models]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/models.py#L11
[packages]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/pet_packages.py#L20
[archive]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/package_archive.py#L22
[images]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/package_images.py#L8
[import]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/import_source.py#L5
[sprite]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/surface/spriteContract.ts#L1
[renderer]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/surface/CodexSpritePetRenderer.tsx#L1
[background]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/background/index.ts#L69
[storage]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/backend/storage.py#L19
[types]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/background/types.ts#L21
[binding]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/background/binding.ts#L25
[controller]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/background/controller.ts#L177
[surface-state]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/surface/surfaceState.ts#L44
[surface-entry]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/surface/index.tsx#L1
[window]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/src/surface/window.ts#L30
[host]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/src/surface/host.ts#L180
[ipc]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/src/surface/ipc.ts#L24
[asset]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/src/assets/localAssetPolicy.ts#L14
[tool-tests]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/tests/test_tool.py#L66
[package-tests]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/desktop_pet/tests/test_pet_packages.py#L18
[window-options]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/src/surface/contract.ts#L46
[desktop-package]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/package.json#L27
[license]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/LICENSE#L1
[sdk-license]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/packages/sdk/LICENSE#L1
[reactbits]: https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/apps/desktop/renderer/src/shared/ui/reactBits/NOTICE.md#L1
