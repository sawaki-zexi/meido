---
id: summarize
version: 1.0.0
description: 使用 summarize CLI 摘要网页、URL、本地文件、YouTube 视频或提取正文与字幕。
tools: [shell, read_file]
activation: explicit
requires.bins: [node, summarize]
---
# 摘要与正文提取

使用主人已单独安装的 `@steipete/summarize` CLI（本技能核验 0.25.2，Node 24+）。需要 Shell 白名单和真实 CLI；激活不会安装依赖或增加权限。
先调用 `--version`、`--help` 核实已安装版本。缺 CLI、文件或凭据时明确报告，不能把 fallback 当原调用成功。

POSIX/npm 入口示例（单个 argv 命令，无管道）：

```text
summarize "https://example.com" --extract --plain --firecrawl off --timeout 15s
summarize "https://example.com/article" --json --length short
summarize "notes.txt" --json
summarize "https://www.youtube.com/watch?v=VIDEO_ID" --extract --youtube web
```

Windows 不调用 `.cmd` shim 或把 cmd.exe 放入白名单；使用已获准的 Node 可执行文件和主人明确配置的真实 CLI.js 路径：

```text
node "C:/configured/summarize/node_modules/@steipete/summarize/dist/cli.js" "https://example.com" --extract --plain --firecrawl off --timeout 15s
```

路径是安装位置示例，必须换成真实已配置位置；未知位置先请主人配置，不能猜测路径。
Shell 只支持 command 字段和 POSIX 引号，不支持 env/cwd/stdin；本地文件使用当前角色 workspace 内实际路径。

`--extract` 为本次核验版本的参数；不要沿用旧文档 `--extract-only`。URL 可无密钥提取。
本地纯文本不支持 `--extract`；用常规文件输入加 `--json`。PDF、音视频的提取可能需要独立媒体依赖。
YouTube 优先已有字幕；缺字幕、转录服务未配置或失败要区分，不能编造完整转录。
使用 `--json` 时检查 `extracted.transcriptSource` 与字幕字符数；`unavailable` 不等于取得字幕，即使 CLI exit 0。
只得到 `- YouTube` 标题或站点通用正文时，报告未取得视频内容，不能把这些文字总结成视频观点。

需要 CLI 调用模型时，主人须单独配置其服务与凭据，配置文件为 `~/.summarize/config.json`（非旧 config.toml）。
不得复制 Meido 模型密钥、输出凭据或把宿主环境变量当作已授予 CLI 的密钥。Shell 最小环境不会继承任意 API key。
无 CLI 模型凭据时，可无密钥提取再由当前正常对话总结；标明这是依据提取正文生成的总结。
JSON 中 `llm:null` 可能表示短内容直接返回，不能声称 CLI 已调用模型。报告实际来源、截断和失败情况。

来源：Shiori-Agent 4f266aeaf42fcd611362e2456417b91f659341cf，apps/backend/skills/summarize/SKILL.md（MIT）；按实际 CLI 0.25.2 和 Meido Shell 边界改写。CLI 官方说明：https://github.com/steipete/summarize/blob/main/docs/install.md 。
