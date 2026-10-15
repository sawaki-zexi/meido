# 使用四个 Shiori 内置 Skill

状态：当前
最后核验：2026-10-16
事实来源：[内置正文](../../backend/app/builtin_skills)、[接口契约](../reference/shiori-builtin-skills.md)、[真实 CLI 验收](../research/shiori-skills-native-acceptance-2026-10-16.md)。

## 选择技能

在角色聊天直接输入 `$weather 上海今天的天气`、`$summarize 总结这个链接……`、`$meme-manage 更新高兴类别的别名` 或 `$skill-creater 创建学习简报技能`。角色需要相应工具许可；技能不会自动启用工具或安装命令。

在角色工具配置中授予需要的文件工具；需要 CLI 时打开 Shell 并填写明确命令白名单、超时和输出上限。Shell 使用 argv，不执行管道、重定向或环境赋值，工作目录固定为该角色 workspace。

## 天气

在启动 Meido 的 PATH 中准备 curl。白名单与实际命令名保持一致，例如使用 `curl` 时允许 `curl`；也可使用真实绝对路径并将该路径加入白名单。

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

把真实 CLI.js 安装路径告诉角色；上述路径是配置示例。Node 可执行文件须单独列入角色白名单。无需给 cmd.exe、PowerShell 或任意环境赋值授权。

先验证 `--version` 和 `--help`。URL 提取可不配置密钥；由角色根据真实正文总结。CLI 自行调用模型时，按其[配置说明](https://github.com/steipete/summarize/blob/main/docs/config.md) 单独配置 `~/.summarize/config.json` 的服务与凭据；不复制 Meido 模型密钥。实际 CLI 读取它自己的配置，Shell 不继承任意后端 API key。

0.25.2 使用 `--extract`，旧 Shiori 的 `--extract-only` 不适用。本地 txt 不支持 `--extract`，应使用 `notes.txt --json`；短文本可能原样返回且 `llm:null`。视频先尝试已有字幕；缺字幕、PDF/音视频额外依赖或转录失败时具体处理，不能当作完成提取。

## 表情包与新技能

表情包目录为当前角色 workspace 的 `memes/`。先放入真实、已授权图片，再让角色维护 `manifest.json` 的类别、描述、别名和启停状态。文件工具的 write_file 写 UTF-8 文本；复制图片需要独立获准命令，不能写一个路径字符串充当图片文件。read_file 可核验实际图像。

新技能写在当前 workspace 的 `skills/<id>/SKILL.md`；可附带 references、assets 和 scripts。创建完成后，下一次对话输入 `$<id>`。技能正文、脚本和支持资源变化会重新校验，声明工具不会获得许可；同名角色技能优先于项目和内置版本。

## 复验

```powershell
python -m pytest tests/python/test_builtin_skills.py tests/python/test_builtin_skills_api.py tests/python/test_capability_skills.py tests/python/test_shell_tool.py -q
$env:MEIDO_SKILL_NATIVE_TESTS = '1'
$env:MEIDO_SUMMARIZE_CLI = 'D:/tools/meido-summarize/node_modules/@steipete/summarize/dist/cli.js'
python -m pytest tests/python/test_builtin_skills_native.py -q
```

原生验收访问公开天气/网页服务，并以独立 HOME、角色 workspace 和本地测试模型服务核验 CLI 摘要链路，不使用付费模型或真实渠道账号。普通 CI 未配置原生验收时会明确跳过；已设置开关却缺依赖会失败。
