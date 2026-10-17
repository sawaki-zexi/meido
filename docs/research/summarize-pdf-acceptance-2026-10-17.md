# summarize 真实 PDF 提取与对话回传验收

状态：当前 / 普通文本 PDF 的本地闭环通过；S02 整体尚未完成。

最后核验：2026-10-17；生产源码固定 `7b008cba3f312f77b65514f680a0ebce9fc4d40d`。

事实来源：内置 `summarize` Skill、现有 Shell/TaskRuntime/WindowsJob、固定外部 CLI、原始 PDF、CLI JSON、两次 Provider 输入、SSE、SessionStore 与进程收尾记录。原始运行及根独立核验索引见下文。

## 已证明的行为

在四个独立应用数据根、独立 HOME/TMP 中创建验收角色，明确授予本次 Node argv 的 Shell 许可，通过真实 `POST /api/roles/{id}/messages` 的 TestClient/ASGI 入口激活内置 Skill。原 Shell 提交受管任务，真实 Node 调用真实 uvx/MarkItDown，解析角色 workspace 中的 PDF；没有替换解析器、子进程输出或工具结果。

输入为本次自编的 799 字节普通文本 PDF，SHA256 `49183f7346dc5ac29e5f4376bdcf7e9f13a2c84b9ac970165159e9e1b88eeff6`。实际提取结果的 `mediaType` 为 `application/pdf`，文件名及来源路径对应真实输入，正文包含独立标记与两句完整自编内容，分别描述虚构天文台测得四十一个轨道周期、绿色信标在黎明前闪烁两次。不是标题、网页模板或预写 transcript。

下一次合成 Provider 调用收到的真实 tool JSON，与 CLI JSON 逐字段相同；只有验证到正文后才返回带 `PDF_TEXT_CONSUMED_FIXTURE` 标记的回复。该 Provider 是**进程内合成接缝**，没有真实模型端点或识读质量证据。CLI 的 `prompt`、`llm`、`summary` 均为 null，七项服务凭据标记均 false，服务请求计数为零；不能称 CLI 调用了模型生成摘要。

SSE 包含成功的工具结束及最终回复，SessionStore 保存同一 tool result 与最终合成回复。Shell 任务 `shell_38a7f75eb45d46759ca08814462cce7e` 实际耗时 8.874 秒，exit0，日志完整、未截断、无错误。原应用两路 closer 返回，原 Windows Job 确认退出并释放 handle，原进程双管道关闭，没有新增存活线程；外层 Python guard 在这些 owner 收尾后恢复。

## 实际资源与隔离范围

| 资源 | 本次身份 |
| --- | --- |
| Windows Python / Node | CPython 3.13.9 / Node 24.15.0 |
| summarize / uv | 0.25.2 / 0.13.1 |
| PDF 转换器 | MarkItDown 0.1.8，`markitdown[all]` 的 58 个固定依赖 |
| 实际 uvx 调用 | `uvx --from markitdown[all] markitdown <真实临时 PDF>`，仅一次，exit0 |
| 实际 Node 调用 | 固定 CLI.js，`--extract --json --preprocess always --format text --firecrawl off --no-cache --no-media-cache --no-slides --retries 0 --timeout 30s --locale en --transcriber auto --theme mono --plain` |

58 包已真实离线安装并执行帮助命令，HTTP 验收明确复用这份已结束预热的 cache，同时使用新的应用根和 UV 工具/配置目录。UV 的 offline、no-index、no-build、固定 Python、禁止 Python 下载与完整 constraints 均实际传入；不把复用 cache 说成重新下载或 fresh cache。

Node 外部守卫的八项网络 API 自检均在底层调用前拦截，正文提取阶段没有其它 JS 网络或异常子进程调用。实际唯一 uvx 的环境严格核对 UV 配置及 `locale=en`、`transcriber=auto`、`theme=mono`，实际临时 PDF 字节与原件一致。该守卫只覆盖 Node JavaScript API，**不证明 native 进程树具有 OS 网络沙箱**。

真实包及 `.pth` 启动代码属于允许执行范围；`coloredlogs.pth` 的 `auto_install` 调用有环境变量条件，本次未提供该变量，不声称调用已发生。未单独追踪 Magika/ONNX 推理，不由依赖存在或诊断文字推定实际推理次数。系统 vault 在应用导入前替换为空读取/禁止写入的外部夹具，记录 146 次空读取、零写尝试，未调用真实 vault 方法。fresh HOME 本身不构成系统 vault 隔离。

早前 HTTP 夹具依次遗漏 CLI 自身写入的 locale、transcriber、theme 环境名，均在 uvx 启动前拒绝，失败原件保留。最终只允许上述三个固定公开配置和值，没有开放任意 `SUMMARIZE_*`，也未更改生产 Shell 权限。

## 原始证据

根持有原 exec `46106` 到自然 exit0，运行目录为 `D:/meido-artifacts/summarize152-pdf-stage3-prep-v2/http-v6-run`。不将观察超时当终态，不重复启动原任务。

| 证据 | SHA256 |
| --- | --- |
| `http-result.json` | `7ab199c0e30b74f63f8e88e1fc22c819153ef671172e582fa4833fe20f809be7` |
| HTTP driver / manifest | `05bd4f90752d86e3e7065f69bbb8c0641628f2ae7b36627c880dd5c4f6c0b6df` / `3e0d6944b0d008dd24f563beb580625fda18ae7c18e443457398329e6852c7fc` |
| 独立有限差异审查 | `9eec330c054af81a594143294157bd273f7d4eaa7d81bf54a15c9791a3e11731` |
| 根独立动态结果核验索引 | `cedecd529791deb75842e143c9ad4c310b332d25938e51340d3bbd619d70e025` |

根索引位于 `D:/meido-artifacts/summarize152-pdf-http-root-verification-v1.json`，核对 11 份原始输出、实际 PDF 字节、下一 Provider 与历史中的同一 JSON、SSE、唯一子进程和原 owner 收尾。资源封存前后完整核对由已审查 driver 执行；根没有重复下载、导入全部依赖或重复运行验收。[公开本次执行计划](https://github.com/sawaki-zexi/meido/issues/152#issuecomment-6225583713)。

## 尚未证明

本次只覆盖自制普通文本 PDF，不覆盖扫描件/OCR、加密或复杂 PDF、音频、Podcast、真实模型服务、打包分发环境或真实平台收件。本次PDF运行没有进行音频转写；之后另在准确df源码完成了独立[WAV/Podcast实际ASR验收](summarize-audio-podcast-acceptance-2026-10-17.md)，不与本PDF证据混计。

视频字幕补验此前被自动审批以 `blocked by policy` 拒绝，未给具体原因，实际执行零次且已停止；本次 PDF 不替代字幕验收，也不重试该动作。S02 及[总复刻目标](../reference/shiori-tool-parity-status.md)保持未完成。
