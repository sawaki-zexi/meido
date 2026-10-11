# Browser Use 原生资源

状态：当前

最后核验：2026-10-11

事实来源：

- 已提取：`backend/app/browser_use/native-runtime.json`
- 已提取：`backend/app/browser_use/runtime.py`
- 已提取：`tests/python/test_browser_use.py`

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

如果资源目录不在 `backend/app/` 下，将 `MEIDO_BROWSER_USE_RESOURCES` 设置为包含 `native/` 子目录的根目录。浏览器 profile 和截图默认写入 Meido 数据目录中的 `browser-use/`；需要迁移或备份时，设置 `MEIDO_BROWSER_USE_DATA_DIR` 指向专用数据目录。

每个对话运行代使用随机 Browser Use session；运行结束后只保留角色 profile 中的站点状态，不复用上一个运行代的标签页。daemon 的 stdout/stderr 由宿主持续排空到有界的内存诊断缓冲，不生成 `daemon.log`；诊断文本和网页结果会脱敏并限制长度。

## 验证

在具备原生资源的 Windows x64 环境设置 `MEIDO_BROWSER_USE_RESOURCES` 后运行：

```powershell
python -m pytest -q tests/python/test_browser_use.py
```

真实页面验收使用本地 HTTP 页面，不需要访问外网。其他平台会跳过原生页面验收；资源缺失时，工具返回结构化的 `unavailable` 状态。
