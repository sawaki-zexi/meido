# summarize 真实音频与 Podcast 转写验收

状态：当前 / 自制英文 WAV 与本地 RSS/enclosure 两项独立闭环通过；S02 整体尚未完成。

最后核验：2026-10-17，生产源码固定 `df109015b3a78521c26c3b0f6129b276567d3ea5`。

事实来源：实际 summarize 0.25.2 / Node 24.15.0、固定 whisper.cpp / tiny.en、真实音频、原生进程审计、公开角色消息 HTTP、下一 Provider、SSE、SessionStore 与原 owner 收尾。运行前的[源码资格](summarize-podcast-source-qualification.md)与本文件的动态证据分开；[普通 PDF 验收](summarize-pdf-acceptance-2026-10-17.md)不代替音频验收。

## 输入与实际模型

使用本机已安装、Enabled 且精确选择的 Microsoft Zira Desktop / en-US，通过 System.Speech 实际生成自编虚构英语；输出到文件，不播放。WAV 为 284046 字节、142000 帧、8.875 秒、16000Hz / 16-bit / mono PCM，实际非静音；SHA256 `c8a594a54fff93c9159489cdf9b15776427ac6c02aff2dbc83da5c2c73b3463a`。

引擎为官方 whisper.cpp v1.9.1（`f049fff95a089aa9969deb009cdd4892b3e74916`），保留原 Windows Release 的相邻 DLL；真实 exe SHA256 `58245314fb73b30fbd0cf0542c5c172e23f02b6eb7cad7b51e792439cf5e1755`。模型为官方维护者仓库固定 revision `5359861c739e955e79d9a303bcbc70fb988958b1` 的 GGML tiny.en，77704715 字节，SHA256 `921e4cf8686fdd993dcd081a5da5b6c365bfde1162e72b08d75ac75289920b1f`。资源完整保留在 `D:/meido-artifacts/summarize152-asr-offline-v1`，原 archive、来源与许可证有独立清单；本次没有重新下载或安装。[官方引擎资产](https://github.com/ggml-org/whisper.cpp/releases/expanded_assets/v1.9.1)、[固定模型 LFS 身份](https://huggingface.co/ggerganov/whisper.cpp/raw/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-tiny.en.bin)。

两项成功运行均真实启动 CPU backend、读取同一模型与实际音频，生成原生输出文件。实际识别结果包含虚构天文台、绿色信标、黎明前两次闪烁、**41 orbits** 和蓝色笔记本。完整原生输出含 leading space/CRLF，SHA256 `0819a12164bfe00e54b92e61a3e288bda9a939067a59c62d5354c0b06a42d4f6`。没有预写 transcript 或替换解析器/ASR/进程结果；这是短英文样本，不能推成所有语言、噪声或长音频质量证据。

## 两项独立闭环

| 项目 | 本地 WAV | Podcast RSS/enclosure |
| --- | --- | --- |
| 原 root exec / 自然终态 | `58142` / exit0 | `8157` / exit0 |
| 实际输入 | 角色 workspace WAV | 本次原 owned `127.0.0.1:0` 的 RSS URL |
| Node PID | 38592 | 84088 |
| 本次 readiness / 实际推理进程 | 4 help / 1 inference（PID93984） | 3 help / 1 inference（PID64748） |
| 原 Shell Task | `shell_73e80284d6954edfaf06415a64a16b71` | `shell_0ac9c7b8dac84514ba2d80c9adc8a53f` |
| 原任务耗时 / 完整日志字节 | 3.966秒 / 143 | 3.206秒 / 6479 |
| 实际 CLI 输出 | 原转录文本 | URL JSON；`transcriptSource=whisper`、`transcriptionProvider=whisper.cpp`、`provider=podcast/kind=rss_enclosure` |
| 下一 Provider / SSE / 存储 | 原工具正文与历史逐字相同，只有读到实际正文才回合成标记 | 原 CLI JSON 与下一 Provider/历史逐字段相同，只有读到实际正文才回合成标记 |

两项均用真实 `POST /api/roles/{id}/messages` TestClient/ASGI 入口选内置 `$summarize`，由原 Shell/TaskRuntime/WindowsJob 执行真实 CLI。聊天 Provider 为**进程内合成接缝**，不是远程模型端点或摘要质量验收；模型真实推理指本地 whisper ASR。CLI 无云凭据，七项服务凭据布尔均 false；Podcast `llm/summary=null`，Firecrawl/Apify 请求为零。URL extract 会构建包含实际正文的 `prompt`，不能称其为 null；本地 WAV 使用文本输出。

Podcast 自制 RSS 没有 `podcast:transcript`、Apple JSON 或预期语音词；真实请求为 RSS GET200、WAV HEAD200、`Range: bytes=0-25165823` GET206。服务返回完整284046字节，Content-Range准确，进入原生推理的临时 WAV hash 与原件一致；没有拿 RSS 标题或预写文本充当 ASR。RSS duration 提示为9秒，实际 WAV 为8.875秒。上游 `extracted.content` 添加合法 `Transcript:\n` 前缀；空白归一化后，严格等于该固定前缀加完整原生正文。diagnostics 的 `provider=whisper` 与 metadata 的 `provider=podcast` 各有不同含义。

未装 ffmpeg 时，原 CLI 无条件附带“first N only”的提示；本次按完整输入字节/hash证明未截断，不把提示当成另一种成功或失败证据。直接 WAV 支持无需转码；不据此宣称任意音视频格式均可处理。

## 配置、进程与收尾

应用导入前新建 DATA/ROLES/OWNER/BROWSER/PROJECT/HOME/TMP，关闭记忆，清空继承服务与凭据。OS vault 外部夹具只空读/禁写，先于 main 导入；WAV 66次空读、Podcast 56次空读，均零写尝试，真实 vault 方法未调用。fresh HOME 本身不是 vault 隔离。

Shell 不继承任意 `SUMMARIZE_*`；原 CLI 从独立 HOME 的 `.summarize/config.json` 顶层 `env` 读取固定引擎/模型与 whisper 选择。保留已安装 npm `.bin` 在实际 PATH，Skill 才能通过真实依赖检查。Node preload 显式设置官方 `SUMMARIZE_DISABLE_FFMPEG_WASM=1`，仅禁可选 WASM fallback；没有伪造 native 探测成功。不存在的 yt-dlp/ffmpeg/ffprobe 可选探测由原 spawn 得到 ENOENT/no PID，实际未执行这些可执行文件；它们的探测不算字幕动作。

两项 Node 网络守卫各通过8项底层前拒绝自检。WAV 不允许实际 JS 网络；Podcast 只允许原 owned server 两个准确 URL 的 GET/HEAD 和相同 host/port 的 socket。子进程只准固定字节 whisper 帮助与一次实际推理。Python/Node 守卫**不证明 OS/native 整棵进程树有网络沙箱**，WindowsJob 证明的是本次进程归属与收尾。

实际 help/ASR 全 exit0，Node 双管道关闭，原 Job `exitConfirmed` 并释放 handle；原 main 两路 closer 返回，无新增存活线程，guard 最后恢复。Podcast server 绑定127.0.0.1随机端口，非 daemon 请求线程且 `block_on_close=True`，原 `shutdown/server_close` 返回、监听 socket 关闭并 join 原 serve thread。未用新 loop 接管、固定等待或 daemon 遗留冒充资源释放。

## 原件与失败记录

| 原件 | SHA256 |
| --- | --- |
| WAV `http-result.json` | `33e8a90a74b7b5ff31d8b4d49d975b6dbdd88617d0f27b47d0a1de888cd7c97f` |
| WAV 根核验索引（12份原件） | `d6e0befbec5ca97b5185e62054688b7f800ec3d76a8a9e15691d042d42d8192f` |
| Podcast `http-result.json` | `f16d6679cf1668e19055fb851d7411ee6422cd2339a2a6abd1e0520e9884f9fa` |
| Podcast 根核验索引（15份原件） | `c06c91284dd5ff8e67115ff4b005fcbc359f64a53ec87ceee9223bf263723a8f` |

运行目录分别为 `D:/meido-artifacts/summarize152-audio-http-prep-v2/http-v2-run`、`D:/meido-artifacts/summarize152-podcast-http-prep-v3/http-v3-run`；根索引为同 artifacts 根下 `summarize152-audio-http-root-verification-v1.json` 和 `summarize152-podcast-http-root-verification-v1.json`。driver 固定的15898起至15921份输入清单在各次执行前后实际复核；根没有重复运行已通过的音频或重下载资源。独立审读另核 WAV 的12份原件字节/hash及完整链。

原 WAV v1 缺 PATH 中的 summarize 入口，Registry 正确拒绝，Node/ASR均零；Podcast v1 漏允许源码固定 `Pragma:no-cache`，底层 HTTP/ASR均零；Podcast v2 实际音频与 ASR成功，但夹具误用无 `Transcript:` 前缀的纯正文相等断言，最终回传未完成，**不计整轮通过**。最终仅修上述夹具依赖/格式，原生产和业务断言未放宽。每次失败的原 server/app/Job/线程均正常收尾，原件保留。成功运行各一次；此前 Podcast v2 还实际执行过一次 ASR，不隐去该次数。

公开执行计划：[WAV v2](https://github.com/sawaki-zexi/meido/issues/152#issuecomment-6226156179)、[Podcast v3](https://github.com/sawaki-zexi/meido/issues/152#issuecomment-6226250075)。

## 仍待验收

这两项只证明自制英文 WAV 和本地 RSS/enclosure，不证明外部 Podcast 平台、扫描 PDF/OCR、打包分发、真实摘要模型服务或任意音视频格式。真实服务与费用仍在各自批准范围之外。

视频字幕补验曾被自动审批以 `blocked by policy` 拒绝，未给具体原因，实际执行零次且已停止，未重试或绕过；音频/Podcast 不抵扣该缺口。[总复刻状态](../reference/shiori-tool-parity-status.md)与 S02 保持未完成。
