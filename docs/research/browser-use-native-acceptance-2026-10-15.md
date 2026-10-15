# Browser Use 原生验收记录（2026-10-15）

状态：当前验收记录；仅证明下述固定资源、本地页面和配置的行为。

最后核验：2026-10-15（Asia/Shanghai）

事实来源：[Issue #144](https://github.com/sawaki-zexi/meido/issues/144)、[原生 manifest](../../backend/app/browser_use/native-runtime.json)、[公开 AgentTool/HTTP 验收](../../tests/python/test_browser_use.py)、[安装和运行手册](../handbook/browser-use-runtime.md)。Meido 基线为 `4daeef288ee40358e7faa57f5b7a1d33ad81d5ed`；本票只扩充测试和文档，没有改写生产运行时或 28 工具声明。

## 资源与环境

已提取：本机为 Windows 11 `10.0.26200`、AMD64、Python `3.13.9`、pytest `9.1.1`。资源来自 manifest 的固定官方 URL，安装到工作区外的专用资源根，并在测试进程中设置 `MEIDO_BROWSER_USE_RESOURCES`。

| 资源 | 版本 | 实际核验 SHA-256 |
| --- | --- | --- |
| agent-browser Windows x64 可执行文件 | 0.38.2 | `a6a9a3654c41ee19c05961b73489db98cff9eae88a77436986df08a7a7a73786` |
| Chrome for Testing Windows x64 压缩包 | 153.0.8010.52 | `df4854428c509fcf2f790ac448bca3f994151bcff22008236fa7c4587294fbe1` |

已提取：两份 `Get-FileHash -Algorithm SHA256` 结果均与 manifest 一致；从该压缩包解压的 `chrome.exe` 文件版本和产品版本均为 `153.0.8010.52`。固定版本的运行通过实际进程执行证明，未借用系统 Chrome 或 Codex IAB。

## 28 工具契约

已提取：将本仓库完整 `tools.json` 与 [Shiori `4f266aeaf42fcd611362e2456417b91f659341cf` 的原文件](https://github.com/YinFengWindy/Shiori-Agent/blob/4f266aeaf42fcd611362e2456417b91f659341cf/plugins/browser_use/backend/tools.json) 解析后逐项比较，两个数组均为 28 项且完全相等，包括名称、说明、标题和全部输入 schema。

上游原始 UTF-8 文件 SHA-256 为 `8acf106912f9f01376489b9c6a9e9c5b56589cd4dc08ab921e070c61290f08b1`。使用 `json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()` 规范化后的完整声明 SHA-256 为 `f7241bc43017bd3ae5590a9f250fe4b982e9bcd91e6e983682bfa83e965f2e59`。测试固定此独立上游摘要，避免只抽查几个名称或在 CI 联网查询可变分支。

## 实际执行与结果

原有单条真实页面流程首次执行结果为 `1 passed, 19 deselected in 21.22s`，但原测试没有实际调用全部 28 工具，不能据此声称完整映射已验收。本票增加剩余工具及可观察清理行为。

审查发现第一版扩充测试只调用 select、switch、close，未充分核对其结果。当前验收使用独立已知页面状态：选择框从初始 `one` 变为 `two`；两个标签页具有不同 URL 和标题，先切回原标签页，再切到新标签页，分别精确检查当前 URL 和标题；关闭新标签页后，重新列出标签页并确认只剩原标签页的 ID 和 URL。忽略这些操作的驱动无法满足这些状态断言。

最终命令（从仓库根执行，资源变量指向上述已校验安装）：

```powershell
python -m pytest -q --tb=short tests/python/test_browser_use.py --junitxml=.data/browser-use-native-acceptance.xml
```

已提取：强化状态断言后的最终结果为 **`21 passed in 40.55s`**；本机 JUnit 报告 `.data/browser-use-native-acceptance.xml` 记录 `tests="21"`、`errors="0"`、`failures="0"`、`skipped="0"`，开始时间为 `2026-10-15T23:58:43+08:00`。两条 native 验收分别实际执行约 24.06 秒和 9.78 秒。报告属于本机临时验收产物，不随源码分发；上面的命令可重新生成。

浏览器生产包 mypy（11 个源码文件）、Python 编译、测试文件 Ruff、TypeScript 和 Vite 构建、`git diff --check` 均通过。下表逐项对应真实 native 流程中的断言，而不是仅靠模拟 transport 的结果。

| 验收范围 | 直接证据 |
| --- | --- |
| 全 28 工具映射 | 公开 AgentTool 实际调用成功集合等于固定目录，所有名称都被执行；本地 HTTP 页面支持全部用例 |
| 表单及观察 | snapshot 获取 ref，fill/type/press/click 改变可读状态；select 前后通过 DOM 求值精确核对 `one` → `two`；check/uncheck 和 scroll 通过页面求值核对真实状态 |
| 阅读和等待 | read 读取本地 HTTP 正文，wait_ms、wait_for_selector/text/load 均执行 |
| 导航和标签页 | open/get_url/get_title/back/forward/reload 验证真实页面或当前 URL；不同 URL/标题的两个标签页先切旧再切新，每次精确核对当前页面，close 后通过列表确认仅新标签页消失，旧标签页 ID/URL 保留 |
| 截图和错误恢复 | 原生返回 PNG 图像；过期 ref 形成失败结果，重新 snapshot 后可继续 |
| 持久化和角色隔离 | 关闭并重新创建整个 BrowserSessions 服务后，角色 A 的 Cookie/localStorage 仍存在；角色 B 没有这些状态 |
| 取消与关闭 | 取消 20 秒等待后，同角色可重新启动；关闭后 profile 锁可重新获取，IPC port/config 文件已清理 |
| HTTP 与图像消费 | 测试模型从角色 HTTP 选择真实浏览器工具，真实截图进入下一次模型请求，普通会话存储不含 PNG 数据 |
| 活动角色删除 | 在实际 Chrome 仍活动时调用角色 DELETE API，profile、截图目录和已记录进程均消失 |
| 进程归属 | 通过 Windows CIM 观察当前 pytest 进程的原生后代，并记录存活 PID 以在父进程消失后仍检查孤儿；不会把其他并发验收的浏览器计入或终止 |

## 限制

已提取：本票使用真实 MCP stdio、agent-browser、Chrome、Windows Job、profile 和本地 HTTP；没有使用 FakeTransport 代替这两条 native 验收。其他测试仍验证授权、脱敏、模型回合和错误边界。

未验证：真实模型服务的自主选工具质量、外部网站登录流程、验证码、用户账号收发或外部事务。本地 Cookie 验收证明 profile 持久化和隔离，不代表任意站点登录永不失效。没有安装资源或没有执行 native 流程的环境不能复用本机通过结论。
