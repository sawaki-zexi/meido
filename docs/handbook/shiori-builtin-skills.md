# 使用 Shiori 内置 Skill

状态：当前
最后核验：2026-10-17
事实来源：[内置正文](../../backend/app/builtin_skills)、[接口契约](../reference/shiori-builtin-skills.md)、[真实 CLI 验收](../research/shiori-skills-native-acceptance-2026-10-16.md)。

## 选择技能

在角色聊天直接输入 `$weather 上海今天的天气`、`$summarize 总结这个链接……`、`$meme-manage 更新高兴类别的别名` 或 `$skill-creater 创建学习简报技能`。角色需要相应工具许可；技能不会自动启用工具或安装命令。

文件和普通 Shell 默认提供，无需逐项授权或填写普通命令白名单。Windows 使用 PowerShell 7，其他平台使用 /bin/sh；支持平台 shell 语法，固定角色 workspace、有限环境和预算。受限任务另需明确命令许可。

## 天气

在启动 Meido 的 PATH 中准备 curl，使用实际命令名或绝对路径；普通对话无需额外填写白名单。

`$weather 上海当前天气` 会按说明查询 wttr.in；失败时可查询 Open-Meteo。结果应包含实际地点、单位和来源。依赖缺失或请求失败时会给出具体原因；网络请求仍受角色 Shell 超时限制。

## 摘要 CLI

本批验证 Node 24.15.0 和 `@steipete/summarize@0.25.2`。可按 [CLI 官方安装说明](https://github.com/steipete/summarize/blob/main/docs/install.md) 选择独立安装位置，例如：

```powershell
npm install --prefix D:/tools/meido-summarize --ignore-scripts --no-audit --no-fund @steipete/summarize@0.25.2
```

将 Node 和实际安装的 `D:/tools/meido-summarize/node_modules/.bin` 加入启动 Meido 所用 PATH。Windows 实际调用使用已许可 Node 可执行文件与真实 CLI.js 路径，例如：

```text
node "D:/tools/meido-summarize/node_modules/@steipete/summarize/dist/cli.js" "https://example.com" --extract --json --firecrawl off --timeout 15s
```

把真实 CLI.js 安装路径告诉角色；上述路径是安装示例。普通 Shell 默认提供，但 Node/CLI 仍须实际安装，不会由 Skill 激活安装依赖。

先验证 `--version` 和 `--help`。URL 提取可不配置密钥；由角色根据真实正文总结。CLI 自行调用模型时，按其[配置说明](https://github.com/steipete/summarize/blob/main/docs/config.md) 单独配置 `~/.summarize/config.json` 的服务与凭据；不复制 Meido 模型密钥。实际 CLI 读取它自己的配置，Shell 不继承任意后端 API key。

0.25.2 使用 `--extract`，旧 Shiori 的 `--extract-only` 不适用。本地 txt 不支持 `--extract`，应使用 `notes.txt --json`；短文本可能原样返回且 `llm:null`。视频先尝试已有字幕；缺字幕、PDF/音视频额外依赖或转录失败时具体处理，不能当作完成提取。

## 本地音频与 Podcast

已用实际 whisper.cpp v1.9.1 / tiny.en 验证短英文 WAV 和独立 RSS/enclosure；配置、实际输出和限制见[验收](../research/summarize-audio-podcast-acceptance-2026-10-17.md)。保留 Windows 引擎原 Release 的相邻 DLL，下载匹配的模型文件并校验来源与 hash。只放一个 exe 不足以保证启动。

在运行 CLI 所用 HOME 的 `.summarize/config.json` 顶层 `env` 配置独立转写依赖，例如：

```json
{
  "env": {
    "SUMMARIZE_TRANSCRIBER": "whisper",
    "SUMMARIZE_WHISPER_CPP_BINARY": "D:/tools/whisper/Release/whisper-cli.exe",
    "SUMMARIZE_WHISPER_CPP_MODEL_PATH": "D:/tools/whisper/models/ggml-tiny.en.bin",
    "SUMMARIZE_DISABLE_LOCAL_WHISPER_CPP": "0"
  }
}
```

这些是安装路径示例，须确认文件存在。任意 SUMMARIZE_* 不会穿过 Shell 的最小环境；Node 和 summarize .bin 仍须在启动 PATH，独立凭据按 CLI 配置。普通 Shell 无需白名单，受限任务许可保持独立。

角色 workspace 的 WAV 可用 `node <真实CLI.js> ./speech.wav --extract --format text --transcriber whisper`，RSS 可用 `node <真实CLI.js> <RSS-URL> --extract --json --format text --transcriber whisper`；所选 URL 的网络访问须在既有许可内。先检查实际非空转写，角色再依据结果总结。短 WAV 的成功不保证所有格式、语言或外部 Podcast 平台；格式需要转码时须另准备所需依赖。URL JSON 的 `prompt` 可以包含实际正文，`llm:null/summary:null` 仍表示未调用 CLI 摘要模型。

## 表情包与新技能

表情包目录为当前角色 workspace 的 `memes/`。先放入真实、已授权图片，再让角色维护 `manifest.json` 的类别、描述、别名和启停状态。文件工具的 write_file 写 UTF-8 文本；复制图片需要独立获准命令，不能写一个路径字符串充当图片文件。read_file 可核验实际图像。

新技能写在当前 workspace 的 `skills/<id>/SKILL.md`；可附带 references、assets 和 scripts。创建完成后，下一次对话输入 `$<id>`。技能正文、脚本和支持资源变化会重新校验，声明工具不会获得许可；同名角色技能优先于项目和内置版本。

## Feed 订阅与查询

`$feed-manage 查看当前订阅` 使用主人已在 MCP 设置中配置并授权的真实服务。服务 ID 不必为 feed，多候选时先选择；每次询问信息来源都重新 list。原验收服务的文章分页、聚合概况和名称匹配删除有明确限制，缓存结果不能当作刷新成功。独立依赖准备、授权与仅 localhost 的真实 HTTP 复验见 [Feed 手册](feed-skill.md)。

## 复验

```powershell
python -m pytest tests/python/test_builtin_skills.py tests/python/test_builtin_skills_api.py tests/python/test_capability_skills.py tests/python/test_shell_tool.py -q
$env:MEIDO_SKILL_NATIVE_TESTS = '1'
$env:MEIDO_SUMMARIZE_CLI = 'D:/tools/meido-summarize/node_modules/@steipete/summarize/dist/cli.js'
python -m pytest tests/python/test_builtin_skills_native.py -q
```

原生验收访问公开天气/网页服务，并以独立 HOME、角色 workspace 和本地测试模型服务核验 CLI 摘要链路，不使用付费模型或真实渠道账号。普通 CI 未配置原生验收时会明确跳过；已设置开关却缺依赖会失败。
