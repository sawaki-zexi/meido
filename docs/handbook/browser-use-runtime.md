# Browser Use 原生资源

状态：当前

最后核验：2026-10-15

事实来源：

- 已提取：`backend/app/browser_use/native-runtime.json`
- 已提取：`backend/app/browser_use/runtime.py`
- 已提取：`tests/python/test_browser_use.py`
- 已提取：[2026-10-15 原生验收记录](../research/browser-use-native-acceptance-2026-10-15.md)

## 安装资源

Browser Use 首版只支持 Windows x64。项目不随源码分发浏览器二进制；使用 `native-runtime.json` 中的固定版本和官方 URL 获取 `agent-browser` 与 Chrome for Testing。

1. 在项目资源目录或单独目录下创建 `native/browser-use/`。
2. 将对应版本的 `agent-browser-win32-x64.exe` 放在 `native/browser-use/agent-browser.exe`。
3. 校验 `agent-browser.exe` 的 SHA-256 与 `native-runtime.json` 一致。
4. 下载 `chrome-win64.zip`，先校验压缩包 SHA-256，再将其中的 `chrome-win64/` 目录解压到 `native/browser-use/`。
5. 确认 `native/browser-use/chrome-win64/chrome.exe` 存在。

PowerShell 校验示例：

```powershell
Get-FileHash .\agent-browser-win32-x64.exe -Algorithm SHA256
Get-FileHash .\chrome-win64.zip -Algorithm SHA256
```

也可以从仓库根目录按固定 manifest 安装到独立资源目录；解压前同时验证两份下载：

```powershell
$manifest = Get-Content .\backend\app\browser_use\native-runtime.json -Raw | ConvertFrom-Json
$resourceRoot = 'D:\meido-browser-resources'
$nativeDir = Join-Path $resourceRoot 'native\browser-use'
New-Item -ItemType Directory -Force -Path $nativeDir | Out-Null
$agentPath = Join-Path $nativeDir 'agent-browser.exe'
$chromeArchive = Join-Path $nativeDir 'chrome-win64.zip'
Invoke-WebRequest -Uri $manifest.agentBrowser.url -OutFile $agentPath
Invoke-WebRequest -Uri $manifest.chrome.url -OutFile $chromeArchive
if ((Get-FileHash -LiteralPath $agentPath -Algorithm SHA256).Hash -ine $manifest.agentBrowser.sha256) { throw 'agent-browser SHA256 不匹配' }
if ((Get-FileHash -LiteralPath $chromeArchive -Algorithm SHA256).Hash -ine $manifest.chrome.sha256) { throw 'Chrome 压缩包 SHA256 不匹配' }
Expand-Archive -LiteralPath $chromeArchive -DestinationPath $nativeDir -Force
$env:MEIDO_BROWSER_USE_RESOURCES = $resourceRoot
```

资源解析器只检查当前平台和组件路径，下载哈希由上述安装步骤校验；不能把“文件存在”当成校验通过。仓库不包含这两份二进制，也不会把其他已安装浏览器自动替代为固定版本。

如果资源目录不在 `backend/app/` 下，将 `MEIDO_BROWSER_USE_RESOURCES` 设置为包含 `native/` 子目录的根目录。浏览器 profile 和截图默认写入 Meido 数据目录中的 `browser-use/`；需要迁移或备份时，设置 `MEIDO_BROWSER_USE_DATA_DIR` 指向专用数据目录。

每个对话运行代使用随机 Browser Use session；运行结束后只保留角色 profile 中的站点状态，不复用上一个运行代的标签页。daemon 的 stdout/stderr 由宿主持续排空到有界的内存诊断缓冲，不生成 `daemon.log`；诊断文本和网页结果会脱敏并限制长度。

## 验证

在具备原生资源的 Windows x64 环境设置 `MEIDO_BROWSER_USE_RESOURCES` 后运行：

```powershell
python -m pytest -q tests/python/test_browser_use.py --junitxml=.data/browser-use-native-acceptance.xml
```

真实页面验收使用本地 HTTP 页面，测试过程不需要外网。资源准备后，应看到两条 native 页面/HTTP 验收实际执行；没有设置资源变量时它们会跳过，这不能作为原生能力通过的证据。配置了错误资源目录时验收失败，工具本身返回结构化的 `unavailable` 状态。

原生页面验收通过公开 AgentTool 执行全部 28 个工具，覆盖表单、勾选、滚动、三类条件等待、导航历史、标签页、截图、过期引用恢复及取消。它还重新创建宿主服务，验证 Cookie/localStorage 持久化和角色隔离，检查 profile 锁、IPC 文件和测试进程所属的原生进程已释放。

另一个原生验收通过公开角色 HTTP 完成工具往返：模拟模型选工具，实际 MCP/Chrome 打开页面并截图，图像进入下一次模型请求且不持久化到会话。随后在真实浏览器仍活动时删除角色，验证 profile、截图及进程清理。模型选择由测试适配器控制；这不是对真实模型服务或用户网站账号的验收。
